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

from centaur import cli  # noqa: E402
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


class CommandGroupHelpTests(unittest.TestCase):
    def test_workspace_without_subcommand_prints_workspace_help_and_returns_nonzero(self) -> None:
        output_buffer = io.StringIO()
        with redirect_stdout(output_buffer):
            rc = cli.main(["workspace"])
        output = output_buffer.getvalue()

        self.assertEqual(rc, 2)
        self.assertIn("usage: centaur workspace", output)
        self.assertIn("{create,list}", output)

    def test_task_without_subcommand_prints_task_help_and_returns_nonzero(self) -> None:
        output_buffer = io.StringIO()
        with redirect_stdout(output_buffer):
            rc = cli.main(["task"])
        output = output_buffer.getvalue()

        self.assertEqual(rc, 2)
        self.assertIn("usage: centaur task", output)
        self.assertIn("{list,new,switch,lint}", output)


class ErrorTemplateTests(unittest.TestCase):
    def assert_error_template(self, output: str) -> None:
        self.assertIn("[CLI_ERROR]", output)
        self.assertIn("[NEXT_STEP]", output)

    def test_workspace_list_missing_root_uses_unified_error_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_root = Path(tmp) / "missing-root"
            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["workspace", "list", "--root", str(missing_root)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("工作区根目录不存在", output)
            self.assert_error_template(output)

    def test_task_new_invalid_name_uses_unified_error_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "new", "_invalid", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("非法任务名", output)
            self.assert_error_template(output)

    def test_migrate_missing_path_uses_unified_error_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_workspace = Path(tmp) / "missing-workspace"
            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["migrate", str(missing_workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("目录不存在", output)
            self.assert_error_template(output)


class RunCommandGuardrailTests(unittest.TestCase):
    def test_run_rejects_from_role_without_force_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_buffer = io.StringIO()
            with patch("centaur.cli.run_workflow") as mock_run_workflow, redirect_stdout(output_buffer):
                rc = cli.main(["run", str(workspace), "--from-role", "worker"])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            mock_run_workflow.assert_not_called()
            self.assertIn("[CLI_ERROR]", output)
            self.assertIn("--force-from-role", output)
            self.assertIn("[NEXT_STEP]", output)

    def test_run_rejects_force_from_role_without_target_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_buffer = io.StringIO()
            with patch("centaur.cli.run_workflow") as mock_run_workflow, redirect_stdout(output_buffer):
                rc = cli.main(["run", str(workspace), "--force-from-role"])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            mock_run_workflow.assert_not_called()
            self.assertIn("[CLI_ERROR]", output)
            self.assertIn("--from-role", output)
            self.assertIn("[NEXT_STEP]", output)

    def test_run_allows_from_role_with_explicit_force_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            observed: dict[str, object] = {}

            def _fake_run_workflow(
                workdir: Path,
                start_step: str | None = None,
                allow_repo_root: bool = False,
                headless: bool = False,
            ) -> None:
                observed["workdir"] = workdir
                observed["start_step"] = start_step
                observed["allow_repo_root"] = allow_repo_root
                observed["headless"] = headless

            with patch("centaur.cli.run_workflow", side_effect=_fake_run_workflow):
                rc = cli.main(
                    [
                        "run",
                        str(workspace),
                        "--from-role",
                        "worker",
                        "--force-from-role",
                        "--allow-repo-root",
                        "--headless",
                    ]
                )

            self.assertEqual(rc, 0)
            self.assertEqual(observed["workdir"], workspace.resolve())
            self.assertEqual(observed["start_step"], "worker")
            self.assertEqual(observed["allow_repo_root"], True)
            self.assertEqual(observed["headless"], True)


class TaskLintCommandTests(unittest.TestCase):
    def test_task_lint_reports_blocked_spec_on_contract_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n"
                    "[CENTAUR_TASK_CONTRACT] "
                    '{"version":1,"unit":"text_exact","allowed_delta":["tests/scripts/test_recovery_auto.sh"]}\n'
                ),
                encoding="utf-8",
            )
            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("`unit=text_exact` 与 `allowed_delta` 冲突", output)

    def test_task_lint_passes_without_contract_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("结论: PASS", output)


if __name__ == "__main__":
    unittest.main()
