import importlib.util
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(os.environ.get("DOCS_PARSERS_CHECKOUT", Path(__file__).resolve().parents[1]))


class LamodaRequestPacingTest(unittest.TestCase):
    def test_consecutive_pages_stay_below_the_rate_limit(self):
        path = ROOT / "Lamoda Docs Parser" / "lamoda_docs_parser.py"
        spec = importlib.util.spec_from_file_location("lamoda_pacing_test", path)
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)

        clock = 0.0
        attempts = []

        def sleep(seconds):
            nonlocal clock
            clock += seconds

        def get(url, *, timeout):
            limited = bool(attempts) and clock - attempts[-1] < 2.0
            attempts.append(clock)
            return SimpleNamespace(status_code=503 if limited else 200, text=url)

        pages = [parser.API_ROOT, parser.API_ROOT + "intro/"]
        with (
            patch.object(parser, "time", SimpleNamespace(sleep=sleep)),
            patch.object(parser, "_SESSION", SimpleNamespace(get=get)),
        ):
            self.assertEqual([parser.fetch_text(url) for url in pages], pages)
        self.assertEqual(len(attempts), len(pages), "Пауза должна предотвращать HTTP 503 при обходе")


if __name__ == "__main__":
    unittest.main()
