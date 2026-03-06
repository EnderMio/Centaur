from contextlib import redirect_stdout
import argparse
from importlib.resources import files
import io
import os
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
            self.assertIn("git status --short --", supervisor_template)
            self.assertIn("开始编码前执行并记录", supervisor_template)

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
            self.assertIn("## 7. 项目规则维护机制 (Rule Maintenance Mechanism)", agents_template)
            self.assertIn("`project.json`（机器规则）", agents_template)
            self.assertIn("`AGENTS.md`（长期约束）", agents_template)
            self.assertIn("`TASK.md`（当轮强约束）", agents_template)
            self.assertIn("`PROJECT_STATUS.md` / `LESSONS.md`（审计沉淀）", agents_template)
            self.assertNotIn("共享内存权限错误需提权重跑", agents_template)

            supervisor_template = (workspace / "SUPERVISOR.md").read_text(encoding="utf-8")
            self.assertIn("当命中 `project.json` 中已登记的项目规则时", supervisor_template)
            self.assertIn("`触发条件 / 动作 / 证据要求`", supervisor_template)
            self.assertNotIn("共享内存权限错误需提权重跑", supervisor_template)

            worker_template = (workspace / "WORKER.md").read_text(encoding="utf-8")
            self.assertIn("若命中 `TASK.md` 已声明的项目规则", worker_template)
            self.assertIn("首次失败证据", worker_template)
            self.assertIn("对应动作", worker_template)
            self.assertNotIn("共享内存权限错误需提权重跑", worker_template)

            validator_template = (workspace / "VALIDATOR.md").read_text(encoding="utf-8")
            self.assertIn("首次失败与后续执行双证据闭环", validator_template)
            self.assertIn("不得仅凭口头描述放行", validator_template)
            self.assertNotIn("共享内存权限错误需提权重跑", validator_template)

            project_status_template = (workspace / "PROJECT_STATUS.md").read_text(encoding="utf-8")
            self.assertIn("## 规则变更审计（必填）", project_status_template)
            self.assertIn("规则变更内容", project_status_template)
            self.assertIn("触发场景", project_status_template)
            self.assertIn("验证结论", project_status_template)

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

            (workspace / "TASK.md").write_text(
                f"# 当前任务 (Task)\n\n## 机审契约\n{contract_line}\n",
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
    def test_task_lint_normalizes_task_md_file_path_argument(self) -> None:
        contract_line = (
            '[CENTAUR_TASK_CONTRACT] '
            '{"version":1,"unit":"set_exact","baseline":"lint-path-normalize","allowed_delta":[],"forbidden_delta":[],"precedence":["forbidden","allowed","wording"]}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            expected_task_path = (workspace / "TASK.md").resolve()
            (workspace / "TASK.md").write_text(
                "# 当前任务 (Task)\n\n## 机审契约\n" + contract_line + "\n",
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


if __name__ == "__main__":
    unittest.main()
