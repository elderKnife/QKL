package prototype

import (
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"

	secp "github.com/decred/dcrd/dcrec/secp256k1/v4"
)

func env(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func envFloat(key string, fallback float64) float64 {
	v, err := strconv.ParseFloat(env(key, strconv.FormatFloat(fallback, 'f', -1, 64)), 64)
	if err != nil || v <= 0 {
		panic("invalid " + key)
	}
	return v
}

func envInt(key string, fallback int) int {
	v, err := strconv.Atoi(env(key, strconv.Itoa(fallback)))
	if err != nil || v <= 0 {
		panic("invalid " + key)
	}
	return v
}

func mysqlDSN(schema string) string {
	return fmt.Sprintf("%s:%s@tcp(%s:%s)/%s?charset=utf8mb4&parseTime=true&loc=UTC",
		env("MYSQL_USER", "prototype"), env("MYSQL_PASSWORD", "prototype-dev-password"),
		env("MYSQL_HOST", "mysql"), env("MYSQL_PORT", "3306"), schema) + "&timeout=5s&readTimeout=10s&writeTimeout=10s"
}

func RunIdentity() error {
	edgeID := env("EDGE_ID", "EDGE-001")
	if !validEdgeID(edgeID) {
		return fmt.Errorf("invalid EDGE_ID")
	}
	_, err := EnsureIdentity(env("EDGE_PRIVATE_KEY_PATH", "/identity/"+edgeID+".key"), env("EDGE_PUBLIC_KEY_PATH", "/public_keys/"+edgeID+".pub"))
	if err == nil {
		log.Printf("identity initialized edgeId=%s", edgeID)
	}
	return err
}

func RunEdge(ctx context.Context) error {
	edgeID := env("EDGE_ID", "EDGE-001")
	if !validEdgeID(edgeID) {
		return fmt.Errorf("invalid EDGE_ID")
	}
	window := envInt("BATCH_WINDOW_SECONDS", 1800)
	minSample, maxSample := envFloat("SAMPLE_INTERVAL_MIN_SECONDS", 600), envFloat("SAMPLE_INTERVAL_MAX_SECONDS", 600)
	if maxSample < minSample {
		return fmt.Errorf("sample max must be >= min")
	}
	schema := env("EDGE_SCHEMA", "edge_data")
	db, err := OpenDB(mysqlDSN(schema))
	if err != nil {
		return err
	}
	defer db.Close()
	if err = InitEdgeSchema(db); err != nil {
		return err
	}
	devices, err := ConfiguredDevicesForEdge(edgeID)
	if err != nil {
		return err
	}
	controller, err := NewSimulationController(db, window, devices)
	if err != nil {
		return err
	}
	if env("INTERNAL_CONTROL_TOKEN", "") == "" {
		return fmt.Errorf("INTERNAL_CONTROL_TOKEN is required")
	}
	keyPath := env("EDGE_PRIVATE_KEY_PATH", "/identity/"+edgeID+".key")
	keyRaw, err := os.ReadFile(keyPath)
	if err != nil {
		return err
	}
	keyBytes, err := hex.DecodeString(strings.TrimSpace(string(keyRaw)))
	if err != nil {
		return err
	}
	key := secp.PrivKeyFromBytes(keyBytes)
	vppURL := env("VPP_BATCH_URL", "http://vpp:8081/v1/batches")
	log.Printf("edge started edgeId=%s database=mysql schema=%s devices=%d sampleSeconds=%.0f windowSeconds=%d", edgeID, schema, len(devices), minSample, window)
	var workers sync.WaitGroup
	for _, spec := range devices {
		spec := spec
		workers.Add(1)
		go func() { defer workers.Done(); deviceLoop(ctx, controller, spec, minSample, maxSample) }()
	}
	workers.Add(2)
	go func() { defer workers.Done(); sealLoop(ctx, db, edgeID, window, key) }()
	go func() { defer workers.Done(); deliveryLoop(ctx, db, vppURL) }()
	server := &http.Server{Addr: env("EDGE_HOST", "0.0.0.0") + ":" + env("EDGE_PORT", "8080"), ReadHeaderTimeout: 5 * time.Second}
	legacy := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodGet && r.URL.Path == "/healthz" {
			databaseHealth(w, r, db, "edge-light-node", nil)
			return
		}
		if r.Method == http.MethodGet && r.URL.Path == "/v1/status" {
			status := EdgeStatus(db)
			status["edgeId"] = edgeID
			writeJSON(w, 200, status)
			return
		}
		writeJSON(w, 404, map[string]any{"error": "not found"})
	})
	mux := http.NewServeMux()
	mux.Handle("/", legacy)
	registerEdgeUI(mux, db, controller, edgeID, window, minSample, maxSample)
	server.Handler = mux
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	err = server.ListenAndServe()
	if err != nil && err != http.ErrServerClosed {
		return err
	}
	workers.Wait()
	return nil
}

func deviceLoop(ctx context.Context, controller *SimulationController, spec DeviceSpec, minDelay, maxDelay float64) {
	rng := rand.New(rand.NewSource(time.Now().UnixNano() + int64(len(spec.ID))))
	for {
		timer := time.NewTimer(time.Duration((minDelay + rng.Float64()*(maxDelay-minDelay)) * float64(time.Second)))
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
		if err := controller.Generate(spec, rng); err != nil {
			log.Printf("record persistence failed deviceId=%s error=%v", spec.ID, err)
		}
	}
}

func sealLoop(ctx context.Context, db *sql.DB, edgeID string, window int, key *secp.PrivateKey) {
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			batches, err := SealDue(db, edgeID, window, func(data []byte) string { return Sign(key, data) })
			if err != nil {
				log.Printf("batch seal failed error=%v", err)
				continue
			}
			for _, b := range batches {
				log.Printf("batch sealed edgeId=%s packetId=%s records=%d state=PENDING", edgeID, b.Payload.PacketID, b.Payload.RecordCount)
			}
		}
	}
}

func deliveryLoop(ctx context.Context, db *sql.DB, vppURL string) {
	client := &http.Client{Timeout: 10 * time.Second}
	ticker := time.NewTicker(500 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			batches, err := DueBatches(db)
			if err != nil {
				log.Printf("outbox query failed error=%v", err)
				continue
			}
			for _, batch := range batches {
				if e := MarkDeliveryAttempt(db, batch.Payload.PacketID); e != nil {
					log.Printf("delivery attempt persistence failed packetId=%s error=%v", batch.Payload.PacketID, e)
					continue
				}
				data, _ := json.Marshal(batch)
				req, err := http.NewRequestWithContext(ctx, http.MethodPost, vppURL, bytes.NewReader(data))
				if err == nil {
					req.Header.Set("Content-Type", "application/json")
					var response *http.Response
					response, err = client.Do(req)
					if err == nil {
						var ack map[string]any
						decodeErr := json.NewDecoder(response.Body).Decode(&ack)
						response.Body.Close()
						if decodeErr != nil {
							err = decodeErr
						} else if response.StatusCode == 200 || response.StatusCode == 201 {
							root, _ := MerkleRoot(batch.Payload.Records)
							if ack["packetId"] == batch.Payload.PacketID && ack["merkleRoot"] == root && ack["status"] == "SIMULATED_REGISTERED" && numeric(ack["recordCount"]) == batch.Payload.RecordCount && ack["batchId"] != "" {
								if e := UpdateVerified(db, batch.Payload.PacketID, fmt.Sprint(ack["batchId"]), root); e != nil {
									log.Printf("batch verification persistence failed packetId=%s error=%v", batch.Payload.PacketID, e)
								} else {
									log.Printf("batch verified edgeId=%s packetId=%s batchId=%s records=%d merkleRoot=%s", batch.Payload.EdgeID, batch.Payload.PacketID, ack["batchId"], batch.Payload.RecordCount, root)
								}
								continue
							}
							err = fmt.Errorf("VPP acknowledgement mismatch")
						} else if response.StatusCode >= 400 && response.StatusCode < 500 && response.StatusCode != 429 {
							message := fmt.Sprintf("VPP HTTP %d", response.StatusCode)
							if detail, ok := ack["error"].(string); ok && detail != "" {
								message += ": " + detail
							}
							_ = UpdateRejected(db, batch.Payload.PacketID, message)
							log.Printf("batch permanently rejected edgeId=%s packetId=%s error=%s", batch.Payload.EdgeID, batch.Payload.PacketID, message)
							continue
						} else {
							err = fmt.Errorf("VPP HTTP %d", response.StatusCode)
						}
					}
				}
				attempt := batch.Attempts + 1
				delay := time.Second * time.Duration(1<<min(attempt-1, 6))
				if delay > time.Minute {
					delay = time.Minute
				}
				if _, e := UpdateRetry(db, batch.Payload.PacketID, fmt.Sprint(err), delay); e != nil {
					log.Printf("retry state update failed packetId=%s error=%v", batch.Payload.PacketID, e)
				} else {
					log.Printf("batch delivery deferred packetId=%s attempt=%d retrySeconds=%d error=%v", batch.Payload.PacketID, attempt, int(delay.Seconds()), err)
				}
			}
		}
	}
}

func numeric(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case float32:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	case int32:
		return int(n)
	}
	return -1
}

func RunVPP(ctx context.Context) error {
	schema := env("VPP_SCHEMA", "vpp_data")
	db, err := OpenDB(mysqlDSN(schema))
	if err != nil {
		return err
	}
	defer db.Close()
	if err = InitVPPSchema(db); err != nil {
		return err
	}
	if env("INTERNAL_CONTROL_TOKEN", "") == "" {
		return fmt.Errorf("INTERNAL_CONTROL_TOKEN is required")
	}
	keyDir := env("VPP_TRUSTED_KEY_DIR", "/public_keys")
	gridURL := env("GRID_BATCH_URL", "http://grid:8083/v1/batches")
	internalToken := env("INTERNAL_CONTROL_TOKEN", "")
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		databaseHealth(w, r, db, "local-vpp-simulator", map[string]any{"chainAdapter": "mysql-mock", "realBlockchain": false})
	})
	mux.HandleFunc("POST /v1/batches", func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, 8<<20)
		var req SignedBatch
		decoder := json.NewDecoder(r.Body)
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&req); err != nil {
			writeJSON(w, 400, map[string]any{"error": "invalid JSON request"})
			return
		}
		p := req.Payload
		if err := ValidatePayload(p); err != nil {
			writeJSON(w, 422, map[string]any{"error": err.Error()})
			return
		}
		pub, err := os.ReadFile(filepath.Join(keyDir, p.EdgeID+".pub"))
		if err != nil {
			writeJSON(w, 401, map[string]any{"error": "edgeId has no trusted public key"})
			return
		}
		canonical, err := Canonical(p)
		if err != nil || Verify(string(pub), canonical, req.Signature) != nil {
			writeJSON(w, 401, map[string]any{"error": "invalid edge batch signature"})
			return
		}
		root, err := MerkleRoot(p.Records)
		if err != nil {
			writeJSON(w, 422, map[string]any{"error": err.Error()})
			return
		}
		digest := sha256.Sum256(canonical)
		ack, replay, err := AcceptBatch(db, p, req.Signature, hex.EncodeToString(digest[:]), root)
		if err != nil {
			if strings.Contains(err.Error(), "different content") || strings.Contains(err.Error(), "recordId conflict") {
				writeJSON(w, 409, map[string]any{"error": err.Error()})
			} else {
				log.Printf("batch persistence error packetId=%s error=%v", p.PacketID, err)
				writeJSON(w, 503, map[string]any{"error": "database persistence unavailable"})
			}
			return
		}
		status := 201
		if replay {
			status = 200
		}
		log.Printf("batch registered edgeId=%s packetId=%s batchId=%v records=%d merkleRoot=%s replay=%t status=SIMULATED_REGISTERED", p.EdgeID, p.PacketID, ack["batchId"], p.RecordCount, root, replay)
		writeJSON(w, status, ack)
	})
	mux.HandleFunc("GET /v1/batches", func(w http.ResponseWriter, r *http.Request) {
		items, err := ListBatches(db, r.URL.Query().Get("edgeId"))
		if err != nil {
			writeJSON(w, 503, map[string]any{"error": "database unavailable"})
			return
		}
		writeJSON(w, 200, map[string]any{"batches": items})
	})
	mux.HandleFunc("GET /v1/batches/{packetId}", func(w http.ResponseWriter, r *http.Request) {
		item, err := GetBatch(db, r.PathValue("packetId"))
		if err != nil {
			writeJSON(w, 404, map[string]any{"error": "batch not found"})
			return
		}
		writeJSON(w, 200, item)
	})
	mux.HandleFunc("GET /v1/mock-chain/batches/{batchId}", func(w http.ResponseWriter, r *http.Request) {
		item, err := GetChainSummary(db, r.PathValue("batchId"))
		if err != nil {
			writeJSON(w, 404, map[string]any{"error": "summary not found"})
			return
		}
		writeJSON(w, 200, item)
	})
	mux.Handle("GET /v1/grid/chain", controlAuth(internalToken, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		batchID, txHash, blockRaw := r.URL.Query().Get("batchId"), r.URL.Query().Get("txHash"), r.URL.Query().Get("blockNumber")
		var block int64
		if _, err := fmt.Sscan(blockRaw, &block); err != nil || batchID == "" || txHash == "" { writeJSON(w,400,map[string]any{"error":"invalid chain locator"}); return }
		item, err := GetChainSummary(db,batchID)
		if err != nil || item["txHash"] != txHash || item["blockNumber"] != block { writeJSON(w,404,map[string]any{"error":"chain locator not found"}); return }
		writeJSON(w,200,item)
	})))
	registerVPPUI(mux, db, keyDir)
	server := &http.Server{Addr: env("VPP_HOST", "0.0.0.0") + ":" + env("VPP_PORT", "8081"), Handler: mux, ReadHeaderTimeout: 5 * time.Second}
	log.Printf("VPP listening address=%s MySQL schema=%s realBlockchain=false", server.Addr, schema)
	go DeliverGridLoop(ctx,db,gridURL,internalToken)
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	err = server.ListenAndServe()
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func validEdgeID(s string) bool {
	if len(s) < 1 || len(s) > 64 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-') {
			return false
		}
	}
	return true
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
