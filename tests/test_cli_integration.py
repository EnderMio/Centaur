from contextlib import redirect_stdout
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from centaur import cli  # noqa: E402
from centaur.engine import EVENTS_FILE, RUNTIME_DIR, append_event, sync_task_bus_to_active  # noqa: E402


class CLIIntegrationTests(unittest.TestCase):
    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        output_buffer = io.StringIO()
        with redirect_stdout(output_buffer):
            rc = cli.main(argv)
        return rc, output_buffer.getvalue()

    def test_workspace_create_task_migrate_doctor_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspaces"
            workspace = root / "demo"

            rc, output = self._run_cli(["workspace", "create", "demo", "--root", str(root)])
            self.assertEqual(rc, 0)
            self.assertIn("已初始化", output)

            rc, output = self._run_cli(["task", "list", str(workspace)])
            self.assertEqual(rc, 0)
            self.assertIn("Active Task: default", output)

            rc, output = self._run_cli(["task", "new", "task-001", str(workspace)])
            self.assertEqual(rc, 0)
            self.assertIn("已创建任务", output)

            rc, output = self._run_cli(["task", "switch", "task-001", str(workspace)])
            self.assertEqual(rc, 0)
            self.assertIn("已切换任务", output)

            rc, output = self._run_cli(["migrate", str(workspace), "--schema"])
            self.assertEqual(rc, 0)
            self.assertIn("迁移完成", output)

            with patch("centaur.cli.codex_available", return_value=True), patch(
                "centaur.cli.collect_prompt_mode_issues", return_value=([], [])
            ):
                rc, output = self._run_cli(["doctor", str(workspace)])
            self.assertEqual(rc, 0)
            self.assertIn("结论: PASS", output)

    def test_init_and_contracts_for_group_rc_and_error_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "init-workspace"

            rc, output = self._run_cli(["init", str(workspace)])
            self.assertEqual(rc, 0)
            self.assertIn("已初始化", output)

            rc, output = self._run_cli(["workspace"])
            self.assertEqual(rc, 2)
            self.assertIn("usage: centaur workspace", output)

            rc, output = self._run_cli(["task"])
            self.assertEqual(rc, 2)
            self.assertIn("usage: centaur task", output)

            missing_workspace = Path(tmp) / "missing-workspace"
            rc, output = self._run_cli(["migrate", str(missing_workspace)])
            self.assertEqual(rc, 1)
            self.assertIn("[CLI_ERROR]", output)
            self.assertIn("[NEXT_STEP]", output)

    def test_run_headless_selfhost_smoke_advances_one_cycle_with_state_and_events_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "selfhost-smoke"

            rc, output = self._run_cli(["init", str(workspace)])
            self.assertEqual(rc, 0)
            self.assertIn("已初始化", output)

            role_trace: list[str] = []
            sync_counter = {"value": 0}
            observed_state: dict[str, object] = {}
            real_sync = sync_task_bus_to_active

            def _fake_run_agent(
                role: str,
                _prompt_filename: str,
                workdir: Path,
                _prompt_mode: str,
                cycle: int = 0,
                headless: bool = False,
            ) -> None:
                self.assertTrue(headless)
                normalized_role = role.lower()
                role_trace.append(normalized_role)
                append_event(workdir, cycle=cycle, event_type="role_start", role=normalized_role)
                append_event(workdir, cycle=cycle, event_type="role_end", role=normalized_role, return_code=0)

            def _fake_human_gate() -> None:
                role_trace.append("human_gate")

            def _fake_sync_task_bus_to_active(workdir: Path, active_task: str) -> None:
                sync_counter["value"] += 1
                real_sync(workdir, active_task)
                if sync_counter["value"] == 5:
                    observed_state.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                    raise SystemExit(91)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), patch(
                "centaur.engine.human_gate", side_effect=_fake_human_gate
            ), patch("centaur.engine.sync_task_bus_to_active", side_effect=_fake_sync_task_bus_to_active), redirect_stdout(
                output_buffer
            ):
                with self.assertRaises(SystemExit) as cm:
                    cli.main(["run", str(workspace), "--headless"])

            self.assertEqual(cm.exception.code, 91)
            self.assertEqual(role_trace, ["supervisor", "human_gate", "worker", "validator"])

            state_path = workspace / RUNTIME_DIR / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 2)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIsNone(state["inflight_role"])
            self.assertIsNone(state["run_id"])
            self.assertIsNone(state["started_at"])
            self.assertEqual(state["attempt"], 0)
            self.assertEqual(observed_state["cycle"], 2)
            self.assertEqual(observed_state["next_step"], "supervisor")

            events_path = workspace / RUNTIME_DIR / EVENTS_FILE
            self.assertTrue(events_path.exists())
            lines = [line for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            events = [json.loads(line) for line in lines]

            cycle_one_events = [event for event in events if event["cycle"] == 1]
            cycle_one_types = [event["event_type"] for event in cycle_one_events]
            self.assertIn("cycle_start", cycle_one_types)
            self.assertIn("role_start", cycle_one_types)
            self.assertIn("role_end", cycle_one_types)
            self.assertIn("cycle_end", cycle_one_types)

            cycle_one_role_starts = [event["role"] for event in cycle_one_events if event["event_type"] == "role_start"]
            cycle_one_role_ends = [event["role"] for event in cycle_one_events if event["event_type"] == "role_end"]
            self.assertEqual(cycle_one_role_starts, ["supervisor", "worker", "validator"])
            self.assertEqual(cycle_one_role_ends, ["supervisor", "worker", "validator"])

            for event in cycle_one_events:
                self.assertIn("timestamp", event)
                self.assertIn("cycle", event)
                self.assertIn("event_type", event)
                if event["event_type"] in {"role_start", "role_end"}:
                    self.assertIn("role", event)
                if event["event_type"] == "role_end":
                    self.assertEqual(event["return_code"], 0)


if __name__ == "__main__":
    unittest.main()
