package prototype

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"
)

type Page struct {
	Items    []map[string]any `json:"items"`
	Total    int64            `json:"total"`
	Page     int              `json:"page"`
	PageSize int              `json:"pageSize"`
}

func pagination(q url.Values) (int, int, error) {
	p, size := 1, 25
	for key, target := range map[string]*int{"page": &p, "pageSize": &size} {
		if raw := q.Get(key); raw != "" {
			n, e := strconv.Atoi(raw)
			if e != nil || n < 1 {
				return 0, 0, fmt.Errorf("invalid %s", key)
			}
			*target = n
		}
	}
	if size > 100 || p > 1000000 {
		return 0, 0, errors.New("pageSize must be <=100 and page <=1000000")
	}
	return p, size, nil
}

func queryMaps(ctx context.Context, db *sql.DB, query string, args ...any) ([]map[string]any, error) {
	rows, err := db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	result := []map[string]any{}
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err = rows.Scan(pointers...); err != nil {
			return nil, err
		}
		item := map[string]any{}
		for i, col := range columns {
			v := values[i]
			if b, ok := v.([]byte); ok {
				v = string(b)
			}
			item[col] = v
		}
		result = append(result, item)
	}
	return result, rows.Err()
}

func queryPage(ctx context.Context, db *sql.DB, q url.Values, selectSQL, countSQL string, args ...any) (Page, error) {
	p, size, err := pagination(q)
	if err != nil {
		return Page{}, err
	}
	result := Page{Page: p, PageSize: size, Items: []map[string]any{}}
	if err = db.QueryRowContext(ctx, countSQL, args...).Scan(&result.Total); err != nil {
		return result, err
	}
	pageArgs := append(append([]any{}, args...), size, (p-1)*size)
	result.Items, err = queryMaps(ctx, db, selectSQL+" LIMIT ? OFFSET ?", pageArgs...)
	return result, err
}

func timeFilters(q url.Values, column string) ([]string, []any, error) {
	var clauses []string
	var args []any
	for _, key := range []string{"from", "to"} {
		if raw := q.Get(key); raw != "" {
			t, err := time.Parse(time.RFC3339Nano, raw)
			if err != nil {
				return nil, nil, fmt.Errorf("invalid %s timestamp", key)
			}
			op := ">="
			if key == "to" {
				op = "<"
			}
			clauses = append(clauses, column+op+"?")
			args = append(args, t.UTC().Format("2006-01-02T15:04:05.000"))
		}
	}
	if q.Get("from") != "" && q.Get("to") != "" {
		a, _ := time.Parse(time.RFC3339Nano, q.Get("from"))
		b, _ := time.Parse(time.RFC3339Nano, q.Get("to"))
		if !b.After(a) {
			return nil, nil, errors.New("to must be after from")
		}
	}
	return clauses, args, nil
}

func uiBatches(ctx context.Context, db *sql.DB, q url.Values, edge bool) (Page, error) {
	table, status, recordTable := "vpp_batches", "chain_status", "vpp_records"
	if edge {
		table, status, recordTable = "edge_batches", "state", "edge_record_index"
	}
	clauses, args, err := timeFilters(q, "b.created_at")
	if err != nil {
		return Page{}, err
	}
	for _, f := range []struct{ query, column string }{{"edgeId", "edge_id"}, {"packetId", "packet_id"}, {"batchId", "batch_id"}, {"status", status}} {
		if v := q.Get(f.query); v != "" {
			clauses = append(clauses, "b."+f.column+"=?")
			args = append(args, v)
		}
	}
	if id := q.Get("deviceId"); id != "" {
		clauses = append(clauses, "EXISTS(SELECT 1 FROM "+recordTable+" r WHERE r.packet_id=b.packet_id AND r.device_id=?)")
		args = append(args, id)
	}
	where := ""
	if len(clauses) > 0 {
		where = " WHERE " + strings.Join(clauses, " AND ")
	}
	fields := `b.packet_id AS packetId,b.edge_id AS edgeId,b.batch_id AS batchId,b.record_count AS recordCount,b.merkle_root AS merkleRoot,b.` + status + ` AS status,b.created_at AS createdAt,b.window_start_at AS windowStart,b.window_end_at AS windowEnd`
	if edge {
		fields += `,b.attempts,b.next_attempt_at AS nextAttemptAt,b.last_error AS lastError`
	} else {
		fields += `,COALESCE((SELECT v.status FROM verification_runs v WHERE v.packet_id=b.packet_id ORDER BY v.checked_at DESC,v.id DESC LIMIT 1),'NOT_CHECKED') AS verificationStatus`
	}
	return queryPage(ctx, db, q, "SELECT "+fields+" FROM "+table+" b"+where+" ORDER BY b.created_at DESC,b.packet_id DESC", "SELECT COUNT(*) FROM "+table+" b"+where, args...)
}

func uiRecords(ctx context.Context, db *sql.DB, q url.Values, edge bool) (Page, error) {
	table := "vpp_records"
	if edge {
		table = "edge_record_index"
	}
	clauses, args, err := timeFilters(q, "r.collected_at")
	if err != nil {
		return Page{}, err
	}
	for _, f := range []struct{ query, column string }{{"deviceId", "device_id"}, {"packetId", "packet_id"}, {"recordId", "record_id"}} {
		if v := q.Get(f.query); v != "" {
			clauses = append(clauses, "r."+f.column+"=?")
			args = append(args, v)
		}
	}
	if edge && q.Get("runId") != "" {
		clauses = append(clauses, "r.run_id=?")
		args = append(args, q.Get("runId"))
	}
	if v := q.Get("edgeId"); v != "" {
		if edge {
			clauses = append(clauses, "(r.packet_id IS NULL OR EXISTS(SELECT 1 FROM edge_batches b WHERE b.packet_id=r.packet_id AND b.edge_id=?))")
		} else {
			clauses = append(clauses, "EXISTS(SELECT 1 FROM vpp_batches b WHERE b.packet_id=r.packet_id AND b.edge_id=?)")
		}
		args = append(args, v)
	}
	where := ""
	if len(clauses) > 0 {
		where = " WHERE " + strings.Join(clauses, " AND ")
	}
	page, err := queryPage(ctx, db, q, "SELECT r.record_id AS recordId,r.packet_id AS packetId,r.payload_json AS raw FROM "+table+" r"+where+" ORDER BY r.collected_at DESC,r.record_id DESC", "SELECT COUNT(*) FROM "+table+" r"+where, args...)
	if err != nil {
		return page, err
	}
	for _, item := range page.Items {
		var r Record
		if err = json.Unmarshal([]byte(item["raw"].(string)), &r); err != nil {
			return Page{}, err
		}
		delete(item, "raw")
		item["record"] = r
	}
	return page, nil
}

func uiBatch(ctx context.Context, db *sql.DB, id string, edge bool) (map[string]any, error) {
	table, status := "vpp_batches", "chain_status"
	if edge {
		table, status = "edge_batches", "state"
	}
	fields := `packet_id AS packetId,edge_id AS edgeId,batch_id AS batchId,record_count AS recordCount,merkle_root AS merkleRoot,` + status + ` AS status,created_at AS createdAt,window_start_at AS windowStart,window_end_at AS windowEnd,signature,payload_hash AS payloadHash`
	if edge {
		fields += `,attempts,next_attempt_at AS nextAttemptAt,last_error AS lastError`
	}
	items, err := queryMaps(ctx, db, "SELECT "+fields+" FROM "+table+" WHERE packet_id=?", id)
	if err != nil {
		return nil, err
	}
	if len(items) == 0 {
		return nil, sql.ErrNoRows
	}
	item := items[0]
	events, err := uiEvents(ctx, db, url.Values{"objectId": {id}, "pageSize": {"25"}})
	if err != nil {
		return nil, err
	}
	item["events"] = events
	if !edge {
		summary, e := queryMaps(ctx, db, `SELECT batch_id AS batchId,packet_id AS packetId,edge_id AS edgeId,merkle_root AS merkleRoot,record_count AS recordCount,window_start AS windowStart,window_end AS windowEnd,status,recorded_at AS recordedAt FROM simulated_chain WHERE batch_id=?`, item["batchId"])
		if e != nil {
			item["summaryError"] = "模拟摘要查询不可用"
		} else if len(summary) == 0 {
			item["summaryError"] = "模拟登记摘要缺失"
		} else {
			item["simulatedSummary"] = summary[0]
		}
		checks, e := uiVerifications(ctx, db, id, url.Values{"pageSize": {"1"}})
		if e != nil {
			item["verificationError"] = "核验历史查询不可用"
		} else {
			item["latestVerification"] = nil
			if len(checks.Items) > 0 {
				item["latestVerification"] = checks.Items[0]["result"]
			}
		}
	}
	return item, nil
}

func uiRawBatch(ctx context.Context, db *sql.DB, id string, edge bool) (map[string]any, error) {
	table := "vpp_batches"
	if edge {
		table = "edge_batches"
	}
	var raw, sig string
	err := db.QueryRowContext(ctx, "SELECT payload_json,signature FROM "+table+" WHERE packet_id=?", id).Scan(&raw, &sig)
	if err != nil {
		return nil, err
	}
	return map[string]any{"payload": json.RawMessage(raw), "canonicalPayload": raw, "signature": sig}, nil
}

func uiEvents(ctx context.Context, db *sql.DB, q url.Values) (Page, error) {
	where := ""
	args := []any{}
	if id := q.Get("objectId"); id != "" {
		where = " WHERE object_id=?"
		args = append(args, id)
	}
	page, err := queryPage(ctx, db, q, `SELECT id,object_id AS objectId,event_type AS eventType,source,occurred_at AS occurredAt,details_json AS details FROM process_events`+where+" ORDER BY id DESC", "SELECT COUNT(*) FROM process_events"+where, args...)
	if err != nil {
		return page, err
	}
	for _, item := range page.Items {
		var details any
		if err = json.Unmarshal([]byte(item["details"].(string)), &details); err != nil {
			return Page{}, err
		}
		item["details"] = details
	}
	return page, nil
}

func uiVerifications(ctx context.Context, db *sql.DB, id string, q url.Values) (Page, error) {
	page, err := queryPage(ctx, db, q, `SELECT id,checked_at AS checkedAt,status,result_json AS result FROM verification_runs WHERE packet_id=? ORDER BY checked_at DESC,id DESC`, `SELECT COUNT(*) FROM verification_runs WHERE packet_id=?`, id)
	if err != nil {
		return page, err
	}
	for _, item := range page.Items {
		var result any
		if err = json.Unmarshal([]byte(item["result"].(string)), &result); err != nil {
			return Page{}, err
		}
		item["result"] = result
	}
	return page, nil
}

func edgeOverview(ctx context.Context, db *sql.DB, c *SimulationController, edgeID string, window int, minSample, maxSample float64) (map[string]any, error) {
	state, err := c.State()
	if err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	start := time.Unix(now.Unix()/int64(window)*int64(window), 0).UTC()
	end := start.Add(time.Duration(window) * time.Second)
	var received, records, awaiting, pending, verified, rejected, totalRecords int64
	if err = db.QueryRowContext(ctx, `SELECT COUNT(DISTINCT device_id),COUNT(*) FROM edge_record_index WHERE collected_at>=? AND collected_at<?`, start.Format("2006-01-02T15:04:05"), end.Format("2006-01-02T15:04:05")).Scan(&received, &records); err != nil {
		return nil, err
	}
	if err = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM edge_records`).Scan(&awaiting); err != nil {
		return nil, err
	}
	if err = db.QueryRowContext(ctx, `SELECT COUNT(*) FROM edge_record_index`).Scan(&totalRecords); err != nil {
		return nil, err
	}
	if err = db.QueryRowContext(ctx, `SELECT COALESCE(SUM(state='PENDING'),0),COALESCE(SUM(state='VERIFIED'),0),COALESCE(SUM(state='REJECTED'),0) FROM edge_batches`).Scan(&pending, &verified, &rejected); err != nil {
		return nil, err
	}
	return map[string]any{"edgeId": edgeID, "simulation": state, "configuredDevices": len(c.devices), "currentWindowDevices": received, "currentWindowRecords": records, "recordsAwaitingWindow": awaiting, "totalRecords": totalRecords, "pendingBatches": pending, "verifiedBatches": verified, "rejectedBatches": rejected, "windowStart": start.Format(time.RFC3339), "windowEnd": end.Format(time.RFC3339), "windowSeconds": window, "sampleMinSeconds": minSample, "sampleMaxSeconds": maxSample, "serverTime": now.Format(time.RFC3339Nano), "database": "ok"}, nil
}

func deviceViews(ctx context.Context, db *sql.DB, devices []DeviceSpec) ([]map[string]any, error) {
	result := []map[string]any{}
	for _, spec := range devices {
		var raw string
		var latest any
		err := db.QueryRowContext(ctx, `SELECT payload_json FROM edge_record_index WHERE device_id=? ORDER BY collected_at DESC,record_id DESC LIMIT 1`, spec.ID).Scan(&raw)
		if err == nil {
			var r Record
			if err = json.Unmarshal([]byte(raw), &r); err != nil {
				return nil, err
			}
			latest = r
		} else if err != sql.ErrNoRows {
			return nil, err
		}
		result = append(result, map[string]any{"deviceId": spec.ID, "deviceType": spec.Type, "minPowerKW": spec.Min, "maxPowerKW": spec.Max, "latestRecord": latest})
	}
	return result, nil
}
