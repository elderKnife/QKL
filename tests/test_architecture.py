"""Architecture tests: the edge side must not depend on the blockchain.

These tests are the machine-checkable part of the experiment's central claim:
edge <-> VPP uses a direct data link, and only VPP <-> grid touches the chain.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

CHAIN_TOKENS = ("src.chain", "bcosclient", "client.bcosclient", "web3", "fisco")


class EdgeHasNoChainDependency(unittest.TestCase):
    def test_edge_service_source_has_no_chain_imports(self):
        path = os.path.join(PROJECT_ROOT, "src", "edge", "service.py")
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
        for token in CHAIN_TOKENS:
            self.assertNotIn(f"import {token}", source, f"edge service must not import {token}")
            self.assertNotIn(f"from {token}", source, f"edge service must not import from {token}")

    def test_importing_edge_service_does_not_load_sdk(self):
        code = (
            "import sys;"
            f"sys.path.insert(0, {PROJECT_ROOT!r});"
            "import src.edge.service;"
            "assert 'client.bcosclient' not in sys.modules, 'chain SDK loaded in edge process';"
            "assert not any(m.startswith('src.chain') for m in sys.modules), 'chain adapter loaded in edge process';"
            "print('edge process has no chain modules')"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=PROJECT_ROOT)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no chain modules", result.stdout)

    def test_vpp_and_grid_do_use_chain(self):
        for module in ("src.vpp.service", "src.grid.service"):
            path = os.path.join(PROJECT_ROOT, *module.split(".")) + ".py"
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            self.assertIn("..chain.factory", source, f"{module} should use the chain adapter")

    def test_topology_is_four_edges_ten_devices_each(self):
        import json

        with open(os.path.join(PROJECT_ROOT, "config", "system.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        self.assertEqual(len(config["edges"]), 4)
        all_devices = []
        for edge in config["edges"]:
            self.assertEqual(len(edge["devices"]), 10, edge["edgeId"])
            all_devices.extend(device["deviceId"] for device in edge["devices"])
        self.assertEqual(len(all_devices), 40)
        self.assertEqual(len(set(all_devices)), 40, "device ids must be globally unique")

    def test_each_vpp_serves_two_edges(self):
        import json

        with open(os.path.join(PROJECT_ROOT, "config", "system.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        mapping = {}
        for edge in config["edges"]:
            mapping.setdefault(edge["vppId"], []).append(edge["edgeId"])
        self.assertEqual(sorted(mapping["vpp-a"]), ["edge-01", "edge-02"])
        self.assertEqual(sorted(mapping["vpp-b"]), ["edge-03", "edge-04"])
        for vpp in config["vpps"]:
            self.assertEqual(len(vpp["trustedEdges"]), 2)
            self.assertIn("trustedGrid", vpp)
        for edge in config["edges"]:
            self.assertIn("trustedGrid", edge)

    def test_edge_config_has_no_chain_section(self):
        import json

        with open(os.path.join(PROJECT_ROOT, "config", "system.json"), encoding="utf-8") as handle:
            config = json.load(handle)
        for edge in config["edges"]:
            self.assertNotIn("chain", edge)
            self.assertNotIn("account", edge)


if __name__ == "__main__":
    unittest.main()
