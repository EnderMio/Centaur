from contextlib import redirect_stdout
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from centaur.engine import (  # noqa: E402
    DEFAULT_TASK_NAME,
    EVENTS_FILE,
    PROJECT_SCHEMA_VERSION,
    RUNTIME_DIR,
    ensure_runtime_layout,
    load_or_init_project_config,
    migrate_schema,
    run_agent,
    run_workflow,
    sync_task_bus_to_active,
    task_file_path,
    validate_task_name,
    ensure_active_task_file,
)


class EngineRuntimeTests(unittest.TestCase):
    def test_default_project_config_schema_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            config = load_or_init_project_config(workspace)
            self.assertEqual(config["schema_version"], PROJECT_SCHEMA_VERSION)
            self.assertIn("active_task", config)
            self.assertIn("controller_version", config)
            self.assertIn("target_repo", config)
            self.assertIn("target_ref", config)
            self.assertIn("target_version", config)

    def test_active_task_file_sync(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\nA", encoding="utf-8")
            config = load_or_init_project_config(workspace)
            active_task, active_path = ensure_active_task_file(workspace, config)

            self.assertEqual(active_task, DEFAULT_TASK_NAME)
            self.assertTrue(active_path.exists())
            self.assertEqual(active_path.read_text(encoding="utf-8"), (workspace / "TASK.md").read_text(encoding="utf-8"))

            (workspace / "TASK.md").write_text("# 当前任务 (Task)\nB", encoding="utf-8")
            sync_task_bus_to_active(workspace, active_task)
            self.assertEqual(active_path.read_text(encoding="utf-8"), "# 当前任务 (Task)\nB")

    def test_migrate_schema_from_legacy_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\nlegacy", encoding="utf-8")
            (workspace / ".centaur_project.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "centaur_version": "0.1.0",
                        "prompt_set_version": "old",
                        "prompt_mode": "global",
                    }
                ),
                encoding="utf-8",
            )
            (workspace / ".centaur_state.json").write_text(
                json.dumps({"cycle": 3, "next_step": "worker"}),
                encoding="utf-8",
            )

            config = migrate_schema(workspace)
            self.assertEqual(config["schema_version"], PROJECT_SCHEMA_VERSION)
            self.assertEqual(config["active_task"], DEFAULT_TASK_NAME)
            self.assertTrue((workspace / RUNTIME_DIR / "project.json").exists())
            self.assertTrue((workspace / RUNTIME_DIR / "state.json").exists())
            self.assertTrue(task_file_path(workspace, DEFAULT_TASK_NAME).exists())

    def test_validate_task_name(self) -> None:
        self.assertTrue(validate_task_name("task-001"))
        self.assertTrue(validate_task_name("a.b_c-1"))
        self.assertFalse(validate_task_name(" bad"))
        self.assertFalse(validate_task_name(""))
        self.assertFalse(validate_task_name("!invalid"))

    @patch("centaur.engine.resolve_prompt_content", return_value=("worker prompt", "测试模板"))
    @patch("centaur.engine.subprocess.run")
    def test_run_agent_log_written_on_success(self, mock_run, _mock_resolve_prompt) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "--full-auto", "worker prompt"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_agent("Worker", "WORKER.md", workspace, "global", cycle=2)
            log_path = workspace / ".centaur" / "logs" / "cycle_2_worker.log"
            self.assertTrue(log_path.exists())
            payload = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["command"], ["codex", "--full-auto", "worker prompt"])
            self.assertTrue(payload["start_time"])
            self.assertTrue(payload["end_time"])
            self.assertEqual(payload["return_code"], 0)
            self.assertEqual(payload["stdout"], "ok\n")
            self.assertEqual(payload["stderr"], "")

    @patch("centaur.engine.resolve_prompt_content", return_value=("validator prompt", "测试模板"))
    @patch("centaur.engine.subprocess.run")
    def test_run_agent_log_written_on_failure(self, mock_run, _mock_resolve_prompt) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "--full-auto", "validator prompt"],
            returncode=3,
            stdout="partial\n",
            stderr="boom\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaises(SystemExit):
                run_agent("Validator", "VALIDATOR.md", workspace, "global", cycle=4)
            log_path = workspace / ".centaur" / "logs" / "cycle_4_validator.log"
            self.assertTrue(log_path.exists())
            payload = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["command"], ["codex", "--full-auto", "validator prompt"])
            self.assertTrue(payload["start_time"])
            self.assertTrue(payload["end_time"])
            self.assertEqual(payload["return_code"], 3)
            self.assertEqual(payload["stdout"], "partial\n")
            self.assertEqual(payload["stderr"], "boom\n")

    @patch("centaur.engine.resolve_prompt_content", return_value=("role prompt", "测试模板"))
    @patch("centaur.engine.subprocess.run")
    def test_event_log_jsonl_append_and_contract(self, mock_run, _mock_resolve_prompt) -> None:
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["codex", "--full-auto", "role prompt"],
                returncode=0,
                stdout="validator-ok\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "--full-auto", "role prompt"],
                returncode=2,
                stdout="supervisor-partial\n",
                stderr="supervisor-error\n",
            ),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 1, "next_step": "validator"}),
                encoding="utf-8",
            )

            with self.assertRaises(SystemExit):
                run_workflow(workdir=workspace)

            events_path = workspace / RUNTIME_DIR / EVENTS_FILE
            self.assertTrue(events_path.exists())
            lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertGreaterEqual(len(lines), 6)
            events = [json.loads(line) for line in lines]

            event_types = {event["event_type"] for event in events}
            self.assertIn("cycle_start", event_types)
            self.assertIn("cycle_end", event_types)
            self.assertIn("role_start", event_types)
            self.assertIn("role_end", event_types)

            for event in events:
                self.assertIn("timestamp", event)
                self.assertIn("cycle", event)
                self.assertIn("event_type", event)
                if event["event_type"] in {"role_start", "role_end"}:
                    self.assertIn("role", event)
                if event["event_type"] == "role_end":
                    self.assertIn("return_code", event)

    @patch("centaur.engine.resolve_prompt_content", return_value=("worker prompt", "测试模板"))
    @patch("centaur.engine.subprocess.run")
    def test_fail_fast_when_event_log_cannot_be_written(self, mock_run, _mock_resolve_prompt) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "--full-auto", "worker prompt"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            events_path = workspace / RUNTIME_DIR / EVENTS_FILE
            events_path.mkdir()

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_agent("Worker", "WORKER.md", workspace, "global", cycle=1)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            self.assertIn("写入事件日志失败", output)


if __name__ == "__main__":
    unittest.main()
