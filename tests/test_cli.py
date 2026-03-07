from __future__ import annotations

from contextlib import redirect_stdout
import argparse
from importlib.resources import files
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from centaur import cli  # noqa: E402
from centaur.cli import cmd_doctor  # noqa: E402
from centaur.engine import load_or_init_project_config, save_project_config  # noqa: E402


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

    def test_doctor_shows_runtime_policy_and_permission_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            config = load_or_init_project_config(workspace)
            config["human_gate_policy"] = "risk"
            config["codex_exec_sandbox"] = "read-only"
            config["codex_exec_dangerously_bypass"] = False
            save_project_config(workspace, config)
            args = argparse.Namespace(path=str(workspace), workspace=None, allow_repo_root=True)

            output_buffer = io.StringIO()
            with patch("centaur.cli.codex_available", return_value=True), patch(
                "centaur.cli.collect_prompt_mode_issues", return_value=([], [])
            ), redirect_stdout(output_buffer):
                rc = cmd_doctor(args)
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("runtime_policy=human_gate_policy=risk, codex_exec=sandbox=read-only", output)
            self.assertIn("codex_exec_permission_args=--sandbox read-only", output)
            self.assertIn("结论: PASS", output)

    def test_doctor_fails_fast_on_invalid_runtime_policy_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "PROPOSAL.md").write_text("proposal", encoding="utf-8")
            config = load_or_init_project_config(workspace)
            config["human_gate_policy"] = "invalid-policy"
            save_project_config(workspace, config)
            args = argparse.Namespace(path=str(workspace), workspace=None, allow_repo_root=True)

            output_buffer = io.StringIO()
            with patch("centaur.cli.codex_available", return_value=True), patch(
                "centaur.cli.collect_prompt_mode_issues", return_value=([], [])
            ), redirect_stdout(output_buffer):
                rc = cmd_doctor(args)
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("运行策略配置非法", output)
            self.assertIn("`human_gate_policy` 非法", output)
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


class InitTemplateRegressionTests(unittest.TestCase):
    def test_init_default_mode_materializes_agents_template(self) -> None:
        expected_agents = files("centaur.templates").joinpath("AGENTS.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["init", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("Prompt 模式: global", output)
            self.assertEqual((workspace / "AGENTS.md").read_text(encoding="utf-8"), expected_agents)
            self.assertFalse((workspace / "SUPERVISOR.md").exists())
            self.assertFalse((workspace / "WORKER.md").exists())
            self.assertFalse((workspace / "VALIDATOR.md").exists())

    def test_init_default_mode_skips_existing_agents_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            expected = "# custom agents\nKEEP\n"
            (workspace / "AGENTS.md").write_text(expected, encoding="utf-8")

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["init", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertEqual((workspace / "AGENTS.md").read_text(encoding="utf-8"), expected)
            self.assertIn("已存在(跳过):", output)
            self.assertIn("AGENTS.md", output)

    def test_init_default_mode_force_overwrites_existing_agents(self) -> None:
        expected_agents = files("centaur.templates").joinpath("AGENTS.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "AGENTS.md").write_text("# custom agents\nSHOULD_BE_OVERWRITTEN\n", encoding="utf-8")

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["init", "--force", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertEqual((workspace / "AGENTS.md").read_text(encoding="utf-8"), expected_agents)
            self.assertIn("已创建/覆盖:", output)
            self.assertIn("AGENTS.md", output)

    def test_init_freeze_prompts_writes_project_status_template(self) -> None:
        required_fields = (
            "更新时间",
            "项目",
            "当前结论",
            "验证结果",
            "已落地能力",
            "风险分级",
            "阻塞项",
            "下一里程碑",
            "口径驳回率",
            "无代码增量驳回率",
            "返工轮次",
            "平均复验次数",
            "问题分流规则",
            "复杂度影响域",
            "复杂度变化依据",
            "测试/基准证据",
            "回滚/缓解动作",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["init", "--freeze-prompts", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("✅ 已初始化", output)

            project_status = (workspace / "PROJECT_STATUS.md").read_text(encoding="utf-8")
            self.assertTrue(project_status.strip())
            self.assertIn("YYYY-MM-DD HH:MM +0800", project_status)
            self.assertIn("高/中/低", project_status)
            for field in required_fields:
                self.assertIn(field, project_status)

    def test_init_freeze_prompts_writes_goal_constraint_acceptance_supervisor_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["init", "--freeze-prompts", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("✅ 已初始化", output)

            supervisor_template = (workspace / "SUPERVISOR.md").read_text(encoding="utf-8")
            self.assertIn("## 任务目标", supervisor_template)
            self.assertIn("## 约束边界", supervisor_template)
            self.assertIn("## 验收标准", supervisor_template)
            self.assertNotIn("## 执行步骤", supervisor_template)
            self.assertIn("默认派单结构必须是“任务目标 / 约束边界 / 验收标准”", supervisor_template)
            self.assertIn("派单必须坚持结果导向", supervisor_template)
            self.assertIn("结果达成 + 边界遵守", supervisor_template)
            self.assertIn("git status --short --", supervisor_template)
            self.assertIn("git diff --name-only --", supervisor_template)
            self.assertIn("开始编码前执行并记录", supervisor_template)
            self.assertIn("[CENTAUR_SUPERVISOR_DISPATCH_GATE]", supervisor_template)
            self.assertIn("[CENTAUR_WORKER_DECISION]", supervisor_template)
            self.assertIn("TASK_KIND", supervisor_template)
            self.assertIn("DISPATCH_DECISION", supervisor_template)
            self.assertIn("SEAL_ONLY", supervisor_template)
            self.assertIn("[CENTAUR_WORKER_END_STATE]", supervisor_template)
            self.assertIn("[CENTAUR_COMPLEXITY_IMPACT]", supervisor_template)
            self.assertIn("[CENTAUR_COMPLEXITY_REVIEW]", supervisor_template)
            self.assertIn("复杂度最小证据标准固定四项", supervisor_template)
            self.assertIn("PATCH_APPLIED", supervisor_template)
            self.assertIn("COMMIT_CREATED", supervisor_template)
            self.assertIn("CARRYOVER_FILES", supervisor_template)
            self.assertIn("SEAL_MODE", supervisor_template)
            self.assertIn("RELEASE_DECISION", supervisor_template)
            self.assertIn("若 `PATCH_APPLIED=1` 且 `COMMIT_CREATED=0`", supervisor_template)
            self.assertIn("流程有效性度量（必填）", supervisor_template)
            self.assertIn("口径驳回率", supervisor_template)
            self.assertIn("无代码增量驳回率", supervisor_template)
            self.assertIn("返工轮次", supervisor_template)
            self.assertIn("平均复验次数", supervisor_template)
            self.assertIn("问题分流规则（必填）", supervisor_template)
            self.assertIn("同步沉淀到 `LESSONS.md`（长期约束）与 `PLAN.md`（下一步可执行任务）", supervisor_template)
            self.assertIn("reason=ABANDONED_DIRECT|ABANDONED_AFTER_BLOCKED", supervisor_template)
            self.assertIn("[~] reason=ABANDONED_DIRECT", supervisor_template)
            self.assertIn("[~] reason=ABANDONED_AFTER_BLOCKED", supervisor_template)
            self.assertIn("历史迁移规则", supervisor_template)

    def test_init_freeze_prompts_writes_rule_maintenance_mechanism_and_role_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["init", "--freeze-prompts", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("✅ 已初始化", output)

            agents_template = (workspace / "AGENTS.md").read_text(encoding="utf-8")
            analyst_template = files("centaur.templates").joinpath("ANALYST.md").read_text(encoding="utf-8")
            librarian_template = files("centaur.templates").joinpath("LIBRARIAN.md").read_text(encoding="utf-8")
            self.assertIn("Analyst / Q&A 核心指令", analyst_template)
            self.assertIn("仅可读取代码、文档与日志", analyst_template)
            self.assertIn("禁止写入业务代码、`TASK.md`、`PROJECT_STATUS.md`", analyst_template)
            self.assertIn("禁止被纳入 `Supervisor -> Human Gate -> Worker -> Validator`", analyst_template)
            self.assertIn("Librarian 核心指令", librarian_template)
            self.assertIn("治理维护专员", librarian_template)
            self.assertIn("仅允许维护治理文档", librarian_template)
            self.assertIn("禁止修改业务代码", librarian_template)
            self.assertIn("禁止被纳入 `Supervisor -> Human Gate -> Worker -> Validator`", librarian_template)
            self.assertIn("`Librarian` 负责治理维护；`Analyst/Q&A` 负责只读问答分析", librarian_template)
            self.assertIn("## 7. 项目规则维护机制 (Rule Maintenance Mechanism)", agents_template)
            self.assertIn("Librarian (治理维护专员 / 非运行时角色)", agents_template)
            self.assertIn("Analyst / Q&A (问答分析师 / 非运行时角色)", agents_template)
            self.assertIn("`Librarian` 仅用于治理文档维护，不参与调度状态机。", agents_template)
            self.assertIn("不参与调度状态机", agents_template)
            self.assertIn("`project.json`（机器规则）", agents_template)
            self.assertIn("`AGENTS.md`（长期约束）", agents_template)
            self.assertIn("`TASK.md`（当轮强约束）", agents_template)
            self.assertIn("`PROJECT_STATUS.md` / `LESSONS.md`（审计沉淀）", agents_template)
            self.assertIn("Rule ID 引用 + 最小正文", agents_template)
            self.assertIn("验收结论必须以结果达成与边界遵守为主", agents_template)
            self.assertIn("## 8. 流程有效性指标与问题分流 (Flow Effectiveness & Triage)", agents_template)
            self.assertIn("口径驳回率", agents_template)
            self.assertIn("无代码增量驳回率", agents_template)
            self.assertIn("返工轮次", agents_template)
            self.assertIn("平均复验次数", agents_template)
            self.assertIn("同步沉淀到 `LESSONS.md`（长期约束）与 `PLAN.md`（下一步可执行任务）", agents_template)
            self.assertIn("## 9. 状态注记机读口径与历史迁移 (State Annotation & Migration)", agents_template)
            self.assertIn("reason=ABANDONED_DIRECT", agents_template)
            self.assertIn("reason=ABANDONED_AFTER_BLOCKED", agents_template)
            self.assertIn("旧写法仅 `[~]`", agents_template)
            self.assertIn("## 10. Rule ID 引用与最小正文派单 (Rule-ID-First Dispatch)", agents_template)
            self.assertNotIn("共享内存权限错误需提权重跑", agents_template)

            supervisor_template = (workspace / "SUPERVISOR.md").read_text(encoding="utf-8")
            self.assertIn("当命中 `project.json` 中已登记的项目规则时", supervisor_template)
            self.assertIn("`Analyst/Q&A` 仅用于非运行时问答分析", supervisor_template)
            self.assertIn("Rule ID 引用 + 最小正文", supervisor_template)
            self.assertIn("## 规则索引 (Rule Index)", supervisor_template)
            self.assertIn("`Librarian` 为治理维护角色", supervisor_template)
            self.assertIn("结构化机审行（如 `[CENTAUR_TASK_CONTRACT]`", supervisor_template)
            self.assertIn("`触发条件 / 动作 / 证据要求`", supervisor_template)
            self.assertNotIn("共享内存权限错误需提权重跑", supervisor_template)

            worker_template = (workspace / "WORKER.md").read_text(encoding="utf-8")
            self.assertIn("若命中 `TASK.md` 已声明的项目规则", worker_template)
            self.assertIn("`Analyst/Q&A` 属于非运行时只读问答角色", worker_template)
            self.assertIn("结果达成 + 边界遵守", worker_template)
            self.assertIn("首次失败证据", worker_template)
            self.assertIn("对应动作", worker_template)
            self.assertIn("[CENTAUR_WORKER_DECISION]", worker_template)
            self.assertIn("candidate_files", worker_template)
            self.assertIn("selected_files", worker_template)
            self.assertIn("rationale", worker_template)
            self.assertIn("[CENTAUR_WORKER_END_STATE]", worker_template)
            self.assertIn("[CENTAUR_COMPLEXITY_IMPACT]", worker_template)
            self.assertIn("complexity_delta", worker_template)
            self.assertIn("测试/基准证据", worker_template)
            self.assertIn("COMMIT_CREATED=1", worker_template)
            self.assertIn("SEAL_MODE=SEALED_BLOCKED", worker_template)
            self.assertIn("结构化机审行禁止反引号包裹", worker_template)
            self.assertNotIn("共享内存权限错误需提权重跑", worker_template)

            validator_template = (workspace / "VALIDATOR.md").read_text(encoding="utf-8")
            self.assertIn("首次失败与后续执行双证据闭环", validator_template)
            self.assertIn("不得仅凭口头描述放行", validator_template)
            self.assertIn("结果达成 + 边界遵守", validator_template)
            self.assertIn("`Analyst/Q&A` 纳入调度状态机", validator_template)
            self.assertIn("反引号包裹或含 `$()` 命令替换污染", validator_template)
            self.assertIn("[CENTAUR_SUPERVISOR_DISPATCH_GATE]", validator_template)
            self.assertIn("[CENTAUR_WORKER_DECISION]", validator_template)
            self.assertIn("SEAL_ONLY", validator_template)
            self.assertIn("[CENTAUR_WORKER_END_STATE]", validator_template)
            self.assertIn("[CENTAUR_COMPLEXITY_IMPACT]", validator_template)
            self.assertIn("[CENTAUR_COMPLEXITY_REVIEW]", validator_template)
            self.assertIn("decision=veto", validator_template)
            self.assertIn("命中 `PATCH_APPLIED=1` 且 `COMMIT_CREATED=0`", validator_template)
            self.assertNotIn("共享内存权限错误需提权重跑", validator_template)

            project_status_template = (workspace / "PROJECT_STATUS.md").read_text(encoding="utf-8")
            self.assertIn("## 规则变更审计（必填）", project_status_template)
            self.assertIn("规则变更内容", project_status_template)
            self.assertIn("触发场景", project_status_template)
            self.assertIn("验证结论", project_status_template)
            self.assertIn("Analyst/Q&A 非运行时约束", project_status_template)
            self.assertIn("状态注记机读口径", project_status_template)
            self.assertIn("reason=ABANDONED_DIRECT|ABANDONED_AFTER_BLOCKED", project_status_template)
            self.assertIn("历史迁移判定与落地原则", project_status_template)
            self.assertIn("复杂度影响域", project_status_template)
            self.assertIn("复杂度变化依据", project_status_template)
            self.assertIn("测试/基准证据", project_status_template)
            self.assertIn("回滚/缓解动作", project_status_template)

    def test_init_freeze_prompts_emits_bare_contract_line_and_task_lint_recognizes_it(self) -> None:
        contract_line = (
            '[CENTAUR_TASK_CONTRACT] '
            '{"version":1,"unit":"set_exact","baseline":"","allowed_delta":[],"forbidden_delta":[],"precedence":["forbidden","allowed","wording"]}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["init", "--freeze-prompts", str(workspace)])

            self.assertEqual(rc, 0)
            supervisor_template = (workspace / "SUPERVISOR.md").read_text(encoding="utf-8")
            self.assertIn(f"\n{contract_line}\n", supervisor_template)
            self.assertNotIn(f"`{contract_line}`", supervisor_template)

            dispatch_gate_line = (
                '[CENTAUR_SUPERVISOR_DISPATCH_GATE] '
                '{"STATUS_CMD":"cd /repo && git status --short -- src/centaur/cli.py","STATUS_RC":0,"STATUS_HAS_UNSEALED_DIRTY":0,'
                '"TARGET_DIFF_CMD":"cd /repo && git diff --name-only -- src/centaur/cli.py","TARGET_DIFF_RC":0,'
                '"TARGET_DIFF_HAS_CHANGES":0,"TASK_KIND":"DIAGNOSE","DISPATCH_DECISION":"ALLOW_FUNCTIONAL"}'
            )
            (workspace / "TASK.md").write_text(
                f"# 当前任务 (Task)\n\n## 机审契约\n{contract_line}\n{dispatch_gate_line}\n",
                encoding="utf-8",
            )
            lint_output_buffer = io.StringIO()
            with redirect_stdout(lint_output_buffer):
                lint_rc = cli.main(["task", "lint", str(workspace)])
            lint_output = lint_output_buffer.getvalue()

            self.assertEqual(lint_rc, 0)
            self.assertNotIn("未声明 `[CENTAUR_TASK_CONTRACT]`", lint_output)
            self.assertIn("结论: PASS", lint_output)


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


class TaskContractLintTests(unittest.TestCase):
    CONTRACT_LINE = (
        '[CENTAUR_TASK_CONTRACT] '
        '{"version":1,"unit":"set_exact","baseline":"lint-path-normalize","allowed_delta":[],"forbidden_delta":[],"precedence":["forbidden","allowed","wording"]}'
    )
    VALID_WORKER_END_STATE = {
        "PATCH_APPLIED": 1,
        "COMMIT_CREATED": 0,
        "CARRYOVER_FILES": [],
        "SEAL_MODE": "UNSEALED",
        "RELEASE_DECISION": "READY",
    }
    VALID_SUPERVISOR_DISPATCH_GATE = {
        "STATUS_CMD": "cd /repo && git status --short -- src/centaur/cli.py tests/test_cli.py",
        "STATUS_RC": 0,
        "STATUS_HAS_UNSEALED_DIRTY": 0,
        "TARGET_DIFF_CMD": "cd /repo && git diff --name-only -- src/centaur/cli.py tests/test_cli.py",
        "TARGET_DIFF_RC": 0,
        "TARGET_DIFF_HAS_CHANGES": 0,
        "TASK_KIND": "DIAGNOSE",
        "DISPATCH_DECISION": "ALLOW_FUNCTIONAL",
    }
    VALID_COMPLEXITY_IMPACT = {
        "change_scope": "src/centaur/cli.py",
        "complexity_delta": "O(n) -> O(n)",
        "runtime_impact": "none",
        "maintainability_impact": "low",
        "risk_level": "low",
        "evidence_refs": ["tests:test_cli.py::TaskContractLintTests"],
    }
    VALID_COMPLEXITY_REVIEW = {
        "decision": "pass",
        "risk_level": "low",
        "reason": "evidence sufficient",
        "required_action": "none",
    }

    @classmethod
    def _worker_end_state_line(cls, overrides: dict[str, object] | None = None) -> str:
        payload = dict(cls.VALID_WORKER_END_STATE)
        if overrides:
            payload.update(overrides)
        return "[CENTAUR_WORKER_END_STATE] " + json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _supervisor_dispatch_gate_line(cls, overrides: dict[str, object] | None = None) -> str:
        payload = dict(cls.VALID_SUPERVISOR_DISPATCH_GATE)
        if overrides:
            payload.update(overrides)
        return "[CENTAUR_SUPERVISOR_DISPATCH_GATE] " + json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _complexity_impact_line(cls, overrides: dict[str, object] | None = None) -> str:
        payload = dict(cls.VALID_COMPLEXITY_IMPACT)
        if overrides:
            payload.update(overrides)
        return "[CENTAUR_COMPLEXITY_IMPACT] " + json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _complexity_review_line(cls, overrides: dict[str, object] | None = None) -> str:
        payload = dict(cls.VALID_COMPLEXITY_REVIEW)
        if overrides:
            payload.update(overrides)
        return "[CENTAUR_COMPLEXITY_REVIEW] " + json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def _write_task_with_worker_end_state(cls, workspace: Path, line: str, gate_line: str | None = None) -> None:
        resolved_gate_line = gate_line if gate_line is not None else cls._supervisor_dispatch_gate_line()
        (workspace / "TASK.md").write_text(
            (
                "# 当前任务 (Task)\n\n"
                "## 机审契约\n"
                f"{cls.CONTRACT_LINE}\n\n"
                f"{resolved_gate_line}\n\n"
                "---\n"
                "## Worker 反馈区\n"
                "### Worker 执行报告 (2026-03-06 12:00 +0800)\n"
                f"{line}\n"
            ),
            encoding="utf-8",
        )

    @classmethod
    def _append_validator_report(cls, workspace: Path, line: str) -> None:
        with (workspace / "TASK.md").open("a", encoding="utf-8") as handle:
            handle.write("### Validator 审查报告 (2026-03-06 12:30 +0800)\n")
            handle.write(f"{line}\n")

    def test_task_lint_normalizes_task_md_file_path_argument(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            expected_task_path = (workspace / "TASK.md").resolve()
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n\n## 机审契约\n"
                    + self.CONTRACT_LINE
                    + "\n"
                    + self._supervisor_dispatch_gate_line()
                    + "\n"
                ),
                encoding="utf-8",
            )

            original_cwd = os.getcwd()
            os.chdir(workspace)
            try:
                scenarios = (
                    ("relative-task-file", ["task", "lint", "TASK.md"]),
                    ("workspace-dot", ["task", "lint", "."]),
                    ("absolute-task-file", ["task", "lint", str(workspace / "TASK.md")]),
                )
                for label, argv in scenarios:
                    with self.subTest(path_mode=label):
                        output_buffer = io.StringIO()
                        with redirect_stdout(output_buffer):
                            rc = cli.main(argv)
                        output = output_buffer.getvalue()

                        self.assertEqual(rc, 0)
                        self.assertIn(f"🧪 TASK 契约检查: {expected_task_path}", output)
                        self.assertNotIn(f"{expected_task_path}/TASK.md", output)
                        self.assertNotIn("未声明 `[CENTAUR_TASK_CONTRACT]`", output)
                        self.assertIn("结论: PASS", output)
            finally:
                os.chdir(original_cwd)

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

    def test_task_lint_requires_supervisor_dispatch_gate_when_contract_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "TASK.md").write_text(
                (
                    "# 当前任务 (Task)\n\n"
                    "## 机审契约\n"
                    f"{self.CONTRACT_LINE}\n"
                ),
                encoding="utf-8",
            )

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("缺少 `[CENTAUR_SUPERVISOR_DISPATCH_GATE]` 派单封板闸门证据", output)

    def test_task_lint_requires_supervisor_dispatch_gate_command_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            gate_line = self._supervisor_dispatch_gate_line(
                {
                    "STATUS_CMD": "cd /repo && git status -- src/centaur/cli.py",
                    "TARGET_DIFF_CMD": "cd /repo && git show --name-only",
                }
            )
            self._write_task_with_worker_end_state(workspace, self._worker_end_state_line(), gate_line=gate_line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("`STATUS_CMD` 必须包含 `git status --short` 证据", output)
            self.assertIn("`TARGET_DIFF_CMD` 必须包含目标文件 `git diff` 证据", output)

    def test_task_lint_blocks_function_task_when_unsealed_dirty_changes_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            gate_line = self._supervisor_dispatch_gate_line(
                {
                    "STATUS_HAS_UNSEALED_DIRTY": 1,
                    "TASK_KIND": "FEATURE",
                    "DISPATCH_DECISION": "ALLOW_FUNCTIONAL",
                }
            )
            self._write_task_with_worker_end_state(workspace, self._worker_end_state_line(), gate_line=gate_line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("检测到未封板业务脏改时，`DISPATCH_DECISION` 必须为 `SEAL_ONLY`", output)
            self.assertIn("检测到未封板业务脏改时，功能任务必须阻断；仅允许 `TASK_KIND=SEAL_ONLY`", output)

    def test_task_lint_allows_seal_only_task_when_unsealed_dirty_changes_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            gate_line = self._supervisor_dispatch_gate_line(
                {
                    "STATUS_HAS_UNSEALED_DIRTY": 1,
                    "TASK_KIND": "SEAL_ONLY",
                    "DISPATCH_DECISION": "SEAL_ONLY",
                }
            )
            worker_lines = self._worker_end_state_line() + "\n" + self._complexity_impact_line()
            self._write_task_with_worker_end_state(workspace, worker_lines, gate_line=gate_line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("结论: PASS", output)

    def test_task_lint_passes_with_valid_worker_end_state_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            worker_lines = self._worker_end_state_line() + "\n" + self._complexity_impact_line()
            self._write_task_with_worker_end_state(workspace, worker_lines)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("结论: PASS", output)

    def test_task_lint_passes_with_valid_complexity_impact_and_review_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            worker_lines = self._worker_end_state_line() + "\n" + self._complexity_impact_line()
            self._write_task_with_worker_end_state(workspace, worker_lines)
            self._append_validator_report(workspace, self._complexity_review_line())

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 0)
            self.assertIn("结论: PASS", output)

    def test_task_lint_blocks_when_worker_complexity_impact_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            self._write_task_with_worker_end_state(workspace, self._worker_end_state_line())

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("缺少 `[CENTAUR_COMPLEXITY_IMPACT]`", output)

    def test_task_lint_blocks_when_worker_complexity_impact_json_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            worker_lines = self._worker_end_state_line() + "\n" + "[CENTAUR_COMPLEXITY_IMPACT] {invalid-json"
            self._write_task_with_worker_end_state(workspace, worker_lines)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("[CENTAUR_COMPLEXITY_IMPACT]", output)
            self.assertIn("JSON 非法", output)

    def test_task_lint_blocks_when_validator_complexity_review_vetoes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            worker_lines = self._worker_end_state_line() + "\n" + self._complexity_impact_line()
            self._write_task_with_worker_end_state(workspace, worker_lines)
            self._append_validator_report(workspace, self._complexity_review_line({"decision": "veto"}))

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("`veto`", output)
            self.assertIn("Fail-Closed", output)

    def test_task_lint_blocks_high_risk_pass_when_evidence_is_insufficient(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            worker_lines = self._worker_end_state_line() + "\n" + self._complexity_impact_line(
                {
                    "risk_level": "high",
                    "evidence_refs": ["tests:single-proof"],
                }
            )
            self._write_task_with_worker_end_state(workspace, worker_lines)
            self._append_validator_report(
                workspace,
                self._complexity_review_line(
                    {
                        "decision": "pass",
                        "risk_level": "high",
                        "reason": "insufficient proof but pass",
                        "required_action": "none",
                    }
                ),
            )

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("高风险复杂度变更证据不足时，`decision` 必须为 `veto`", output)

    def test_task_lint_blocks_backtick_wrapped_worker_end_state_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            wrapped_line = f"`{self._worker_end_state_line()}`"
            self._write_task_with_worker_end_state(workspace, wrapped_line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("被反引号/引号包裹", output)
            self.assertIn("[CENTAUR_WORKER_END_STATE]", output)

    def test_task_lint_blocks_command_substitution_polluted_dispatch_gate_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            polluted_gate_line = "[CENTAUR_SUPERVISOR_DISPATCH_GATE] $(cat /tmp/fake_gate.json)"
            self._write_task_with_worker_end_state(
                workspace,
                self._worker_end_state_line(),
                gate_line=polluted_gate_line,
            )

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("载荷包含 `$(` 命令替换片段", output)
            self.assertIn("[CENTAUR_SUPERVISOR_DISPATCH_GATE]", output)

    def test_task_lint_requires_worker_end_state_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            payload = dict(self.VALID_WORKER_END_STATE)
            payload.pop("RELEASE_DECISION")
            line = "[CENTAUR_WORKER_END_STATE] " + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            self._write_task_with_worker_end_state(workspace, line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("结束态回填缺少 `RELEASE_DECISION`", output)

    def test_task_lint_requires_commit_metadata_when_commit_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            line = self._worker_end_state_line({"COMMIT_CREATED": 1})
            self._write_task_with_worker_end_state(workspace, line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("`commit_sha` 必须是非空字符串", output)
            self.assertIn("`commit_files` 必须是字符串数组", output)

    def test_task_lint_requires_carryover_metadata_when_sealed_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            line = self._worker_end_state_line({"SEAL_MODE": "SEALED_BLOCKED"})
            self._write_task_with_worker_end_state(workspace, line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("`carryover_reason` 必须是非空字符串", output)
            self.assertIn("`owner` 必须是非空字符串", output)
            self.assertIn("`next_min_action` 必须是非空字符串", output)
            self.assertIn("`SEAL_MODE=SEALED_BLOCKED` 时必须提供非空 `due_cycle`", output)

    def test_task_lint_blocks_feature_task_kind_in_non_git_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            gate_line = self._supervisor_dispatch_gate_line({"TASK_KIND": "FEATURE"})
            self._write_task_with_worker_end_state(workspace, self._worker_end_state_line(), gate_line=gate_line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("非 Git 工作区禁止 `TASK_KIND=FEATURE`", output)
            self.assertIn("[NEXT_STEP]", output)

    def test_task_lint_rejects_forged_commit_files_when_commit_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            tracked_file = workspace / "tracked.txt"
            tracked_file.write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Centaur Bot"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "centaur@example.com"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True, text=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=workspace, check=True, capture_output=True, text=True
            ).stdout.strip()

            gate_line = self._supervisor_dispatch_gate_line({"TASK_KIND": "FEATURE", "DISPATCH_DECISION": "ALLOW_FUNCTIONAL"})
            line = self._worker_end_state_line(
                {
                    "COMMIT_CREATED": 1,
                    "commit_sha": head_sha,
                    "commit_files": ["wrong.txt"],
                }
            )
            self._write_task_with_worker_end_state(workspace, line, gate_line=gate_line)

            output_buffer = io.StringIO()
            with redirect_stdout(output_buffer):
                rc = cli.main(["task", "lint", str(workspace)])
            output = output_buffer.getvalue()

            self.assertEqual(rc, 1)
            self.assertIn("BLOCKED_SPEC", output)
            self.assertIn("`commit_files` 与 `git show --name-only` 不一致", output)


if __name__ == "__main__":
    unittest.main()
