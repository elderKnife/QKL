"""Mapping tests for the console compatibility layer."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.console import service  # noqa: E402


class ConsoleMappingTests(unittest.TestCase):
    def test_ui_edge_labels(self):
        self.assertEqual(service.ui_edge_label("edge-01"), "EDGE-001")
        self.assertEqual(service.ui_edge_label("edge-02"), "EDGE-002")
        self.assertEqual(service.ui_edge_label("edge-03"), "EDGE-003")
        self.assertEqual(service.ui_edge_label("edge-04"), "EDGE-004")

    def test_source_aliases(self):
        self.assertEqual(service.edge_for_ui("EDGE-001"), "edge-01")
        self.assertIsNone(service.edge_for_ui("EDGE-999"))
        self.assertEqual(service.ui_edge_for_source("edge"), "EDGE-001")
        self.assertEqual(service.ui_edge_for_source("edge2"), "EDGE-002")
        self.assertEqual(service.vpp_for_source("vpp"), "vpp-a")

    def test_batch_and_packet_ids(self):
        self.assertEqual(service.batch_id_from_packet("PKT-1"), "BATCH-PKT-1")
        self.assertEqual(service.packet_id_from_batch("BATCH-PKT-1"), "PKT-1")
        self.assertEqual(service.packet_id_from_batch("PKT-1"), "PKT-1")

    def test_power_ranges_and_device_types(self):
        self.assertEqual(service.DEVICE_TYPE["pv"], "photovoltaic")
        self.assertEqual(service.DEVICE_TYPE["storage"], "battery_storage")
        self.assertEqual(service.DEVICE_TYPE["charger"], "charging_pile")
        self.assertEqual(service.POWER_RANGE["pv"], (0, 50))

    def test_ui_topology_matches_vpp_a(self):
        self.assertEqual(service.UI_EDGES["EDGE-001"], "edge-01")
        self.assertEqual(service.UI_EDGES["EDGE-002"], "edge-02")
        self.assertEqual(service.EDGE_SPECS["edge-01"]["vppId"], "vpp-a")
        self.assertEqual(service.EDGE_SPECS["edge-02"]["vppId"], "vpp-a")


if __name__ == "__main__":
    unittest.main()
