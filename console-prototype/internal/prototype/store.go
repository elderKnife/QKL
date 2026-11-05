package prototype

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	_ "github.com/go-sql-driver/mysql"
)

func OpenDB(dsn string) (*sql.DB, error) {
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(12)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(5 * time.Minute)
	for i := 0; i < 60; i++ {
		if err = db.Ping(); err == nil {
			return db, nil
		}
		time.Sleep(time.Second)
	}
	db.Close()
	return nil, fmt.Errorf("MySQL not ready: %w", err)
}

func InitEdgeSchema(db *sql.DB) error {
	_, err := db.Exec(`CREATE TABLE IF NOT EXISTS edge_records (
 record_id VARCHAR(191) PRIMARY KEY, window_start BIGINT NOT NULL, window_seconds INT NOT NULL DEFAULT 300, timestamp VARCHAR(40) NOT NULL, payload_json LONGTEXT NOT NULL,
 INDEX edge_records_window_idx(window_start)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`)
	if err != nil {
		return err
	}
	_, err = db.Exec(`CREATE TABLE IF NOT EXISTS edge_batches (
 packet_id VARCHAR(191) PRIMARY KEY, window_start BIGINT NOT NULL, payload_json LONGTEXT NOT NULL,
		signature VARCHAR(160) NOT NULL, payload_hash CHAR(64) NOT NULL, state VARCHAR(16) NOT NULL DEFAULT 'PENDING',
		attempts INT NOT NULL DEFAULT 0, next_attempt_at DOUBLE NOT NULL DEFAULT 0, batch_id VARCHAR(191), merkle_root CHAR(64),
 last_error VARCHAR(500), created_at VARCHAR(40) NOT NULL, INDEX edge_batches_retry_idx(state,next_attempt_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`)
	if err != nil {
		return err
	}
	var columnCount int
	if err = db.QueryRow(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='edge_records' AND COLUMN_NAME='window_seconds'`).Scan(&columnCount); err != nil {
		return err
	}
	if columnCount == 0 {
		if _, err = db.Exec(`ALTER TABLE edge_records ADD COLUMN window_seconds INT NOT NULL DEFAULT 300 AFTER window_start`); err != nil {
			return err
		}
	}
	_, err = db.Exec(`ALTER TABLE edge_batches MODIFY signature VARCHAR(160) NOT NULL`)
	if err != nil {
		return err
	}
	return initUISchema(db, true)
}

func InitVPPSchema(db *sql.DB) error {
	stmts := []string{
		`CREATE TABLE IF NOT EXISTS vpp_batches (packet_id VARCHAR(191) PRIMARY KEY, edge_id VARCHAR(64) NOT NULL, batch_id VARCHAR(191) NOT NULL UNIQUE, payload_hash CHAR(64) NOT NULL, payload_json LONGTEXT NOT NULL, signature VARCHAR(160) NOT NULL, record_count INT NOT NULL, merkle_root CHAR(64) NOT NULL, chain_status VARCHAR(32) NOT NULL, created_at VARCHAR(40) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		`CREATE TABLE IF NOT EXISTS simulated_chain (batch_id VARCHAR(191) PRIMARY KEY, packet_id VARCHAR(191) NOT NULL UNIQUE, edge_id VARCHAR(64) NOT NULL, merkle_root CHAR(64) NOT NULL, record_count INT NOT NULL, window_start VARCHAR(40) NOT NULL, window_end VARCHAR(40) NOT NULL, status VARCHAR(32) NOT NULL, recorded_at VARCHAR(40) NOT NULL, tx_hash CHAR(64) NOT NULL, block_number BIGINT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		`CREATE TABLE IF NOT EXISTS vpp_records (record_id VARCHAR(191) PRIMARY KEY, packet_id VARCHAR(191) NOT NULL, payload_json LONGTEXT NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		`CREATE TABLE IF NOT EXISTS grid_delivery_outbox (packet_id VARCHAR(191) PRIMARY KEY, envelope_json LONGTEXT NOT NULL, attempts INT NOT NULL DEFAULT 0, next_attempt_at DOUBLE NOT NULL DEFAULT 0, delivered_at VARCHAR(40), last_error VARCHAR(500), INDEX grid_outbox_retry_idx(delivered_at,next_attempt_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
	}
	for _, s := range stmts {
		if _, err := db.Exec(s); err != nil {
			return err
		}
	}
	for _, column := range []struct{name, definition string}{{"tx_hash", "CHAR(64) NOT NULL DEFAULT ''"}, {"block_number", "BIGINT NOT NULL DEFAULT 0"}} {
		var count int
		if err := db.QueryRow(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='simulated_chain' AND COLUMN_NAME=?`, column.name).Scan(&count); err != nil { return err }
		if count == 0 { if _, err := db.Exec("ALTER TABLE simulated_chain ADD COLUMN "+column.name+" "+column.definition); err != nil { return err } }
	}
	if _, err := db.Exec(`UPDATE simulated_chain SET tx_hash=LOWER(SHA2(CONCAT(batch_id,':',merkle_root),256)),block_number=UNIX_TIMESTAMP(recorded_at) WHERE tx_hash='' OR block_number=0`); err != nil { return err }
	_, err := db.Exec(`ALTER TABLE vpp_batches MODIFY signature VARCHAR(160) NOT NULL`)
	if err != nil {
		return err
	}
	return initUISchema(db, false)
}

func AddRecord(db *sql.DB, window int, r Record) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err = addRecordTx(tx, window, r, ""); err != nil {
		return err
	}
	return tx.Commit()
}

func SealDue(db *sql.DB, edgeID string, window int, keySigner func([]byte) string) ([]SignedBatch, error) {
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	now := time.Now().UTC().Unix()
	rows, err := tx.Query(`SELECT window_start,window_seconds FROM edge_records WHERE window_start + window_seconds <= ? ORDER BY window_start,window_seconds FOR UPDATE`, now)
	if err != nil {
		return nil, err
	}
	type windowGroup struct {
		start   int64
		seconds int
	}
	var groups []windowGroup
	var previous windowGroup
	for rows.Next() {
		var group windowGroup
		if err = rows.Scan(&group.start, &group.seconds); err != nil {
			rows.Close()
			return nil, err
		}
		if group != previous {
			groups = append(groups, group)
			previous = group
		}
	}
	if err = rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	var result []SignedBatch
	for _, group := range groups {
		s := group.start
		rs, e := tx.Query(`SELECT record_id,payload_json FROM edge_records WHERE window_start=? AND window_seconds=? ORDER BY record_id FOR UPDATE`, s, group.seconds)
		if e != nil {
			return nil, e
		}
		var records []Record
		var ids []string
		for rs.Next() {
			var id, raw string
			if e = rs.Scan(&id, &raw); e != nil {
				rs.Close()
				return nil, e
			}
			var r Record
			if e = json.Unmarshal([]byte(raw), &r); e != nil {
				rs.Close()
				return nil, e
			}
			records = append(records, r)
			ids = append(ids, id)
		}
		if e = rs.Err(); e != nil {
			rs.Close()
			return nil, e
		}
		rs.Close()
		if len(records) == 0 {
			continue
		}
		start := time.Unix(s, 0).UTC()
		p := Payload{1, NewPacketID(edgeID), edgeID, start.Format(time.RFC3339), start.Add(time.Duration(group.seconds) * time.Second).Format(time.RFC3339), len(records), records}
		b, e := Canonical(p)
		if e != nil {
			return nil, e
		}
		sig := keySigner(b)
		sum := sha256Hex(b)
		_, e = tx.Exec(`INSERT INTO edge_batches(packet_id,window_start,payload_json,signature,payload_hash,created_at) VALUES(?,?,?,?,?,?)`, p.PacketID, s, string(b), sig, sum, time.Now().UTC().Format(time.RFC3339))
		if e != nil {
			return nil, e
		}
		for _, id := range ids {
			if _, e = tx.Exec(`UPDATE edge_record_index SET packet_id=? WHERE record_id=?`, p.PacketID, id); e != nil {
				return nil, e
			}
			if _, e = tx.Exec(`DELETE FROM edge_records WHERE record_id=?`, id); e != nil {
				return nil, e
			}
		}
		if e = addEvent(tx, p.PacketID, "BATCH_SEALED_SIGNED", "EDGE", map[string]any{"recordCount": p.RecordCount, "windowStart": p.WindowStart, "windowEnd": p.WindowEnd}); e != nil {
			return nil, e
		}
		result = append(result, SignedBatch{Payload: p, Signature: sig})
	}
	return result, tx.Commit()
}

func sha256Hex(b []byte) string { h := sha256.Sum256(b); return fmt.Sprintf("%x", h[:]) }

func DueBatches(db *sql.DB) ([]SignedBatch, error) {
	rows, err := db.Query(`SELECT payload_json,signature,attempts FROM edge_batches WHERE state='PENDING' AND next_attempt_at<=? ORDER BY created_at LIMIT 50`, float64(time.Now().UnixNano())/1e9)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []SignedBatch
	for rows.Next() {
		var raw, sig string
		var attempts int
		if err = rows.Scan(&raw, &sig, &attempts); err != nil {
			return nil, err
		}
		var p Payload
		if err = json.Unmarshal([]byte(raw), &p); err != nil {
			return nil, err
		}
		result = append(result, SignedBatch{Payload: p, Signature: sig, Attempts: attempts})
	}
	return result, rows.Err()
}

func UpdateVerified(db *sql.DB, packetID, batchID, root string) error {
	tx, e := db.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	var raw string
	if e=tx.QueryRow(`SELECT payload_json FROM edge_batches WHERE packet_id=? AND state='PENDING' FOR UPDATE`,packetID).Scan(&raw);e!=nil{return e}
	var payload Payload
	if e=json.Unmarshal([]byte(raw),&payload);e!=nil{return e}
	r, e := tx.Exec(`UPDATE edge_batches SET state='VERIFIED',batch_id=?,merkle_root=?,last_error=NULL WHERE packet_id=? AND state='PENDING'`, batchID, root, packetID)
	if e != nil {
		return e
	}
	n, _ := r.RowsAffected()
	if n != 1 {
		return errors.New("batch state changed before verification")
	}
	if e = addEvent(tx, packetID, "RECEIPT_VERIFIED", "EDGE", map[string]any{"batchId": batchID, "merkleRoot": root}); e != nil {
		return e
	}
	for _,record:=range payload.Records{if _,e=tx.Exec(`DELETE FROM edge_record_index WHERE record_id=? AND packet_id=?`,record.RecordID,packetID);e!=nil{return e}}
	if _,e=tx.Exec(`DELETE FROM edge_batches WHERE packet_id=? AND state='VERIFIED'`,packetID);e!=nil{return e}
	return tx.Commit()
}
func UpdateRejected(db *sql.DB, packetID, msg string) error {
	tx, e := db.Begin()
	if e != nil {
		return e
	}
	defer tx.Rollback()
	_, e = tx.Exec(`UPDATE edge_batches SET state='REJECTED',last_error=? WHERE packet_id=? AND state='PENDING'`, msg, packetID)
	if e != nil {
		return e
	}
	if e = addEvent(tx, packetID, "DELIVERY_REJECTED", "EDGE", map[string]any{"error": msg}); e != nil {
		return e
	}
	return tx.Commit()
}
func UpdateRetry(db *sql.DB, packetID, msg string, delay time.Duration) (int, error) {
	var attempts int
	err := db.QueryRow(`SELECT attempts FROM edge_batches WHERE packet_id=?`, packetID).Scan(&attempts)
	if err != nil {
		return 0, err
	}
	tx, err := db.Begin()
	if err != nil {
		return 0, err
	}
	defer tx.Rollback()
	_, err = tx.Exec(`UPDATE edge_batches SET next_attempt_at=?,last_error=? WHERE packet_id=? AND state='PENDING'`, float64(time.Now().Add(delay).UnixNano())/1e9, msg[:min(len(msg), 500)], packetID)
	if err != nil {
		return 0, err
	}
	if err = addEvent(tx, packetID, "DELIVERY_DEFERRED", "EDGE", map[string]any{"error": msg, "retrySeconds": delay.Seconds()}); err != nil {
		return 0, err
	}
	return attempts, tx.Commit()
}

func EdgeStatus(db *sql.DB) map[string]any {
	var records int
	_ = db.QueryRow(`SELECT COUNT(*) FROM edge_records`).Scan(&records)
	status := map[string]any{"recordsAwaitingWindow": records}
	rows, err := db.Query(`SELECT state,COUNT(*) FROM edge_batches GROUP BY state`)
	if err == nil {
		defer rows.Close()
		for rows.Next() {
			var s string
			var n int
			_ = rows.Scan(&s, &n)
			key := strings.ToLower(s)
			if key != "" {
				key = strings.ToUpper(key[:1]) + key[1:]
			}
			status[key+"Batches"] = n
		}
	}
	return status
}

func AcceptBatch(db *sql.DB, p Payload, signature, payloadHash, root string) (map[string]any, bool, error) {
	tx, e := db.Begin()
	if e != nil {
		return nil, false, e
	}
	defer tx.Rollback()
	var oldHash, batchID, oldRoot, status, txHash string
	var blockNumber int64
	var count int
	err := tx.QueryRow(`SELECT payload_hash,b.batch_id,b.merkle_root,b.record_count,b.chain_status,c.tx_hash,c.block_number FROM vpp_batches b JOIN simulated_chain c ON c.batch_id=b.batch_id WHERE b.packet_id=? FOR UPDATE`, p.PacketID).Scan(&oldHash, &batchID, &oldRoot, &count, &status, &txHash, &blockNumber)
	if err == nil {
		if oldHash != payloadHash {
			return nil, false, errors.New("packetId already exists with different content")
		}
		return receiptWithLocator(p.PacketID, batchID, count, oldRoot, status, txHash, blockNumber), true, nil
	}
	if err != sql.ErrNoRows {
		return nil, false, err
	}
	batchID = NewPacketID("VPP")
	now := time.Now().UTC().Format(time.RFC3339)
	status = "SIMULATED_REGISTERED"
	txDigest := sha256.Sum256([]byte(batchID + ":" + root))
	txHash = hex.EncodeToString(txDigest[:])
	blockNumber = time.Now().UTC().Unix()
	for _, record := range p.Records {
		raw, _ := Canonical(record)
		if _, e = tx.Exec(`INSERT INTO vpp_records(record_id,packet_id,payload_json) VALUES(?,?,?)`, record.RecordID, p.PacketID, string(raw)); e != nil {
			return nil, false, fmt.Errorf("recordId conflict or database error: %w", e)
		}
	}
	payload, _ := Canonical(p)
	if _, e = tx.Exec(`INSERT INTO simulated_chain(batch_id,packet_id,edge_id,merkle_root,record_count,window_start,window_end,status,recorded_at,tx_hash,block_number) VALUES(?,?,?,?,?,?,?,?,?,?,?)`, batchID, p.PacketID, p.EdgeID, root, p.RecordCount, p.WindowStart, p.WindowEnd, status, now, txHash, blockNumber); e != nil {
		return nil, false, e
	}
	if _, e = tx.Exec(`INSERT INTO vpp_batches(packet_id,edge_id,batch_id,payload_hash,payload_json,signature,record_count,merkle_root,chain_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)`, p.PacketID, p.EdgeID, batchID, payloadHash, string(payload), signature, p.RecordCount, root, status, now); e != nil {
		return nil, false, e
	}
	envelope, _ := json.Marshal(map[string]any{"vppId": env("VPP_ID", "VPP-001"), "batch": SignedBatch{Payload:p, Signature:signature}, "locator": map[string]any{"batchId":batchID,"txHash":txHash,"blockNumber":blockNumber,"merkleRoot":root,"adapter":"mysql-mock"}})
	if _, e = tx.Exec(`INSERT INTO grid_delivery_outbox(packet_id,envelope_json) VALUES(?,?)`, p.PacketID, string(envelope)); e != nil { return nil, false, e }
	if e = addEvent(tx, p.PacketID, "SIGNATURE_VERIFIED_SIMULATED_REGISTERED", "VPP", map[string]any{"batchId": batchID, "recordCount": p.RecordCount, "merkleRoot": root, "ruleVersion": "prototype-v1"}); e != nil {
		return nil, false, e
	}
	if e = tx.Commit(); e != nil {
		return nil, false, e
	}
	return receiptWithLocator(p.PacketID, batchID, p.RecordCount, root, status, txHash, blockNumber), false, nil
}

func receipt(packetID, batchID string, count int, root, status string) map[string]any {
	return map[string]any{"packetId": packetID, "batchId": batchID, "recordCount": count, "merkleRoot": root, "status": status}
}

func receiptWithLocator(packetID, batchID string, count int, root, status, txHash string, blockNumber int64) map[string]any {
	r := receipt(packetID, batchID, count, root, status)
	r["txHash"], r["blockNumber"], r["chainAdapter"] = txHash, blockNumber, "mysql-mock"
	return r
}

func DueGridDeliveries(db *sql.DB) ([]map[string]string, error) {
	rows, err := db.Query(`SELECT packet_id,envelope_json,attempts FROM grid_delivery_outbox WHERE delivered_at IS NULL AND next_attempt_at<=? ORDER BY packet_id LIMIT 50`, float64(time.Now().UnixNano())/1e9)
	if err != nil { return nil, err }
	defer rows.Close()
	var result []map[string]string
	for rows.Next() { var packet, envelope string; var attempts int; if err := rows.Scan(&packet,&envelope,&attempts); err != nil { return nil,err }; result=append(result,map[string]string{"packetId":packet,"envelope":envelope,"attempts":strconv.Itoa(attempts)}) }
	return result,rows.Err()
}

func UpdateGridDelivery(db *sql.DB, packetID string, delivered bool, cause string, delay time.Duration) error {
	if delivered { _,err:=db.Exec(`UPDATE grid_delivery_outbox SET delivered_at=?,last_error=NULL WHERE packet_id=? AND delivered_at IS NULL`,nowUTC(),packetID); return err }
	_,err:=db.Exec(`UPDATE grid_delivery_outbox SET attempts=attempts+1,next_attempt_at=?,last_error=? WHERE packet_id=? AND delivered_at IS NULL`,float64(time.Now().Add(delay).UnixNano())/1e9,cause,packetID)
	return err
}

func ListBatches(db *sql.DB, edgeID string) ([]map[string]any, error) {
	query := `SELECT packet_id,edge_id,batch_id,record_count,merkle_root,chain_status FROM vpp_batches`
	var rows *sql.Rows
	var err error
	if edgeID != "" {
		rows, err = db.Query(query+` WHERE edge_id=? ORDER BY created_at DESC LIMIT 100`, edgeID)
	} else {
		rows, err = db.Query(query + ` ORDER BY created_at DESC LIMIT 100`)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []map[string]any
	for rows.Next() {
		var packet, edge, batch, root, state string
		var count int
		if err = rows.Scan(&packet, &edge, &batch, &count, &root, &state); err != nil {
			return nil, err
		}
		out = append(out, map[string]any{"packetId": packet, "edgeId": edge, "batchId": batch, "recordCount": count, "merkleRoot": root, "status": state})
	}
	return out, rows.Err()
}

func GetBatch(db *sql.DB, packetID string) (map[string]any, error) {
	var raw, sig, batchID, edgeID, root, status, hash, created, start, end string
	var count int
	err := db.QueryRow(`SELECT payload_json,signature,batch_id,edge_id,merkle_root,chain_status,payload_hash,record_count,created_at FROM vpp_batches WHERE packet_id=?`, packetID).Scan(&raw, &sig, &batchID, &edgeID, &root, &status, &hash, &count, &created)
	if err != nil {
		return nil, err
	}
	var p Payload
	if err = json.Unmarshal([]byte(raw), &p); err != nil {
		return nil, err
	}
	start = p.WindowStart
	end = p.WindowEnd
	return map[string]any{"protocolVersion": p.ProtocolVersion, "packetId": packetID, "edgeId": edgeID, "windowStart": start, "windowEnd": end, "batchId": batchID, "recordCount": count, "merkleRoot": root, "status": status, "payloadHash": hash, "records": p.Records, "signature": sig, "createdAt": created}, nil
}

func GetChainSummary(db *sql.DB, batchID string) (map[string]any, error) {
	var packetID, edgeID, root, start, end, status, recorded, txHash string
	var count int
	var blockNumber int64
	err := db.QueryRow(`SELECT packet_id,edge_id,merkle_root,record_count,window_start,window_end,status,recorded_at,tx_hash,block_number FROM simulated_chain WHERE batch_id=?`, batchID).Scan(&packetID, &edgeID, &root, &count, &start, &end, &status, &recorded,&txHash,&blockNumber)
	if err != nil {
		return nil, err
	}
	return map[string]any{"batchId": batchID, "packetId": packetID, "edgeId": edgeID, "merkleRoot": root, "recordCount": count, "windowStart": start, "windowEnd": end, "status": status, "recordedAt": recorded,"txHash":txHash,"blockNumber":blockNumber,"chainAdapter":"mysql-mock"}, nil
}
