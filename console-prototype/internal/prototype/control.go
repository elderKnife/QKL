package prototype

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"math/rand"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

var ErrRunning = errors.New("continuous sampling must be paused first")
var ErrNotPending = errors.New("only PENDING batches can be retried")

type SimulationState struct {
	Running   bool   `json:"running"`
	RunID     string `json:"runId"`
	UpdatedAt string `json:"updatedAt"`
}

type SimulationController struct {
	mu      sync.Mutex
	db      *sql.DB
	window  int
	devices []DeviceSpec
	state   SimulationState
}

func nowUTC() string { return time.Now().UTC().Format(time.RFC3339Nano) }

func ConfiguredDevices() ([]DeviceSpec, error) {
	return ConfiguredDevicesForEdge(env("EDGE_ID", "EDGE-001"))
}

func ConfiguredDevicesForEdge(edgeID string) ([]DeviceSpec, error) {
	specs := append([]DeviceSpec(nil), DeviceSpecs...)
	if edgeID == "EDGE-002" {
		specs = make([]DeviceSpec, 0, 30)
		for i := 1; i <= 12; i++ {
			specs = append(specs, DeviceSpec{fmt.Sprintf("PV-%02d", i), "photovoltaic", 0, 100})
		}
		for i := 1; i <= 12; i++ {
			specs = append(specs, DeviceSpec{fmt.Sprintf("BESS-%02d", i), "battery_storage", -50, 50})
		}
		for i := 1; i <= 6; i++ {
			specs = append(specs, DeviceSpec{fmt.Sprintf("EVSE-%02d", i), "charging_pile", 0, 22})
		}
	}
	for i := range specs {
		spec := &specs[i]
		prefix := "DEVICE_" + strings.ReplaceAll(spec.ID, "-", "_")
		for suffix, target := range map[string]*float64{"_MIN_POWER_KW": &spec.Min, "_MAX_POWER_KW": &spec.Max} {
			if raw := os.Getenv(prefix + suffix); raw != "" {
				v, e := strconv.ParseFloat(raw, 64)
				if e != nil || math.IsNaN(v) || math.IsInf(v, 0) {
					return nil, fmt.Errorf("invalid %s%s", prefix, suffix)
				}
				*target = v
			}
		}
		// The receiver's frozen v1 validation limits remain authoritative.
		baseMin,baseMax:=0.0,100.0
		switch spec.Type { case "battery_storage": baseMin,baseMax=-50,50; case "charging_pile": baseMin,baseMax=0,22 }
		if spec.Min > spec.Max || spec.Min < baseMin || spec.Max > baseMax {
			return nil, fmt.Errorf("%s bounds must stay within protocol v1 limits", spec.ID)
		}
	}
	return specs, nil
}

func NewSimulationController(db *sql.DB, window int, devices []DeviceSpec) (*SimulationController, error) {
	tx, err := db.Begin()
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	c := &SimulationController{db: db, window: window, devices: devices}
	err = tx.QueryRow(`SELECT running,run_id,updated_at FROM simulation_control WHERE id=1 FOR UPDATE`).Scan(&c.state.Running, &c.state.RunID, &c.state.UpdatedAt)
	if err == sql.ErrNoRows {
		c.state = SimulationState{true, NewPacketID("run"), nowUTC()}
		if _, err = tx.Exec(`INSERT INTO simulation_runs(run_id,kind,created_at) VALUES(?,'CONTINUOUS',?)`, c.state.RunID, c.state.UpdatedAt); err != nil {
			return nil, err
		}
		if _, err = tx.Exec(`INSERT INTO simulation_control(id,running,run_id,updated_at) VALUES(1,TRUE,?,?)`, c.state.RunID, c.state.UpdatedAt); err != nil {
			return nil, err
		}
		if err = addEvent(tx, c.state.RunID, "SAMPLING_STARTED", "EDGE", map[string]any{"reason": "first start"}); err != nil {
			return nil, err
		}
	} else if err != nil {
		return nil, err
	}
	return c, tx.Commit()
}

func (c *SimulationController) State() (SimulationState, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var s SimulationState
	err := c.db.QueryRow(`SELECT running,run_id,updated_at FROM simulation_control WHERE id=1`).Scan(&s.Running, &s.RunID, &s.UpdatedAt)
	return s, err
}

func (c *SimulationController) SetRunning(running bool) (SimulationState, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if c.state.Running == running {
		var s SimulationState
		err := c.db.QueryRow(`SELECT running,run_id,updated_at FROM simulation_control WHERE id=1`).Scan(&s.Running, &s.RunID, &s.UpdatedAt)
		return s, err
	}
	tx, err := c.db.Begin()
	if err != nil {
		return SimulationState{}, err
	}
	defer tx.Rollback()
	s := c.state
	s.Running = running
	s.UpdatedAt = nowUTC()
	kind := "SAMPLING_PAUSED"
	if running {
		s.RunID = NewPacketID("run")
		kind = "SAMPLING_STARTED"
		_, err = tx.Exec(`INSERT INTO simulation_runs(run_id,kind,created_at) VALUES(?,'CONTINUOUS',?)`, s.RunID, s.UpdatedAt)
	} else {
		_, err = tx.Exec(`UPDATE simulation_runs SET ended_at=? WHERE run_id=? AND ended_at IS NULL`, s.UpdatedAt, s.RunID)
	}
	if err != nil {
		return SimulationState{}, err
	}
	if _, err = tx.Exec(`UPDATE simulation_control SET running=?,run_id=?,updated_at=? WHERE id=1`, s.Running, s.RunID, s.UpdatedAt); err != nil {
		return SimulationState{}, err
	}
	if err = addEvent(tx, s.RunID, kind, "EDGE", s); err != nil {
		return SimulationState{}, err
	}
	if err = tx.Commit(); err != nil {
		return SimulationState{}, err
	}
	c.state = s
	return s, nil
}

func generateRecord(spec DeviceSpec, rng *rand.Rand, at time.Time) Record {
	value := spec.Min + rng.Float64()*(spec.Max-spec.Min)
	return Record{RecordID: spec.ID + "-" + NewPacketID("r"), DeviceID: spec.ID, DeviceType: spec.Type, Metric: "active_power", Value: math.Round(value*100) / 100, Unit: "kW", Timestamp: at.UTC().Format("2006-01-02T15:04:05.000Z")}
}

// The mutex covers both sampling and pause acknowledgement, including database commit.
func (c *SimulationController) Generate(spec DeviceSpec, rng *rand.Rand) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.state.Running {
		return nil
	}
	r := generateRecord(spec, rng, time.Now())
	if err := ValidateRecord(r); err != nil {
		return err
	}
	tx, err := c.db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if err = addRecordTx(tx, c.window, r, c.state.RunID); err != nil {
		return err
	}
	return tx.Commit()
}

func (c *SimulationController) SampleOnce(requestKey string) (map[string]any, bool, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	var id string
	var count int
	err := c.db.QueryRow(`SELECT run_id,generated_count FROM simulation_runs WHERE request_key=?`, requestKey).Scan(&id, &count)
	if err == nil {
		return map[string]any{"runId": id, "generatedRecords": count, "status": "GENERATED"}, true, nil
	}
	if err != sql.ErrNoRows {
		return nil, false, err
	}
	if c.state.Running {
		return nil, false, ErrRunning
	}
	tx, err := c.db.Begin()
	if err != nil {
		return nil, false, err
	}
	defer tx.Rollback()
	id = NewPacketID("run")
	at := time.Now().UTC()
	stamp := at.Format(time.RFC3339Nano)
	if _, err = tx.Exec(`INSERT INTO simulation_runs(run_id,request_key,kind,created_at,ended_at) VALUES(?,?,'SINGLE',?,?)`, id, requestKey, stamp, stamp); err != nil {
		return nil, false, err
	}
	rng := rand.New(rand.NewSource(at.UnixNano()))
	for _, spec := range c.devices {
		r := generateRecord(spec, rng, at)
		if err = ValidateRecord(r); err != nil {
			return nil, false, err
		}
		if err = addRecordTx(tx, c.window, r, id); err != nil {
			return nil, false, err
		}
	}
	result := map[string]any{"runId": id, "generatedRecords": len(c.devices), "status": "GENERATED"}
	if err = addEvent(tx, id, "SINGLE_ROUND_GENERATED", "EDGE", result); err != nil {
		return nil, false, err
	}
	return result, false, tx.Commit()
}

func addRecordTx(tx *sql.Tx, window int, r Record, runID string) error {
	b, err := Canonical(r)
	if err != nil {
		return err
	}
	t, err := time.Parse(time.RFC3339Nano, r.Timestamp)
	if err != nil {
		return err
	}
	start := t.Unix() / int64(window) * int64(window)
	if _, err = tx.Exec(`INSERT INTO edge_records(record_id,window_start,window_seconds,timestamp,payload_json) VALUES(?,?,?,?,?)`, r.RecordID, start, window, r.Timestamp, string(b)); err != nil {
		return err
	}
	if err = indexRecord(tx, r, string(b), "", runID, false); err != nil {
		return err
	}
	if runID != "" {
		_, err = tx.Exec(`UPDATE simulation_runs SET generated_count=generated_count+1 WHERE run_id=?`, runID)
	}
	return err
}

func addEvent(tx *sql.Tx, objectID, kind, source string, details any) error {
	b, err := json.Marshal(details)
	if err != nil {
		return err
	}
	_, err = tx.Exec(`INSERT INTO process_events(object_id,event_type,source,occurred_at,details_json) VALUES(?,?,?,?,?)`, objectID, kind, source, nowUTC(), string(b))
	return err
}

func RetryPending(db *sql.DB, packetID string) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	var state string
	if err = tx.QueryRow(`SELECT state FROM edge_batches WHERE packet_id=? FOR UPDATE`, packetID).Scan(&state); err != nil {
		return err
	}
	if state != "PENDING" {
		return ErrNotPending
	}
	if _, err = tx.Exec(`UPDATE edge_batches SET next_attempt_at=0 WHERE packet_id=? AND state='PENDING'`, packetID); err != nil {
		return err
	}
	if err = addEvent(tx, packetID, "MANUAL_RETRY_REQUESTED", "EDGE", map[string]any{"state": state}); err != nil {
		return err
	}
	return tx.Commit()
}

func MarkDeliveryAttempt(db *sql.DB, packetID string) error {
	tx, err := db.Begin()
	if err != nil {
		return err
	}
	defer tx.Rollback()
	result, err := tx.Exec(`UPDATE edge_batches SET attempts=attempts+1 WHERE packet_id=? AND state='PENDING'`, packetID)
	if err != nil {
		return err
	}
	n, err := result.RowsAffected()
	if err != nil {
		return err
	}
	if n != 1 {
		return ErrNotPending
	}
	if err = addEvent(tx, packetID, "DELIVERY_ATTEMPT", "EDGE", map[string]any{"target": "VPP"}); err != nil {
		return err
	}
	return tx.Commit()
}
