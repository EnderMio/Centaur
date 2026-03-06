from __future__ import annotations

import argparse
import json
from importlib.resources import files
from pathlib import Path
import subprocess
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
    TASK_CONTRACT_MODE_ENFORCE,
    TASK_CONTRACT_MODE_OFF,
    TASK_CONTRACT_MODE_WARN,
    TASKS_DIR,
    LOGS_DIR,
    STATE_FILE,
    codex_available,
    collect_prompt_mode_issues,
    build_codex_exec_permission_args,
    default_project_config,
    ensure_active_task_file,
    ensure_runtime_layout,
    format_runtime_policy_audit,
    infer_prompt_mode_from_workspace,
    init_state_file,
    is_framework_repo_root,
    list_tasks,
    load_project_config,
    load_or_init_project_config,
    migrate_schema,
    parse_runtime_policy,
    run_workflow,
    save_project_config,
    sync_task_bus_to_active,
    task_file_path,
    lint_task_contract,
    validate_task_name,
)


RUNTIME_STATE_PATH = f"{RUNTIME_DIR}/{STATE_FILE}"
RUNTIME_PROJECT_PATH = f"{RUNTIME_DIR}/{PROJECT_FILE}"
DOCTOR_LOG_WRITE_PROBE = ".doctor_write_probe"
WORKER_REPORT_HEADER = "### Worker 执行报告"
WORKER_END_STATE_PREFIX = "[CENTAUR_WORKER_END_STATE] "
WORKER_END_STATE_REQUIRED_FIELDS = (
    "PATCH_APPLIED",
    "COMMIT_CREATED",
    "CARRYOVER_FILES",
    "SEAL_MODE",
    "RELEASE_DECISION",
)
SUPERVISOR_DISPATCH_GATE_PREFIX = "[CENTAUR_SUPERVISOR_DISPATCH_GATE] "
SUPERVISOR_DISPATCH_GATE_REQUIRED_FIELDS = (
    "STATUS_CMD",
    "STATUS_RC",
    "STATUS_HAS_UNSEALED_DIRTY",
    "TARGET_DIFF_CMD",
    "TARGET_DIFF_RC",
    "TARGET_DIFF_HAS_CHANGES",
    "TASK_KIND",
    "DISPATCH_DECISION",
)
SUPERVISOR_DISPATCH_GATE_TASK_KINDS = ("FEATURE", "INIT", "DIAGNOSE", "SEAL_ONLY")
NON_GIT_ALLOWED_TASK_KINDS = ("INIT", "DIAGNOSE", "SEAL_ONLY")
SUPERVISOR_DISPATCH_GATE_DECISIONS = ("ALLOW_FUNCTIONAL", "SEAL_ONLY")


def _resolve_workspace(path_arg: str, workspace_arg: str | None) -> Path:
    target = workspace_arg if workspace_arg else path_arg
    return Path(target).resolve()


def _resolve_task_lint_workspace(path_arg: str, workspace_arg: str | None) -> Path:
    target = _resolve_workspace(path_arg, workspace_arg)
    if target.is_dir():
        return target
    if target.name == "TASK.md":
        return target.parent
    return target


def _read_task_lines(task_path: Path) -> tuple[list[str], list[str]]:
    try:
        return task_path.read_text(encoding="utf-8").splitlines(), []
    except OSError as exc:
        return [], [f"读取 TASK.md 失败: {exc}"]


def _run_git(workdir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=workdir, check=False, capture_output=True, text=True)


def _is_git_workspace(workdir: Path) -> bool:
    probe = _run_git(workdir, ["rev-parse", "--is-inside-work-tree"])
    return probe.returncode == 0 and probe.stdout.strip().lower() == "true"


def _normalize_required_string_list(value: object, field_name: str, errors: list[str], *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"`{field_name}` 必须是字符串数组")
        return []

    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"`{field_name}[{index}]` 必须是非空字符串")
            continue
        normalized.append(item.strip())

    if not allow_empty and not normalized:
        errors.append(f"`{field_name}` 不能为空")
    return normalized


def _normalize_required_binary(value: object, field_name: str, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        errors.append(f"`{field_name}` 必须是 0 或 1")
        return None
    return value


def _normalize_required_nonempty_string(value: object, field_name: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"`{field_name}` 必须是非空字符串")
        return ""
    return value.strip()


def _normalize_required_nonnegative_int(value: object, field_name: str, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"`{field_name}` 必须是非负整数")
        return None
    return value


def _find_latest_supervisor_dispatch_gate_payload(task_path: Path) -> tuple[dict[str, object] | None, list[str]]:
    lines, read_errors = _read_task_lines(task_path)
    if read_errors:
        return None, read_errors

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line.startswith(SUPERVISOR_DISPATCH_GATE_PREFIX):
            continue
        payload_text = line[len(SUPERVISOR_DISPATCH_GATE_PREFIX) :].strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            return None, [f"`{SUPERVISOR_DISPATCH_GATE_PREFIX.strip()}` JSON 非法: {exc}"]
        if not isinstance(payload, dict):
            return None, [f"`{SUPERVISOR_DISPATCH_GATE_PREFIX.strip()}` 载荷必须是 JSON 对象"]
        return payload, []
    return None, [f"缺少 `{SUPERVISOR_DISPATCH_GATE_PREFIX.strip()}` 派单封板闸门证据"]


def _lint_supervisor_dispatch_gate(task_path: Path, workspace: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    payload, parse_errors = _find_latest_supervisor_dispatch_gate_payload(task_path)
    if parse_errors:
        return parse_errors, warnings
    if payload is None:
        return errors, warnings

    for field_name in SUPERVISOR_DISPATCH_GATE_REQUIRED_FIELDS:
        if field_name not in payload:
            errors.append(f"派单封板闸门缺少 `{field_name}`")

    status_cmd = _normalize_required_nonempty_string(payload.get("STATUS_CMD"), "STATUS_CMD", errors)
    status_rc = _normalize_required_nonnegative_int(payload.get("STATUS_RC"), "STATUS_RC", errors)
    status_has_unsealed_dirty = _normalize_required_binary(
        payload.get("STATUS_HAS_UNSEALED_DIRTY"), "STATUS_HAS_UNSEALED_DIRTY", errors
    )
    target_diff_cmd = _normalize_required_nonempty_string(payload.get("TARGET_DIFF_CMD"), "TARGET_DIFF_CMD", errors)
    target_diff_rc = _normalize_required_nonnegative_int(payload.get("TARGET_DIFF_RC"), "TARGET_DIFF_RC", errors)
    _normalize_required_binary(payload.get("TARGET_DIFF_HAS_CHANGES"), "TARGET_DIFF_HAS_CHANGES", errors)

    task_kind_raw = _normalize_required_nonempty_string(payload.get("TASK_KIND"), "TASK_KIND", errors)
    dispatch_decision_raw = _normalize_required_nonempty_string(payload.get("DISPATCH_DECISION"), "DISPATCH_DECISION", errors)

    if status_cmd and "git status --short" not in status_cmd:
        errors.append("`STATUS_CMD` 必须包含 `git status --short` 证据")
    if status_rc is not None and status_rc != 0:
        errors.append("`STATUS_RC` 必须为 0，否则无法确认派单前封板闸门已执行")

    if target_diff_cmd and "git diff" not in target_diff_cmd:
        errors.append("`TARGET_DIFF_CMD` 必须包含目标文件 `git diff` 证据")
    if target_diff_rc is not None and target_diff_rc != 0:
        errors.append("`TARGET_DIFF_RC` 必须为 0，否则无法确认目标文件 diff 检查已执行")

    task_kind = task_kind_raw.upper()
    dispatch_decision = dispatch_decision_raw.upper()
    if task_kind and task_kind not in SUPERVISOR_DISPATCH_GATE_TASK_KINDS:
        errors.append(f"`TASK_KIND` 非法，必须是 {SUPERVISOR_DISPATCH_GATE_TASK_KINDS}")
    if dispatch_decision and dispatch_decision not in SUPERVISOR_DISPATCH_GATE_DECISIONS:
        errors.append(f"`DISPATCH_DECISION` 非法，必须是 {SUPERVISOR_DISPATCH_GATE_DECISIONS}")

    if status_has_unsealed_dirty == 1:
        if dispatch_decision != "SEAL_ONLY":
            errors.append("检测到未封板业务脏改时，`DISPATCH_DECISION` 必须为 `SEAL_ONLY`")
        if task_kind != "SEAL_ONLY":
            errors.append("检测到未封板业务脏改时，功能任务必须阻断；仅允许 `TASK_KIND=SEAL_ONLY`")

    if task_kind and not _is_git_workspace(workspace):
        if task_kind == "FEATURE":
            errors.append("非 Git 工作区禁止 `TASK_KIND=FEATURE`，仅允许 `INIT/DIAGNOSE/SEAL_ONLY`")
        elif task_kind not in NON_GIT_ALLOWED_TASK_KINDS:
            errors.append(f"非 Git 工作区 `TASK_KIND` 非法，必须是 {NON_GIT_ALLOWED_TASK_KINDS}")

    return errors, warnings


def _find_latest_worker_end_state_payload(task_path: Path) -> tuple[dict[str, object] | None, list[str], bool]:
    lines, read_errors = _read_task_lines(task_path)
    if read_errors:
        return None, read_errors, False

    latest_worker_index = -1
    for index, raw_line in enumerate(lines):
        if raw_line.strip().startswith(WORKER_REPORT_HEADER):
            latest_worker_index = index

    if latest_worker_index < 0:
        return None, [], False

    for raw_line in reversed(lines[latest_worker_index + 1 :]):
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith(WORKER_END_STATE_PREFIX):
            continue

        payload_text = line[len(WORKER_END_STATE_PREFIX) :].strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            return None, [f"`{WORKER_END_STATE_PREFIX.strip()}` JSON 非法: {exc}"], True

        if not isinstance(payload, dict):
            return None, [f"`{WORKER_END_STATE_PREFIX.strip()}` 载荷必须是 JSON 对象"], True
        return payload, [], True

    return None, [f"最新 Worker 执行报告缺少 `{WORKER_END_STATE_PREFIX.strip()}` 回填字段"], True


def _lint_worker_end_state(task_path: Path, workspace: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    payload, parse_errors, worker_report_found = _find_latest_worker_end_state_payload(task_path)
    if parse_errors:
        return parse_errors, warnings
    if not worker_report_found:
        return errors, warnings
    if payload is None:
        return errors, warnings

    for field_name in WORKER_END_STATE_REQUIRED_FIELDS:
        if field_name not in payload:
            errors.append(f"结束态回填缺少 `{field_name}`")

    _normalize_required_binary(payload.get("PATCH_APPLIED"), "PATCH_APPLIED", errors)
    commit_created = _normalize_required_binary(payload.get("COMMIT_CREATED"), "COMMIT_CREATED", errors)
    _normalize_required_string_list(payload.get("CARRYOVER_FILES"), "CARRYOVER_FILES", errors, allow_empty=True)
    seal_mode = _normalize_required_nonempty_string(payload.get("SEAL_MODE"), "SEAL_MODE", errors)
    _normalize_required_nonempty_string(payload.get("RELEASE_DECISION"), "RELEASE_DECISION", errors)

    commit_sha = ""
    commit_files: list[str] = []
    if commit_created == 1:
        commit_sha = _normalize_required_nonempty_string(payload.get("commit_sha"), "commit_sha", errors)
        commit_files = _normalize_required_string_list(payload.get("commit_files"), "commit_files", errors, allow_empty=False)

    if seal_mode.upper() == "SEALED_BLOCKED":
        _normalize_required_nonempty_string(payload.get("carryover_reason"), "carryover_reason", errors)
        _normalize_required_nonempty_string(payload.get("owner"), "owner", errors)
        _normalize_required_nonempty_string(payload.get("next_min_action"), "next_min_action", errors)
        due_cycle = payload.get("due_cycle")
        if (
            isinstance(due_cycle, bool)
            or due_cycle is None
            or (isinstance(due_cycle, str) and not due_cycle.strip())
            or (not isinstance(due_cycle, (int, str)))
        ):
            errors.append("`SEAL_MODE=SEALED_BLOCKED` 时必须提供非空 `due_cycle`")

    if commit_created == 1 and commit_sha:
        if not _is_git_workspace(workspace):
            errors.append("非 Git 工作区无法验证 `commit_sha/commit_files`，请改用可复验的 Git 证据。")
            return errors, warnings

        verify = _run_git(workspace, ["cat-file", "-e", f"{commit_sha}^{{commit}}"])
        if verify.returncode != 0:
            detail = verify.stderr.strip() or verify.stdout.strip() or "unknown"
            errors.append(f"`commit_sha` 不可达: {commit_sha} ({detail})")
            return errors, warnings

        show = _run_git(workspace, ["show", "--name-only", "--pretty=format:", commit_sha])
        if show.returncode != 0:
            detail = show.stderr.strip() or show.stdout.strip() or "unknown"
            errors.append(f"`commit_files` 校验失败：无法执行 `git show --name-only --pretty=format: {commit_sha}` ({detail})")
            return errors, warnings

        declared_files = sorted({item.strip() for item in commit_files if item.strip()})
        actual_files = sorted({line.strip() for line in show.stdout.splitlines() if line.strip()})
        if declared_files != actual_files:
            errors.append(
                "`commit_files` 与 `git show --name-only` 不一致: "
                f"declared={declared_files}, actual={actual_files}"
            )

    return errors, warnings


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


def _doctor_log_dir_writability_error(target_dir: Path) -> str | None:
    logs_dir = target_dir / RUNTIME_DIR / LOGS_DIR
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"日志目录不可写: 无法创建 {logs_dir}（{exc}）"

    if not logs_dir.is_dir():
        return f"日志目录不可写: {logs_dir} 不是目录。"

    probe_path = logs_dir / DOCTOR_LOG_WRITE_PROBE
    try:
        with probe_path.open("w", encoding="utf-8") as handle:
            handle.write("ok\n")
    except OSError as exc:
        return f"日志目录不可写: {logs_dir}（{exc}）"
    finally:
        try:
            probe_path.unlink()
        except OSError:
            pass

    return None


def _emit_cli_error(reason: str, next_step: str) -> None:
    print(f"[CLI_ERROR] {reason}")
    print(f"[NEXT_STEP] {next_step}")


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
        _emit_cli_error(
            f"工作区根目录不存在: {root}",
            "请检查 --root 路径是否正确，或先执行 `centaur workspace create <name> --root <path>` 创建工作区。",
        )
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
        _emit_cli_error(
            f"工作区不存在: {target_dir}",
            "请确认路径后重试，或先执行 `centaur init -w <workspace>` 初始化工作区。",
        )
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
        _emit_cli_error(
            "非法任务名。允许字母/数字/._-，长度 1-64，且必须以字母或数字开头。",
            "请改用合法任务名后重试，例如 `centaur task new task-001`。",
        )
        return 1

    config = load_or_init_project_config(target_dir)
    ensure_runtime_layout(target_dir)
    target = task_file_path(target_dir, args.name)
    if target.exists() and not args.force:
        _emit_cli_error(
            f"任务已存在: {args.name}（如需覆盖请加 --force）",
            "请改用新任务名，或显式追加 `--force` 后重试。",
        )
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
        _emit_cli_error(
            "非法任务名。允许字母/数字/._-，长度 1-64，且必须以字母或数字开头。",
            "请改用合法任务名后重试，例如 `centaur task switch task-001`。",
        )
        return 1

    config = load_or_init_project_config(target_dir)
    old_active = str(config.get("active_task", DEFAULT_TASK_NAME))
    target = task_file_path(target_dir, args.name)
    if not target.exists():
        _emit_cli_error(
            f"任务不存在: {args.name}（先执行 `centaur task new {args.name}`）",
            f"请先创建任务 `centaur task new {args.name}`，再执行 switch。",
        )
        return 1

    sync_task_bus_to_active(target_dir, old_active)
    config["active_task"] = args.name
    save_project_config(target_dir, config)
    ensure_active_task_file(target_dir, config)
    print(f"✅ 已切换任务: {old_active} -> {args.name}")
    return 0


def cmd_task_lint(args: argparse.Namespace) -> int:
    target_dir = _resolve_task_lint_workspace(args.path, args.workspace)
    if not target_dir.exists():
        _emit_cli_error(
            f"工作区不存在: {target_dir}",
            "请确认路径后重试，或先执行 `centaur init -w <workspace>` 初始化工作区。",
        )
        return 1

    errors, warnings, contract = lint_task_contract(target_dir)
    if contract is not None:
        dispatch_gate_errors, dispatch_gate_warnings = _lint_supervisor_dispatch_gate(target_dir / "TASK.md", target_dir)
        errors.extend(dispatch_gate_errors)
        warnings.extend(dispatch_gate_warnings)

        end_state_errors, end_state_warnings = _lint_worker_end_state(target_dir / "TASK.md", target_dir)
        errors.extend(end_state_errors)
        warnings.extend(end_state_warnings)

    print(f"🧪 TASK 契约检查: {target_dir / 'TASK.md'}")
    if contract is not None:
        print(f"- INFO: unit={contract.get('unit')} | baseline={contract.get('baseline', '')}")
    for item in warnings:
        print(f"- WARN: {item}")
    for item in errors:
        print(f"- ERROR: {item}")

    if errors:
        print("结论: BLOCKED_SPEC")
        print("[NEXT_STEP] 修复 TASK.md 机审字段后重试 `centaur task lint`")
        print("[NEXT_STEP] 若是任务类型冲突，请由 Supervisor 修正 `TASK_KIND` 后重新派单")
        return 1
    print("结论: PASS")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.force_from_role and args.from_role is None:
        _emit_cli_error(
            "检测到 `--force-from-role`，但缺少 `--from-role` 目标角色。",
            "请改为 `centaur run --from-role <role> --force-from-role`，或移除 `--force-from-role`。",
        )
        return 1

    if args.from_role is not None and not args.force_from_role:
        _emit_cli_error(
            "默认恢复路径禁止直接使用 `--from-role` 覆盖 inflight 自动恢复。",
            "移除 `--from-role` 以走自动恢复；如需强制覆盖，请显式追加 `--force-from-role`。",
        )
        return 1

    run_workflow(
        _resolve_workspace(args.path, args.workspace),
        start_step=args.from_role if args.force_from_role else None,
        allow_repo_root=args.allow_repo_root,
        headless=args.headless,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    errors: list[str] = []
    warnings: list[str] = []
    infos: list[str] = []

    if not target_dir.exists():
        _emit_cli_error(
            f"工作区不存在: {target_dir}",
            "请确认路径后重试，或先执行 `centaur init -w <workspace>` 初始化工作区。",
        )
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
        task_contract_mode = TASK_CONTRACT_MODE_ENFORCE
        runtime_config = default_project_config(prompt_mode=inferred_mode)
    else:
        prompt_mode = str(config.get("prompt_mode", PROMPT_MODE_GLOBAL))
        active_task = str(config.get("active_task", DEFAULT_TASK_NAME))
        task_contract_mode = str(config.get("task_contract_mode", TASK_CONTRACT_MODE_ENFORCE))
        if task_contract_mode not in (TASK_CONTRACT_MODE_OFF, TASK_CONTRACT_MODE_WARN, TASK_CONTRACT_MODE_ENFORCE):
            task_contract_mode = TASK_CONTRACT_MODE_ENFORCE
        runtime_config = config
        infos.append(f"prompt_mode={prompt_mode}")
        infos.append(f"project_config={target_dir / RUNTIME_PROJECT_PATH}")
        infos.append(f"active_task={active_task}")
        infos.append(f"task_contract_mode={task_contract_mode}")
        infos.append(f"controller_version={config.get('controller_version', '')}")
        infos.append(f"target_repo={config.get('target_repo', '')}")
        infos.append(f"target_ref={config.get('target_ref', '')}")
        infos.append(f"target_version={config.get('target_version', '')}")

    infos.append(f"human_gate_policy={runtime_config.get('human_gate_policy', 'always')!r}")
    infos.append(f"codex_exec_sandbox={runtime_config.get('codex_exec_sandbox')!r}")
    infos.append(f"codex_exec_dangerously_bypass={runtime_config.get('codex_exec_dangerously_bypass', False)!r}")
    try:
        runtime_policy = parse_runtime_policy(runtime_config)
    except ValueError as exc:
        errors.append(f"运行策略配置非法: {exc}")
    else:
        infos.append(f"runtime_policy={format_runtime_policy_audit(runtime_policy)}")
        infos.append("codex_exec_permission_args=" + " ".join(build_codex_exec_permission_args(runtime_policy)))

    pm_errors, pm_warnings = collect_prompt_mode_issues(target_dir, prompt_mode)
    errors.extend(pm_errors)
    warnings.extend(pm_warnings)

    active_task_file = task_file_path(target_dir, active_task)
    if not active_task_file.exists():
        warnings.append(f"active_task 文件不存在: {active_task_file}（运行时会自动补齐）")

    if task_contract_mode != TASK_CONTRACT_MODE_OFF:
        contract_errors, contract_warnings, _contract = lint_task_contract(target_dir)
        for warning in contract_warnings:
            warnings.append(f"TASK 契约: {warning}")
        if contract_errors:
            if task_contract_mode == TASK_CONTRACT_MODE_ENFORCE:
                errors.extend([f"TASK 契约冲突: {item}" for item in contract_errors])
            else:
                warnings.extend([f"TASK 契约冲突(未阻断): {item}" for item in contract_errors])

    log_writability_error = _doctor_log_dir_writability_error(target_dir)
    if log_writability_error is not None:
        errors.append(log_writability_error)

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
        _emit_cli_error(
            f"Doctor 检查未通过，共 {len(errors)} 项错误。",
            "请按上面的 `- ERROR` 项逐条修复后，再执行 `centaur doctor` 复检。",
        )
        print("结论: FAIL")
        return 1
    print("结论: PASS")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    target_dir = _resolve_workspace(args.path, args.workspace)
    if not target_dir.exists():
        _emit_cli_error(
            f"目录不存在: {target_dir}",
            "请确认目标路径后重试，或先执行 `centaur init -w <workspace>` 初始化工作区。",
        )
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
    workspace_parser.set_defaults(group_parser=workspace_parser)
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
        help="Target role to force-override resume state (requires --force-from-role).",
    )
    run_parser.add_argument(
        "--force-from-role",
        action="store_true",
        help="Explicit confirmation to bypass inflight auto-recovery and apply --from-role.",
    )
    run_parser.add_argument(
        "--allow-repo-root",
        action="store_true",
        help="Allow running in Centaur source repository root (not recommended).",
    )
    run_parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without interactive TTY (use `codex exec` for automation/CI).",
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
    task_parser.set_defaults(group_parser=task_parser)
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

    task_lint_parser = task_subparsers.add_parser("lint", help="Validate structured acceptance contract in TASK.md.")
    task_lint_parser.add_argument("path", nargs="?", default=".", help="Workspace directory (default: current directory).")
    task_lint_parser.add_argument(
        "-w",
        "--workspace",
        help="Workspace directory override (same purpose as positional `path`).",
    )
    task_lint_parser.set_defaults(func=cmd_task_lint)

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
        if hasattr(args, "group_parser"):
            args.group_parser.print_help()
            return 2
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
