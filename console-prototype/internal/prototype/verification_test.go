package prototype

import (
	"crypto/rand"
	"encoding/hex"
	"testing"

	secp "github.com/decred/dcrd/dcrec/secp256k1/v4"
)

func verificationFixture(t *testing.T) (Payload, string, string, string, string, map[string]any) {
	t.Helper()
	p := Payload{ProtocolVersion: 1, PacketID: "EDGE-001-proof-test", EdgeID: "EDGE-001", WindowStart: "2026-11-04T00:00:00Z", WindowEnd: "2026-11-04T00:05:00Z", RecordCount: 2, Records: []Record{
		{RecordID: "PV-01-one", DeviceID: "PV-01", DeviceType: "photovoltaic", Metric: "active_power", Value: 12.25, Unit: "kW", Timestamp: "2026-11-04T00:01:00.000Z"},
		{RecordID: "BESS-01-one", DeviceID: "BESS-01", DeviceType: "battery_storage", Metric: "active_power", Value: -3.5, Unit: "kW", Timestamp: "2026-11-04T00:02:00.000Z"},
	}}
	canonical, err := Canonical(p)
	if err != nil {
		t.Fatal(err)
	}
	key, err := secp.GeneratePrivateKeyFromRand(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	sig := Sign(key, canonical)
	pub := hex.EncodeToString(key.PubKey().SerializeCompressed())
	root, err := MerkleRoot(p.Records)
	if err != nil {
		t.Fatal(err)
	}
	hash := sha256Hex(canonical)
	summary := map[string]any{"packetId": p.PacketID, "edgeId": p.EdgeID, "recordCount": int64(p.RecordCount), "merkleRoot": root, "windowStart": p.WindowStart, "windowEnd": p.WindowEnd, "status": "SIMULATED_REGISTERED"}
	return p, string(canonical), sig, pub, hash, summary
}

func TestVPPReverificationPassesOriginalBatch(t *testing.T) {
	p, raw, sig, pub, hash, summary := verificationFixture(t)
	root, _ := MerkleRoot(p.Records)
	v := newVerification(p.PacketID)
	evaluateVerification(&v, p, raw, sig, pub, hash, root, p.Records, summary, "")
	if v.Status != "PASS" {
		t.Fatalf("status=%s checks=%+v", v.Status, v.Checks)
	}
	if len(v.Checks) != 4 || numeric(summary["recordCount"]) != 2 {
		t.Fatalf("verification did not return four passing checks: %+v", v.Checks)
	}
}

func TestVPPReverificationSeparatesTamperingFromMissingSummary(t *testing.T) {
	p, raw, sig, pub, hash, summary := verificationFixture(t)
	root, _ := MerkleRoot(p.Records)
	changedPayload := p
	changedPayload.Records = append([]Record(nil), p.Records...)
	changedPayload.Records[0].Value++
	v := newVerification(p.PacketID)
	evaluateVerification(&v, changedPayload, raw, sig, pub, hash, root, p.Records, summary, "")
	if v.Status != "FAIL" || v.Checks[0].Status != "FAIL" || v.Checks[1].Status != "FAIL" || v.Checks[2].Status != "FAIL" {
		t.Fatalf("tampered payload dependent checks did not fail: %+v", v)
	}

	v = newVerification(p.PacketID)
	evaluateVerification(&v, p, raw, sig, pub, hash, root, p.Records, nil, "")
	if v.Status != "FAIL" && v.Status != "ERROR" {
		t.Fatalf("missing summary was reported as %s", v.Status)
	}
	if v.Checks[len(v.Checks)-1].Status != "ERROR" {
		t.Fatalf("missing summary must be an ERROR: %+v", v.Checks)
	}
}

func TestVPPReverificationChecksSavedRecordCopyAndUntrustedKey(t *testing.T) {
	p, raw, sig, pub, hash, summary := verificationFixture(t)
	root, _ := MerkleRoot(p.Records)
	changed := append([]Record(nil), p.Records...)
	changed[0].Value++
	v := newVerification(p.PacketID)
	evaluateVerification(&v, p, raw, sig, pub, hash, root, changed, summary, "")
	if v.Checks[2].Status != "FAIL" {
		t.Fatalf("altered database record was not detected: %+v", v.Checks)
	}
	v = newVerification(p.PacketID)
	evaluateVerification(&v, p, raw, sig, "", hash, root, p.Records, summary, "")
	if v.Checks[1].Status != "ERROR" {
		t.Fatalf("missing trusted key must be ERROR: %+v", v.Checks)
	}
}
