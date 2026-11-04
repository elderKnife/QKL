"""Tests for the downlink command/ack protocol (one command = one anchor)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.common.canonical import canonical_bytes  # noqa: E402
from src.common.merkle import object_root_hex  # noqa: E402
from src.common.models import (  # noqa: E402
    ACK_ENVELOPE_SCHEMA,
    ACK_SCHEMA,
    COMMAND_SCHEMA,
    ProtocolError,
    ack_batch_id,
    command_batch_id,
    validate_ack,
    validate_ack_envelope,
    validate_command,
)


def a_command(**overrides) -> dict:
    command = {
        "schemaVersion": COMMAND_SCHEMA,
        "commandId": "CMD-TEST-001",
        "gridId": "grid",
        "targetEdgeId": "edge-01",
        "targetDeviceId": "PV-101",
        "action": "set_voltage",
        "params": {"voltageV": 230.5},
        "issuedAt": "2026-11-04T10:00:00.000Z",
        "expiresAt": "2026-11-04T10:01:00.000Z",
        "nonce": "nonce-1",
    }
    command.update(overrides)
    return command


def an_ack(**overrides) -> dict:
    ack = {
        "schemaVersion": ACK_SCHEMA,
        "ackId": "ACK-TEST-001",
        "commandId": "CMD-TEST-001",
        "commandDigest": "a" * 64,
        "edgeId": "edge-01",
        "deviceId": "PV-101",
        "action": "set_voltage",
        "result": "APPLIED",
        "appliedState": {"on": True, "voltageV": 230.5},
        "executedAt": "2026-11-04T10:00:01.000Z",
    }
    ack.update(overrides)
    return ack


class CommandValidationTests(unittest.TestCase):
    def test_valid_command(self):
        command = validate_command(a_command())
        self.assertEqual(command["params"], {"voltageV": 230.5})

    def test_voltage_range_rejected(self):
        for voltage in (179.9, 260.1):
            with self.assertRaises(ProtocolError):
                validate_command(a_command(params={"voltageV": voltage}))

    def test_voltage_precision_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_command(a_command(params={"voltageV": 230.55}))

    def test_power_action_must_not_have_params(self):
        with self.assertRaises(ProtocolError):
            validate_command(a_command(action="power_on", params={"voltageV": 230.0}))
        validate_command(a_command(action="power_off", params={}))

    def test_unknown_action_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_command(a_command(action="reboot"))

    def test_ttl_limits(self):
        with self.assertRaises(ProtocolError):
            validate_command(a_command(expiresAt="2026-11-04T10:00:00.000Z"))
        with self.assertRaises(ProtocolError):
            validate_command(a_command(expiresAt="2026-11-04T11:00:00.000Z"))

    def test_ids_are_used_for_anchor_batches(self):
        self.assertEqual(command_batch_id("CMD-1"), "BATCH-CMD-CMD-1")
        self.assertEqual(ack_batch_id("ACK-1"), "BATCH-ACK-ACK-1")

    def test_object_root_is_stable_and_sensitive(self):
        command = validate_command(a_command())
        root = object_root_hex(command, canonical_bytes)
        self.assertEqual(len(root), 64)
        self.assertEqual(root, object_root_hex(dict(reversed(list(command.items()))), canonical_bytes))
        changed = dict(command, params={"voltageV": 231.0})
        self.assertNotEqual(root, object_root_hex(changed, canonical_bytes))


class AckValidationTests(unittest.TestCase):
    def test_valid_ack(self):
        validate_ack(an_ack())

    def test_bad_result_rejected(self):
        with self.assertRaises(ProtocolError):
            validate_ack(an_ack(result="MAYBE"))

    def test_applied_state_requires_bool(self):
        with self.assertRaises(ProtocolError):
            validate_ack(an_ack(appliedState={"on": "yes"}))

    def test_ack_envelope(self):
        command = validate_command(a_command())
        ack = validate_ack(an_ack(commandDigest="b" * 64))
        envelope = {
            "schemaVersion": ACK_ENVELOPE_SCHEMA,
            "vppId": "vpp-a",
            "ack": ack,
            "command": command,
            "builtAt": "2026-11-04T10:00:02.000Z",
        }
        validate_ack_envelope(envelope)


if __name__ == "__main__":
    unittest.main()
