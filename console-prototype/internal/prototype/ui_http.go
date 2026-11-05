package prototype

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/json"
	"errors"
	"log"
	"mime"
	"net/http"
	"strings"
	"time"
)

func apiError(w http.ResponseWriter, status int, code, stage, message string, retry bool) {
	writeJSON(w, status, map[string]any{"error": map[string]any{"code": code, "stage": stage, "message": message, "retryable": retry}})
}

func uiFailure(w http.ResponseWriter, err error) {
	switch {
	case errors.Is(err, sql.ErrNoRows):
		apiError(w, 404, "NOT_FOUND", "query", "对象不存在", false)
	case errors.Is(err, ErrRunning):
		apiError(w, 409, "SAMPLING_RUNNING", "simulation", "请先暂停连续采集，再执行单轮生成", false)
	case errors.Is(err, ErrNotPending):
		apiError(w, 409, "INVALID_STATE", "delivery", "仅 PENDING 批次允许立即重试", false)
	case strings.HasPrefix(err.Error(), "invalid ") || strings.HasPrefix(err.Error(), "pageSize ") || strings.HasPrefix(err.Error(), "to must "):
		apiError(w, 400, "INVALID_QUERY", "query", err.Error(), false)
	default:
		log.Printf("management operation failed: %v", err)
		apiError(w, 503, "DEPENDENCY_UNAVAILABLE", "storage", "数据库或依赖不可用，本次操作未获成功确认", true)
	}
}

func controlAuth(token string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		provided := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
		if token == "" || subtle.ConstantTimeCompare([]byte(provided), []byte(token)) != 1 {
			apiError(w, 401, "UNAUTHORIZED", "access", "需要内部服务凭据", false)
			return
		}
		if r.Method == http.MethodPost {
			media, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
			if err != nil || media != "application/json" {
				apiError(w, 415, "INVALID_CONTENT_TYPE", "request", "控制请求必须使用 JSON", false)
				return
			}
			r.Body = http.MaxBytesReader(w, r.Body, 1024)
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || len(body) > 0 {
				apiError(w, 400, "INVALID_SCHEMA", "request", "本操作请求体必须为 {}", false)
				return
			}
		}
		ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
		defer cancel()
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func bearerAuth(token string,next http.Handler)http.Handler{return http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){provided:=strings.TrimPrefix(r.Header.Get("Authorization"),"Bearer ");if token==""||subtle.ConstantTimeCompare([]byte(provided),[]byte(token))!=1{apiError(w,401,"UNAUTHORIZED","access","需要内部服务凭据",false);return};ctx,cancel:=context.WithTimeout(r.Context(),15*time.Second);defer cancel();next.ServeHTTP(w,r.WithContext(ctx))})}

func databaseHealth(w http.ResponseWriter, r *http.Request, db *sql.DB, service string, extra map[string]any) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	data := map[string]any{"status": "ok", "service": service, "database": "ok"}
	for k, v := range extra {
		data[k] = v
	}
	status := 200
	if err := db.PingContext(ctx); err != nil {
		status = 503
		data["status"] = "error"
		data["database"] = "unavailable"
	}
	writeJSON(w, status, data)
}

func registerEdgeUI(root *http.ServeMux, db *sql.DB, c *SimulationController, edgeID string, window int, minSample, maxSample float64) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/ui/overview", func(w http.ResponseWriter, r *http.Request) {
		data, err := edgeOverview(r.Context(), db, c, edgeID, window, minSample, maxSample)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	mux.HandleFunc("GET /v1/ui/devices", func(w http.ResponseWriter, r *http.Request) {
		data, err := deviceViews(r.Context(), db, c.devices)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"items": data})
	})
	registerUIQueries(mux, db, true)
	for _, op := range []string{"start", "pause"} {
		op := op
		mux.HandleFunc("POST /v1/ui/simulation/"+op, func(w http.ResponseWriter, r *http.Request) {
			state, err := c.SetRunning(op == "start")
			if err != nil {
				uiFailure(w, err)
				return
			}
			writeJSON(w, 200, state)
		})
	}
	mux.HandleFunc("POST /v1/ui/simulation/sample-once", func(w http.ResponseWriter, r *http.Request) {
		key := r.Header.Get("Idempotency-Key")
		if len(key) < 1 || len(key) > 128 {
			apiError(w, 400, "INVALID_IDEMPOTENCY_KEY", "request", "需要长度 1–128 的 Idempotency-Key", false)
			return
		}
		result, replay, err := c.SampleOnce(key)
		if err != nil {
			uiFailure(w, err)
			return
		}
		status := 201
		if replay {
			status = 200
		}
		writeJSON(w, status, result)
	})
	mux.HandleFunc("POST /v1/ui/batches/{packetId}/retry", func(w http.ResponseWriter, r *http.Request) {
		if err := RetryPending(db, r.PathValue("packetId")); err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 202, map[string]any{"packetId": r.PathValue("packetId"), "status": "RETRY_SCHEDULED"})
	})
	mux.HandleFunc("GET /v1/ui/runs", func(w http.ResponseWriter, r *http.Request) {
		if _, _, err := pagination(r.URL.Query()); err != nil {
			uiFailure(w, err)
			return
		}
		data, err := queryPage(r.Context(), db, r.URL.Query(), `SELECT run_id AS runId,kind,generated_count AS generatedRecords,created_at AS createdAt,ended_at AS endedAt FROM simulation_runs ORDER BY created_at DESC,run_id DESC`, `SELECT COUNT(*) FROM simulation_runs`)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	root.Handle("/v1/ui/", controlAuth(env("INTERNAL_CONTROL_TOKEN", ""), mux))
}

func registerVPPUI(root *http.ServeMux, db *sql.DB, keyDir string) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/ui/workbench/overview",func(w http.ResponseWriter,r *http.Request){
		var batches,records,pending int64
		if err:=db.QueryRowContext(r.Context(),`SELECT COUNT(*),COALESCE(SUM(record_count),0) FROM vpp_batches`).Scan(&batches,&records);err!=nil{uiFailure(w,err);return}
		if err:=db.QueryRowContext(r.Context(),`SELECT COUNT(*) FROM grid_delivery_outbox WHERE delivered_at IS NULL`).Scan(&pending);err!=nil{uiFailure(w,err);return}
		page,err:=queryPage(r.Context(),db,r.URL.Query(),`SELECT b.packet_id AS packetId,b.edge_id AS edgeId,b.record_count AS recordCount,b.merkle_root AS merkleRoot,b.chain_status AS chainStatus,c.tx_hash AS txHash,c.block_number AS blockNumber,b.created_at AS createdAt,'PASS' AS signatureStatus FROM vpp_batches b JOIN simulated_chain c ON c.batch_id=b.batch_id ORDER BY b.created_at DESC,b.packet_id DESC`,`SELECT COUNT(*) FROM vpp_batches`);if err!=nil{uiFailure(w,err);return}
		edges:=[]map[string]any{{"edgeId":"EDGE-001","deviceCount":5,"status":"CONFIGURED","sampleIntervalSeconds":envInt("EDGE1_SAMPLE_SECONDS",600),"batchWindowSeconds":envInt("EDGE1_BATCH_SECONDS",1800)},{"edgeId":"EDGE-002","deviceCount":30,"status":"CONFIGURED","sampleIntervalSeconds":envInt("EDGE2_SAMPLE_SECONDS",600),"batchWindowSeconds":envInt("EDGE2_BATCH_SECONDS",1800)}}
		writeJSON(w,200,map[string]any{"vppId":env("VPP_ID","VPP-001"),"edgeRoster":edges,"edgeNodes":edges,"batches":page,"batchCount":batches,"recordCount":records,"pendingGridDeliveries":pending,"chainAdapter":"mysql-mock","chainMode":"mysql-mock","realBlockchain":false,"serverTime":nowUTC()})
	})
	mux.HandleFunc("GET /v1/ui/overview", func(w http.ResponseWriter, r *http.Request) {
		var batches, records, declared int64
		if err := db.QueryRowContext(r.Context(), `SELECT COUNT(*),COALESCE(SUM(record_count),0) FROM vpp_batches`).Scan(&batches, &declared); err != nil {
			uiFailure(w, err)
			return
		}
		if err := db.QueryRowContext(r.Context(), `SELECT COUNT(*) FROM vpp_records`).Scan(&records); err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, map[string]any{"database": "ok", "batchCount": batches, "recordCount": records, "declaredRecordCount": declared, "chainAdapter": "mysql-mock", "realBlockchain": false, "serverTime": nowUTC()})
	})
	registerUIQueries(mux, db, false)
	mux.HandleFunc("POST /v1/ui/batches/{packetId}/verify", func(w http.ResponseWriter, r *http.Request) {
		result, err := verifyStoredBatch(r.Context(), db, r.PathValue("packetId"), keyDir)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 201, result)
	})
	mux.HandleFunc("GET /v1/ui/batches/{packetId}/verifications", func(w http.ResponseWriter, r *http.Request) {
		if _, _, err := pagination(r.URL.Query()); err != nil {
			uiFailure(w, err)
			return
		}
		var exists int
		if err := db.QueryRowContext(r.Context(), `SELECT 1 FROM vpp_batches WHERE packet_id=?`, r.PathValue("packetId")).Scan(&exists); err != nil {
			uiFailure(w, err)
			return
		}
		data, err := uiVerifications(r.Context(), db, r.PathValue("packetId"), r.URL.Query())
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	root.Handle("/v1/ui/", controlAuth(env("INTERNAL_CONTROL_TOKEN", ""), mux))
}

func registerUIQueries(mux *http.ServeMux, db *sql.DB, edge bool) {
	mux.HandleFunc("GET /v1/ui/batches", func(w http.ResponseWriter, r *http.Request) {
		if _, _, err := pagination(r.URL.Query()); err != nil {
			uiFailure(w, err)
			return
		}
		data, err := uiBatches(r.Context(), db, r.URL.Query(), edge)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	mux.HandleFunc("GET /v1/ui/records", func(w http.ResponseWriter, r *http.Request) {
		if _, _, err := pagination(r.URL.Query()); err != nil {
			uiFailure(w, err)
			return
		}
		data, err := uiRecords(r.Context(), db, r.URL.Query(), edge)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	mux.HandleFunc("GET /v1/ui/batches/{packetId}", func(w http.ResponseWriter, r *http.Request) {
		data, err := uiBatch(r.Context(), db, r.PathValue("packetId"), edge)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	mux.HandleFunc("GET /v1/ui/batches/{packetId}/raw", func(w http.ResponseWriter, r *http.Request) {
		data, err := uiRawBatch(r.Context(), db, r.PathValue("packetId"), edge)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	mux.HandleFunc("GET /v1/ui/batches/{packetId}/records", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		q.Set("packetId", r.PathValue("packetId"))
		if _, _, err := pagination(q); err != nil {
			uiFailure(w, err)
			return
		}
		var exists int
		table := "vpp_batches"
		if edge {
			table = "edge_batches"
		}
		if err := db.QueryRowContext(r.Context(), "SELECT 1 FROM "+table+" WHERE packet_id=?", r.PathValue("packetId")).Scan(&exists); err != nil {
			uiFailure(w, err)
			return
		}
		data, err := uiRecords(r.Context(), db, q, edge)
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
	mux.HandleFunc("GET /v1/ui/events", func(w http.ResponseWriter, r *http.Request) {
		if _, _, err := pagination(r.URL.Query()); err != nil {
			uiFailure(w, err)
			return
		}
		data, err := uiEvents(r.Context(), db, r.URL.Query())
		if err != nil {
			uiFailure(w, err)
			return
		}
		writeJSON(w, 200, data)
	})
}
