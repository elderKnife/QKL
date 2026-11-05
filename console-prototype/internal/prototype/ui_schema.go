package prototype

import (
	"database/sql"
	"encoding/json"
	"fmt"
)

// UI indexes and operation metadata never change the signed protocol payload.
func initUISchema(db *sql.DB, edge bool) error {
	statements := []string{
		`CREATE TABLE IF NOT EXISTS process_events (id BIGINT AUTO_INCREMENT PRIMARY KEY, object_id VARCHAR(191) NOT NULL, event_type VARCHAR(64) NOT NULL, source VARCHAR(16) NOT NULL, occurred_at VARCHAR(40) NOT NULL, details_json LONGTEXT NOT NULL, INDEX events_object_idx(object_id,id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
	}
	if edge {
		statements = append(statements,
			`CREATE TABLE IF NOT EXISTS simulation_control (id INT PRIMARY KEY, running BOOLEAN NOT NULL, run_id VARCHAR(191) NOT NULL, updated_at VARCHAR(40) NOT NULL) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
			`CREATE TABLE IF NOT EXISTS simulation_runs (run_id VARCHAR(191) PRIMARY KEY, request_key VARCHAR(128) UNIQUE, kind VARCHAR(24) NOT NULL, generated_count BIGINT NOT NULL DEFAULT 0, created_at VARCHAR(40) NOT NULL, ended_at VARCHAR(40)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
			`CREATE TABLE IF NOT EXISTS edge_record_index (record_id VARCHAR(191) PRIMARY KEY, device_id VARCHAR(64) NOT NULL, device_type VARCHAR(64) NOT NULL, collected_at VARCHAR(40) NOT NULL, payload_json LONGTEXT NOT NULL, packet_id VARCHAR(191), run_id VARCHAR(191), INDEX edge_device_time_idx(device_id,collected_at), INDEX edge_record_time_idx(collected_at,record_id), INDEX edge_record_packet_idx(packet_id,record_id), INDEX edge_record_run_idx(run_id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
			`CREATE TABLE IF NOT EXISTS ui_migrations (name VARCHAR(100) PRIMARY KEY) ENGINE=InnoDB`,
		)
	} else {
		statements = append(statements, `CREATE TABLE IF NOT EXISTS verification_runs (id VARCHAR(191) PRIMARY KEY, packet_id VARCHAR(191) NOT NULL, checked_at VARCHAR(40) NOT NULL, status VARCHAR(16) NOT NULL, result_json LONGTEXT NOT NULL, INDEX verification_packet_idx(packet_id,checked_at,id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`)
	}
	for _, statement := range statements {
		if _, err := db.Exec(statement); err != nil {
			return err
		}
	}
	table := "vpp_batches"
	if edge {
		table = "edge_batches"
	}
	columns := map[string]string{
		"window_start_at": `VARCHAR(40) GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.windowStart'))) STORED`,
		"window_end_at":   `VARCHAR(40) GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.windowEnd'))) STORED`,
	}
	if edge {
		columns["record_count"] = `INT GENERATED ALWAYS AS (JSON_EXTRACT(payload_json,'$.recordCount')) STORED`
		columns["edge_id"] = `VARCHAR(64) GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.edgeId'))) STORED`
	}
	for column, definition := range columns {
		if err := ensureUIColumn(db, table, column, definition); err != nil {
			return err
		}
	}
	if err := ensureUIIndex(db, table, "ui_batch_time_idx", "created_at,packet_id"); err != nil {
		return err
	}
	if !edge {
		for column, path := range map[string]string{"device_id": "deviceId", "device_type": "deviceType", "collected_at": "timestamp"} {
			definition := fmt.Sprintf("VARCHAR(64) GENERATED ALWAYS AS (JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.%s'))) STORED", path)
			if err := ensureUIColumn(db, "vpp_records", column, definition); err != nil {
				return err
			}
		}
		for name, cols := range map[string]string{"ui_record_packet_idx": "packet_id,record_id", "ui_record_time_idx": "collected_at,record_id", "ui_record_device_idx": "device_id,collected_at"} {
			if err := ensureUIIndex(db, "vpp_records", name, cols); err != nil {
				return err
			}
		}
		return nil
	}
	return backfillEdgeIndex(db)
}

func ensureUIColumn(db *sql.DB, table, column, definition string) error {
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND COLUMN_NAME=?`, table, column).Scan(&count); err != nil {
		return err
	}
	if count == 0 {
		_, err := db.Exec("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)
		return err
	}
	return nil
}

func ensureUIIndex(db *sql.DB, table, name, columns string) error {
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND INDEX_NAME=?`, table, name).Scan(&count); err != nil {
		return err
	}
	if count == 0 {
		_, err := db.Exec("ALTER TABLE " + table + " ADD INDEX " + name + " (" + columns + ")")
		return err
	}
	return nil
}

func indexRecord(tx *sql.Tx, r Record, raw, packetID, runID string, historical bool) error {
	verb := "INSERT"
	if historical {
		verb = "INSERT IGNORE"
	}
	_, err := tx.Exec(verb+` INTO edge_record_index(record_id,device_id,device_type,collected_at,payload_json,packet_id,run_id) VALUES(?,?,?,?,?,NULLIF(?,''),NULLIF(?,''))`, r.RecordID, r.DeviceID, r.DeviceType, r.Timestamp, raw, packetID, runID)
	return err
}

// Restartable insert-only migration. Existing evidence and original timestamps are retained.
func backfillEdgeIndex(db *sql.DB) error {
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM ui_migrations WHERE name='edge-record-index-v1'`).Scan(&count); err != nil {
		return err
	}
	if count > 0 {
		return nil
	}
	if _, err := db.Exec(`INSERT IGNORE INTO edge_record_index(record_id,device_id,device_type,collected_at,payload_json) SELECT record_id,JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.deviceId')),JSON_UNQUOTE(JSON_EXTRACT(payload_json,'$.deviceType')),timestamp,payload_json FROM edge_records`); err != nil {
		return err
	}
	cursor := ""
	for {
		rows, err := db.Query(`SELECT packet_id,payload_json FROM edge_batches WHERE packet_id>? ORDER BY packet_id LIMIT 50`, cursor)
		if err != nil {
			return err
		}
		type oldBatch struct{ id, raw string }
		var batches []oldBatch
		for rows.Next() {
			var b oldBatch
			if err = rows.Scan(&b.id, &b.raw); err != nil {
				rows.Close()
				return err
			}
			batches = append(batches, b)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if len(batches) == 0 {
			break
		}
		tx, err := db.Begin()
		if err != nil {
			return err
		}
		for _, b := range batches {
			var p Payload
			if err = json.Unmarshal([]byte(b.raw), &p); err != nil {
				tx.Rollback()
				return err
			}
			for _, r := range p.Records {
				raw, e := Canonical(r)
				if e != nil {
					tx.Rollback()
					return e
				}
				if e = indexRecord(tx, r, string(raw), b.id, "", true); e != nil {
					tx.Rollback()
					return e
				}
			}
			cursor = b.id
		}
		if err = tx.Commit(); err != nil {
			return err
		}
	}
	_, err := db.Exec(`INSERT IGNORE INTO ui_migrations(name) VALUES('edge-record-index-v1')`)
	return err
}
