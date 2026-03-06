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
    CONTROL_DIR,
    CONTROL_TASKS_FILE,
    DEFAULT_CODEX_EXEC_SANDBOX,
    DEFAULT_TASK_NAME,
    EVENTS_FILE,
    PROJECT_SCHEMA_VERSION,
    RUNTIME_METRICS_FILE,
    RUNTIME_DIR,
    SCHEDULER_STATE_FILE,
    TASK_CONTRACT_MODE_ENFORCE,
    TASK_COMPLETION_EVIDENCE_PREFIX,
    append_event,
    build_codex_exec_permission_args,
    ensure_runtime_layout,
    lint_task_contract,
    load_state,
    load_or_init_project_config,
    migrate_schema,
    parse_runtime_policy,
    run_agent,
    run_workflow,
    save_project_config,
    refresh_runtime_metrics,
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
            self.assertIn("human_gate_policy", config)
            self.assertIn("codex_exec_sandbox", config)
            self.assertIn("codex_exec_dangerously_bypass", config)

    def test_runtime_policy_parse_and_headless_permission_args(self) -> None:
        cases = (
            (
                "explicit_sandbox",
                {
                    "human_gate_policy": "always",
                    "codex_exec_sandbox": "read-only",
                    "codex_exec_dangerously_bypass": False,
                },
                ["--sandbox", "read-only"],
            ),
            (
                "default_workspace_write",
                {
                    "human_gate_policy": "risk",
                    "codex_exec_sandbox": None,
                    "codex_exec_dangerously_bypass": False,
                },
                ["--sandbox", DEFAULT_CODEX_EXEC_SANDBOX],
            ),
            (
                "dangerously_bypass",
                {
                    "human_gate_policy": "off",
                    "codex_exec_sandbox": None,
                    "codex_exec_dangerously_bypass": True,
                },
                ["--dangerously-bypass-approvals-and-sandbox"],
            ),
        )

        for case_name, payload, expected_args in cases:
            with self.subTest(case=case_name):
                policy = parse_runtime_policy(payload)
                self.assertEqual(build_codex_exec_permission_args(policy), expected_args)

    def test_runtime_policy_parse_rejects_bypass_and_sandbox_conflict(self) -> None:
        with self.assertRaises(ValueError) as cm:
            parse_runtime_policy(
                {
                    "human_gate_policy": "always",
                    "codex_exec_sandbox": "workspace-write",
                    "codex_exec_dangerously_bypass": True,
                }
            )
        self.assertIn("禁止显式设置 `codex_exec_sandbox`", str(cm.exception))

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

    def test_control_schema_files_created_with_minimum_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)

            tasks_path = workspace / RUNTIME_DIR / CONTROL_DIR / CONTROL_TASKS_FILE
            scheduler_path = workspace / RUNTIME_DIR / CONTROL_DIR / SCHEDULER_STATE_FILE
            self.assertTrue(tasks_path.exists())
            self.assertTrue(scheduler_path.exists())
            self.assertEqual(
                json.loads(tasks_path.read_text(encoding="utf-8")),
                {"schema_version": 1, "mode": "serial", "tasks": []},
            )
            self.assertEqual(
                json.loads(scheduler_path.read_text(encoding="utf-8")),
                {
                    "schema_version": 1,
                    "mode": "serial",
                    "max_parallelism": 1,
                    "inflight_tasks": [],
                    "path_locks": {},
                },
            )

    def test_control_schema_does_not_rewrite_existing_valid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            control_dir = workspace / RUNTIME_DIR / CONTROL_DIR
            control_dir.mkdir(parents=True, exist_ok=True)

            tasks_path = control_dir / CONTROL_TASKS_FILE
            scheduler_path = control_dir / SCHEDULER_STATE_FILE
            tasks_content = (
                json.dumps(
                    {"schema_version": 1, "mode": "serial", "tasks": [{"id": "t-1"}]},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            scheduler_content = (
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "serial",
                        "max_parallelism": 1,
                        "inflight_tasks": ["t-1"],
                        "path_locks": {"/tmp/path": "t-1"},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n"
            )
            tasks_path.write_text(tasks_content, encoding="utf-8")
            scheduler_path.write_text(scheduler_content, encoding="utf-8")

            ensure_runtime_layout(workspace)

            self.assertEqual(tasks_path.read_text(encoding="utf-8"), tasks_content)
            self.assertEqual(scheduler_path.read_text(encoding="utf-8"), scheduler_content)

    def test_control_schema_fail_fast_on_invalid_json_or_contract(self) -> None:
        invalid_cases = (
            ("tasks_invalid_json", CONTROL_TASKS_FILE, "{invalid-json", "控制面文件 JSON 非法"),
            (
                "scheduler_invalid_field",
                SCHEDULER_STATE_FILE,
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "serial",
                        "max_parallelism": "1",
                        "inflight_tasks": [],
                        "path_locks": {},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                "控制面文件契约校验失败",
            ),
        )

        for case_name, target_file, payload, marker in invalid_cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    control_dir = workspace / RUNTIME_DIR / CONTROL_DIR
                    control_dir.mkdir(parents=True, exist_ok=True)

                    tasks_path = control_dir / CONTROL_TASKS_FILE
                    scheduler_path = control_dir / SCHEDULER_STATE_FILE
                    if target_file != CONTROL_TASKS_FILE:
                        tasks_path.write_text(
                            json.dumps({"schema_version": 1, "mode": "serial", "tasks": []}, ensure_ascii=False, indent=2)
                            + "\n",
                            encoding="utf-8",
                        )
                    if target_file != SCHEDULER_STATE_FILE:
                        scheduler_path.write_text(
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "mode": "serial",
                                    "max_parallelism": 1,
                                    "inflight_tasks": [],
                                    "path_locks": {},
                                },
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n",
                            encoding="utf-8",
                        )

                    target_path = control_dir / target_file
                    target_path.write_text(payload, encoding="utf-8")

                    output_buffer = io.StringIO()
                    with redirect_stdout(output_buffer):
                        with self.assertRaises(SystemExit) as cm:
                            ensure_runtime_layout(workspace)
                    output = output_buffer.getvalue()

                    self.assertEqual(cm.exception.code, 1)
                    self.assertIn(marker, output)
                    self.assertEqual(target_path.read_text(encoding="utf-8"), payload)

    def test_load_state_backfills_transaction_fields_for_old_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            state_path = workspace / RUNTIME_DIR / "state.json"
            state_path.write_text(json.dumps({"cycle": 2, "next_step": "worker"}), encoding="utf-8")

            state = load_state(workspace)
            self.assertEqual(state["cycle"], 2)
            self.assertEqual(state["next_step"], "worker")
            self.assertIsNone(state["inflight_role"])
            self.assertIsNone(state["run_id"])
            self.assertIsNone(state["started_at"])
            self.assertEqual(state["attempt"], 0)
            self.assertIsNone(state["last_checkpoint_sha"])

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("inflight_role", persisted)
            self.assertIn("run_id", persisted)
            self.assertIn("started_at", persisted)
            self.assertIn("attempt", persisted)
            self.assertIn("last_checkpoint_sha", persisted)
            self.assertIsNone(persisted["inflight_role"])
            self.assertIsNone(persisted["run_id"])
            self.assertIsNone(persisted["started_at"])
            self.assertEqual(persisted["attempt"], 0)
            self.assertIsNone(persisted["last_checkpoint_sha"])

    def test_load_state_fail_fast_on_invalid_state_contract(self) -> None:
        invalid_cases = (
            (
                "invalid_next_step",
                {
                    "cycle": 2,
                    "next_step": "oops",
                },
                "`next_step`",
            ),
            (
                "invalid_inflight_role",
                {
                    "cycle": 2,
                    "next_step": "worker",
                    "inflight_role": "oops",
                    "run_id": "run-1",
                    "started_at": "2026-03-05T00:00:00+00:00",
                    "attempt": 1,
                },
                "`inflight_role`",
            ),
            (
                "inflight_fields_mismatch",
                {
                    "cycle": 2,
                    "next_step": "validator",
                    "inflight_role": "worker",
                    "run_id": "run-1",
                    "started_at": "2026-03-05T00:00:00+00:00",
                    "attempt": 1,
                },
                "在途状态不一致",
            ),
            (
                "invalid_attempt_type",
                {
                    "cycle": 2,
                    "next_step": "worker",
                    "inflight_role": "worker",
                    "run_id": "run-1",
                    "started_at": "2026-03-05T00:00:00+00:00",
                    "attempt": "1",
                },
                "`attempt`",
            ),
        )
        for case_name, payload, marker in invalid_cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    ensure_runtime_layout(workspace)
                    state_path = workspace / RUNTIME_DIR / "state.json"
                    state_path.write_text(json.dumps(payload), encoding="utf-8")

                    output_buffer = io.StringIO()
                    with redirect_stdout(output_buffer):
                        with self.assertRaises(SystemExit) as cm:
                            load_state(workspace)
                    output = output_buffer.getvalue()

                    self.assertEqual(cm.exception.code, 1)
                    self.assertIn("状态文件契约校验失败", output)
                    self.assertIn(marker, output)
                    self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), payload)

    def test_sh_2_13_load_state_prefers_structured_evidence_over_task_inference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            (workspace / "TASK.md").write_text(
                "# 当前任务 (Task)\n\n### Validator 审查报告\n- 状态: `[成功]`\n",
                encoding="utf-8",
            )
            append_event(workspace, cycle=4, event_type="role_start", role="worker")
            append_event(workspace, cycle=4, event_type="role_end", role="worker", return_code=0)

            state = load_state(workspace)

            self.assertEqual(state["cycle"], 4)
            self.assertEqual(state["next_step"], "validator")
            self.assertIsNone(state["inflight_role"])
            self.assertEqual(state["attempt"], 0)

    def test_sh_2_13_load_state_falls_back_to_task_inference_when_structured_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            (workspace / "TASK.md").write_text(
                "# 当前任务 (Task)\n\n### Worker 执行报告\n- 状态: `[成功]`\n",
                encoding="utf-8",
            )

            state = load_state(workspace)

            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["next_step"], "validator")
            self.assertIsNone(state["inflight_role"])
            self.assertEqual(state["attempt"], 0)

    def test_sh_g2_load_state_routes_worker_nonzero_role_end_to_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            append_event(workspace, cycle=5, event_type="role_start", role="worker")
            append_event(workspace, cycle=5, event_type="role_end", role="worker", return_code=7)

            state = load_state(workspace)

            self.assertEqual(state["cycle"], 5)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIsNone(state["inflight_role"])
            self.assertEqual(state["attempt"], 0)

    def test_task_contract_lint_detects_text_exact_with_allowed_delta_conflict(self) -> None:
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

            errors, warnings, contract = lint_task_contract(workspace)

            self.assertTrue(errors)
            self.assertIn("`unit=text_exact` 与 `allowed_delta` 冲突", "\n".join(errors))
            self.assertEqual(warnings, [])
            self.assertEqual(contract["unit"], "text_exact")

    def test_run_workflow_blocks_worker_when_task_contract_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n"
                    "## 验收标准\n"
                    "- [ ] demo\n"
                    "[CENTAUR_TASK_CONTRACT] "
                    '{"version":1,"unit":"text_exact","allowed_delta":["tests/scripts/test_recovery_auto.sh"]}\n'
                ),
                encoding="utf-8",
            )
            config = load_or_init_project_config(workspace)
            config["task_contract_mode"] = TASK_CONTRACT_MODE_ENFORCE
            save_project_config(workspace, config)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 2, "next_step": "worker"}),
                encoding="utf-8",
            )

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent") as mock_run_agent, redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            mock_run_agent.assert_not_called()
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 2)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIsNone(state["inflight_role"])
            self.assertIn("[BLOCKED_SPEC]", output)
            self.assertIn("centaur task lint", output)

    def test_run_workflow_human_gate_policy_always_enters_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            config = load_or_init_project_config(workspace)
            config["human_gate_policy"] = "always"
            save_project_config(workspace, config)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 2, "next_step": "human_gate"}),
                encoding="utf-8",
            )

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                if role == "Worker":
                    append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=130)
                raise SystemExit(1)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), patch(
                "centaur.engine.human_gate"
            ) as mock_gate, redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(mock_gate.call_count, 1)
            self.assertIn("policy=always", output)
            self.assertIn("decision=enter_gate", output)

    def test_run_workflow_human_gate_policy_risk_supports_auto_pass_and_trigger(self) -> None:
        scenarios = ("auto_pass", "trigger")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
                    if scenario == "trigger":
                        (workspace / "TASK.md").write_text(
                            (
                                "# 当前任务 (Task)\n"
                                "[CENTAUR_TASK_CONTRACT] "
                                '{"version":1,"unit":"text_exact","allowed_delta":["tests/scripts/test_recovery_auto.sh"]}\n'
                            ),
                            encoding="utf-8",
                        )
                    else:
                        (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")

                    config = load_or_init_project_config(workspace)
                    config["human_gate_policy"] = "risk"
                    save_project_config(workspace, config)
                    ensure_runtime_layout(workspace)
                    (workspace / RUNTIME_DIR / "state.json").write_text(
                        json.dumps({"cycle": 2, "next_step": "human_gate"}),
                        encoding="utf-8",
                    )

                    def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                        if role == "Worker":
                            append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=130)
                        raise SystemExit(1)

                    output_buffer = io.StringIO()
                    with patch("centaur.engine.human_gate") as mock_gate, patch(
                        "centaur.engine.run_agent", side_effect=_fake_run_agent
                    ) as mock_run_agent, redirect_stdout(output_buffer):
                        with self.assertRaises(SystemExit) as cm:
                            run_workflow(workdir=workspace, headless=True)
                    output = output_buffer.getvalue()

                    self.assertEqual(cm.exception.code, 1)
                    self.assertIn("policy=risk", output)
                    if scenario == "trigger":
                        self.assertEqual(mock_gate.call_count, 1)
                        mock_run_agent.assert_not_called()
                        self.assertIn("decision=enter_gate", output)
                    else:
                        self.assertEqual(mock_gate.call_count, 0)
                        self.assertIn("decision=auto_pass", output)

    def test_run_workflow_human_gate_policy_off_fail_closed_and_skip(self) -> None:
        scenarios = (
            ("skip_gate", True, 0, "decision=skip_gate"),
            ("fail_closed", False, 1, "decision=enter_gate_fail_closed"),
        )
        for scenario_name, codex_is_available, expected_gate_calls, expected_decision in scenarios:
            with self.subTest(scenario=scenario_name):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
                    (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
                    config = load_or_init_project_config(workspace)
                    config["human_gate_policy"] = "off"
                    save_project_config(workspace, config)
                    ensure_runtime_layout(workspace)
                    (workspace / RUNTIME_DIR / "state.json").write_text(
                        json.dumps({"cycle": 2, "next_step": "human_gate"}),
                        encoding="utf-8",
                    )

                    def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                        if role == "Worker":
                            append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=130)
                        raise SystemExit(1)

                    output_buffer = io.StringIO()
                    with patch("centaur.engine.codex_available", return_value=codex_is_available), patch(
                        "centaur.engine.human_gate"
                    ) as mock_gate, patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(
                        output_buffer
                    ):
                        with self.assertRaises(SystemExit) as cm:
                            run_workflow(workdir=workspace, headless=True)
                    output = output_buffer.getvalue()

                    self.assertEqual(cm.exception.code, 1)
                    self.assertEqual(mock_gate.call_count, expected_gate_calls)
                    self.assertIn("policy=off", output)
                    self.assertIn(expected_decision, output)

    def test_run_workflow_headless_permission_args_follow_runtime_policy(self) -> None:
        cases = (
            (
                "explicit_sandbox",
                {"codex_exec_sandbox": "read-only", "codex_exec_dangerously_bypass": False},
                ["--sandbox", "read-only"],
            ),
            (
                "default_sandbox",
                {"codex_exec_sandbox": None, "codex_exec_dangerously_bypass": False},
                ["--sandbox", DEFAULT_CODEX_EXEC_SANDBOX],
            ),
            (
                "dangerously_bypass",
                {"codex_exec_sandbox": None, "codex_exec_dangerously_bypass": True},
                ["--dangerously-bypass-approvals-and-sandbox"],
            ),
        )
        for case_name, policy_updates, expected_exec_args in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
                    (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
                    config = load_or_init_project_config(workspace)
                    config.update(policy_updates)
                    save_project_config(workspace, config)
                    ensure_runtime_layout(workspace)
                    (workspace / RUNTIME_DIR / "state.json").write_text(
                        json.dumps({"cycle": 2, "next_step": "worker"}),
                        encoding="utf-8",
                    )

                    observed: dict[str, object] = {}

                    def _fake_run_agent(role: str, *_args, **kwargs) -> None:
                        observed["role"] = role
                        observed["headless_exec_args"] = kwargs.get("headless_exec_args")
                        raise SystemExit(77)

                    with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                        with self.assertRaises(SystemExit) as cm:
                            run_workflow(workdir=workspace, start_step="worker", headless=True)

                    self.assertEqual(cm.exception.code, 77)
                    self.assertEqual(observed["role"], "Worker")
                    self.assertEqual(observed["headless_exec_args"], expected_exec_args)

    def test_run_workflow_fails_fast_on_invalid_runtime_policy(self) -> None:
        invalid_cases = (
            ("invalid_human_gate_policy", {"human_gate_policy": "invalid-token"}, "`human_gate_policy` 非法"),
            (
                "bypass_conflicts_with_sandbox",
                {"codex_exec_dangerously_bypass": True, "codex_exec_sandbox": "read-only"},
                "禁止显式设置 `codex_exec_sandbox`",
            ),
        )
        for case_name, updates, marker in invalid_cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
                    (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
                    config = load_or_init_project_config(workspace)
                    config.update(updates)
                    save_project_config(workspace, config)

                    output_buffer = io.StringIO()
                    with patch("centaur.engine.run_agent") as mock_run_agent, redirect_stdout(output_buffer):
                        with self.assertRaises(SystemExit) as cm:
                            run_workflow(workdir=workspace, start_step="worker", headless=True)
                    output = output_buffer.getvalue()

                    self.assertEqual(cm.exception.code, 1)
                    mock_run_agent.assert_not_called()
                    self.assertIn("[RUNTIME_CONFIG_ERROR]", output)
                    self.assertIn("[NEXT_STEP]", output)
                    self.assertIn(marker, output)

    def test_sh_2_13_load_state_fail_fast_on_invalid_control_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            control_dir = workspace / RUNTIME_DIR / CONTROL_DIR
            control_dir.mkdir(parents=True, exist_ok=True)
            tasks_path = control_dir / CONTROL_TASKS_FILE
            scheduler_path = control_dir / SCHEDULER_STATE_FILE
            tasks_path.write_text("{invalid-json", encoding="utf-8")
            scheduler_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "serial",
                        "max_parallelism": 1,
                        "inflight_tasks": [],
                        "path_locks": {},
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    load_state(workspace)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            self.assertIn("控制面文件 JSON 非法", output)
            self.assertEqual(tasks_path.read_text(encoding="utf-8"), "{invalid-json")

    def test_run_workflow_does_not_advance_when_state_contract_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)

            state_path = workspace / RUNTIME_DIR / "state.json"
            invalid_state = {
                "cycle": 2,
                "next_step": "worker",
                "inflight_role": "worker",
                "run_id": "run-1",
                "started_at": "2026-03-05T00:00:00+00:00",
                "attempt": "1",
            }
            state_path.write_text(json.dumps(invalid_state), encoding="utf-8")

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent") as mock_run_agent, redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            mock_run_agent.assert_not_called()
            self.assertIn("状态文件契约校验失败", output)
            self.assertIn("`attempt`", output)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8")), invalid_state)

    def test_run_workflow_persists_transaction_fields_before_role_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps(
                    {
                        "cycle": 3,
                        "next_step": "validator",
                        "inflight_role": "validator",
                        "run_id": "stale-run-id",
                        "started_at": "2026-03-05T00:00:00+00:00",
                        "attempt": 2,
                    }
                ),
                encoding="utf-8",
            )

            observed: dict[str, object] = {}

            def _fake_run_agent(*_args, **_kwargs) -> None:
                state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
                observed.update(state)
                raise SystemExit(9)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                with self.assertRaises(SystemExit):
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(observed["inflight_role"], "validator")
            self.assertTrue(observed["run_id"])
            self.assertTrue(observed["started_at"])
            self.assertEqual(observed["attempt"], 3)

    def test_run_workflow_replays_inflight_role_after_agent_abrupt_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            state_path = workspace / RUNTIME_DIR / "state.json"
            state_path.write_text(
                json.dumps({"cycle": 2, "next_step": "worker"}),
                encoding="utf-8",
            )

            called_roles: list[str] = []
            observed: list[dict[str, object]] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                observed.append(json.loads(state_path.read_text(encoding="utf-8")))
                if len(called_roles) == 1:
                    raise SystemExit(2)
                raise SystemExit(41)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                with self.assertRaises(SystemExit) as first_cm:
                    run_workflow(workdir=workspace, headless=True)
                state_after_first = json.loads(state_path.read_text(encoding="utf-8"))
                with self.assertRaises(SystemExit) as second_cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(first_cm.exception.code, 2)
            self.assertEqual(second_cm.exception.code, 41)
            self.assertEqual(called_roles, ["Worker", "Worker"])
            self.assertEqual(state_after_first["cycle"], 2)
            self.assertEqual(state_after_first["next_step"], "worker")
            self.assertEqual(state_after_first["inflight_role"], "worker")
            self.assertEqual(state_after_first["attempt"], 1)
            self.assertEqual(observed[1]["cycle"], 2)
            self.assertEqual(observed[1]["next_step"], "worker")
            self.assertEqual(observed[1]["inflight_role"], "worker")
            self.assertEqual(observed[1]["attempt"], 2)

    def test_run_workflow_routes_to_supervisor_after_worker_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            state_path = workspace / RUNTIME_DIR / "state.json"
            state_path.write_text(
                json.dumps({"cycle": 2, "next_step": "worker"}),
                encoding="utf-8",
            )

            called_roles: list[str] = []
            observed: list[dict[str, object]] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                observed.append(json.loads(state_path.read_text(encoding="utf-8")))
                if len(called_roles) == 1:
                    append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=130)
                    raise SystemExit(1)
                raise SystemExit(43)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                with self.assertRaises(SystemExit) as first_cm:
                    run_workflow(workdir=workspace, headless=True)
                state_after_first = json.loads(state_path.read_text(encoding="utf-8"))
                with self.assertRaises(SystemExit) as second_cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(first_cm.exception.code, 1)
            self.assertEqual(second_cm.exception.code, 43)
            self.assertEqual(called_roles, ["Worker", "Supervisor"])
            self.assertEqual(state_after_first["cycle"], 2)
            self.assertEqual(state_after_first["next_step"], "supervisor")
            self.assertIsNone(state_after_first["inflight_role"])
            self.assertEqual(state_after_first["attempt"], 0)
            self.assertEqual(observed[1]["cycle"], 2)
            self.assertEqual(observed[1]["next_step"], "supervisor")
            self.assertEqual(observed[1]["inflight_role"], "supervisor")
            self.assertEqual(observed[1]["attempt"], 1)

    def test_run_workflow_blocks_supervisor_progress_when_real_completion_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            state_path = workspace / RUNTIME_DIR / "state.json"
            state_path.write_text(
                json.dumps({"cycle": 1, "next_step": "supervisor"}),
                encoding="utf-8",
            )

            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                append_event(workspace, cycle=1, event_type="role_end", role="supervisor", return_code=0)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(called_roles, ["Supervisor"])

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIsNone(state["inflight_role"])
            self.assertIsNone(state["run_id"])
            self.assertIsNone(state["started_at"])
            self.assertEqual(state["attempt"], 0)

            self.assertIn("真实完成闸门失败", output)
            self.assertIn("缺少 Supervisor 派单结构字段", output)
            evidence_lines = [
                line
                for line in (workspace / "TASK.md").read_text(encoding="utf-8").splitlines()
                if line.startswith(TASK_COMPLETION_EVIDENCE_PREFIX)
            ]
            self.assertEqual(evidence_lines, [])

    def test_run_workflow_recovery_keeps_supervisor_replay_when_real_completion_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            run_id = "1-supervisor-a1-recovered"
            started_at = "2026-03-05T00:00:00+00:00"
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
                payload = {"cycle": 1, "role": "supervisor", "run_id": run_id, "status": "completed"}
                handle.write(f"{TASK_COMPLETION_EVIDENCE_PREFIX}{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps(
                    {
                        "cycle": 1,
                        "next_step": "supervisor",
                        "inflight_role": "supervisor",
                        "run_id": run_id,
                        "started_at": started_at,
                        "attempt": 1,
                    }
                ),
                encoding="utf-8",
            )
            append_event(workspace, cycle=1, event_type="role_start", role="supervisor")
            append_event(workspace, cycle=1, event_type="role_end", role="supervisor", return_code=0)

            observed: dict[str, object] = {}
            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                raise SystemExit(61)

            with patch("centaur.engine.human_gate", side_effect=AssertionError("human_gate should not be entered")), patch(
                "centaur.engine.run_agent", side_effect=_fake_run_agent
            ):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(cm.exception.code, 61)
            self.assertEqual(called_roles, ["Supervisor"])
            self.assertEqual(observed["cycle"], 1)
            self.assertEqual(observed["next_step"], "supervisor")
            self.assertEqual(observed["inflight_role"], "supervisor")
            self.assertEqual(observed["attempt"], 2)

    def test_run_workflow_replays_inflight_role_when_last_event_not_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps(
                    {
                        "cycle": 2,
                        "next_step": "worker",
                        "inflight_role": "worker",
                        "run_id": "2-worker-a1-open",
                        "started_at": "2026-03-05T00:00:00+00:00",
                        "attempt": 1,
                    }
                ),
                encoding="utf-8",
            )
            append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=0)
            append_event(workspace, cycle=2, event_type="role_start", role="worker")

            observed: dict[str, object] = {}
            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                raise SystemExit(19)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(cm.exception.code, 19)
            self.assertEqual(called_roles, ["Worker"])
            self.assertEqual(observed["next_step"], "worker")
            self.assertEqual(observed["inflight_role"], "worker")
            self.assertEqual(observed["attempt"], 2)

    def test_run_workflow_does_not_rollback_when_inflight_role_already_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)

            run_id = "2-worker-a1-closed"
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps(
                    {
                        "cycle": 2,
                        "next_step": "worker",
                        "inflight_role": "worker",
                        "run_id": run_id,
                        "started_at": "2026-03-05T00:00:00+00:00",
                        "attempt": 1,
                    }
                ),
                encoding="utf-8",
            )
            append_event(workspace, cycle=2, event_type="role_start", role="worker")
            append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=0)
            payload = {"cycle": 2, "role": "worker", "run_id": run_id, "status": "completed"}
            with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
                handle.write(f"{TASK_COMPLETION_EVIDENCE_PREFIX}{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n")

            observed: dict[str, object] = {}
            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                raise SystemExit(23)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(cm.exception.code, 23)
            self.assertEqual(called_roles, ["Validator"])
            self.assertEqual(observed["cycle"], 2)
            self.assertEqual(observed["next_step"], "validator")
            self.assertEqual(observed["inflight_role"], "validator")
            self.assertEqual(observed["attempt"], 1)

    def test_run_workflow_does_not_silently_advance_when_inflight_events_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)

            run_id = "2-worker-a1-no-events"
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps(
                    {
                        "cycle": 2,
                        "next_step": "worker",
                        "inflight_role": "worker",
                        "run_id": run_id,
                        "started_at": "2026-03-05T00:00:00+00:00",
                        "attempt": 1,
                    }
                ),
                encoding="utf-8",
            )
            payload = {"cycle": 2, "role": "worker", "run_id": run_id, "status": "completed"}
            with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
                handle.write(f"{TASK_COMPLETION_EVIDENCE_PREFIX}{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n")

            observed: dict[str, object] = {}
            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                raise SystemExit(29)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(cm.exception.code, 29)
            self.assertEqual(called_roles, ["Worker"])
            self.assertEqual(observed["next_step"], "worker")
            self.assertEqual(observed["inflight_role"], "worker")

    def test_run_workflow_recovers_inflight_worker_failure_to_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)

            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps(
                    {
                        "cycle": 2,
                        "next_step": "worker",
                        "inflight_role": "worker",
                        "run_id": "2-worker-a1-failed",
                        "started_at": "2026-03-05T00:00:00+00:00",
                        "attempt": 1,
                    }
                ),
                encoding="utf-8",
            )
            append_event(workspace, cycle=2, event_type="role_start", role="worker")
            append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=7)

            observed: dict[str, object] = {}
            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                raise SystemExit(31)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(cm.exception.code, 31)
            self.assertEqual(called_roles, ["Supervisor"])
            self.assertEqual(observed["cycle"], 2)
            self.assertEqual(observed["next_step"], "supervisor")
            self.assertEqual(observed["inflight_role"], "supervisor")
            self.assertEqual(observed["attempt"], 1)

    def test_run_workflow_clears_transaction_fields_after_role_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 2, "next_step": "worker"}),
                encoding="utf-8",
            )

            sync_counter = {"value": 0}
            observed: dict[str, object] = {}

            def _fake_sync_task_bus_to_active(_workdir: Path, _active_task: str) -> None:
                sync_counter["value"] += 1
                if sync_counter["value"] == 2:
                    state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
                    observed.update(state)
                    raise SystemExit(11)

            def _fake_run_agent(*_args, **_kwargs) -> None:
                append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=0)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), patch(
                "centaur.engine.sync_task_bus_to_active", side_effect=_fake_sync_task_bus_to_active
            ):
                with self.assertRaises(SystemExit):
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(observed["next_step"], "validator")
            self.assertIsNone(observed["inflight_role"])
            self.assertIsNone(observed["run_id"])
            self.assertIsNone(observed["started_at"])
            self.assertEqual(observed["attempt"], 0)

            evidence_lines = [
                line
                for line in (workspace / "TASK.md").read_text(encoding="utf-8").splitlines()
                if line.startswith(TASK_COMPLETION_EVIDENCE_PREFIX)
            ]
            self.assertTrue(evidence_lines)
            payload = json.loads(evidence_lines[-1][len(TASK_COMPLETION_EVIDENCE_PREFIX) :])
            self.assertEqual(payload["cycle"], 2)
            self.assertEqual(payload["role"], "worker")
            self.assertEqual(payload["status"], "completed")
            self.assertTrue(payload["run_id"])

    def test_run_workflow_validator_checkpoint_skips_non_git_workspace_without_crash(self) -> None:
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

            sync_counter = {"value": 0}
            observed: dict[str, object] = {}

            def _fake_run_agent(*_args, **_kwargs) -> None:
                append_event(workspace, cycle=1, event_type="role_end", role="validator", return_code=0)

            def _fake_sync_task_bus_to_active(_workdir: Path, _active_task: str) -> None:
                sync_counter["value"] += 1
                if sync_counter["value"] == 2:
                    observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                    raise SystemExit(95)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), patch(
                "centaur.engine.sync_task_bus_to_active", side_effect=_fake_sync_task_bus_to_active
            ), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 95)
            self.assertEqual(observed["cycle"], 2)
            self.assertEqual(observed["next_step"], "supervisor")
            self.assertIsNone(observed["last_checkpoint_sha"])
            self.assertIn("不是 Git 仓库", output)

    def test_run_workflow_validator_hard_rejects_unsealed_uncommitted_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            worker_end_state = "[CENTAUR_WORKER_END_STATE] " + json.dumps(
                {
                    "PATCH_APPLIED": 1,
                    "COMMIT_CREATED": 0,
                    "CARRYOVER_FILES": [],
                    "SEAL_MODE": "UNSEALED",
                    "RELEASE_DECISION": "READY",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n\n"
                    "---\n"
                    "## Worker 反馈区\n"
                    "### Worker 执行报告 (2026-03-06 12:00 +0800)\n"
                    f"{worker_end_state}\n"
                ),
                encoding="utf-8",
            )
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 1, "next_step": "validator"}),
                encoding="utf-8",
            )

            def _fake_run_agent(*_args, **_kwargs) -> None:
                append_event(workspace, cycle=1, event_type="role_end", role="validator", return_code=0)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIsNone(state["inflight_role"])
            self.assertIsNone(state["run_id"])
            self.assertIsNone(state["started_at"])
            self.assertEqual(state["attempt"], 0)

            self.assertIn("Validator 硬驳回", output)
            self.assertIn("PATCH_APPLIED=1", output)
            self.assertIn("SEALED_BLOCKED", output)

            evidence_lines = [
                line
                for line in (workspace / "TASK.md").read_text(encoding="utf-8").splitlines()
                if line.startswith(TASK_COMPLETION_EVIDENCE_PREFIX)
            ]
            validator_evidence = [
                json.loads(line[len(TASK_COMPLETION_EVIDENCE_PREFIX) :])
                for line in evidence_lines
                if json.loads(line[len(TASK_COMPLETION_EVIDENCE_PREFIX) :]).get("role") == "validator"
            ]
            self.assertFalse(validator_evidence)

    def test_run_workflow_validator_allows_commit_or_sealed_blocked_end_state(self) -> None:
        cases = (
            (
                "commit_created",
                {
                    "PATCH_APPLIED": 1,
                    "COMMIT_CREATED": 1,
                    "CARRYOVER_FILES": [],
                    "SEAL_MODE": "UNSEALED",
                    "RELEASE_DECISION": "READY",
                    "commit_sha": "abc123",
                    "commit_files": ["src/centaur/engine.py"],
                },
            ),
            (
                "sealed_blocked",
                {
                    "PATCH_APPLIED": 1,
                    "COMMIT_CREATED": 0,
                    "CARRYOVER_FILES": ["src/centaur/engine.py"],
                    "SEAL_MODE": "SEALED_BLOCKED",
                    "RELEASE_DECISION": "PENDING",
                    "carryover_reason": "等待主管确认",
                    "owner": "worker",
                    "next_min_action": "补齐提交策略",
                    "due_cycle": 2,
                },
            ),
        )

        for case_name, end_state_payload in cases:
            with self.subTest(case=case_name):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
                    worker_end_state = "[CENTAUR_WORKER_END_STATE] " + json.dumps(
                        end_state_payload, ensure_ascii=False, sort_keys=True
                    )
                    (workspace / "TASK.md").write_text(
                        (
                            "# 当前任务 (Task)\n\n"
                            "---\n"
                            "## Worker 反馈区\n"
                            "### Worker 执行报告 (2026-03-06 12:00 +0800)\n"
                            f"{worker_end_state}\n"
                        ),
                        encoding="utf-8",
                    )
                    load_or_init_project_config(workspace)
                    ensure_runtime_layout(workspace)
                    (workspace / RUNTIME_DIR / "state.json").write_text(
                        json.dumps({"cycle": 1, "next_step": "validator"}),
                        encoding="utf-8",
                    )

                    sync_counter = {"value": 0}
                    observed: dict[str, object] = {}

                    def _fake_run_agent(*_args, **_kwargs) -> None:
                        append_event(workspace, cycle=1, event_type="role_end", role="validator", return_code=0)

                    def _fake_sync_task_bus_to_active(_workdir: Path, _active_task: str) -> None:
                        sync_counter["value"] += 1
                        if sync_counter["value"] == 2:
                            observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                            raise SystemExit(206)

                    output_buffer = io.StringIO()
                    with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), patch(
                        "centaur.engine.sync_task_bus_to_active", side_effect=_fake_sync_task_bus_to_active
                    ), redirect_stdout(output_buffer):
                        with self.assertRaises(SystemExit) as cm:
                            run_workflow(workdir=workspace, headless=True)
                    output = output_buffer.getvalue()

                    self.assertEqual(cm.exception.code, 206)
                    self.assertEqual(observed["cycle"], 2)
                    self.assertEqual(observed["next_step"], "supervisor")
                    self.assertNotIn("Validator 硬驳回", output)

                    evidence_lines = [
                        line
                        for line in (workspace / "TASK.md").read_text(encoding="utf-8").splitlines()
                        if line.startswith(TASK_COMPLETION_EVIDENCE_PREFIX)
                    ]
                    validator_evidence = [
                        json.loads(line[len(TASK_COMPLETION_EVIDENCE_PREFIX) :])
                        for line in evidence_lines
                        if json.loads(line[len(TASK_COMPLETION_EVIDENCE_PREFIX) :]).get("role") == "validator"
                    ]
                    self.assertTrue(validator_evidence)
                    self.assertEqual(validator_evidence[-1]["cycle"], 1)
                    self.assertEqual(validator_evidence[-1]["status"], "completed")

    def test_run_workflow_cycle_boundary_allows_clean_git_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            (workspace / "feature.txt").write_text("v1\n", encoding="utf-8")
            for filename in ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md"):
                (workspace / filename).write_text("", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Centaur Bot"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "config", "user.email", "centaur@example.com"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)

            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 2, "next_step": "supervisor"}),
                encoding="utf-8",
            )

            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                raise SystemExit(196)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 196)
            self.assertEqual(called_roles, ["Supervisor"])
            self.assertNotIn("工作树不洁净", output)

    def test_run_workflow_cycle_boundary_blocks_dirty_git_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            tracked_file = workspace / "feature.txt"
            tracked_file.write_text("v1\n", encoding="utf-8")
            for filename in ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md"):
                (workspace / filename).write_text("", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Centaur Bot"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "config", "user.email", "centaur@example.com"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            init_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
            ).stdout.strip()

            tracked_file.write_text("v1\nv2\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 1, "next_step": "validator"}),
                encoding="utf-8",
            )

            def _fake_run_agent(*_args, **_kwargs) -> None:
                append_event(workspace, cycle=1, event_type="role_end", role="validator", return_code=0)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 2)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIsNone(state["inflight_role"])
            self.assertIsNone(state["run_id"])
            self.assertIsNone(state["started_at"])
            self.assertEqual(state["attempt"], 0)
            self.assertIsNone(state["last_checkpoint_sha"])

            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(head_sha, init_sha)

            self.assertIn("已阻断进入第 2 轮", output)
            self.assertIn("feature.txt", output)
            self.assertIn("[NEXT_STEP] git status", output)
            self.assertIn("[NEXT_STEP] git add <files>", output)
            self.assertIn('[NEXT_STEP] git commit -m "<message>"', output)

    def test_run_workflow_cycle_boundary_ignores_centaur_runtime_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            (workspace / "feature.txt").write_text("v1\n", encoding="utf-8")
            for filename in ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md"):
                (workspace / filename).write_text("", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Centaur Bot"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "config", "user.email", "centaur@example.com"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)

            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 2, "next_step": "supervisor"}),
                encoding="utf-8",
            )
            (workspace / RUNTIME_DIR / "noise.log").write_text("runtime-noise\n", encoding="utf-8")

            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                raise SystemExit(197)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 197)
            self.assertEqual(called_roles, ["Supervisor"])
            self.assertNotIn("工作树不洁净", output)

    def test_run_workflow_cycle_boundary_only_applies_on_round_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            tracked_file = workspace / "feature.txt"
            tracked_file.write_text("v1\n", encoding="utf-8")
            for filename in ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md"):
                (workspace / filename).write_text("", encoding="utf-8")

            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Centaur Bot"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "config", "user.email", "centaur@example.com"],
                cwd=workspace,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            tracked_file.write_text("v1\ndirty\n", encoding="utf-8")

            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 2, "next_step": "worker"}),
                encoding="utf-8",
            )

            sync_counter = {"value": 0}
            observed: dict[str, object] = {}

            def _fake_run_agent(*_args, **_kwargs) -> None:
                append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=0)

            def _fake_sync_task_bus_to_active(_workdir: Path, _active_task: str) -> None:
                sync_counter["value"] += 1
                if sync_counter["value"] == 2:
                    observed.update(json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8")))
                    raise SystemExit(198)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), patch(
                "centaur.engine.sync_task_bus_to_active", side_effect=_fake_sync_task_bus_to_active
            ), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 198)
            self.assertEqual(observed["cycle"], 2)
            self.assertEqual(observed["next_step"], "validator")
            self.assertIsNone(observed["inflight_role"])
            self.assertNotIn("工作树不洁净", output)

    def test_run_workflow_blocks_progress_when_task_run_id_evidence_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(
                json.dumps({"cycle": 2, "next_step": "worker"}),
                encoding="utf-8",
            )

            def _fake_run_agent(*_args, **_kwargs) -> None:
                append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=0)

            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), patch(
                "centaur.engine.append_task_completion_evidence", return_value=None
            ):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)

            self.assertEqual(cm.exception.code, 1)
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 2)
            self.assertEqual(state["next_step"], "worker")
            self.assertIsNone(state["inflight_role"])
            self.assertIsNone(state["run_id"])
            self.assertIsNone(state["started_at"])
            self.assertEqual(state["attempt"], 0)

    def test_run_workflow_blocks_progress_when_role_end_gate_fails(self) -> None:
        for case in ("return_code_nonzero", "missing_role_end"):
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp)
                    (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
                    (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
                    load_or_init_project_config(workspace)
                    ensure_runtime_layout(workspace)
                    (workspace / RUNTIME_DIR / "state.json").write_text(
                        json.dumps({"cycle": 2, "next_step": "worker"}),
                        encoding="utf-8",
                    )

                    called_roles: list[str] = []

                    def _fake_run_agent(*_args, **_kwargs) -> None:
                        called_roles.append(str(_args[0]))
                        if case == "return_code_nonzero":
                            append_event(workspace, cycle=2, event_type="role_end", role="worker", return_code=7)

                    with patch("centaur.engine.run_agent", side_effect=_fake_run_agent):
                        with self.assertRaises(SystemExit) as cm:
                            run_workflow(workdir=workspace, headless=True)

                    self.assertEqual(cm.exception.code, 1)
                    self.assertEqual(called_roles, ["Worker"])
                    state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
                    self.assertEqual(state["cycle"], 2)
                    expected_next_step = "supervisor" if case == "return_code_nonzero" else "worker"
                    self.assertEqual(state["next_step"], expected_next_step)
                    self.assertIsNone(state["inflight_role"])
                    self.assertIsNone(state["run_id"])
                    self.assertIsNone(state["started_at"])
                    self.assertEqual(state["attempt"], 0)

                    evidence_lines = [
                        line
                        for line in (workspace / "TASK.md").read_text(encoding="utf-8").splitlines()
                        if line.startswith(TASK_COMPLETION_EVIDENCE_PREFIX)
                    ]
                    self.assertTrue(evidence_lines)
                    payload = json.loads(evidence_lines[-1][len(TASK_COMPLETION_EVIDENCE_PREFIX) :])
                    self.assertEqual(payload["cycle"], 2)
                    self.assertEqual(payload["role"], "worker")
                    self.assertEqual(payload["status"], "completed")
                    self.assertTrue(payload["run_id"])

    def test_run_workflow_blocks_before_validator_when_worker_end_state_mismatches_git_machine_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tracked_file = workspace / "feature.txt"
            tracked_file.write_text("v1\n", encoding="utf-8")
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n\n"
                    "## 机审契约\n"
                    '[CENTAUR_TASK_CONTRACT] {"version":1,"unit":"set_exact","baseline":"worker-gate","allowed_delta":[],"forbidden_delta":[],"precedence":["forbidden","allowed","wording"]}\n'
                    '[CENTAUR_SUPERVISOR_DISPATCH_GATE] {"STATUS_CMD":"cd /repo && git status --short -- feature.txt","STATUS_RC":0,"STATUS_HAS_UNSEALED_DIRTY":0,"TARGET_DIFF_CMD":"cd /repo && git diff --name-only -- feature.txt","TARGET_DIFF_RC":0,"TARGET_DIFF_HAS_CHANGES":0,"TASK_KIND":"FEATURE","DISPATCH_DECISION":"ALLOW_FUNCTIONAL"}\n'
                    "---\n"
                    "## Worker 反馈区\n"
                ),
                encoding="utf-8",
            )

            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Centaur Bot"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "centaur@example.com"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)

            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(json.dumps({"cycle": 1, "next_step": "worker"}), encoding="utf-8")

            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                tracked_file.write_text("v1\ndirty\n", encoding="utf-8")
                with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
                    handle.write("### Worker 执行报告 (2026-03-06 12:00 +0800)\n")
                    handle.write(
                        '[CENTAUR_WORKER_END_STATE] {"PATCH_APPLIED":0,"COMMIT_CREATED":0,"CARRYOVER_FILES":[],"SEAL_MODE":"UNSEALED","RELEASE_DECISION":"READY"}\n'
                    )
                append_event(workspace, cycle=1, event_type="role_end", role="worker", return_code=0)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(called_roles, ["Worker"])
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIn("Worker->Validator 强制契约闸门失败", output)
            self.assertIn("PATCH_APPLIED(claim=0, auto=1)", output)
            self.assertIn("git rev-parse --is-inside-work-tree", output)
            self.assertIn("git status --porcelain --untracked-files=all", output)

    def test_run_workflow_blocks_before_validator_on_forged_commit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tracked_file = workspace / "feature.txt"
            tracked_file.write_text("v1\n", encoding="utf-8")
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n\n"
                    "## 机审契约\n"
                    '[CENTAUR_TASK_CONTRACT] {"version":1,"unit":"set_exact","baseline":"worker-gate-commit","allowed_delta":[],"forbidden_delta":[],"precedence":["forbidden","allowed","wording"]}\n'
                    '[CENTAUR_SUPERVISOR_DISPATCH_GATE] {"STATUS_CMD":"cd /repo && git status --short -- feature.txt","STATUS_RC":0,"STATUS_HAS_UNSEALED_DIRTY":0,"TARGET_DIFF_CMD":"cd /repo && git diff --name-only -- feature.txt","TARGET_DIFF_RC":0,"TARGET_DIFF_HAS_CHANGES":0,"TASK_KIND":"FEATURE","DISPATCH_DECISION":"ALLOW_FUNCTIONAL"}\n'
                    "---\n"
                    "## Worker 反馈区\n"
                ),
                encoding="utf-8",
            )

            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Centaur Bot"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "centaur@example.com"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)

            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(json.dumps({"cycle": 1, "next_step": "worker"}), encoding="utf-8")

            called_roles: list[str] = []

            def _fake_run_agent(role: str, *_args, **_kwargs) -> None:
                called_roles.append(role)
                tracked_file.write_text("v1\nv2\n", encoding="utf-8")
                with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
                    handle.write("### Worker 执行报告 (2026-03-06 12:00 +0800)\n")
                subprocess.run(["git", "add", "feature.txt", "TASK.md"], cwd=workspace, check=True, capture_output=True, text=True)
                subprocess.run(["git", "commit", "-m", "worker change"], cwd=workspace, check=True, capture_output=True, text=True)
                real_head = subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
                ).stdout.strip()
                with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
                    handle.write(
                        (
                            "[CENTAUR_WORKER_END_STATE] "
                            + json.dumps(
                                {
                                    "PATCH_APPLIED": 1,
                                    "COMMIT_CREATED": 1,
                                    "CARRYOVER_FILES": [],
                                    "SEAL_MODE": "UNSEALED",
                                    "RELEASE_DECISION": "READY",
                                    "commit_sha": real_head,
                                    "commit_files": ["fake.txt"],
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                            + "\n"
                        )
                    )
                append_event(workspace, cycle=1, event_type="role_end", role="worker", return_code=0)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            self.assertEqual(called_roles, ["Worker"])
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIn("Worker->Validator 强制契约闸门失败", output)
            self.assertIn("`commit_files` 与 Git 机证不一致", output)

    def test_run_workflow_validator_fail_closed_on_missing_worker_end_state_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n\n"
                    "---\n"
                    "## Worker 反馈区\n"
                    "### Worker 执行报告 (2026-03-06 12:00 +0800)\n"
                    "- 状态: 成功\n"
                ),
                encoding="utf-8",
            )
            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(json.dumps({"cycle": 1, "next_step": "validator"}), encoding="utf-8")

            def _fake_run_agent(*_args, **_kwargs) -> None:
                append_event(workspace, cycle=1, event_type="role_end", role="validator", return_code=0)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIn("结束态机审失败（Fail-Closed）", output)
            self.assertIn("缺少 `[CENTAUR_WORKER_END_STATE]`", output)

    def test_run_workflow_blocks_non_git_feature_task_kind_before_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n\n"
                    "## 机审契约\n"
                    '[CENTAUR_TASK_CONTRACT] {"version":1,"unit":"set_exact","baseline":"non-git-feature","allowed_delta":[],"forbidden_delta":[],"precedence":["forbidden","allowed","wording"]}\n'
                    '[CENTAUR_SUPERVISOR_DISPATCH_GATE] {"STATUS_CMD":"cd /repo && git status --short -- feature.txt","STATUS_RC":0,"STATUS_HAS_UNSEALED_DIRTY":0,"TARGET_DIFF_CMD":"cd /repo && git diff --name-only -- feature.txt","TARGET_DIFF_RC":0,"TARGET_DIFF_HAS_CHANGES":0,"TASK_KIND":"FEATURE","DISPATCH_DECISION":"ALLOW_FUNCTIONAL"}\n'
                    "---\n"
                    "## Worker 反馈区\n"
                ),
                encoding="utf-8",
            )

            load_or_init_project_config(workspace)
            ensure_runtime_layout(workspace)
            (workspace / RUNTIME_DIR / "state.json").write_text(json.dumps({"cycle": 1, "next_step": "worker"}), encoding="utf-8")

            def _fake_run_agent(*_args, **_kwargs) -> None:
                with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
                    handle.write("### Worker 执行报告 (2026-03-06 12:00 +0800)\n")
                    handle.write(
                        '[CENTAUR_WORKER_END_STATE] {"PATCH_APPLIED":0,"COMMIT_CREATED":0,"CARRYOVER_FILES":[],"SEAL_MODE":"UNSEALED","RELEASE_DECISION":"READY"}\n'
                    )
                append_event(workspace, cycle=1, event_type="role_end", role="worker", return_code=0)

            output_buffer = io.StringIO()
            with patch("centaur.engine.run_agent", side_effect=_fake_run_agent), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace, headless=True)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            state = json.loads((workspace / RUNTIME_DIR / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["cycle"], 1)
            self.assertEqual(state["next_step"], "supervisor")
            self.assertIn("非 Git 工作区禁止 `TASK_KIND=FEATURE`", output)
            self.assertIn("[NEXT_STEP]", output)

    def test_append_event_refreshes_runtime_metrics_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            append_event(workspace, cycle=1, event_type="cycle_start")

            metrics_path = workspace / RUNTIME_DIR / RUNTIME_METRICS_FILE
            self.assertTrue(metrics_path.exists())
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            self.assertEqual(metrics["source"], f"{RUNTIME_DIR}/{EVENTS_FILE}")
            self.assertEqual(metrics["summary"]["total_cycles"], 1)

    def test_runtime_metrics_cover_cycle_duration_role_duration_and_pass_rate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            events_path = workspace / RUNTIME_DIR / EVENTS_FILE
            event_lines = [
                {"timestamp": "2026-03-05T00:00:00+00:00", "cycle": 1, "event_type": "cycle_start"},
                {"timestamp": "2026-03-05T00:00:01+00:00", "cycle": 1, "event_type": "role_start", "role": "supervisor"},
                {
                    "timestamp": "2026-03-05T00:00:03+00:00",
                    "cycle": 1,
                    "event_type": "role_end",
                    "role": "supervisor",
                    "return_code": 0,
                },
                {"timestamp": "2026-03-05T00:00:05+00:00", "cycle": 1, "event_type": "role_start", "role": "worker"},
                {"timestamp": "2026-03-05T00:00:09+00:00", "cycle": 1, "event_type": "role_end", "role": "worker", "return_code": 0},
                {"timestamp": "2026-03-05T00:00:10+00:00", "cycle": 1, "event_type": "role_start", "role": "validator"},
                {
                    "timestamp": "2026-03-05T00:00:13+00:00",
                    "cycle": 1,
                    "event_type": "role_end",
                    "role": "validator",
                    "return_code": 0,
                },
                {"timestamp": "2026-03-05T00:00:15+00:00", "cycle": 1, "event_type": "cycle_end"},
                {"timestamp": "2026-03-05T00:01:00+00:00", "cycle": 2, "event_type": "cycle_start"},
                {"timestamp": "2026-03-05T00:01:02+00:00", "cycle": 2, "event_type": "role_start", "role": "worker"},
                {"timestamp": "2026-03-05T00:01:05+00:00", "cycle": 2, "event_type": "role_end", "role": "worker", "return_code": 0},
            ]
            events_path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in event_lines) + "\n",
                encoding="utf-8",
            )

            refresh_runtime_metrics(workspace)

            metrics_path = workspace / RUNTIME_DIR / RUNTIME_METRICS_FILE
            self.assertTrue(metrics_path.exists())
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            summary = metrics["summary"]
            self.assertEqual(summary["total_cycles"], 2)
            self.assertEqual(summary["successful_cycles"], 1)
            self.assertEqual(summary["pass_rate"], 0.5)

            cycles = metrics["cycles"]
            self.assertEqual([item["cycle"] for item in cycles], [1, 2])
            self.assertEqual(cycles[0]["status"], "passed")
            self.assertEqual(cycles[0]["duration_seconds"], 15.0)
            self.assertEqual(cycles[0]["role_durations"]["supervisor"]["total_seconds"], 2.0)
            self.assertEqual(cycles[0]["role_durations"]["worker"]["total_seconds"], 4.0)
            self.assertEqual(cycles[0]["role_durations"]["validator"]["total_seconds"], 3.0)
            self.assertEqual(cycles[1]["status"], "incomplete")
            self.assertIsNone(cycles[1]["duration_seconds"])
            self.assertEqual(cycles[1]["role_durations"]["worker"]["total_seconds"], 3.0)

            role_durations = metrics["role_durations"]
            self.assertEqual(role_durations["worker"]["total_seconds"], 7.0)
            self.assertEqual(role_durations["worker"]["runs"], 2)
            self.assertEqual(role_durations["worker"]["avg_seconds"], 3.5)

    def test_runtime_metrics_handle_incomplete_or_invalid_samples_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            ensure_runtime_layout(workspace)
            events_path = workspace / RUNTIME_DIR / EVENTS_FILE
            raw_lines = [
                json.dumps({"timestamp": "2026-03-05T00:00:00+00:00", "cycle": 1, "event_type": "cycle_start"}, ensure_ascii=False),
                json.dumps(
                    {"timestamp": "2026-03-05T00:00:02+00:00", "cycle": 1, "event_type": "role_start", "role": "worker"},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-05T00:00:06+00:00",
                        "cycle": 1,
                        "event_type": "role_end",
                        "role": "worker",
                        "return_code": 0,
                    },
                    ensure_ascii=False,
                ),
                json.dumps({"timestamp": "bad-ts", "cycle": 1, "event_type": "cycle_end"}, ensure_ascii=False),
                json.dumps({"timestamp": "2026-03-05T00:01:00+00:00", "cycle": 2, "event_type": "cycle_start"}, ensure_ascii=False),
                json.dumps(
                    {
                        "timestamp": "2026-03-05T00:01:02+00:00",
                        "cycle": 2,
                        "event_type": "role_end",
                        "role": "worker",
                        "return_code": 0,
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"timestamp": "2026-03-05T00:01:03+00:00", "cycle": 2, "event_type": "role_start", "role": "worker"},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "timestamp": "2026-03-05T00:01:07+00:00",
                        "cycle": 2,
                        "event_type": "role_end",
                        "role": "worker",
                        "return_code": 0,
                    },
                    ensure_ascii=False,
                ),
                json.dumps({"timestamp": "2026-03-05T00:01:08+00:00", "event_type": "role_start", "role": "worker"}, ensure_ascii=False),
                "{bad-json",
            ]
            events_path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

            refresh_runtime_metrics(workspace)

            metrics = json.loads((workspace / RUNTIME_DIR / RUNTIME_METRICS_FILE).read_text(encoding="utf-8"))
            summary = metrics["summary"]
            self.assertEqual(summary["total_cycles"], 2)
            self.assertEqual(summary["successful_cycles"], 1)
            self.assertEqual(summary["pass_rate"], 0.5)
            self.assertEqual(summary["incomplete_cycle_duration_count"], 2)
            self.assertEqual(summary["incomplete_role_duration_count"], 1)
            self.assertEqual(summary["invalid_event_count"], 2)

            cycles = metrics["cycles"]
            self.assertEqual(cycles[0]["cycle"], 1)
            self.assertIsNone(cycles[0]["duration_seconds"])
            self.assertEqual(cycles[1]["cycle"], 2)
            self.assertIsNone(cycles[1]["duration_seconds"])
            self.assertEqual(cycles[1]["role_durations"]["worker"]["total_seconds"], 4.0)

    @patch("centaur.engine.resolve_prompt_content", return_value=("worker prompt", "测试模板"))
    @patch("centaur.engine.subprocess.run")
    def test_run_agent_log_written_on_success(self, mock_run, _mock_resolve_prompt) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "exec", "--full-auto", "worker prompt"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            run_agent("Worker", "WORKER.md", workspace, "global", cycle=2, headless=True)
            log_path = workspace / ".centaur" / "logs" / "cycle_2_worker.log"
            self.assertTrue(log_path.exists())
            payload = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["command"], ["codex", "exec", "--full-auto", "worker prompt"])
            self.assertTrue(payload["start_time"])
            self.assertTrue(payload["end_time"])
            self.assertEqual(payload["return_code"], 0)
            self.assertEqual(payload["execution_mode"], "headless")
            self.assertEqual(payload["stdout"], "ok\n")
            self.assertEqual(payload["stderr"], "")

    @patch("centaur.engine.resolve_prompt_content", return_value=("validator prompt", "测试模板"))
    @patch("centaur.engine.subprocess.run")
    def test_run_agent_log_written_on_failure(self, mock_run, _mock_resolve_prompt) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex", "exec", "--full-auto", "validator prompt"],
            returncode=3,
            stdout="partial\n",
            stderr="boom\n",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with self.assertRaises(SystemExit):
                run_agent("Validator", "VALIDATOR.md", workspace, "global", cycle=4, headless=True)
            log_path = workspace / ".centaur" / "logs" / "cycle_4_validator.log"
            self.assertTrue(log_path.exists())
            payload = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["command"], ["codex", "exec", "--full-auto", "validator prompt"])
            self.assertTrue(payload["start_time"])
            self.assertTrue(payload["end_time"])
            self.assertEqual(payload["return_code"], 3)
            self.assertEqual(payload["execution_mode"], "headless")
            self.assertEqual(payload["stdout"], "partial\n")
            self.assertEqual(payload["stderr"], "boom\n")

    @patch("centaur.engine.resolve_prompt_content", return_value=("role prompt", "测试模板"))
    @patch("centaur.engine.subprocess.run")
    def test_event_log_jsonl_append_and_contract(self, mock_run, _mock_resolve_prompt) -> None:
        mock_run.side_effect = [
            subprocess.CompletedProcess(
                args=["codex", "exec", "--full-auto", "role prompt"],
                returncode=0,
                stdout="validator-ok\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["codex", "exec", "--full-auto", "role prompt"],
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

            with patch("centaur.engine.enforce_next_cycle_git_worktree_guard", return_value=None), patch(
                "centaur.engine.try_create_validator_checkpoint", return_value=None
            ):
                with self.assertRaises(SystemExit):
                    run_workflow(workdir=workspace, headless=True)

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

    def test_run_workflow_fails_fast_without_tty_in_interactive_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            (workspace / "TASK.md").write_text("# 当前任务 (Task)\n", encoding="utf-8")
            load_or_init_project_config(workspace)

            output_buffer = io.StringIO()
            with patch("centaur.engine.has_interactive_tty", return_value=False), redirect_stdout(output_buffer):
                with self.assertRaises(SystemExit) as cm:
                    run_workflow(workdir=workspace)
            output = output_buffer.getvalue()

            self.assertEqual(cm.exception.code, 1)
            self.assertIn("不是交互终端", output)
            self.assertIn("--headless", output)


if __name__ == "__main__":
    unittest.main()
