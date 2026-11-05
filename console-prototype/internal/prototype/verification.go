package prototype

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type VerificationCheck struct {
	Name    string `json:"name"`
	Status  string `json:"status"`
	Code    string `json:"code"`
	Message string `json:"message"`
}

type VerificationResult struct {
	ID             string              `json:"id"`
	PacketID       string              `json:"packetId"`
	CheckedAt      string              `json:"checkedAt"`
	Status         string              `json:"status"`
	Observer       string              `json:"observer"`
	RuleVersion    string              `json:"ruleVersion"`
	Method         string              `json:"method"`
	RealBlockchain bool                `json:"realBlockchain"`
	ComputedRoot   string              `json:"computedRoot,omitempty"`
	Checks         []VerificationCheck `json:"checks"`
}

func newVerification(packetID string) VerificationResult {
	return VerificationResult{ID: NewPacketID("verify"), PacketID: packetID, CheckedAt: nowUTC(), Status: "PASS", Observer: "VPP", RuleVersion: "prototype-v1", Method: "VPP 本地整批核验 / MySQL 模拟登记依据", Checks: []VerificationCheck{}}
}

func (v *VerificationResult) check(name, status, code, message string) {
	v.Checks = append(v.Checks, VerificationCheck{name, status, code, message})
	if status == "FAIL" {
		v.Status = "FAIL"
	} else if status == "ERROR" && v.Status != "FAIL" {
		v.Status = "ERROR"
	}
}

// evaluateVerification uses the existing v1 canonicalization and cryptography only.
func evaluateVerification(v *VerificationResult, p Payload, raw, signature, pub, storedHash, storedRoot string, records []Record, summary map[string]any, storageIssue string) {
	canonical, err := Canonical(p)
	if e := ValidatePayload(p); e != nil {
		v.check("包格式与编号", "FAIL", "INVALID_SCHEMA", e.Error())
	} else if err != nil || !bytes.Equal(canonical, []byte(raw)) || sha256Hex([]byte(raw)) != storedHash {
		v.check("包格式与编号", "FAIL", "PAYLOAD_MISMATCH", "规范化原文或原文摘要不一致")
	} else {
		v.check("包格式与编号", "PASS", "OK", "格式、数量、唯一性、窗口和原文摘要一致")
	}
	if pub == "" {
		v.check("边缘签名", "ERROR", "TRUSTED_KEY_UNAVAILABLE", "可信公钥不可用")
	} else if err != nil {
		v.check("边缘签名", "ERROR", "INVALID_SCHEMA", "无法恢复规范化签名输入")
	} else if e := Verify(pub, canonical, signature); e != nil {
		v.check("边缘签名", "FAIL", "SIGNATURE_INVALID", e.Error())
	} else {
		v.check("边缘签名", "PASS", "OK", "已使用服务端预置信任公钥验签")
	}
	same := len(p.Records) == len(records) && storageIssue == ""
	expected := map[string][]byte{}
	for _, r := range p.Records {
		b, e := Canonical(r)
		if e != nil {
			same = false
		}
		expected[r.RecordID] = b
	}
	seen := map[string]bool{}
	for _, r := range records {
		b, e := Canonical(r)
		if e != nil || !bytes.Equal(expected[r.RecordID], b) || seen[r.RecordID] {
			same = false
		}
		seen[r.RecordID] = true
	}
	if !same {
		v.check("记录存储一致性", "FAIL", "RECORDS_MISMATCH", "记录表与签名包的数量、编号或内容不一致")
	} else {
		v.check("记录存储一致性", "PASS", "OK", "所有保存记录均与签名原文一致")
	}
	root, e := MerkleRoot(records)
	v.ComputedRoot = root
	if summary == nil {
		v.check("Merkle 根与模拟摘要", "ERROR", "SUMMARY_UNAVAILABLE", "模拟登记摘要缺失或查询不可用")
		return
	}
	matches := e == nil && root == storedRoot && root == summary["merkleRoot"] && summary["packetId"] == p.PacketID && summary["edgeId"] == p.EdgeID && numeric(summary["recordCount"]) == len(records) && summary["windowStart"] == p.WindowStart && summary["windowEnd"] == p.WindowEnd && summary["status"] == "SIMULATED_REGISTERED"
	if !matches {
		v.check("Merkle 根与模拟摘要", "FAIL", "ROOT_MISMATCH", "重算根、批次元数据或模拟登记内容不一致")
	} else {
		v.check("Merkle 根与模拟摘要", "PASS", "OK", "重算根与本地批次及模拟登记摘要匹配；不代表真实链上确认")
	}
}

func verifyStoredBatch(ctx context.Context, db *sql.DB, packetID, keyDir string) (VerificationResult, error) {
	v := newVerification(packetID)
	tx, err := db.BeginTx(ctx, &sql.TxOptions{Isolation: sql.LevelRepeatableRead})
	if err != nil {
		return v, err
	}
	defer tx.Rollback()
	var raw, sig, hash, root, batchID string
	if err = tx.QueryRowContext(ctx, `SELECT payload_json,signature,payload_hash,merkle_root,batch_id FROM vpp_batches WHERE packet_id=?`, packetID).Scan(&raw, &sig, &hash, &root, &batchID); err != nil {
		return v, err
	}
	var p Payload
	if err = json.Unmarshal([]byte(raw), &p); err != nil {
		v.check("包格式与编号", "FAIL", "INVALID_SCHEMA", "保存的包原文不能解析")
		for _, name := range []string{"边缘签名", "记录存储一致性", "Merkle 根与模拟摘要"} {
			v.check(name, "ERROR", "INVALID_SCHEMA", "原文不可解析，检查未完成")
		}
	} else {
		var records []Record
		storageIssue := ""
		rows, e := tx.QueryContext(ctx, `SELECT record_id,payload_json FROM vpp_records WHERE packet_id=? ORDER BY record_id`, packetID)
		if e != nil {
			return v, e
		}
		for rows.Next() {
			var id, b string
			if err = rows.Scan(&id, &b); err != nil {
				rows.Close()
				return v, err
			}
			var r Record
			if err = json.Unmarshal([]byte(b), &r); err != nil {
				storageIssue = "invalid record JSON"
			} else if id != r.RecordID {
				storageIssue = "recordId mismatch"
			}
			records = append(records, r)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return v, err
		}
		var sPacket, sEdge, sRoot, sStart, sEnd, sStatus string
		var count int
		e = tx.QueryRowContext(ctx, `SELECT packet_id,edge_id,merkle_root,record_count,window_start,window_end,status FROM simulated_chain WHERE batch_id=?`, batchID).Scan(&sPacket, &sEdge, &sRoot, &count, &sStart, &sEnd, &sStatus)
		var summary map[string]any
		if e == nil {
			summary = map[string]any{"packetId": sPacket, "edgeId": sEdge, "merkleRoot": sRoot, "recordCount": count, "windowStart": sStart, "windowEnd": sEnd, "status": sStatus}
		} else if e != sql.ErrNoRows {
			return v, e
		}
		var pub []byte
		if validEdgeID(p.EdgeID) {
			pub, _ = os.ReadFile(filepath.Join(keyDir, p.EdgeID+".pub"))
		}
		evaluateVerification(&v, p, raw, sig, string(pub), hash, root, records, summary, storageIssue)
		if p.PacketID != packetID {
			v.check("查询对象关联", "FAIL", "TARGET_MISMATCH", "原文 packetId 与查询对象不一致")
		}
	}
	b, err := json.Marshal(v)
	if err != nil {
		return v, err
	}
	if _, err = tx.ExecContext(ctx, `INSERT INTO verification_runs(id,packet_id,checked_at,status,result_json) VALUES(?,?,?,?,?)`, v.ID, packetID, v.CheckedAt, v.Status, string(b)); err != nil {
		return v, err
	}
	if err = addEvent(tx, packetID, "BATCH_REVERIFIED", "VPP", map[string]any{"verificationId": v.ID, "status": v.Status}); err != nil {
		return v, err
	}
	if err = tx.Commit(); err != nil {
		return v, fmt.Errorf("verification persistence unavailable: %w", err)
	}
	return v, nil
}
