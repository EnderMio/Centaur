from contextlib import redirect_stdout
import argparse
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from centaur.cli import cmd_doctor  # noqa: E402


class DoctorCommandTests(unittest.TestCase):
    def test_doctor_passes_when_log_dir_is_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            args = argparse.Namespace(path=str(workspace), workspace=None, allow_repo_root=True)

            output_buffer = io.StringIO()
            with patch("centaur.cli.codex_available", return_value=True), patch(
                "centaur.cli.collect_prompt_mode_issues", return_value=([], [])
            ), redirect_stdout(output_buffer):
                rc = cmd_doctor(args)
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("结论: PASS", output)

    def test_doctor_fails_when_log_dir_is_not_writable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / ".centaur").mkdir(parents=True, exist_ok=True)
            (workspace / ".centaur" / "logs").write_text("not a directory", encoding="utf-8")
            args = argparse.Namespace(path=str(workspace), workspace=None, allow_repo_root=True)

            output_buffer = io.StringIO()
            with patch("centaur.cli.codex_available", return_value=True), patch(
                "centaur.cli.collect_prompt_mode_issues", return_value=([], [])
            ), redirect_stdout(output_buffer):
                rc = cmd_doctor(args)
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("日志目录不可写", output)
            self.assertIn("结论: FAIL", output)


if __name__ == "__main__":
    unittest.main()
