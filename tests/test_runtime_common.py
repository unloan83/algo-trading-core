import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import scripts.runtime_common as runtime_common


class TestRuntimeCommon(unittest.TestCase):
    def test_save_market_filters_records_generation_timestamp(self):
        generated_at = datetime(2026, 9, 22, 10, 45, 0)

        with tempfile.TemporaryDirectory() as tmp:
            script_dir = Path(tmp) / "scripts"
            script_dir.mkdir()
            with patch.object(
                runtime_common,
                "__file__",
                str(script_dir / "runtime_common.py"),
            ), patch.object(
                runtime_common,
                "now_ist_naive",
                return_value=generated_at,
            ):
                path = runtime_common.save_market_filters(
                    ["HALTED", "HALTED"],
                    ["ACTION", "ACTION"],
                )

            payload = json.loads(Path(path).read_text(encoding="utf-8"))

        self.assertEqual(payload["date"], "2026-09-22")
        self.assertEqual(payload["generated_at"], "2026-09-22T10:45:00")
        self.assertEqual(payload["halted"], ["HALTED"])
        self.assertEqual(payload["corporate_actions"], ["ACTION"])


if __name__ == "__main__":
    unittest.main()
