import importlib.util
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).parent


class ApiConcurrencyTests(unittest.TestCase):
    def test_shared_slot_limits_concurrent_calls(self):
        import api_concurrency

        original = api_concurrency._api_semaphore
        api_concurrency._api_semaphore = threading.BoundedSemaphore(1)
        active = 0
        peak = 0
        lock = threading.Lock()

        def call_api():
            nonlocal active, peak
            with api_concurrency.api_call_slot():
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.03)
                with lock:
                    active -= 1

        try:
            threads = [threading.Thread(target=call_api) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            api_concurrency._api_semaphore = original
        self.assertEqual(peak, 1)

    def test_translation_and_metadata_use_shared_slot(self):
        for filename in ("pdf_translate.py", "paper_metadata.py"):
            source = (ROOT / filename).read_text(encoding="utf-8")
            self.assertIn("from api_concurrency import api_call_slot", source)
            self.assertIn("with api_call_slot():", source)

    def test_config_example_exposes_shared_translation_api_limit(self):
        config_path = ROOT / "config.example.py"
        text = config_path.read_text(encoding="utf-8")
        self.assertIn("TRANSLATE_API_MAX_CONCURRENCY", text)
        self.assertIn("PAPER_TRANSLATE_API_MAX_CONCURRENCY", text)


if __name__ == "__main__":
    unittest.main()
