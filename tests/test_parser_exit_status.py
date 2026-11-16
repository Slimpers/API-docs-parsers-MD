import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(os.environ.get("DOCS_PARSERS_CHECKOUT", Path(__file__).resolve().parents[1]))


def load(name):
    folder = "YA" if name == "ya" else name.title()
    spec = importlib.util.spec_from_file_location(
        name, ROOT / f"{folder} Docs Parser" / f"{name}_docs_parser.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


class IncompleteUpdate(unittest.TestCase):
    def setUp(self):
        self.out = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"DOCS_OUT_ROOT": str(self.out)}))
        self.index = self.out / "_README.md"
        self.index.write_text("previous index", encoding="utf-8")

    def test_wb_and_ozon_report_failed_or_empty_sections(self):
        for name, fetch_name in [("wb", "fetch_state"), ("ozon", "fetch_spec")]:
            mod = load(name)
            browser = Mock(pages=[])
            chromium = SimpleNamespace(launch=Mock(return_value=browser),
                                       launch_persistent_context=Mock(return_value=browser))
            stealth = SimpleNamespace(use_sync=lambda _context: contextlib.nullcontext(
                SimpleNamespace(chromium=chromium)))
            for response, count in [(None, 0), ({"spec": {"data": {}}}, 0)]:
                with (
                    self.subTest(name=name, response=response),
                    patch.object(mod, "Stealth", return_value=stealth),
                    patch.object(mod, "sync_playwright"),
                    patch.object(mod, fetch_name, return_value=response),
                    patch.object(mod, "build_for_slug", return_value=count),
                    patch.object(mod, "PROFILE_DIR", self.out / "profile", create=True),
                ):
                    self.assertEqual(mod.run(["test"]), 1)
                    self.assertEqual(self.index.read_text(encoding="utf-8"), "previous index")

    def test_lamoda_reports_missing_pages_and_preserves_index(self):
        mod = load("lamoda")
        with patch.object(mod, "fetch_text", return_value=None):
            self.assertEqual(mod.run(), 1)
        self.assertEqual(self.index.read_text(encoding="utf-8"), "previous index")

    def test_yandex_rejects_spec_without_operations(self):
        mod = load("ya")
        with (
            patch.object(mod, "fetch_repo", return_value=self.out),
            patch.object(mod, "Bundler") as bundler,
        ):
            bundler.return_value.bundle.return_value = {"paths": {}}
            with self.assertRaisesRegex(RuntimeError, "не содержит операций"):
                mod.run("partner-api", "main", True)
        self.assertEqual(self.index.read_text(encoding="utf-8"), "previous index")

    def test_orchestrator_reports_early_failure_to_dashboard(self):
        report = self.out / "result.json"
        run = subprocess.run(
            [sys.executable, "-B", str(ROOT / "update_docs_bases.py"),
             "--root", str(self.out / "missing"), "--result-file", str(report)],
            capture_output=True,
        )
        self.assertEqual(run.returncode, 1, run.stderr.decode("utf-8", errors="replace"))
        outcome = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(outcome["exit_code"], 1)
        self.assertTrue(outcome["finished"])


if __name__ == "__main__":
    unittest.main()
