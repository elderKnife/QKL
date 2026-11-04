"""Tests for the read-only visualization dashboard (static assets + wiring)."""
from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

STATIC = os.path.join(PROJECT_ROOT, "src", "web", "static")


class WebAssetsTests(unittest.TestCase):
    def test_static_assets_exist(self):
        for name in ("index.html", "styles.css", "app.js"):
            path = os.path.join(STATIC, name)
            self.assertTrue(os.path.exists(path), f"missing {name}")

    def test_index_wires_assets(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn("/static/styles.css", html)
        self.assertIn("/static/app.js", html)
        self.assertIn('id="chain-pill"', html)
        self.assertIn('id="batch-list"', html)
        self.assertIn('id="detail"', html)

    def test_frontend_calls_expected_endpoints(self):
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        for endpoint in ("/api/overview", "/api/batches", "/api/feed", "/api/tamper",
                         "/api/collection", "/api/commands", "/api/meta"):
            self.assertIn(endpoint, js)

    def test_record_json_modal_wiring(self):
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id="record-modal"', html)
        self.assertIn('id="record-code"', html)
        self.assertIn('id="record-download"', html)
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        for token in ("openRecordModal", "RECORD_FIELD_NOTES", "recordJsonText", "data-record"):
            self.assertIn(token, js, f"missing {token} in app.js")

    def test_record_meta_is_exposed_by_backend(self):
        with open(os.path.join(PROJECT_ROOT, "src", "web", "service.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"recordMeta"', source)
        self.assertIn("leaf_hash", source)

    def test_dashboard_does_not_import_trusted_path_modules(self):
        with open(os.path.join(PROJECT_ROOT, "src", "web", "service.py"), encoding="utf-8") as handle:
            source = handle.read()
        # Read-only aggregation: no signing keys, no transaction submission.
        for forbidden in ("Signer", "register_batch", "sendRawTransaction"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
