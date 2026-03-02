from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path
import time

from centaur import __version__
from centaur.engine import (
    MEMORY_FILES,
    PROJECT_FILE,
    PROJECT_TEMPLATE_FILES,
    PROMPT_MODE_FROZEN,
    PROMPT_MODE_GLOBAL,
    PROMPT_MODES,
    PROMPT_SET_VERSION,
    RUNTIME_DIR,
    ROLE_ORDER,
    ROLE_TEMPLATE_FILES,
    STATE_FILE,
    codex_available,
    collect_prompt_mode_issues,
    default_project_config,
    infer_prompt_mode_from_workspace,
    init_state_file,
    is_framework_repo_root,
    load_project_config,
    run_workflow,
    save_project_config,
)


RUNTIME_STATE_PATH = f"{RUNTIME_DIR}/{STATE_FILE}"
RUNTIME_PROJECT_PATH = f"{RUNTIME_DIR}/{PROJECT_FILE}"


def _resolve_workspace(path_arg: str, workspace_arg: str | None) -> Path:
    target = workspace_arg if workspace_arg else path_arg
    return Path(target).resolve()


def _write_templates(target_dir: Path, template_names: tuple[str, ...], force: bool) -> tuple[list[str], list[str]]:
    template_dir = files("centaur.templates")
    written: list[str] = []
    skipped: list[str] = []

    for name in template_names:
        target_file = target_dir / name
        if target_file.exists() and not force:
            skipped.append(name)
            continue
        content = template_dir.joinpath(name).read_text(encoding="utf-8")
        target_file.write_text(content, encoding="utf-8")
        written.append(name)
    return written, skipped


def _archive_local_role_prompts(target_dir: Path) -> tuple[Path | None, list[str]]:
    local_prompts = [name for name in ROLE_TEMPLATE_FILES if (target_dir / name).exists()]
    if not local_prompts:
        return None, []
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = target_dir / ".centaur_prompts_backup" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in local_prompts:
        (target_dir / name).rename(backup_dir / name)
    return backup_dir, local_prompts


def cmd_init(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []
    prompt_mode = PROMPT_MODE_FROZEN if args.freeze_prompts else PROMPT_MODE_GLOBAL

    project_written, project_skipped = _write_templates(target_dir, PROJECT_TEMPLATE_FILES, force=args.force)
    written.extend(project_written)
    skipped.extend(project_skipped)

    if args.freeze_prompts:
        role_written, role_skipped = _write_templates(target_dir, ROLE_TEMPLATE_FILES, force=args.force)
        written.extend(role_written)
        skipped.extend(role_skipped)

    for name in MEMORY_FILES:
        target_file = target_dir / name
        if target_file.exists():
            skipped.append(name)
            continue
        target_file.touch()
        written.append(name)

    task_file = target_dir / "TASK.md"
    if task_file.exists():
        skipped.append("TASK.md")
    else:
        task_file.write_text("# 当前任务 (Task)\n", encoding="utf-8")
        written.append("TASK.md")

    if init_state_file(target_dir, force=args.force):
        written.append(RUNTIME_STATE_PATH)
    else:
        skipped.append(RUNTIME_STATE_PATH)

    existing_config = load_project_config(target_dir)
    if existing_config is None:
        config = default_project_config(prompt_mode=prompt_mode)
        save_project_config(target_dir, config)
        written.append(RUNTIME_PROJECT_PATH)
    elif args.force:
        config = default_project_config(prompt_mode=prompt_mode)
        save_project_config(target_dir, config)
        written.append(RUNTIME_PROJECT_PATH)
    elif args.freeze_prompts and existing_config.get("prompt_mode") != PROMPT_MODE_FROZEN:
        existing_config["prompt_mode"] = PROMPT_MODE_FROZEN
        existing_config["centaur_version"] = __version__
        existing_config["prompt_set_version"] = PROMPT_SET_VERSION
        save_project_config(target_dir, existing_config)
        written.append(RUNTIME_PROJECT_PATH)
        notes.append("检测到 --freeze-prompts，已将项目 prompt_mode 同步为 frozen。")
    else:
        skipped.append(RUNTIME_PROJECT_PATH)

    print(f"✅ 已初始化: {target_dir}")
    effective_mode = load_project_config(target_dir)
    resolved_mode = effective_mode["prompt_mode"] if effective_mode is not None else prompt_mode
    print(f"Prompt 模式: {resolved_mode} ({'项目冻结提示词' if resolved_mode == PROMPT_MODE_FROZEN else '全局提示词 + 项目可覆盖'})")
    if written:
        print("已创建/覆盖: " + ", ".join(written))
    if skipped:
        print("已存在(跳过): " + ", ".join(skipped))
    for note in notes:
        print("说明: " + note)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    run_workflow(
        _resolve_workspace(args.path, args.workspace),
        start_step=args.from_role,
        allow_repo_root=args.allow_repo_root,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    errors: list[str] = []
    warnings: list[str] = []
    infos: list[str] = []

    if not target_dir.exists():
        print(f"❌ 工作区不存在: {target_dir}")
        return 1

    if is_framework_repo_root(target_dir):
        warnings.append("当前目录是框架源码根，建议使用 `--workspace` 指向独立工作区。")
        if not args.allow_repo_root:
            errors.append("默认策略不建议在源码根运行。可在 run 时显式加 `--allow-repo-root`。")

    proposal_path = target_dir / "PROPOSAL.md"
    if not proposal_path.exists():
        errors.append("缺少 PROPOSAL.md（run 的最小前置条件）。")

    config = load_project_config(target_dir)
    if config is None:
        inferred_mode = infer_prompt_mode_from_workspace(target_dir)
        warnings.append(f"未发现 {RUNTIME_PROJECT_PATH}，将按旧项目形态推断 prompt_mode={inferred_mode}。")
        prompt_mode = inferred_mode
    else:
        prompt_mode = str(config.get("prompt_mode", PROMPT_MODE_GLOBAL))
        infos.append(f"prompt_mode={prompt_mode}")
        infos.append(f"project_config={target_dir / RUNTIME_PROJECT_PATH}")

    pm_errors, pm_warnings = collect_prompt_mode_issues(target_dir, prompt_mode)
    errors.extend(pm_errors)
    warnings.extend(pm_warnings)

    if not codex_available():
        errors.append("未找到 `codex` 命令（run 无法唤醒角色）。")
    else:
        infos.append("codex=available")

    print(f"🩺 Doctor 工作区: {target_dir}")
    for item in infos:
        print(f"- INFO: {item}")
    for item in warnings:
        print(f"- WARN: {item}")
    for item in errors:
        print(f"- ERROR: {item}")

    if errors:
        print("结论: FAIL")
        return 1
    print("结论: PASS")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    if not target_dir.exists():
        print(f"❌ 目录不存在: {target_dir}")
        return 1

    written: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []

    config = load_project_config(target_dir)
    if config is None:
        inferred_mode = infer_prompt_mode_from_workspace(target_dir)
        config = default_project_config(prompt_mode=inferred_mode)
        notes.append(f"未发现 {RUNTIME_PROJECT_PATH}，已按旧项目形态推断为 `{inferred_mode}`。")

    target_mode = args.prompts or config["prompt_mode"]

    if target_mode == PROMPT_MODE_FROZEN:
        role_written, role_skipped = _write_templates(target_dir, ROLE_TEMPLATE_FILES, force=args.force)
        written.extend(role_written)
        skipped.extend(role_skipped)
    elif not args.keep_local_prompts:
        backup_dir, moved = _archive_local_role_prompts(target_dir)
        if moved:
            notes.append(f"已归档本地角色提示词到 {backup_dir}")
            notes.append("归档文件: " + ", ".join(moved))
    else:
        notes.append("已保留项目内角色提示词文件，但在 global 模式下运行时会忽略这些文件。")

    config["prompt_mode"] = target_mode
    config["centaur_version"] = __version__
    config["prompt_set_version"] = PROMPT_SET_VERSION
    save_project_config(target_dir, config)
    written.append(RUNTIME_PROJECT_PATH)

    print(f"✅ 迁移完成: {target_dir}")
    print(f"Prompt 模式: {config['prompt_mode']}")
    print(f"Centaur 版本记录: {config['centaur_version']} | prompt_set_version: {config['prompt_set_version']}")
    if written:
        print("已更新: " + ", ".join(written))
    if skipped:
        print("已存在(跳过): " + ", ".join(skipped))
    for note in notes:
        print("说明: " + note)
    return 0


def cmd_version(_: argparse.Namespace) -> int:
    print(__version__)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="centaur", description="Centaur file-driven multi-agent workflow CLI.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Initialize Centaur markdown templates in a directory.")
    init_parser.add_argument("path", nargs="?", default=".", help="Target directory (default: current directory).")
    init_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    init_parser.add_argument(
        "--freeze-prompts",
        action="store_true",
        help="Copy AGENTS/SUPERVISOR/WORKER/VALIDATOR prompts into the project directory.",
    )
    init_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help=f"Overwrite existing files created by init (including {RUNTIME_PROJECT_PATH}).",
    )
    init_parser.set_defaults(func=cmd_init)

    run_parser = subparsers.add_parser("run", help="Run the Supervisor -> Worker -> Validator workflow loop.")
    run_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    run_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    run_parser.add_argument(
        "--from-role",
        choices=ROLE_ORDER,
        help="Override resume state and force the next role for this workflow.",
    )
    run_parser.add_argument(
        "--allow-repo-root",
        action="store_true",
        help="Allow running in Centaur source repository root (not recommended).",
    )
    run_parser.set_defaults(func=cmd_run)

    doctor_parser = subparsers.add_parser("doctor", help="Check workspace readiness and safety before run.")
    doctor_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    doctor_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    doctor_parser.add_argument(
        "--allow-repo-root",
        action="store_true",
        help="Treat repository-root execution warning as non-fatal.",
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    migrate_parser = subparsers.add_parser("migrate", help="Migrate project metadata and prompt mode.")
    migrate_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    migrate_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    migrate_parser.add_argument(
        "--prompts",
        choices=PROMPT_MODES,
        help="Target prompt mode. `global` uses package templates by default; `frozen` writes local role prompts.",
    )
    migrate_parser.add_argument(
        "--keep-local-prompts",
        action="store_true",
        help="Keep local role prompt files when migrating to global mode.",
    )
    migrate_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite local role prompt files when migrating to frozen mode.",
    )
    migrate_parser.set_defaults(func=cmd_migrate)

    version_parser = subparsers.add_parser("version", help="Print Centaur CLI version.")
    version_parser.set_defaults(func=cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
