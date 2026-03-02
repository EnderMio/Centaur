from __future__ import annotations

import argparse
from importlib.resources import files
from pathlib import Path
import time

from centaur import __version__
from centaur.engine import (
    DEFAULT_TASK_NAME,
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
    TASKS_DIR,
    LOGS_DIR,
    STATE_FILE,
    codex_available,
    collect_prompt_mode_issues,
    default_project_config,
    ensure_active_task_file,
    ensure_runtime_layout,
    infer_prompt_mode_from_workspace,
    init_state_file,
    is_framework_repo_root,
    list_tasks,
    load_project_config,
    load_or_init_project_config,
    migrate_schema,
    run_workflow,
    save_project_config,
    sync_task_bus_to_active,
    task_file_path,
    validate_task_name,
)


RUNTIME_STATE_PATH = f"{RUNTIME_DIR}/{STATE_FILE}"
RUNTIME_PROJECT_PATH = f"{RUNTIME_DIR}/{PROJECT_FILE}"


def _resolve_workspace(path_arg: str, workspace_arg: str | None) -> Path:
    target = workspace_arg if workspace_arg else path_arg
    return Path(target).resolve()


def _init_workspace(target_dir: Path, freeze_prompts: bool, force: bool) -> int:
    target_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []
    prompt_mode = PROMPT_MODE_FROZEN if freeze_prompts else PROMPT_MODE_GLOBAL

    project_written, project_skipped = _write_templates(target_dir, PROJECT_TEMPLATE_FILES, force=force)
    written.extend(project_written)
    skipped.extend(project_skipped)

    if freeze_prompts:
        role_written, role_skipped = _write_templates(target_dir, ROLE_TEMPLATE_FILES, force=force)
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

    ensure_runtime_layout(target_dir)
    written.append(f"{RUNTIME_DIR}/{TASKS_DIR}")
    written.append(f"{RUNTIME_DIR}/{LOGS_DIR}")

    if init_state_file(target_dir, force=force):
        written.append(RUNTIME_STATE_PATH)
    else:
        skipped.append(RUNTIME_STATE_PATH)

    existing_config = load_project_config(target_dir)
    if existing_config is None:
        config = default_project_config(prompt_mode=prompt_mode)
        save_project_config(target_dir, config)
        written.append(RUNTIME_PROJECT_PATH)
    elif force:
        config = default_project_config(prompt_mode=prompt_mode)
        save_project_config(target_dir, config)
        written.append(RUNTIME_PROJECT_PATH)
    elif freeze_prompts and existing_config.get("prompt_mode") != PROMPT_MODE_FROZEN:
        existing_config["prompt_mode"] = PROMPT_MODE_FROZEN
        existing_config["centaur_version"] = __version__
        existing_config["prompt_set_version"] = PROMPT_SET_VERSION
        save_project_config(target_dir, existing_config)
        written.append(RUNTIME_PROJECT_PATH)
        notes.append("检测到 --freeze-prompts，已将项目 prompt_mode 同步为 frozen。")
    else:
        skipped.append(RUNTIME_PROJECT_PATH)

    config = load_or_init_project_config(target_dir)
    active_task, active_path = ensure_active_task_file(target_dir, config)
    written.append(str(active_path.relative_to(target_dir)))
    sync_task_bus_to_active(target_dir, active_task)

    print(f"✅ 已初始化: {target_dir}")
    print(f"Prompt 模式: {config['prompt_mode']} ({'项目冻结提示词' if config['prompt_mode'] == PROMPT_MODE_FROZEN else '全局提示词 + 项目可覆盖'})")
    print(f"Active Task: {active_task}")
    if written:
        print("已创建/覆盖: " + ", ".join(dict.fromkeys(written)))
    if skipped:
        print("已存在(跳过): " + ", ".join(dict.fromkeys(skipped)))
    for note in notes:
        print("说明: " + note)
    return 0


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
    return _init_workspace(target_dir, freeze_prompts=args.freeze_prompts, force=args.force)


def cmd_workspace_create(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    workspace = (root / args.name).resolve()
    return _init_workspace(workspace, freeze_prompts=args.freeze_prompts, force=args.force)


def cmd_workspace_list(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    if not root.exists():
        print(f"❌ 工作区根目录不存在: {root}")
        return 1

    candidates: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "PROPOSAL.md").exists() or (child / RUNTIME_DIR / PROJECT_FILE).exists():
            candidates.append(child)

    print(f"📁 工作区根目录: {root}")
    if not candidates:
        print("- (empty)")
        return 0

    for ws in candidates:
        config = load_project_config(ws)
        if config is None:
            print(f"- {ws.name} | mode=unknown | active_task={DEFAULT_TASK_NAME}")
            continue
        active_task = str(config.get("active_task", DEFAULT_TASK_NAME))
        print(f"- {ws.name} | mode={config.get('prompt_mode')} | active_task={active_task}")
    return 0


def cmd_task_list(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    if not target_dir.exists():
        print(f"❌ 工作区不存在: {target_dir}")
        return 1
    config = load_or_init_project_config(target_dir)
    active_task, _ = ensure_active_task_file(target_dir, config)
    tasks = list_tasks(target_dir)
    print(f"🧷 Active Task: {active_task}")
    if not tasks:
        print("- (empty)")
        return 0
    for name in tasks:
        marker = "*" if name == active_task else " "
        print(f"{marker} {name}")
    return 0


def cmd_task_new(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    if not validate_task_name(args.name):
        print("❌ 非法任务名。允许字母/数字/._-，长度 1-64，且必须以字母或数字开头。")
        return 1

    config = load_or_init_project_config(target_dir)
    ensure_runtime_layout(target_dir)
    target = task_file_path(target_dir, args.name)
    if target.exists() and not args.force:
        print(f"❌ 任务已存在: {args.name}（如需覆盖请加 --force）")
        return 1

    bus = target_dir / "TASK.md"
    if args.from_current and bus.exists():
        content = bus.read_text(encoding="utf-8")
    else:
        content = "# 当前任务 (Task)\n"
    target.write_text(content, encoding="utf-8")

    if args.switch:
        old_active = str(config.get("active_task", DEFAULT_TASK_NAME))
        sync_task_bus_to_active(target_dir, old_active)
        config["active_task"] = args.name
        save_project_config(target_dir, config)
        ensure_active_task_file(target_dir, config)
        print(f"✅ 已创建并切换任务: {args.name}")
    else:
        print(f"✅ 已创建任务: {args.name}")
    return 0


def cmd_task_switch(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    if not validate_task_name(args.name):
        print("❌ 非法任务名。允许字母/数字/._-，长度 1-64，且必须以字母或数字开头。")
        return 1

    config = load_or_init_project_config(target_dir)
    old_active = str(config.get("active_task", DEFAULT_TASK_NAME))
    target = task_file_path(target_dir, args.name)
    if not target.exists():
        print(f"❌ 任务不存在: {args.name}（先执行 `centaur task new {args.name}`）")
        return 1

    sync_task_bus_to_active(target_dir, old_active)
    config["active_task"] = args.name
    save_project_config(target_dir, config)
    ensure_active_task_file(target_dir, config)
    print(f"✅ 已切换任务: {old_active} -> {args.name}")
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
        active_task = DEFAULT_TASK_NAME
    else:
        prompt_mode = str(config.get("prompt_mode", PROMPT_MODE_GLOBAL))
        active_task = str(config.get("active_task", DEFAULT_TASK_NAME))
        infos.append(f"prompt_mode={prompt_mode}")
        infos.append(f"project_config={target_dir / RUNTIME_PROJECT_PATH}")
        infos.append(f"active_task={active_task}")
        infos.append(f"controller_version={config.get('controller_version', '')}")
        infos.append(f"target_repo={config.get('target_repo', '')}")
        infos.append(f"target_ref={config.get('target_ref', '')}")
        infos.append(f"target_version={config.get('target_version', '')}")

    pm_errors, pm_warnings = collect_prompt_mode_issues(target_dir, prompt_mode)
    errors.extend(pm_errors)
    warnings.extend(pm_warnings)

    active_task_file = task_file_path(target_dir, active_task)
    if not active_task_file.exists():
        warnings.append(f"active_task 文件不存在: {active_task_file}（运行时会自动补齐）")

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

    schema_changed = False
    if args.schema:
        config = migrate_schema(target_dir)
        written.append(RUNTIME_PROJECT_PATH)
        written.append(RUNTIME_STATE_PATH)
        written.append(f"{RUNTIME_DIR}/{TASKS_DIR}")
        written.append(f"{RUNTIME_DIR}/{LOGS_DIR}")
        schema_changed = True
    else:
        config = load_project_config(target_dir)

    if config is None:
        inferred_mode = infer_prompt_mode_from_workspace(target_dir)
        config = default_project_config(prompt_mode=inferred_mode)
        notes.append(f"未发现 {RUNTIME_PROJECT_PATH}，已按旧项目形态推断为 `{inferred_mode}`。")

    if args.prompts is not None or not args.schema:
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
    if schema_changed:
        print(f"Schema 版本: {config.get('schema_version')} (latest)")
    print(f"Prompt 模式: {config['prompt_mode']}")
    print(
        f"Centaur 版本记录: {config['centaur_version']} | "
        f"prompt_set_version: {config['prompt_set_version']} | active_task: {config.get('active_task', DEFAULT_TASK_NAME)}"
    )
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

    workspace_parser = subparsers.add_parser("workspace", help="Workspace management commands.")
    workspace_subparsers = workspace_parser.add_subparsers(dest="workspace_command")

    workspace_create_parser = workspace_subparsers.add_parser("create", help="Create and initialize a new workspace.")
    workspace_create_parser.add_argument("name", help="Workspace name.")
    workspace_create_parser.add_argument(
        "--root",
        default="./workspaces",
        help="Workspace root directory (default: ./workspaces).",
    )
    workspace_create_parser.add_argument(
        "--freeze-prompts",
        action="store_true",
        help="Copy AGENTS/SUPERVISOR/WORKER/VALIDATOR prompts into the workspace.",
    )
    workspace_create_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite existing files if workspace exists.",
    )
    workspace_create_parser.set_defaults(func=cmd_workspace_create)

    workspace_list_parser = workspace_subparsers.add_parser("list", help="List workspaces under a root directory.")
    workspace_list_parser.add_argument(
        "--root",
        default="./workspaces",
        help="Workspace root directory (default: ./workspaces).",
    )
    workspace_list_parser.set_defaults(func=cmd_workspace_list)

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

    task_parser = subparsers.add_parser("task", help="Task bus management commands.")
    task_subparsers = task_parser.add_subparsers(dest="task_command")

    task_list_parser = task_subparsers.add_parser("list", help="List tasks and show active task.")
    task_list_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    task_list_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    task_list_parser.set_defaults(func=cmd_task_list)

    task_new_parser = task_subparsers.add_parser("new", help="Create a task file under .centaur/tasks.")
    task_new_parser.add_argument("name", help="Task name (A-Za-z0-9._-).")
    task_new_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    task_new_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    task_new_parser.add_argument(
        "--from-current",
        action="store_true",
        help="Initialize new task content from current TASK.md.",
    )
    task_new_parser.add_argument(
        "--switch",
        action="store_true",
        help="Switch to the new task after creation.",
    )
    task_new_parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite if task already exists.",
    )
    task_new_parser.set_defaults(func=cmd_task_new)

    task_switch_parser = task_subparsers.add_parser("switch", help="Switch active task.")
    task_switch_parser.add_argument("name", help="Target task name.")
    task_switch_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    task_switch_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    task_switch_parser.set_defaults(func=cmd_task_switch)

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
    migrate_parser.add_argument(
        "--schema",
        action="store_true",
        help="Run explicit schema/runtime migration to latest layout.",
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
