package prototype

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"

	secp "github.com/decred/dcrd/dcrec/secp256k1/v4"
)

func TestCanonicalJSONMatchesHistoricalPythonFormat(t *testing.T) {
	r := Record{RecordID: "PV-01-1", DeviceID: "PV-01", DeviceType: "photovoltaic", Metric: "active_power", Value: 12, Unit: "kW", Timestamp: "2026-11-03T12:00:00.000Z"}
	got, err := Canonical(r)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"deviceId":"PV-01","deviceType":"photovoltaic","metric":"active_power","recordId":"PV-01-1","timestamp":"2026-11-03T12:00:00.000Z","unit":"kW","value":12.0}`
	if string(got) != want {
		t.Fatalf("canonical mismatch\n got: %s\nwant: %s", got, want)
	}
}

func TestHistoricalMerkleVector(t *testing.T) {
	data, err := os.ReadFile("../../examples/historical/A/edge_records.json")
	if err != nil {
		t.Fatal(err)
	}
	var input struct {
		Records []any `json:"records"`
	}
	if err = json.Unmarshal(data, &input); err != nil {
		t.Fatal(err)
	}
	vector, err := os.ReadFile("../../examples/historical/A/merkle_inputs.json")
	if err != nil {
		t.Fatal(err)
	}
	var expected struct {
		Root string `json:"merkleRoot"`
	}
	if err = json.Unmarshal(vector, &expected); err != nil {
		t.Fatal(err)
	}
	got, err := MerkleRootValues(input.Records)
	if err != nil {
		t.Fatal(err)
	}
	if got != expected.Root {
		t.Fatalf("root mismatch: got %s want %s", got, expected.Root)
	}
}

func TestConfiguredDeviceProfiles(t *testing.T) {
	first,err:=ConfiguredDevicesForEdge("EDGE-001");if err!=nil{t.Fatal(err)}
	second,err:=ConfiguredDevicesForEdge("EDGE-002");if err!=nil{t.Fatal(err)}
	if len(first)!=5||len(second)!=30{t.Fatalf("device counts edge1=%d edge2=%d",len(first),len(second))}
	counts:=map[string]int{};ids:=map[string]bool{}
	for _,spec:=range second{counts[spec.Type]++;if ids[spec.ID]{t.Fatalf("duplicate edge2 device %s",spec.ID)};ids[spec.ID]=true}
	if counts["photovoltaic"]!=12||counts["battery_storage"]!=12||counts["charging_pile"]!=6{t.Fatalf("unexpected device type distribution: %+v",counts)}
}

func TestFiveDevicePowerBoundsAndSignatureTamperDetection(t *testing.T) {
	if len(DeviceSpecs) != 5 {
		t.Fatalf("got %d device specs", len(DeviceSpecs))
	}
	for _, spec := range DeviceSpecs {
		value := spec.Min + (spec.Max-spec.Min)*0.5
		record := Record{RecordID: spec.ID + "-r1", DeviceID: spec.ID, DeviceType: spec.Type, Metric: "active_power", Value: value, Unit: "kW", Timestamp: "2026-11-03T12:00:00.000Z"}
		if err := ValidateRecord(record); err != nil {
			t.Fatalf("%s: %v", spec.ID, err)
		}
	}
	priv, err := secp.GeneratePrivateKeyFromRand(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	data := []byte(`{"hello":"world"}`)
	sig := Sign(priv, data)
	pubHex := hex.EncodeToString(priv.PubKey().SerializeCompressed())
	if err = Verify(pubHex, data, sig); err != nil {
		t.Fatal(err)
	}
	if err = Verify(pubHex, []byte(`{"hello":"tampered"}`), sig); err == nil {
		t.Fatal("tampered payload verified")
	}
}
