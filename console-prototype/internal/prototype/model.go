package prototype

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
)

type Record struct {
	RecordID   string  `json:"recordId"`
	DeviceID   string  `json:"deviceId"`
	DeviceType string  `json:"deviceType"`
	Metric     string  `json:"metric"`
	Value      float64 `json:"value"`
	Unit       string  `json:"unit"`
	Timestamp  string  `json:"timestamp"`
}

type DeviceSpec struct {
	ID, Type string
	Min, Max float64
}

var DeviceSpecs = []DeviceSpec{
	{"PV-01", "photovoltaic", 0, 100},
	{"PV-02", "photovoltaic", 0, 100},
	{"BESS-01", "battery_storage", -50, 50},
	{"BESS-02", "battery_storage", -50, 50},
	{"EVSE-01", "charging_pile", 0, 22},
}

type Payload struct {
	ProtocolVersion int      `json:"protocolVersion"`
	PacketID        string   `json:"packetId"`
	EdgeID          string   `json:"edgeId"`
	WindowStart     string   `json:"windowStart"`
	WindowEnd       string   `json:"windowEnd"`
	RecordCount     int      `json:"recordCount"`
	Records         []Record `json:"records"`
}

type SignedBatch struct {
	Payload   Payload `json:"payload"`
	Signature string  `json:"signature"`
	Attempts  int     `json:"-"`
}

func Canonical(v any) ([]byte, error) {
	var buf bytes.Buffer
	if err := writeCanonical(&buf, reflect.ValueOf(v)); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func writeCanonical(out *bytes.Buffer, value reflect.Value) error {
	if !value.IsValid() {
		out.WriteString("null")
		return nil
	}
	for value.Kind() == reflect.Interface || value.Kind() == reflect.Pointer {
		if value.IsNil() {
			out.WriteString("null")
			return nil
		}
		value = value.Elem()
	}
	switch value.Kind() {
	case reflect.Struct:
		fields := map[string]reflect.Value{}
		typ := value.Type()
		for i := 0; i < value.NumField(); i++ {
			field := typ.Field(i)
			if field.PkgPath != "" {
				continue
			}
			name := strings.Split(field.Tag.Get("json"), ",")[0]
			if name == "-" {
				continue
			}
			if name == "" {
				name = field.Name
			}
			fields[name] = value.Field(i)
		}
		return writeCanonicalMap(out, fields)
	case reflect.Map:
		if value.Type().Key().Kind() != reflect.String {
			return fmt.Errorf("canonical JSON map keys must be strings")
		}
		fields := map[string]reflect.Value{}
		iter := value.MapRange()
		for iter.Next() {
			fields[iter.Key().String()] = iter.Value()
		}
		return writeCanonicalMap(out, fields)
	case reflect.Slice, reflect.Array:
		out.WriteByte('[')
		for i := 0; i < value.Len(); i++ {
			if i > 0 {
				out.WriteByte(',')
			}
			if err := writeCanonical(out, value.Index(i)); err != nil {
				return err
			}
		}
		out.WriteByte(']')
	case reflect.String:
		out.WriteString(strconv.Quote(value.String()))
	case reflect.Bool:
		out.WriteString(strconv.FormatBool(value.Bool()))
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		out.WriteString(strconv.FormatInt(value.Int(), 10))
	case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64:
		out.WriteString(strconv.FormatUint(value.Uint(), 10))
	case reflect.Float32, reflect.Float64:
		n := value.Float()
		if math.IsNaN(n) || math.IsInf(n, 0) {
			return errors.New("non-finite JSON number")
		}
		text := strconv.FormatFloat(n, 'f', -1, value.Type().Bits())
		if !strings.ContainsAny(text, ".eE") {
			text += ".0"
		}
		out.WriteString(text)
	default:
		return fmt.Errorf("unsupported canonical JSON value %s", value.Kind())
	}
	return nil
}

func writeCanonicalMap(out *bytes.Buffer, fields map[string]reflect.Value) error {
	keys := make([]string, 0, len(fields))
	for key := range fields {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	out.WriteByte('{')
	for i, key := range keys {
		if i > 0 {
			out.WriteByte(',')
		}
		out.WriteString(strconv.Quote(key))
		out.WriteByte(':')
		if err := writeCanonical(out, fields[key]); err != nil {
			return err
		}
	}
	out.WriteByte('}')
	return nil
}

func ValidateRecord(r Record) error {
	if r.RecordID == "" || r.DeviceID == "" || r.DeviceType == "" || r.Timestamp == "" {
		return errors.New("record identifiers, type, and timestamp are required")
	}
	if r.Metric != "active_power" || r.Unit != "kW" {
		return errors.New("records must use active_power in kW")
	}
	t, err := time.Parse(time.RFC3339Nano, r.Timestamp)
	if err != nil || t.Location() != time.UTC || !strings.HasSuffix(r.Timestamp, "Z") {
		return errors.New("timestamp must be UTC RFC3339")
	}
	if r.Value != r.Value || r.Value > 1e308 || r.Value < -1e308 {
		return errors.New("value must be finite")
	}
	switch r.DeviceType {
	case "photovoltaic":
		if r.Value < 0 || r.Value > 100 {
			return errors.New("PV power out of range")
		}
	case "battery_storage":
		if r.Value < -50 || r.Value > 50 {
			return errors.New("BESS power out of range")
		}
	case "charging_pile":
		if r.Value < 0 || r.Value > 22 {
			return errors.New("EVSE power out of range")
		}
	default:
		return errors.New("unsupported deviceType")
	}
	return nil
}

func ValidatePayload(p Payload) error {
	if p.ProtocolVersion != 1 || p.PacketID == "" || p.EdgeID == "" || len(p.Records) == 0 {
		return errors.New("invalid or empty protocol version 1 payload")
	}
	if p.RecordCount != len(p.Records) {
		return errors.New("recordCount mismatch")
	}
	start, err := time.Parse(time.RFC3339, p.WindowStart)
	if err != nil || start.Location() != time.UTC {
		return errors.New("invalid UTC windowStart")
	}
	end, err := time.Parse(time.RFC3339, p.WindowEnd)
	if err != nil || end.Location() != time.UTC || !end.After(start) {
		return errors.New("invalid UTC windowEnd")
	}
	seen := make(map[string]struct{}, len(p.Records))
	for _, r := range p.Records {
		if err := ValidateRecord(r); err != nil {
			return err
		}
		t, _ := time.Parse(time.RFC3339Nano, r.Timestamp)
		if t.Before(start) || !t.Before(end) {
			return errors.New("record timestamp outside half-open window")
		}
		if _, ok := seen[r.RecordID]; ok {
			return errors.New("duplicate recordId")
		}
		seen[r.RecordID] = struct{}{}
	}
	return nil
}

var leafDomain = []byte("FISCO-BCOS-EVIDENCE-LEAF-V1:")
var nodeDomain = []byte("FISCO-BCOS-EVIDENCE-NODE-V1:")

func MerkleRoot(records []Record) (string, error) {
	values := make([]any, len(records))
	for i := range records {
		values[i] = records[i]
	}
	return MerkleRootValues(values)
}

func MerkleRootValues(records []any) (string, error) {
	if len(records) == 0 {
		return "", errors.New("cannot build empty Merkle tree")
	}
	type leaf struct {
		id    string
		value any
	}
	ordered := make([]leaf, 0, len(records))
	for _, value := range records {
		encoded, err := json.Marshal(value)
		if err != nil {
			return "", err
		}
		var fields map[string]any
		if err = json.Unmarshal(encoded, &fields); err != nil {
			return "", err
		}
		id, ok := fields["recordId"].(string)
		if !ok || id == "" {
			return "", errors.New("recordId required")
		}
		ordered = append(ordered, leaf{id, value})
	}
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].id < ordered[j].id })
	level := make([][]byte, 0, len(ordered))
	for i, r := range ordered {
		if i > 0 && ordered[i-1].id == r.id {
			return "", fmt.Errorf("duplicate recordId %s", r.id)
		}
		data, err := Canonical(r.value)
		if err != nil {
			return "", err
		}
		h := sha256.Sum256(append(append([]byte{}, leafDomain...), data...))
		level = append(level, h[:])
	}
	for len(level) > 1 {
		next := make([][]byte, 0, (len(level)+1)/2)
		for i := 0; i < len(level); i += 2 {
			if i+1 == len(level) {
				next = append(next, level[i])
				continue
			}
			input := append(append(append([]byte{}, nodeDomain...), level[i]...), level[i+1]...)
			h := sha256.Sum256(input)
			next = append(next, h[:])
		}
		level = next
	}
	return hex.EncodeToString(level[0]), nil
}
