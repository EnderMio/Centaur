from __future__ import annotations

import json
from importlib.resources import files
import subprocess
import shutil
import re
from datetime import datetime, timezone
import sys
from pathlib import Path
from typing import Any
import uuid

from centaur import __version__

ROLE_TEMPLATE_FILES = ("AGENTS.md", "SUPERVISOR.md", "WORKER.md", "VALIDATOR.md")
PROJECT_TEMPLATE_FILES = ("PROPOSAL.md",)
CORE_FILES = ROLE_TEMPLATE_FILES
REQUIRED_WORKSPACE_FILES = ("PROPOSAL.md",)
MEMORY_FILES = ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md")
ROLE_ORDER = ("supervisor", "human_gate", "worker", "validator")
TRANSACTIONAL_ROLES = ("supervisor", "worker", "validator")
PROMPT_MODE_GLOBAL = "global"
PROMPT_MODE_FROZEN = "frozen"
PROMPT_MODES = (PROMPT_MODE_GLOBAL, PROMPT_MODE_FROZEN)
PROJECT_SCHEMA_VERSION = 2
PROMPT_SET_VERSION = "2026-03-02"
RUNTIME_DIR = ".centaur"
STATE_FILE = "state.json"
PROJECT_FILE = "project.json"
LEGACY_STATE_FILE = ".centaur_state.json"
LEGACY_PROJECT_FILE = ".centaur_project.json"
TASKS_DIR = "tasks"
LOGS_DIR = "logs"
CONTROL_DIR = "control"
CONTROL_TASKS_FILE = "tasks.json"
SCHEDULER_STATE_FILE = "scheduler_state.json"
EVENTS_FILE = "events.jsonl"
DEFAULT_TASK_NAME = "default"
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CONTROL_SCHEMA_VERSION = 1
CONTROL_MODE_SERIAL = "serial"
ROLE_LABELS = {
    "supervisor": "Supervisor",
    "human_gate": "Human Gate",
    "worker": "Worker",
    "validator": "Validator",
}
TRANSACTION_STATE_FIELDS = ("inflight_role", "run_id", "started_at", "attempt")
TASK_COMPLETION_EVIDENCE_PREFIX = "[CENTAUR_ROLE_COMPLETION] "


def is_framework_repo_root(workdir: Path) -> bool:
    return (workdir / ".git").exists() and (workdir / "pyproject.toml").exists() and (workdir / "src" / "centaur").exists()


def enforce_workspace_guard(workdir: Path, allow_repo_root: bool = False) -> None:
    if allow_repo_root or not is_framework_repo_root(workdir):
        return
    print(f"❌ 检测到你正在框架源码根目录运行: {workdir}")
    print("👉 请使用 `centaur run --workspace <path>` 在独立工作区运行。")
    print("👉 如确需在源码根运行，请显式添加 `--allow-repo-root`。")
    raise SystemExit(1)


def _template_dir():
    return files("centaur.templates")


def template_exists(filename: str) -> bool:
    try:
        _template_dir().joinpath(filename).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    return True


def read_packaged_template(filename: str) -> str:
    return _template_dir().joinpath(filename).read_text(encoding="utf-8")


def resolve_prompt_content(workdir: Path, filename: str, prompt_mode: str) -> tuple[str, str]:
    local_path = workdir / filename
    if prompt_mode == PROMPT_MODE_FROZEN:
        if local_path.exists():
            return local_path.read_text(encoding="utf-8"), "项目冻结"
        raise FileNotFoundError(filename)
    if template_exists(filename):
        return read_packaged_template(filename), "全局模板"
    raise FileNotFoundError(filename)


def check_env(workdir: Path) -> None:
    missing_workspace = [name for name in REQUIRED_WORKSPACE_FILES if not (workdir / name).exists()]
    if missing_workspace:
        print(f"❌ 启动失败：缺少工作区文件 {missing_workspace}")
        print("👉 请先运行 `centaur init` 初始化模板。")
        raise SystemExit(1)


def _role_log_filename(role: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", role.strip().lower()).strip("_")
    role_name = normalized or "unknown"
    return f"{role_name}.log"


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_events_path(workdir: Path) -> Path:
    return _runtime_dir(workdir) / EVENTS_FILE


def append_event(
    workdir: Path,
    cycle: int,
    event_type: str,
    role: str | None = None,
    return_code: int | None = None,
) -> None:
    ensure_runtime_layout(workdir)
    event_path = get_events_path(workdir)
    payload: dict[str, Any] = {
        "timestamp": _iso_utc_now(),
        "cycle": int(cycle),
        "event_type": event_type,
    }
    if role is not None:
        payload["role"] = role
    if return_code is not None:
        payload["return_code"] = int(return_code)

    try:
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(
            f"❌ 写入事件日志失败: {event_path}（{exc}）。"
            f"请检查目录权限与磁盘空间，确保 `{event_path.parent}` 可写。"
        )
        raise SystemExit(1)


def _write_role_execution_log(
    workdir: Path,
    role: str,
    cycle: int,
    command: list[str],
    start_time: str,
    end_time: str,
    return_code: int | None,
    stdout: str,
    stderr: str,
    execution_mode: str,
) -> None:
    ensure_runtime_layout(workdir)
    filename = f"cycle_{cycle}_{_role_log_filename(role)}"
    log_path = get_logs_dir(workdir) / filename
    payload = {
        "role": role,
        "cycle": cycle,
        "command": command,
        "start_time": start_time,
        "end_time": end_time,
        "return_code": return_code,
        "execution_mode": execution_mode,
        "stdout": stdout,
        "stderr": stderr,
    }
    try:
        log_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[⚠️] 写入角色执行日志失败: {exc}")


def run_agent(
    role: str,
    prompt_filename: str,
    workdir: Path,
    prompt_mode: str,
    cycle: int = 0,
    headless: bool = False,
) -> None:
    try:
        prompt_content, source = resolve_prompt_content(workdir, prompt_filename, prompt_mode)
    except FileNotFoundError as exc:
        if prompt_mode == PROMPT_MODE_FROZEN:
            print(f"❌ 缺少项目角色提示词文件：{exc}. 请执行 `centaur migrate --prompts frozen --force` 修复。")
        else:
            print(f"❌ 缺少全局角色提示词模板：{exc}. 请重新安装 Centaur CLI，或执行 `centaur migrate --prompts frozen`。")
        raise SystemExit(1)
    except OSError as exc:
        print(f"❌ 读取提示词失败: {exc}")
        raise SystemExit(1)

    execution_mode = "headless" if headless else "interactive"
    print(f"\n[🚀] 正在唤醒 {role}... (提示词来源: {source})")
    command = ["codex", "exec", "--full-auto", prompt_content] if headless else ["codex", "--full-auto", prompt_content]
    start_time = _iso_utc_now()
    end_time = start_time
    return_code = 1
    stdout_text = ""
    stderr_text = ""
    role_started = False
    try:
        append_event(workdir, cycle=cycle, event_type="role_start", role=role)
        role_started = True
        if headless:
            completed = subprocess.run(command, check=False, cwd=workdir, capture_output=True, text=True)
            stdout_text = completed.stdout or ""
            stderr_text = completed.stderr or ""
            if stdout_text:
                print(stdout_text, end="" if stdout_text.endswith("\n") else "\n")
            if stderr_text:
                print(stderr_text, end="" if stderr_text.endswith("\n") else "\n")
        else:
            completed = subprocess.run(command, check=False, cwd=workdir)
            # 交互模式输出已实时流向终端，不做二次捕获。
            stdout_text = "[streamed_to_terminal]"
            stderr_text = "[streamed_to_terminal]"
        end_time = _iso_utc_now()
        return_code = completed.returncode
        if completed.returncode != 0:
            print(f"[❌] {role} 异常退出 (RC={completed.returncode})，请检查日志。")
            raise SystemExit(1)
        print(f"[✅] {role} 运行结束。")
    except FileNotFoundError:
        end_time = _iso_utc_now()
        return_code = 127
        stderr_text = "未找到 `codex` 命令"
        print("❌ 未找到 `codex` 命令，请先安装并配置 Codex CLI。")
        raise SystemExit(1)
    except KeyboardInterrupt:
        end_time = _iso_utc_now()
        return_code = 130
        stderr_text = "执行被手动中止"
        print(f"\n[⚠️] 手动中止 {role}。")
        raise SystemExit(1)
    finally:
        _write_role_execution_log(
            workdir=workdir,
            role=role,
            cycle=cycle,
            command=command,
            start_time=start_time,
            end_time=end_time,
            return_code=return_code,
            stdout=stdout_text,
            stderr=stderr_text,
            execution_mode=execution_mode,
        )
        if role_started:
            append_event(workdir, cycle=cycle, event_type="role_end", role=role, return_code=return_code)


def human_gate() -> None:
    """人类验收门：设立在 Supervisor 规划之后，拦截发散。"""
    while True:
        print("\n" + "=" * 60)
        print("🚦 [人类验收门 / Human-in-the-Loop]")
        print("Supervisor 已更新全局状态并生成了新 TASK.md。")
        print("=" * 60)

        choice = input("👉 操作: [回车]放行 Worker | [e]去 VSCode 微调 TASK.md | [q]退出 > ").strip().lower()
        if choice in ("", "y"):
            print("🟢 审查通过，放行！")
            return
        if choice == "e":
            print("📝 请在 VSCode 中手动编辑 TASK.md。")
            input("编辑完成后按回车返回验收门继续 > ")
            continue
        if choice == "q":
            print("👋 已安全退出。")
            raise SystemExit(0)


def init_memory_files(workdir: Path) -> None:
    for name in MEMORY_FILES:
        (workdir / name).touch(exist_ok=True)


def _runtime_dir(workdir: Path) -> Path:
    return workdir / RUNTIME_DIR


def ensure_runtime_dir(workdir: Path) -> Path:
    runtime_dir = _runtime_dir(workdir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def ensure_runtime_layout(workdir: Path) -> None:
    runtime_dir = ensure_runtime_dir(workdir)
    (runtime_dir / TASKS_DIR).mkdir(parents=True, exist_ok=True)
    (runtime_dir / LOGS_DIR).mkdir(parents=True, exist_ok=True)
    ensure_control_schema(workdir)


def _project_path(workdir: Path) -> Path:
    return _runtime_dir(workdir) / PROJECT_FILE


def _legacy_project_path(workdir: Path) -> Path:
    return workdir / LEGACY_PROJECT_FILE


def infer_prompt_mode_from_workspace(workdir: Path) -> str:
    if any((workdir / name).exists() for name in ROLE_TEMPLATE_FILES):
        return PROMPT_MODE_FROZEN
    return PROMPT_MODE_GLOBAL


def validate_task_name(task_name: str) -> bool:
    return bool(TASK_NAME_RE.match(task_name))


def _default_task_content() -> str:
    return "# 当前任务 (Task)\n"


def get_tasks_dir(workdir: Path) -> Path:
    return _runtime_dir(workdir) / TASKS_DIR


def get_logs_dir(workdir: Path) -> Path:
    return _runtime_dir(workdir) / LOGS_DIR


def get_control_dir(workdir: Path) -> Path:
    return _runtime_dir(workdir) / CONTROL_DIR


def _control_tasks_path(workdir: Path) -> Path:
    return get_control_dir(workdir) / CONTROL_TASKS_FILE


def _scheduler_state_path(workdir: Path) -> Path:
    return get_control_dir(workdir) / SCHEDULER_STATE_FILE


def _default_control_tasks() -> dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "mode": CONTROL_MODE_SERIAL,
        "tasks": [],
    }


def _default_scheduler_state() -> dict[str, Any]:
    return {
        "schema_version": CONTROL_SCHEMA_VERSION,
        "mode": CONTROL_MODE_SERIAL,
        "max_parallelism": 1,
        "inflight_tasks": [],
        "path_locks": {},
    }


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_control_tasks_schema(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("根节点必须是 JSON 对象")

    schema_version = raw.get("schema_version")
    if not _is_positive_int(schema_version):
        raise ValueError(f"`schema_version` 必须是正整数，当前值={schema_version!r}")

    mode = raw.get("mode")
    if mode != CONTROL_MODE_SERIAL:
        raise ValueError(f"`mode` 必须为 {CONTROL_MODE_SERIAL!r}，当前值={mode!r}")

    tasks = raw.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError(f"`tasks` 必须是数组，当前值={tasks!r}")


def _validate_scheduler_state_schema(raw: Any) -> None:
    if not isinstance(raw, dict):
        raise ValueError("根节点必须是 JSON 对象")

    schema_version = raw.get("schema_version")
    if not _is_positive_int(schema_version):
        raise ValueError(f"`schema_version` 必须是正整数，当前值={schema_version!r}")

    mode = raw.get("mode")
    if mode != CONTROL_MODE_SERIAL:
        raise ValueError(f"`mode` 必须为 {CONTROL_MODE_SERIAL!r}，当前值={mode!r}")

    max_parallelism = raw.get("max_parallelism")
    if isinstance(max_parallelism, bool) or not isinstance(max_parallelism, int) or max_parallelism != 1:
        raise ValueError(f"`max_parallelism` 在串行模式下必须为 1，当前值={max_parallelism!r}")

    inflight_tasks = raw.get("inflight_tasks")
    if not isinstance(inflight_tasks, list):
        raise ValueError(f"`inflight_tasks` 必须是数组，当前值={inflight_tasks!r}")

    path_locks = raw.get("path_locks")
    if not isinstance(path_locks, dict):
        raise ValueError(f"`path_locks` 必须是对象，当前值={path_locks!r}")


def _write_control_file(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        print(f"❌ 控制面文件写入失败（{path.name}）: {exc}")
        raise SystemExit(1)


def _read_control_file(path: Path) -> Any:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"❌ 控制面文件读取失败（{path.name}）: {exc}")
        raise SystemExit(1)
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        print(f"❌ 控制面文件 JSON 非法（{path.name}）: {exc}")
        raise SystemExit(1)


def _validate_control_file(path: Path, validator: Any) -> None:
    raw = _read_control_file(path)
    try:
        validator(raw)
    except ValueError as exc:
        print(f"❌ 控制面文件契约校验失败（{path.name}）: {exc}")
        raise SystemExit(1)


def ensure_control_schema(workdir: Path) -> None:
    control_dir = get_control_dir(workdir)
    try:
        control_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"❌ 控制面目录创建失败（{control_dir}）: {exc}")
        raise SystemExit(1)

    tasks_path = _control_tasks_path(workdir)
    if tasks_path.exists():
        _validate_control_file(tasks_path, _validate_control_tasks_schema)
    else:
        _write_control_file(tasks_path, _default_control_tasks())

    scheduler_state_path = _scheduler_state_path(workdir)
    if scheduler_state_path.exists():
        _validate_control_file(scheduler_state_path, _validate_scheduler_state_schema)
    else:
        _write_control_file(scheduler_state_path, _default_scheduler_state())


def task_file_path(workdir: Path, task_name: str) -> Path:
    return get_tasks_dir(workdir) / f"{task_name}.md"


def default_project_config(prompt_mode: str | None = None) -> dict[str, Any]:
    mode = prompt_mode if prompt_mode in PROMPT_MODES else PROMPT_MODE_GLOBAL
    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "centaur_version": __version__,
        "prompt_set_version": PROMPT_SET_VERSION,
        "prompt_mode": mode,
        "active_task": DEFAULT_TASK_NAME,
        "controller_version": __version__,
        "target_repo": "",
        "target_ref": "main",
        "target_version": "",
    }


def _normalize_project_config(raw: dict[str, Any], fallback_mode: str) -> dict[str, Any]:
    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, int) or schema_version <= 0:
        schema_version = PROJECT_SCHEMA_VERSION

    centaur_version = raw.get("centaur_version")
    if not isinstance(centaur_version, str) or not centaur_version.strip():
        centaur_version = __version__

    prompt_set_version = raw.get("prompt_set_version")
    if not isinstance(prompt_set_version, str) or not prompt_set_version.strip():
        prompt_set_version = PROMPT_SET_VERSION

    prompt_mode = raw.get("prompt_mode")
    if prompt_mode not in PROMPT_MODES:
        prompt_mode = fallback_mode

    active_task = raw.get("active_task")
    if not isinstance(active_task, str) or not validate_task_name(active_task):
        active_task = DEFAULT_TASK_NAME

    controller_version = raw.get("controller_version")
    if not isinstance(controller_version, str) or not controller_version.strip():
        controller_version = __version__

    target_repo = raw.get("target_repo")
    if not isinstance(target_repo, str):
        target_repo = ""

    target_ref = raw.get("target_ref")
    if not isinstance(target_ref, str) or not target_ref.strip():
        target_ref = "main"

    target_version = raw.get("target_version")
    if not isinstance(target_version, str):
        target_version = ""

    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "centaur_version": centaur_version,
        "prompt_set_version": prompt_set_version,
        "prompt_mode": prompt_mode,
        "active_task": active_task,
        "controller_version": controller_version,
        "target_repo": target_repo,
        "target_ref": target_ref,
        "target_version": target_version,
    }


def save_project_config(workdir: Path, config: dict[str, Any]) -> None:
    ensure_runtime_layout(workdir)
    path = _project_path(workdir)
    tmp_path = path.with_name(f"{path.name}.tmp")
    content = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def load_project_config(workdir: Path) -> dict[str, Any] | None:
    fallback_mode = infer_prompt_mode_from_workspace(workdir)
    path = _project_path(workdir)
    legacy_path = _legacy_project_path(workdir)

    for candidate in (path, legacy_path):
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            print(f"⚠️ 项目配置读取失败，将自动重建：{candidate.name}")
            continue
        config = _normalize_project_config(raw, fallback_mode)
        if candidate == legacy_path:
            save_project_config(workdir, config)
            print(f"ℹ️ 已迁移旧项目配置到 {_project_path(workdir)}")
        return config

    return None


def load_or_init_project_config(workdir: Path) -> dict[str, Any]:
    ensure_runtime_layout(workdir)
    config = load_project_config(workdir)
    if config is not None:
        return config
    inferred_mode = infer_prompt_mode_from_workspace(workdir)
    config = default_project_config(prompt_mode=inferred_mode)
    save_project_config(workdir, config)
    print(f"ℹ️ 已创建 {_project_path(workdir)} (prompt_mode={inferred_mode})")
    return config


def validate_prompt_mode_env(workdir: Path, prompt_mode: str) -> None:
    errors, warnings = collect_prompt_mode_issues(workdir, prompt_mode)
    for warning in warnings:
        print(f"ℹ️ {warning}")
    if errors:
        print(f"❌ 启动失败：{errors[0]}")
        if prompt_mode == PROMPT_MODE_FROZEN:
            print("👉 请执行 `centaur migrate --prompts frozen --force` 修复。")
        else:
            print("👉 请重新安装 Centaur CLI，或切换到 frozen: `centaur migrate --prompts frozen`")
        raise SystemExit(1)


def collect_prompt_mode_issues(workdir: Path, prompt_mode: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if prompt_mode == PROMPT_MODE_FROZEN:
        missing_local = [name for name in CORE_FILES if not (workdir / name).exists()]
        if missing_local:
            errors.append(f"frozen 模式缺少项目角色提示词文件 {missing_local}")
        return errors, warnings

    missing_packaged = [name for name in CORE_FILES if not template_exists(name)]
    if missing_packaged:
        errors.append(f"global 模式缺少安装包模板 {missing_packaged}")
        return errors, warnings

    ignored_local = [name for name in CORE_FILES if (workdir / name).exists()]
    if ignored_local:
        warnings.append("当前为 global 模式，已忽略项目内角色提示词文件: " + ", ".join(ignored_local))
        warnings.append("如需使用项目提示词，请执行 `centaur migrate --prompts frozen`。")
    return errors, warnings


def codex_available() -> bool:
    return shutil.which("codex") is not None


def _build_state(cycle: int, next_step: str) -> dict[str, Any]:
    return {
        "cycle": cycle,
        "next_step": next_step,
        "inflight_role": None,
        "run_id": None,
        "started_at": None,
        "attempt": 0,
    }


def _default_state() -> dict[str, Any]:
    return _build_state(cycle=1, next_step="supervisor")


def _state_path(workdir: Path) -> Path:
    return _runtime_dir(workdir) / STATE_FILE


def _legacy_state_path(workdir: Path) -> Path:
    return workdir / LEGACY_STATE_FILE


def _normalize_attempt(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("状态根节点必须是 JSON 对象")

    errors: list[str] = []

    cycle_raw = raw.get("cycle")
    if isinstance(cycle_raw, bool) or not isinstance(cycle_raw, int) or cycle_raw <= 0:
        errors.append(f"`cycle` 必须是正整数，当前值={cycle_raw!r}")
        cycle = 1
    else:
        cycle = cycle_raw

    next_step_raw = raw.get("next_step")
    if not isinstance(next_step_raw, str) or next_step_raw not in ROLE_ORDER:
        errors.append(f"`next_step` 非法，必须为 {ROLE_ORDER} 之一，当前值={next_step_raw!r}")
        next_step = "supervisor"
    else:
        next_step = next_step_raw

    if "inflight_role" not in raw:
        inflight_role = None
    else:
        inflight_raw = raw.get("inflight_role")
        if inflight_raw is None:
            inflight_role = None
        elif isinstance(inflight_raw, str) and inflight_raw in ROLE_ORDER:
            inflight_role = inflight_raw
        else:
            errors.append(f"`inflight_role` 非法，必须为 null 或 {ROLE_ORDER} 之一，当前值={inflight_raw!r}")
            inflight_role = None

    if "run_id" not in raw:
        run_id = None
    else:
        run_id_raw = raw.get("run_id")
        if run_id_raw is None:
            run_id = None
        elif isinstance(run_id_raw, str) and run_id_raw.strip():
            run_id = run_id_raw
        else:
            errors.append(f"`run_id` 非法，必须为非空字符串或 null，当前值={run_id_raw!r}")
            run_id = None

    if "started_at" not in raw:
        started_at = None
    else:
        started_at_raw = raw.get("started_at")
        if started_at_raw is None:
            started_at = None
        elif isinstance(started_at_raw, str) and started_at_raw.strip():
            started_at = started_at_raw
        else:
            errors.append(f"`started_at` 非法，必须为非空字符串或 null，当前值={started_at_raw!r}")
            started_at = None

    if "attempt" not in raw:
        attempt = 0
    else:
        attempt_raw = raw.get("attempt")
        if isinstance(attempt_raw, bool) or not isinstance(attempt_raw, int) or attempt_raw < 0:
            errors.append(f"`attempt` 非法，必须为 >= 0 的整数，当前值={attempt_raw!r}")
            attempt = 0
        else:
            attempt = attempt_raw

    if not errors:
        if inflight_role is None:
            if run_id is not None:
                errors.append("`inflight_role` 为 null 时，`run_id` 必须为 null")
            if started_at is not None:
                errors.append("`inflight_role` 为 null 时，`started_at` 必须为 null")
            if attempt != 0:
                errors.append("`inflight_role` 为 null 时，`attempt` 必须为 0")
        else:
            if run_id is None:
                errors.append("`inflight_role` 非 null 时，`run_id` 不能为空")
            if started_at is None:
                errors.append("`inflight_role` 非 null 时，`started_at` 不能为空")
            if attempt <= 0:
                errors.append("`inflight_role` 非 null 时，`attempt` 必须 >= 1")
            if next_step != inflight_role:
                errors.append("在途状态不一致：`next_step` 必须与 `inflight_role` 一致")

    if errors:
        raise ValueError("; ".join(errors))

    return {
        "cycle": cycle,
        "next_step": next_step,
        "inflight_role": inflight_role,
        "run_id": run_id,
        "started_at": started_at,
        "attempt": attempt,
    }


def save_state(workdir: Path, state: dict[str, Any]) -> None:
    ensure_runtime_layout(workdir)
    path = _state_path(workdir)
    tmp_path = path.with_name(f"{path.name}.tmp")
    content = json.dumps(state, ensure_ascii=False, indent=2) + "\n"
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)


def infer_state_from_task(workdir: Path) -> dict[str, Any]:
    task_path = workdir / "TASK.md"
    if not task_path.exists():
        return _default_state()

    text = task_path.read_text(encoding="utf-8")
    if not text.strip() or text.strip() == "# 当前任务 (Task)":
        return _default_state()

    worker_marker = "### Worker 执行报告"
    validator_marker = "### Validator 审查报告"
    worker_pos = text.rfind(worker_marker)
    validator_pos = text.rfind(validator_marker)
    completed_cycles = text.count(validator_marker)
    cycle = max(1, completed_cycles + 1)

    if worker_pos == -1 and validator_pos == -1:
        if "## Worker 反馈区" in text or "@Worker" in text:
            return _build_state(cycle=cycle, next_step="human_gate")
        return _default_state()
    if worker_pos > validator_pos:
        return _build_state(cycle=cycle, next_step="validator")
    if validator_pos > worker_pos:
        return _build_state(cycle=cycle, next_step="supervisor")
    return _default_state()


def _state_needs_backfill(raw: dict[str, Any], normalized: dict[str, Any]) -> bool:
    return any(key not in raw or raw.get(key) != normalized[key] for key in normalized)


def _infer_state_from_events(workdir: Path) -> dict[str, Any] | None:
    event_path = get_events_path(workdir)
    if not event_path.exists():
        return None

    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        cycle = payload.get("cycle")
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle <= 0:
            continue

        event_type = str(payload.get("event_type", "")).strip().lower()
        if event_type == "cycle_end":
            return _build_state(cycle=cycle + 1, next_step="supervisor")
        if event_type == "cycle_start":
            return _build_state(cycle=cycle, next_step="supervisor")
        if event_type not in {"role_start", "role_end"}:
            continue

        role = str(payload.get("role", "")).strip().lower()
        if role not in TRANSACTIONAL_ROLES:
            continue

        if event_type == "role_start":
            recovered = _build_state(cycle=cycle, next_step=role)
            recovered["inflight_role"] = role
            recovered["run_id"] = f"{cycle}-{role}-recovered"
            timestamp = payload.get("timestamp")
            recovered["started_at"] = timestamp if isinstance(timestamp, str) and timestamp.strip() else _iso_utc_now()
            recovered["attempt"] = 1
            return recovered

        return_code = payload.get("return_code")
        if isinstance(return_code, bool) or not isinstance(return_code, int):
            continue
        if return_code == 0:
            if role == "supervisor":
                return _build_state(cycle=cycle, next_step="human_gate")
            if role == "worker":
                return _build_state(cycle=cycle, next_step="validator")
            if role == "validator":
                return _build_state(cycle=cycle + 1, next_step="supervisor")
        return _build_state(cycle=cycle, next_step=role)

    return None


def load_state(workdir: Path) -> dict[str, Any]:
    ensure_control_schema(workdir)
    path = _state_path(workdir)
    legacy_path = _legacy_state_path(workdir)

    for candidate in (path, legacy_path):
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"❌ 状态文件读取失败（{candidate.name}）: {exc}")
            raise SystemExit(1)
        except json.JSONDecodeError as exc:
            print(f"❌ 状态文件 JSON 非法（{candidate.name}）: {exc}")
            raise SystemExit(1)
        try:
            state = _normalize_state(raw)
        except ValueError as exc:
            print(f"❌ 状态文件契约校验失败（{candidate.name}）: {exc}")
            raise SystemExit(1)
        if candidate == legacy_path or _state_needs_backfill(raw, state):
            save_state(workdir, state)
        if candidate == legacy_path:
            print(f"ℹ️ 已迁移旧状态文件到 {_state_path(workdir)}")
        return state

    inferred_from_events = _infer_state_from_events(workdir)
    if inferred_from_events is not None:
        save_state(workdir, inferred_from_events)
        return inferred_from_events

    inferred = infer_state_from_task(workdir)
    save_state(workdir, inferred)
    return inferred


def init_state_file(workdir: Path, force: bool = False) -> bool:
    path = _state_path(workdir)
    if path.exists() and not force:
        return False
    save_state(workdir, _default_state())
    return True


def ensure_active_task_file(workdir: Path, project_config: dict[str, Any]) -> tuple[str, Path]:
    ensure_runtime_layout(workdir)
    active_task = str(project_config.get("active_task", DEFAULT_TASK_NAME))
    if not validate_task_name(active_task):
        active_task = DEFAULT_TASK_NAME
        project_config["active_task"] = active_task
        save_project_config(workdir, project_config)

    target = task_file_path(workdir, active_task)
    bus = workdir / "TASK.md"

    if not target.exists() and bus.exists():
        target.write_text(bus.read_text(encoding="utf-8"), encoding="utf-8")
    elif target.exists() and not bus.exists():
        bus.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    elif target.exists() and bus.exists():
        try:
            bus_mtime = bus.stat().st_mtime
            task_mtime = target.stat().st_mtime
            if bus_mtime > task_mtime + 1e-6:
                target.write_text(bus.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                bus.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError:
            bus.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        content = _default_task_content()
        target.write_text(content, encoding="utf-8")
        bus.write_text(content, encoding="utf-8")
    return active_task, target


def sync_task_bus_to_active(workdir: Path, active_task: str) -> None:
    bus = workdir / "TASK.md"
    target = task_file_path(workdir, active_task)
    if not bus.exists():
        return
    ensure_runtime_layout(workdir)
    target.write_text(bus.read_text(encoding="utf-8"), encoding="utf-8")


def _normalize_role_token(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip().lower()


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def append_task_completion_evidence(workdir: Path, cycle: int, role: str, run_id: str) -> None:
    role_token = _normalize_role_token(role)
    run_token = run_id.strip() if isinstance(run_id, str) else ""
    if not role_token or not run_token:
        print(f"❌ TASK 完成证据参数非法: cycle={cycle}, role={role}, run_id={run_id}")
        raise SystemExit(1)

    task_path = workdir / "TASK.md"
    if not task_path.exists():
        print("❌ 缺少 TASK.md，无法写入角色完成证据。")
        raise SystemExit(1)

    payload = {
        "cycle": int(cycle),
        "role": role_token,
        "run_id": run_token,
        "status": "completed",
    }
    line = f"{TASK_COMPLETION_EVIDENCE_PREFIX}{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n"
    try:
        with task_path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError as exc:
        print(f"❌ 写入 TASK.md 完成证据失败: {exc}")
        raise SystemExit(1)


def _task_has_completion_evidence(workdir: Path, cycle: int, role: str, run_id: str) -> bool:
    task_path = workdir / "TASK.md"
    if not task_path.exists():
        return False

    target_role = _normalize_role_token(role)
    target_run = run_id.strip()
    target_cycle = int(cycle)
    try:
        lines = task_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line.startswith(TASK_COMPLETION_EVIDENCE_PREFIX):
            continue
        payload_text = line[len(TASK_COMPLETION_EVIDENCE_PREFIX) :].strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if _coerce_int(payload.get("cycle")) != target_cycle:
            continue
        if _normalize_role_token(payload.get("role")) != target_role:
            continue
        if str(payload.get("run_id", "")).strip() != target_run:
            continue
        if str(payload.get("status", "")).strip().lower() != "completed":
            continue
        return True
    return False


def _has_successful_role_end_event(workdir: Path, cycle: int, role: str) -> bool:
    event_path = get_events_path(workdir)
    if not event_path.exists():
        return False

    target_cycle = int(cycle)
    target_role = _normalize_role_token(role)
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("event_type", "")).strip().lower()
        if event_type not in {"role_start", "role_end"}:
            continue
        if _coerce_int(payload.get("cycle")) != target_cycle:
            continue
        if _normalize_role_token(payload.get("role")) != target_role:
            continue
        if event_type != "role_end":
            return False
        return _coerce_int(payload.get("return_code")) == 0
    return False


def _verify_role_dual_gate(workdir: Path, cycle: int, role: str, run_id: str) -> list[str]:
    failures: list[str] = []
    if not _has_successful_role_end_event(workdir, cycle=cycle, role=role):
        failures.append(f"闸门A失败：缺少 cycle={cycle}, role={role} 的 role_end(return_code=0) 证据")
    if not _task_has_completion_evidence(workdir, cycle=cycle, role=role, run_id=run_id):
        failures.append(
            "闸门B失败：TASK.md 缺少 "
            f"cycle={cycle}, role={role}, run_id={run_id}, status=completed 证据"
        )
    return failures


def _fail_dual_gate_and_stop(
    workdir: Path,
    state: dict[str, Any],
    cycle: int,
    role: str,
    run_id: str,
    active_task: str,
    failures: list[str],
) -> None:
    _clear_role_transaction(state)
    state["cycle"] = cycle
    state["next_step"] = role
    save_state(workdir, state)
    sync_task_bus_to_active(workdir, active_task)
    print(f"❌ 双闸门校验失败，已阻断推进: cycle={cycle}, role={role}, run_id={run_id}")
    for reason in failures:
        print(f"   - {reason}")
    raise SystemExit(1)


def list_tasks(workdir: Path) -> list[str]:
    tasks_dir = get_tasks_dir(workdir)
    if not tasks_dir.exists():
        return []
    names: list[str] = []
    for item in sorted(tasks_dir.glob("*.md")):
        names.append(item.stem)
    return names


def migrate_schema(workdir: Path) -> dict[str, Any]:
    ensure_runtime_layout(workdir)
    config = load_or_init_project_config(workdir)
    state = load_state(workdir)
    save_state(workdir, state)
    active_task, _ = ensure_active_task_file(workdir, config)
    config["schema_version"] = PROJECT_SCHEMA_VERSION
    config["centaur_version"] = __version__
    config["active_task"] = active_task
    if "controller_version" not in config or not str(config["controller_version"]).strip():
        config["controller_version"] = __version__
    if "target_repo" not in config or not isinstance(config["target_repo"], str):
        config["target_repo"] = ""
    if "target_ref" not in config or not str(config["target_ref"]).strip():
        config["target_ref"] = "main"
    if "target_version" not in config or not isinstance(config["target_version"], str):
        config["target_version"] = ""
    save_project_config(workdir, config)
    return config


def _resolve_start_step(state: dict[str, Any], start_step: str | None) -> dict[str, Any]:
    if start_step is None:
        return state
    if start_step not in ROLE_ORDER:
        print(f"❌ 非法起始角色: {start_step}")
        raise SystemExit(1)
    state["next_step"] = start_step
    return state


def _apply_success_transition_from_recovered_role(workdir: Path, state: dict[str, Any], role: str, cycle: int) -> None:
    _clear_role_transaction(state)
    if role == "supervisor":
        state["next_step"] = "human_gate"
        return
    if role == "worker":
        state["next_step"] = "validator"
        return
    if role == "validator":
        append_event(workdir, cycle=cycle, event_type="cycle_end")
        state["cycle"] = cycle + 1
        state["next_step"] = "supervisor"
        return
    state["next_step"] = role


def _recover_inflight_role_state(workdir: Path, state: dict[str, Any]) -> dict[str, Any]:
    inflight_role = state.get("inflight_role")
    if not isinstance(inflight_role, str) or inflight_role not in TRANSACTIONAL_ROLES:
        return state

    cycle = int(state["cycle"])
    run_id = str(state.get("run_id") or "")

    if not _has_successful_role_end_event(workdir, cycle=cycle, role=inflight_role):
        state["next_step"] = inflight_role
        return state

    gate_failures = _verify_role_dual_gate(workdir, cycle=cycle, role=inflight_role, run_id=run_id)
    if gate_failures:
        state["next_step"] = inflight_role
        return state

    _apply_success_transition_from_recovered_role(workdir, state, inflight_role, cycle)
    return state


def _start_role_transaction(state: dict[str, Any], role: str, cycle: int) -> None:
    previous_attempt = _normalize_attempt(state.get("attempt"))
    if state.get("inflight_role") == role and state.get("cycle") == cycle:
        attempt = previous_attempt + 1
    else:
        attempt = 1
    state["inflight_role"] = role
    state["run_id"] = f"{cycle}-{role}-a{attempt}-{uuid.uuid4().hex[:12]}"
    state["started_at"] = _iso_utc_now()
    state["attempt"] = attempt


def _clear_role_transaction(state: dict[str, Any]) -> None:
    state["inflight_role"] = None
    state["run_id"] = None
    state["started_at"] = None
    state["attempt"] = 0


def _ensure_supervisor_bootstrap(workdir: Path, state: dict[str, Any]) -> dict[str, Any]:
    if (workdir / "TASK.md").exists():
        return state
    if state.get("next_step") != "supervisor" or state.get("cycle") != 1:
        print("ℹ️ 检测到 TASK.md 缺失，已强制从 Supervisor 开始首轮建模。")
    return _default_state()


def has_interactive_tty() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def run_workflow(
    workdir: Path | None = None,
    start_step: str | None = None,
    allow_repo_root: bool = False,
    headless: bool = False,
) -> None:
    base = (workdir or Path.cwd()).resolve()

    print("🤖 Codex Agent 2.0 (红蓝对抗版) 已启动！")
    enforce_workspace_guard(base, allow_repo_root=allow_repo_root)
    check_env(base)
    init_memory_files(base)
    project_config = load_or_init_project_config(base)
    active_task, _ = ensure_active_task_file(base, project_config)
    prompt_mode = str(project_config.get("prompt_mode", PROMPT_MODE_GLOBAL))
    validate_prompt_mode_env(base, prompt_mode)
    if not headless and not has_interactive_tty():
        print("❌ 当前会话不是交互终端（TTY），默认模式无法运行。")
        print("👉 请在真实终端运行 `centaur run`，或显式使用 `centaur run --headless`。")
        raise SystemExit(1)
    if headless:
        print("ℹ️ 已启用 headless 模式：将使用 `codex exec` 非交互执行。")
    state = load_state(base)
    state = _resolve_start_step(state, start_step)
    if start_step is None:
        state = _recover_inflight_role_state(base, state)
    state = _ensure_supervisor_bootstrap(base, state)
    save_state(base, state)
    sync_task_bus_to_active(base, active_task)
    print(f"🧭 Prompt 模式: {prompt_mode}")
    print(f"🧷 当前任务: {active_task}")
    print(f"♻️ 自动恢复状态：第 {state['cycle']} 轮，下一角色 {ROLE_LABELS[state['next_step']]}")

    active_cycle: int | None = None
    while True:
        cycle = int(state["cycle"])
        next_step = str(state["next_step"])
        if active_cycle != cycle:
            append_event(base, cycle=cycle, event_type="cycle_start")
            active_cycle = cycle

        print(f"\n{'█' * 60}")
        print(f"🔄 第 {cycle} 轮开发周期 | 当前阶段: {ROLE_LABELS[next_step]}")
        print("█" * 60)

        if next_step == "supervisor":
            _start_role_transaction(state, role="supervisor", cycle=cycle)
            save_state(base, state)
            run_id = str(state.get("run_id") or "")
            run_agent("Supervisor", "SUPERVISOR.md", base, prompt_mode, cycle=cycle, headless=headless)
            append_task_completion_evidence(base, cycle=cycle, role="supervisor", run_id=run_id)
            gate_failures = _verify_role_dual_gate(base, cycle=cycle, role="supervisor", run_id=run_id)
            if gate_failures:
                _fail_dual_gate_and_stop(
                    workdir=base,
                    state=state,
                    cycle=cycle,
                    role="supervisor",
                    run_id=run_id,
                    active_task=active_task,
                    failures=gate_failures,
                )
            _clear_role_transaction(state)
            state["next_step"] = "human_gate"
            save_state(base, state)
            sync_task_bus_to_active(base, active_task)
            continue

        if next_step == "human_gate":
            human_gate()
            state["next_step"] = "worker"
            save_state(base, state)
            sync_task_bus_to_active(base, active_task)
            continue

        if next_step == "worker":
            _start_role_transaction(state, role="worker", cycle=cycle)
            save_state(base, state)
            run_id = str(state.get("run_id") or "")
            run_agent("Worker", "WORKER.md", base, prompt_mode, cycle=cycle, headless=headless)
            append_task_completion_evidence(base, cycle=cycle, role="worker", run_id=run_id)
            gate_failures = _verify_role_dual_gate(base, cycle=cycle, role="worker", run_id=run_id)
            if gate_failures:
                _fail_dual_gate_and_stop(
                    workdir=base,
                    state=state,
                    cycle=cycle,
                    role="worker",
                    run_id=run_id,
                    active_task=active_task,
                    failures=gate_failures,
                )
            _clear_role_transaction(state)
            state["next_step"] = "validator"
            save_state(base, state)
            sync_task_bus_to_active(base, active_task)
            continue

        print("\n🔍 Validator 正在审查 Worker 的代码与数据契约...")
        _start_role_transaction(state, role="validator", cycle=cycle)
        save_state(base, state)
        run_id = str(state.get("run_id") or "")
        run_agent("Validator", "VALIDATOR.md", base, prompt_mode, cycle=cycle, headless=headless)
        append_task_completion_evidence(base, cycle=cycle, role="validator", run_id=run_id)
        gate_failures = _verify_role_dual_gate(base, cycle=cycle, role="validator", run_id=run_id)
        if gate_failures:
            _fail_dual_gate_and_stop(
                workdir=base,
                state=state,
                cycle=cycle,
                role="validator",
                run_id=run_id,
                active_task=active_task,
                failures=gate_failures,
            )
        _clear_role_transaction(state)
        append_event(base, cycle=cycle, event_type="cycle_end")
        state["cycle"] = cycle + 1
        state["next_step"] = "supervisor"
        save_state(base, state)
        sync_task_bus_to_active(base, active_task)
