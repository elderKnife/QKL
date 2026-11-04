"""Unit tests for the wire models (records, packets, batch envelopes)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.common.models import (  # noqa: E402
    PACKET_SCHEMA,
    ProtocolError,
    batch_id_for,
    validate_batch_envelope,
    validate_packet,
    validate_record,
)


def a_record(**overrides) -> dict:
    record = {
        "recordId": "REC-1",
        "deviceId": "PV-101",
        "deviceType": "pv",
        "metric": "active_power_kw",
        "value": 12.345,
        "unit": "kW",
        "timestamp": "2026-11-04T10:00:00.123Z",
    }
    record.update(overrides)
    return record


def a_packet(**overrides) -> dict:
    packet = {
        "schemaVersion": PACKET_SCHEMA,
        "packetId": "PKT-1",
        "edgeId": "edge-01",
        "collectedAt": "2026-11-04T10:00:01.000Z",
        "records": [a_record()],
    }
    packet.update(overrides)
    return packet


class RecordValidationTests(unittest.TestCase):
    def test_valid_record_normalised(self):
        record = validate_record(a_record())
        self.assertEqual(record["value"], 12.345)

    def test_missing_field_rejected(self):
        record = a_record()
        del record["deviceId"]
        with self.assertRaises(ProtocolError) as ctx:
            validate_record(record)
        self.assertEqual(ctx.exception.code, "INVALID_SCHEMA")

    def test_extra_field_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_record(a_record(extra="x"))

    def test_non_finite_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_record(a_record(value=float("inf")))

    def test_precision_beyond_three_decimals_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_record(a_record(value=1.2345))

    def test_negative_pv_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_record(a_record(value=-1.0))

    def test_storage_may_be_negative(self):
        validate_record(a_record(deviceType="storage", value=-3.5))

    def test_bad_timestamp_rejected(self):
        for ts in ("2026-11-04 10:00:00", "2026-11-04T10:00:00Z", "2026-13-04T10:00:00.000Z"):
            with self.assertRaises(ProtocolError):
                validate_record(a_record(timestamp=ts))

    def test_bad_metric_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_record(a_record(metric="voltage"))


class PacketValidationTests(unittest.TestCase):
    def test_valid_packet(self):
        packet = validate_packet(a_packet())
        self.assertEqual(packet["schemaVersion"], PACKET_SCHEMA)

    def test_unknown_schema_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_packet(a_packet(schemaVersion="EDGE-PACKET-V0"))

    def test_duplicate_record_ids_rejected(self):
        packet = a_packet(records=[a_record(), a_record(deviceId="PV-102")])
        with self.assertRaises(ProtocolError) as ctx:
            validate_packet(packet)
        self.assertEqual(ctx.exception.code, "DUPLICATE_ID")

    def test_empty_records_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_packet(a_packet(records=[]))

    def test_batch_id_derivation_is_stable(self):
        self.assertEqual(batch_id_for("PKT-1"), "BATCH-PKT-1")


class BatchEnvelopeTests(unittest.TestCase):
    def valid_envelope(self) -> dict:
        return {
            "schemaVersion": "VPP-BATCH-V1",
            "batchId": "BATCH-PKT-1",
            "vppId": "vpp-a",
            "edgeId": "edge-01",
            "packetId": "PKT-1",
            "recordCount": 1,
            "rootHash": "aa" * 32,
            "builtAt": "2026-11-04T10:00:02.000Z",
            "merkleRuleVersion": "MERKLE-V1",
            "canonicalRuleVersion": "CANON-JSON-V1",
            "records": [a_record()],
            "proofs": [{"ruleVersion": "MERKLE-V1", "recordId": "REC-1", "index": 0, "treeSize": 1, "siblings": []}],
        }

    def test_valid(self):
        validate_batch_envelope(self.valid_envelope())

    def test_count_mismatch_rejected(self):
        envelope = self.valid_envelope()
        envelope["recordCount"] = 2
        with self.assertRaises(ProtocolError):
            validate_batch_envelope(envelope)

    def test_missing_proofs_rejected(self):
        envelope = self.valid_envelope()
        envelope["proofs"] = []
        with self.assertRaises(ProtocolError):
            validate_batch_envelope(envelope)

    def test_bad_root_rejected(self):
        envelope = self.valid_envelope()
        envelope["rootHash"] = "zz" * 32
        with self.assertRaises(ProtocolError):
            validate_batch_envelope(envelope)


if __name__ == "__main__":
    unittest.main()
