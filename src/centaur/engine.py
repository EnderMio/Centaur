from __future__ import annotations

import json
from importlib.resources import files
import subprocess
import shutil
import re
from dataclasses import dataclass
from datetime import datetime, timezone
import sys
from pathlib import Path
from typing import Any
import uuid

from centaur import __version__

ROLE_TEMPLATE_FILES = ("AGENTS.md", "SUPERVISOR.md", "WORKER.md", "VALIDATOR.md")
PROJECT_TEMPLATE_FILES = ("PROPOSAL.md", "PROJECT_STATUS.md", "AGENTS.md")
CORE_FILES = ROLE_TEMPLATE_FILES
PROMPT_MODE_INFER_FROZEN_FILES = tuple(name for name in ROLE_TEMPLATE_FILES if name != "AGENTS.md")
REQUIRED_WORKSPACE_FILES = ("PROPOSAL.md",)
MEMORY_FILES = ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md")
ROLE_ORDER = ("supervisor", "human_gate", "worker", "validator")
TRANSACTIONAL_ROLES = ("supervisor", "worker", "validator")
PROMPT_MODE_GLOBAL = "global"
PROMPT_MODE_FROZEN = "frozen"
PROMPT_MODES = (PROMPT_MODE_GLOBAL, PROMPT_MODE_FROZEN)
PROJECT_SCHEMA_VERSION = 3
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
RUNTIME_METRICS_FILE = "runtime_metrics.json"
RUNTIME_METRICS_SCHEMA_VERSION = 1
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
TASK_CONTRACT_PREFIX = "[CENTAUR_TASK_CONTRACT] "
CHECKPOINT_ROLE = "validator"
WORKER_RESULT_SUCCESS = "success"
WORKER_RESULT_FAILED = "failed"
WORKER_RESULT_BLOCKED = "blocked"
WORKER_RESULT_INCOMPLETE = "incomplete"
SUPERVISOR_TASK_REQUIRED_SECTIONS = (
    "## 任务目标",
    "## 约束边界",
    "## 验收标准",
    "## Worker 反馈区",
)
TASK_CONTRACT_MODE_OFF = "off"
TASK_CONTRACT_MODE_WARN = "warn"
TASK_CONTRACT_MODE_ENFORCE = "enforce"
TASK_CONTRACT_MODES = (
    TASK_CONTRACT_MODE_OFF,
    TASK_CONTRACT_MODE_WARN,
    TASK_CONTRACT_MODE_ENFORCE,
)
TASK_CONTRACT_UNITS = ("text_exact", "set_exact", "set_plus")
HUMAN_GATE_POLICY_ALWAYS = "always"
HUMAN_GATE_POLICY_RISK = "risk"
HUMAN_GATE_POLICY_OFF = "off"
HUMAN_GATE_POLICIES = (
    HUMAN_GATE_POLICY_ALWAYS,
    HUMAN_GATE_POLICY_RISK,
    HUMAN_GATE_POLICY_OFF,
)
CODEX_EXEC_SANDBOX_VALUES = ("read-only", "workspace-write", "danger-full-access")
DEFAULT_CODEX_EXEC_SANDBOX = "workspace-write"


@dataclass(frozen=True)
class RuntimePolicy:
    human_gate_policy: str
    codex_exec_sandbox: str | None
    codex_exec_dangerously_bypass: bool


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


def get_runtime_metrics_path(workdir: Path) -> Path:
    return _runtime_dir(workdir) / RUNTIME_METRICS_FILE


def _parse_event_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token:
        return None
    try:
        parsed = datetime.fromisoformat(token)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _round_metric_seconds(value: float) -> float:
    return round(float(value), 6)


def _derive_runtime_metrics(workdir: Path) -> dict[str, Any]:
    event_path = get_events_path(workdir)
    if event_path.exists():
        try:
            raw_lines = event_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            raw_lines = []
    else:
        raw_lines = []

    line_count = 0
    recognized_event_count = 0
    invalid_event_count = 0
    incomplete_role_duration_count = 0
    cycles_seen: set[int] = set()
    successful_cycles: set[int] = set()
    cycle_starts: dict[int, datetime] = {}
    cycle_ends: dict[int, datetime] = {}
    cycle_role_totals: dict[int, dict[str, float]] = {}
    cycle_role_runs: dict[int, dict[str, int]] = {}
    open_role_spans: dict[tuple[int, str], datetime] = {}

    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line:
            continue
        line_count += 1
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            invalid_event_count += 1
            continue
        if not isinstance(payload, dict):
            invalid_event_count += 1
            continue

        cycle = _coerce_int(payload.get("cycle"))
        if cycle is None or cycle <= 0:
            invalid_event_count += 1
            continue

        event_type = str(payload.get("event_type", "")).strip().lower()
        if event_type not in {"cycle_start", "cycle_end", "role_start", "role_end"}:
            continue

        recognized_event_count += 1
        cycles_seen.add(cycle)
        timestamp = _parse_event_timestamp(payload.get("timestamp"))

        if event_type == "cycle_start":
            if timestamp is None:
                continue
            current = cycle_starts.get(cycle)
            if current is None or timestamp < current:
                cycle_starts[cycle] = timestamp
            continue

        if event_type == "cycle_end":
            successful_cycles.add(cycle)
            if timestamp is None:
                continue
            current = cycle_ends.get(cycle)
            if current is None or timestamp > current:
                cycle_ends[cycle] = timestamp
            continue

        role = _normalize_role_token(payload.get("role"))
        if role not in TRANSACTIONAL_ROLES:
            invalid_event_count += 1
            continue

        key = (cycle, role)
        if event_type == "role_start":
            if timestamp is None:
                incomplete_role_duration_count += 1
                continue
            if key in open_role_spans:
                incomplete_role_duration_count += 1
            open_role_spans[key] = timestamp
            continue

        if timestamp is None:
            incomplete_role_duration_count += 1
            continue
        start_ts = open_role_spans.pop(key, None)
        if start_ts is None:
            incomplete_role_duration_count += 1
            continue
        duration_seconds = (timestamp - start_ts).total_seconds()
        if duration_seconds < 0:
            incomplete_role_duration_count += 1
            continue

        role_totals = cycle_role_totals.setdefault(cycle, {})
        role_runs = cycle_role_runs.setdefault(cycle, {})
        role_totals[role] = role_totals.get(role, 0.0) + duration_seconds
        role_runs[role] = role_runs.get(role, 0) + 1

    incomplete_role_duration_count += len(open_role_spans)

    cycles_payload: list[dict[str, Any]] = []
    incomplete_cycle_duration_count = 0
    role_totals_all: dict[str, float] = {}
    role_runs_all: dict[str, int] = {}

    for cycle in sorted(cycles_seen):
        start_ts = cycle_starts.get(cycle)
        end_ts = cycle_ends.get(cycle)
        duration_seconds: float | None = None
        if start_ts is not None and end_ts is not None:
            delta_seconds = (end_ts - start_ts).total_seconds()
            if delta_seconds >= 0:
                duration_seconds = _round_metric_seconds(delta_seconds)
        if duration_seconds is None:
            incomplete_cycle_duration_count += 1

        role_payload: dict[str, Any] = {}
        cycle_totals = cycle_role_totals.get(cycle, {})
        cycle_runs = cycle_role_runs.get(cycle, {})
        for role in sorted(cycle_totals):
            total_seconds = cycle_totals[role]
            runs = cycle_runs.get(role, 0)
            role_totals_all[role] = role_totals_all.get(role, 0.0) + total_seconds
            role_runs_all[role] = role_runs_all.get(role, 0) + runs
            role_payload[role] = {
                "total_seconds": _round_metric_seconds(total_seconds),
                "runs": runs,
                "avg_seconds": _round_metric_seconds(total_seconds / runs) if runs > 0 else None,
            }

        cycles_payload.append(
            {
                "cycle": cycle,
                "status": "passed" if cycle in successful_cycles else "incomplete",
                "duration_seconds": duration_seconds,
                "role_durations": role_payload,
            }
        )

    role_durations_payload: dict[str, Any] = {}
    for role in sorted(role_totals_all):
        total_seconds = role_totals_all[role]
        runs = role_runs_all[role]
        role_durations_payload[role] = {
            "total_seconds": _round_metric_seconds(total_seconds),
            "runs": runs,
            "avg_seconds": _round_metric_seconds(total_seconds / runs) if runs > 0 else None,
        }

    total_cycles = len(cycles_seen)
    successful_cycle_count = len(successful_cycles)
    pass_rate = _round_metric_seconds(successful_cycle_count / total_cycles) if total_cycles > 0 else None

    return {
        "schema_version": RUNTIME_METRICS_SCHEMA_VERSION,
        "generated_at": _iso_utc_now(),
        "source": f"{RUNTIME_DIR}/{EVENTS_FILE}",
        "summary": {
            "event_line_count": line_count,
            "recognized_event_count": recognized_event_count,
            "invalid_event_count": invalid_event_count,
            "incomplete_cycle_duration_count": incomplete_cycle_duration_count,
            "incomplete_role_duration_count": incomplete_role_duration_count,
            "total_cycles": total_cycles,
            "successful_cycles": successful_cycle_count,
            "pass_rate": pass_rate,
        },
        "cycles": cycles_payload,
        "role_durations": role_durations_payload,
    }


def refresh_runtime_metrics(workdir: Path) -> None:
    metrics_path = get_runtime_metrics_path(workdir)
    tmp_path = metrics_path.with_name(f"{metrics_path.name}.tmp")
    content = json.dumps(_derive_runtime_metrics(workdir), ensure_ascii=False, indent=2) + "\n"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(metrics_path)
    except OSError as exc:
        print(f"[⚠️] 写入运行统计失败: {exc}")


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
    try:
        refresh_runtime_metrics(workdir)
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"[⚠️] 刷新运行统计失败: {exc}")


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
    headless_exec_args: list[str] | None = None,
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
    if headless:
        exec_args = list(headless_exec_args or [])
        command = ["codex", "exec", "--full-auto", *exec_args, prompt_content]
    else:
        command = ["codex", "--full-auto", prompt_content]
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
    if any((workdir / name).exists() for name in PROMPT_MODE_INFER_FROZEN_FILES):
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
        "task_contract_mode": TASK_CONTRACT_MODE_ENFORCE,
        "human_gate_policy": HUMAN_GATE_POLICY_ALWAYS,
        "codex_exec_sandbox": None,
        "codex_exec_dangerously_bypass": False,
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

    task_contract_mode = raw.get("task_contract_mode")
    if task_contract_mode not in TASK_CONTRACT_MODES:
        task_contract_mode = TASK_CONTRACT_MODE_ENFORCE

    human_gate_policy = raw.get("human_gate_policy", HUMAN_GATE_POLICY_ALWAYS)
    codex_exec_sandbox = raw["codex_exec_sandbox"] if "codex_exec_sandbox" in raw else None
    codex_exec_dangerously_bypass = raw.get("codex_exec_dangerously_bypass", False)

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
        "task_contract_mode": task_contract_mode,
        "human_gate_policy": human_gate_policy,
        "codex_exec_sandbox": codex_exec_sandbox,
        "codex_exec_dangerously_bypass": codex_exec_dangerously_bypass,
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


def _emit_runtime_config_error(reason: str, next_step: str) -> None:
    print(f"[RUNTIME_CONFIG_ERROR] {reason}")
    print(f"[NEXT_STEP] {next_step}")


def _normalize_policy_token(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip().lower()
    return token if token else None


def parse_runtime_policy(config: dict[str, Any]) -> RuntimePolicy:
    errors: list[str] = []

    policy_token = _normalize_policy_token(config.get("human_gate_policy", HUMAN_GATE_POLICY_ALWAYS))
    if policy_token is None or policy_token not in HUMAN_GATE_POLICIES:
        errors.append(
            "`human_gate_policy` 非法，必须是 "
            f"{HUMAN_GATE_POLICIES} 之一，当前值={config.get('human_gate_policy')!r}"
        )
        policy_token = HUMAN_GATE_POLICY_ALWAYS

    raw_sandbox = config.get("codex_exec_sandbox")
    if raw_sandbox is None:
        sandbox_token: str | None = None
    elif isinstance(raw_sandbox, str) and raw_sandbox.strip():
        sandbox_token = raw_sandbox.strip().lower()
        if sandbox_token not in CODEX_EXEC_SANDBOX_VALUES:
            errors.append(
                "`codex_exec_sandbox` 非法，必须是 "
                f"{CODEX_EXEC_SANDBOX_VALUES} 之一，当前值={raw_sandbox!r}"
            )
    else:
        sandbox_token = None
        errors.append(f"`codex_exec_sandbox` 非法，必须是字符串或 null，当前值={raw_sandbox!r}")

    raw_bypass = config.get("codex_exec_dangerously_bypass", False)
    if isinstance(raw_bypass, bool):
        bypass_enabled = raw_bypass
    else:
        bypass_enabled = False
        errors.append(
            "`codex_exec_dangerously_bypass` 非法，必须是布尔值，"
            f"当前值={raw_bypass!r}"
        )

    if bypass_enabled and sandbox_token is not None:
        errors.append(
            "`codex_exec_dangerously_bypass=true` 时禁止显式设置 `codex_exec_sandbox`，"
            "请删除 sandbox 或关闭 bypass"
        )

    if errors:
        raise ValueError("; ".join(errors))

    return RuntimePolicy(
        human_gate_policy=policy_token,
        codex_exec_sandbox=sandbox_token,
        codex_exec_dangerously_bypass=bypass_enabled,
    )


def resolve_runtime_policy_or_exit(config: dict[str, Any]) -> RuntimePolicy:
    try:
        return parse_runtime_policy(config)
    except ValueError as exc:
        _emit_runtime_config_error(
            str(exc),
            "请修复 .centaur/project.json 中的运行策略配置后重试，可先执行 `centaur doctor` 预检。",
        )
        raise SystemExit(1)


def build_codex_exec_permission_args(policy: RuntimePolicy) -> list[str]:
    if policy.codex_exec_dangerously_bypass:
        return ["--dangerously-bypass-approvals-and-sandbox"]

    sandbox = policy.codex_exec_sandbox or DEFAULT_CODEX_EXEC_SANDBOX
    return ["--sandbox", sandbox]


def format_runtime_policy_audit(policy: RuntimePolicy) -> str:
    if policy.codex_exec_dangerously_bypass:
        exec_mode = "dangerously-bypass-approvals-and-sandbox"
    else:
        exec_mode = f"sandbox={policy.codex_exec_sandbox or DEFAULT_CODEX_EXEC_SANDBOX}"
    return f"human_gate_policy={policy.human_gate_policy}, codex_exec={exec_mode}"


def _normalize_task_contract_mode(value: Any) -> str:
    token = str(value).strip().lower()
    if token in TASK_CONTRACT_MODES:
        return token
    return TASK_CONTRACT_MODE_ENFORCE


def _build_state(cycle: int, next_step: str) -> dict[str, Any]:
    return {
        "cycle": cycle,
        "next_step": next_step,
        "inflight_role": None,
        "run_id": None,
        "started_at": None,
        "attempt": 0,
        "last_checkpoint_sha": None,
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

    if "last_checkpoint_sha" not in raw:
        last_checkpoint_sha = None
    else:
        checkpoint_raw = raw.get("last_checkpoint_sha")
        if checkpoint_raw is None:
            last_checkpoint_sha = None
        elif isinstance(checkpoint_raw, str) and checkpoint_raw.strip():
            last_checkpoint_sha = checkpoint_raw.strip()
        else:
            errors.append(
                "`last_checkpoint_sha` 非法，必须为非空字符串或 null，"
                f"当前值={checkpoint_raw!r}"
            )
            last_checkpoint_sha = None

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
        "last_checkpoint_sha": last_checkpoint_sha,
    }


def _run_git(workdir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=workdir, check=False, capture_output=True, text=True)


def _is_git_workspace(workdir: Path) -> bool:
    probe = _run_git(workdir, ["rev-parse", "--is-inside-work-tree"])
    return probe.returncode == 0 and probe.stdout.strip().lower() == "true"


def _git_status_excluding_runtime(workdir: Path) -> subprocess.CompletedProcess[str]:
    return _run_git(
        workdir,
        ["status", "--porcelain", "--untracked-files=all", "--", ".", f":(exclude){RUNTIME_DIR}", f":(exclude){RUNTIME_DIR}/**"],
    )


def enforce_next_cycle_git_worktree_guard(workdir: Path, next_cycle: int) -> None:
    if not _is_git_workspace(workdir):
        print("ℹ️ 当前工作区不是 Git 仓库，已跳过跨轮次工作树闸门检查。")
        return

    status = _git_status_excluding_runtime(workdir)
    if status.returncode != 0:
        print("❌ Git 工作树闸门检查失败，已阻断进入下一轮。")
        detail = (status.stderr or status.stdout).strip()
        if detail:
            print(f"   [DETAIL] {detail}")
        print("   [NEXT_STEP] git status")
        print("   [NEXT_STEP] git add <files>")
        print('   [NEXT_STEP] git commit -m "<message>"')
        raise SystemExit(1)

    dirty_lines = [line for line in status.stdout.splitlines() if line.strip()]
    if not dirty_lines:
        return

    print(f"❌ 检测到跨轮次前 Git 工作树不洁净（已排除 .centaur/），已阻断进入第 {int(next_cycle)} 轮。")
    for line in dirty_lines[:20]:
        print(f"   [DIRTY] {line}")
    if len(dirty_lines) > 20:
        print(f"   [DIRTY] ... 其余 {len(dirty_lines) - 20} 条已省略")
    print("   [NEXT_STEP] git status")
    print("   [NEXT_STEP] git add <files>")
    print('   [NEXT_STEP] git commit -m "<message>"')
    raise SystemExit(1)


def _emit_checkpoint_failure(reason: str, details: str = "") -> None:
    print(f"⚠️ Git checkpoint 创建失败（不中断流程）: {reason}")
    detail = details.strip()
    if detail:
        print(f"   [DETAIL] {detail}")
    print("   [NEXT_STEP] git status")
    print('   [NEXT_STEP] git config user.name "<your-name>"')
    print('   [NEXT_STEP] git config user.email "<your-email>"')


def try_create_validator_checkpoint(workdir: Path, cycle: int, run_id: str) -> str | None:
    run_token = run_id.strip() if isinstance(run_id, str) else ""
    if not run_token:
        _emit_checkpoint_failure("run_id 为空，无法生成可审计元数据")
        return None

    if not _is_git_workspace(workdir):
        print("ℹ️ 当前工作区不是 Git 仓库，已跳过本轮 checkpoint（不中断流程）。")
        print("   [NEXT_STEP] 若需启用 checkpoint，请先执行 git init 并创建首个提交")
        return None

    user_name = _run_git(workdir, ["config", "--get", "user.name"])
    user_email = _run_git(workdir, ["config", "--get", "user.email"])
    if (
        user_name.returncode != 0
        or not user_name.stdout.strip()
        or user_email.returncode != 0
        or not user_email.stdout.strip()
    ):
        _emit_checkpoint_failure("缺少 Git 提交身份配置")
        return None

    stage = _run_git(
        workdir,
        ["add", "-A", "--", ".", f":(exclude){RUNTIME_DIR}", f":(exclude){RUNTIME_DIR}/**"],
    )
    if stage.returncode != 0:
        _emit_checkpoint_failure("暂存改动失败", details=stage.stderr or stage.stdout)
        return None

    has_staged = _run_git(workdir, ["diff", "--cached", "--quiet", "--exit-code"])
    if has_staged.returncode == 0:
        print("ℹ️ 本轮无可提交改动（已默认排除 .centaur/），跳过 checkpoint。")
        return None
    if has_staged.returncode != 1:
        _emit_checkpoint_failure("检查暂存区失败", details=has_staged.stderr or has_staged.stdout)
        return None

    metadata = {
        "cycle": int(cycle),
        "role": CHECKPOINT_ROLE,
        "run_id": run_token,
        "timestamp": _iso_utc_now(),
    }
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    subject = f"centaur checkpoint cycle={cycle} role={CHECKPOINT_ROLE} run_id={run_token}"
    commit = _run_git(workdir, ["commit", "--no-gpg-sign", "-m", subject, "-m", metadata_json])
    if commit.returncode != 0:
        _emit_checkpoint_failure("提交 checkpoint 失败", details=commit.stderr or commit.stdout)
        return None

    rev = _run_git(workdir, ["rev-parse", "HEAD"])
    if rev.returncode != 0:
        _emit_checkpoint_failure("读取 checkpoint SHA 失败", details=rev.stderr or rev.stdout)
        return None
    sha = rev.stdout.strip()
    if not sha:
        _emit_checkpoint_failure("读取到空 SHA")
        return None

    print(f"✅ 已创建 Git checkpoint: {sha}")
    print(f"   [CHECKPOINT_METADATA] {metadata_json}")
    return sha


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
        if role == "worker":
            return _build_state(cycle=cycle, next_step="supervisor")
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


def _normalize_task_contract_string_list(value: Any, field_name: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"`{field_name}` 必须是字符串数组")
        return []

    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(f"`{field_name}[{index}]` 必须是非空字符串")
            continue
        normalized.append(item.strip())
    return normalized


def _latest_task_contract_payload(workdir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    task_path = workdir / "TASK.md"
    if not task_path.exists():
        return None, []

    try:
        lines = task_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return None, [f"读取 TASK.md 失败: {exc}"]

    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line.startswith(TASK_CONTRACT_PREFIX):
            continue
        payload_text = line[len(TASK_CONTRACT_PREFIX) :].strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            return None, [f"`{TASK_CONTRACT_PREFIX.strip()}` JSON 非法: {exc}"]
        if not isinstance(payload, dict):
            return None, [f"`{TASK_CONTRACT_PREFIX.strip()}` 载荷必须是 JSON 对象"]
        return payload, []
    return None, []


def lint_task_contract(workdir: Path) -> tuple[list[str], list[str], dict[str, Any] | None]:
    errors: list[str] = []
    warnings: list[str] = []

    payload, parse_errors = _latest_task_contract_payload(workdir)
    if parse_errors:
        return parse_errors, warnings, None
    if payload is None:
        warnings.append(f"TASK.md 未声明 `{TASK_CONTRACT_PREFIX.strip()}` 结构化契约，将沿用自然语言验收。")
        return errors, warnings, None

    version = _coerce_int(payload.get("version"))
    if version is None or version <= 0:
        errors.append("`version` 必须是正整数")
        version = 1

    unit = str(payload.get("unit", "")).strip().lower()
    if unit not in TASK_CONTRACT_UNITS:
        errors.append(f"`unit` 非法，必须是 {TASK_CONTRACT_UNITS}")
        unit = ""

    allowed_delta = _normalize_task_contract_string_list(payload.get("allowed_delta"), "allowed_delta", errors)
    forbidden_delta = _normalize_task_contract_string_list(payload.get("forbidden_delta"), "forbidden_delta", errors)

    if unit == "text_exact" and allowed_delta:
        errors.append("`unit=text_exact` 与 `allowed_delta` 冲突：逐字一致场景不允许声明新增差异")

    overlap = sorted(set(allowed_delta) & set(forbidden_delta))
    if overlap:
        errors.append("`allowed_delta` 与 `forbidden_delta` 存在重叠: " + ", ".join(overlap))

    if unit == "set_plus" and not allowed_delta:
        errors.append("`unit=set_plus` 必须声明非空 `allowed_delta`")

    precedence = payload.get("precedence")
    if precedence is None:
        normalized_precedence = ["forbidden", "allowed", "wording"]
    elif (
        isinstance(precedence, list)
        and all(isinstance(item, str) and item.strip() for item in precedence)
        and set(item.strip().lower() for item in precedence) == {"forbidden", "allowed", "wording"}
    ):
        normalized_precedence = [item.strip().lower() for item in precedence]
    else:
        errors.append("`precedence` 非法，必须包含且仅包含 `forbidden/allowed/wording`")
        normalized_precedence = ["forbidden", "allowed", "wording"]

    normalized_contract = {
        "version": version,
        "unit": unit,
        "baseline": str(payload.get("baseline", "")).strip(),
        "allowed_delta": sorted(dict.fromkeys(allowed_delta)),
        "forbidden_delta": sorted(dict.fromkeys(forbidden_delta)),
        "precedence": normalized_precedence,
    }
    return errors, warnings, normalized_contract


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


def _latest_role_event_signal(workdir: Path, cycle: int, role: str) -> tuple[str | None, int | None]:
    event_path = get_events_path(workdir)
    if not event_path.exists():
        return None, None

    target_cycle = int(cycle)
    target_role = _normalize_role_token(role)
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None, None

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
            return event_type, None
        return event_type, _coerce_int(payload.get("return_code"))
    return None, None


def _classify_worker_outcome(workdir: Path, cycle: int, run_id: str) -> tuple[str, list[str]]:
    event_type, return_code = _latest_role_event_signal(workdir, cycle=cycle, role="worker")
    if event_type != "role_end":
        return WORKER_RESULT_INCOMPLETE, ["缺少 cycle 对齐的 worker role_end(return_code) 结构化证据"]

    if return_code is None:
        return WORKER_RESULT_INCOMPLETE, ["worker role_end 缺少有效 return_code"]

    if return_code != 0:
        result = WORKER_RESULT_BLOCKED if return_code == 130 else WORKER_RESULT_FAILED
        return result, [f"worker role_end(return_code={return_code})"]

    run_token = run_id.strip() if isinstance(run_id, str) else ""
    if not run_token:
        return WORKER_RESULT_INCOMPLETE, ["worker run_id 为空，无法对齐 TASK.md 机审完成证据"]
    if not _task_has_completion_evidence(workdir, cycle=cycle, role="worker", run_id=run_token):
        return (
            WORKER_RESULT_INCOMPLETE,
            [f"TASK.md 缺少 cycle={cycle}, role=worker, run_id={run_token}, status=completed 机审证据"],
        )
    return WORKER_RESULT_SUCCESS, []


def _verify_supervisor_real_completion(workdir: Path, cycle: int, started_at: Any) -> list[str]:
    failures: list[str] = []
    task_path = workdir / "TASK.md"
    if not task_path.exists():
        failures.append("真实完成闸门失败：缺少 TASK.md，无法确认 Supervisor 已完成派单。")
        return failures

    try:
        task_text = task_path.read_text(encoding="utf-8")
    except OSError as exc:
        failures.append(f"真实完成闸门失败：读取 TASK.md 失败: {exc}")
        return failures

    missing_sections = [marker for marker in SUPERVISOR_TASK_REQUIRED_SECTIONS if marker not in task_text]
    if missing_sections:
        failures.append(
            "真实完成闸门失败：TASK.md 缺少 Supervisor 派单结构字段: " + ", ".join(missing_sections)
        )

    started_at_dt = _parse_event_timestamp(started_at)
    if started_at_dt is None:
        failures.append(
            "真实完成闸门失败：缺少有效 `started_at`，无法确认 TASK.md 是否在本次 Supervisor 执行窗口内更新。"
        )
        return failures

    try:
        task_mtime = datetime.fromtimestamp(task_path.stat().st_mtime, tz=timezone.utc)
    except OSError as exc:
        failures.append(f"真实完成闸门失败：读取 TASK.md 修改时间失败: {exc}")
        return failures

    # 允许 1 秒容差，兼容低精度文件系统时间戳。
    if task_mtime.timestamp() < started_at_dt.timestamp() - 1.0:
        failures.append(
            "真实完成闸门失败：TASK.md 未在本次 Supervisor 执行窗口内更新 "
            f"(task_mtime={task_mtime.isoformat()}, started_at={started_at_dt.isoformat()})"
        )
    return failures


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


def _route_blocked_spec_and_stop(
    workdir: Path,
    state: dict[str, Any],
    cycle: int,
    active_task: str,
    reasons: list[str],
) -> None:
    _clear_role_transaction(state)
    state["cycle"] = cycle
    state["next_step"] = "supervisor"
    save_state(workdir, state)
    sync_task_bus_to_active(workdir, active_task)
    print("❌ [BLOCKED_SPEC] TASK 验收契约存在冲突，已阻断 Worker 执行并回流 Supervisor。")
    for reason in reasons:
        print(f"   - {reason}")
    print("   [NEXT_STEP] centaur task lint")
    print("   [NEXT_STEP] 由 Supervisor 先修复 TASK.md 契约歧义，再放行 Worker")
    raise SystemExit(1)


def _route_worker_non_success_and_stop(
    workdir: Path,
    state: dict[str, Any],
    cycle: int,
    run_id: str,
    active_task: str,
    outcome: str,
    reasons: list[str],
) -> None:
    _clear_role_transaction(state)
    state["cycle"] = cycle
    if outcome in {WORKER_RESULT_FAILED, WORKER_RESULT_BLOCKED}:
        state["next_step"] = "supervisor"
        message = "⚠️ Worker 明确失败/阻塞，已跳过 Validator 并回流 Supervisor。"
    else:
        state["next_step"] = "worker"
        message = "❌ Worker 完成证据未闭环，已阻断 Validator 并保持 Worker 可重放。"
    save_state(workdir, state)
    sync_task_bus_to_active(workdir, active_task)
    print(message)
    print(f"   - cycle={cycle}, run_id={run_id}, outcome={outcome}")
    for reason in reasons:
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
    if config.get("task_contract_mode") not in TASK_CONTRACT_MODES:
        config["task_contract_mode"] = TASK_CONTRACT_MODE_ENFORCE
    if "human_gate_policy" not in config:
        config["human_gate_policy"] = HUMAN_GATE_POLICY_ALWAYS
    if "codex_exec_sandbox" not in config:
        config["codex_exec_sandbox"] = None
    if "codex_exec_dangerously_bypass" not in config:
        config["codex_exec_dangerously_bypass"] = False
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

    if inflight_role == "supervisor":
        completion_failures = _verify_supervisor_real_completion(
            workdir,
            cycle=cycle,
            started_at=state.get("started_at"),
        )
        if completion_failures:
            state["next_step"] = inflight_role
            return state

        gate_failures = _verify_role_dual_gate(workdir, cycle=cycle, role=inflight_role, run_id=run_id)
        if gate_failures:
            state["next_step"] = inflight_role
            return state

        _apply_success_transition_from_recovered_role(workdir, state, inflight_role, cycle)
        return state

    if inflight_role == "worker":
        outcome, _reasons = _classify_worker_outcome(workdir, cycle=cycle, run_id=run_id)
        if outcome == WORKER_RESULT_SUCCESS:
            _apply_success_transition_from_recovered_role(workdir, state, inflight_role, cycle)
            return state
        if outcome in {WORKER_RESULT_FAILED, WORKER_RESULT_BLOCKED}:
            _clear_role_transaction(state)
            state["next_step"] = "supervisor"
            return state
        state["next_step"] = inflight_role
        return state

    if not _has_successful_role_end_event(workdir, cycle=cycle, role=inflight_role):
        state["next_step"] = inflight_role
        return state

    gate_failures = _verify_role_dual_gate(workdir, cycle=cycle, role=inflight_role, run_id=run_id)
    if gate_failures:
        state["next_step"] = inflight_role
        return state

    _apply_success_transition_from_recovered_role(workdir, state, inflight_role, cycle)
    if inflight_role == CHECKPOINT_ROLE:
        checkpoint_sha = try_create_validator_checkpoint(workdir, cycle=cycle, run_id=run_id)
        if checkpoint_sha:
            state["last_checkpoint_sha"] = checkpoint_sha
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


def _load_runtime_settings(
    workdir: Path,
) -> tuple[dict[str, Any], str, str, str, RuntimePolicy]:
    project_config = load_or_init_project_config(workdir)
    active_task, _ = ensure_active_task_file(workdir, project_config)
    prompt_mode = str(project_config.get("prompt_mode", PROMPT_MODE_GLOBAL))
    task_contract_mode = _normalize_task_contract_mode(project_config.get("task_contract_mode"))
    validate_prompt_mode_env(workdir, prompt_mode)
    runtime_policy = resolve_runtime_policy_or_exit(project_config)
    return project_config, active_task, prompt_mode, task_contract_mode, runtime_policy


def _git_dirtiness_signal(workdir: Path) -> tuple[bool, str]:
    if not _is_git_workspace(workdir):
        return False, "git=non-repo"
    status = _git_status_excluding_runtime(workdir)
    if status.returncode != 0:
        detail = (status.stderr or status.stdout).strip() or "unknown"
        return True, f"git=status-error:{detail}"
    dirty_lines = [line for line in status.stdout.splitlines() if line.strip()]
    if dirty_lines:
        return True, f"git=dirty(count={len(dirty_lines)})"
    return False, "git=clean"


def _audit_human_gate_decision(cycle: int, policy: str, decision: str, evidence: list[str]) -> None:
    print(f"[AUDIT] human_gate cycle={cycle} policy={policy} decision={decision}")
    if not evidence:
        print("   [EVIDENCE] none")
        return
    for item in evidence:
        print(f"   [EVIDENCE] {item}")


def _evaluate_risk_policy(workdir: Path, task_contract_mode: str) -> tuple[bool, list[str]]:
    triggers: list[str] = []
    auto_pass: list[str] = []

    dirty, git_signal = _git_dirtiness_signal(workdir)
    if dirty:
        triggers.append(f"trigger:{git_signal}")
    else:
        auto_pass.append(f"auto-pass:{git_signal}")

    if task_contract_mode != TASK_CONTRACT_MODE_OFF:
        contract_errors, contract_warnings, _contract = lint_task_contract(workdir)
        if contract_errors:
            triggers.append(f"trigger:task_contract_conflict(count={len(contract_errors)})")
        else:
            auto_pass.append("auto-pass:task_contract_clean")
        for warning in contract_warnings:
            auto_pass.append(f"auto-pass:task_contract_warning={warning}")
    else:
        auto_pass.append("auto-pass:task_contract_mode=off")

    if triggers:
        return True, triggers
    return False, auto_pass or ["auto-pass:no_risk_signal"]


def _evaluate_off_policy(
    workdir: Path,
    state: dict[str, Any],
    task_contract_mode: str,
) -> tuple[bool, list[str], list[str]]:
    passed: list[str] = []
    blockers: list[str] = []

    if state.get("inflight_role") is None:
        passed.append("check:transaction_clean")
    else:
        blockers.append("check_failed:transaction_inflight")

    if codex_available():
        passed.append("check:codex_available")
    else:
        blockers.append("check_failed:codex_unavailable")

    dirty, git_signal = _git_dirtiness_signal(workdir)
    if dirty:
        blockers.append(f"check_failed:{git_signal}")
    else:
        passed.append(f"check:{git_signal}")

    if task_contract_mode != TASK_CONTRACT_MODE_OFF:
        contract_errors, _contract_warnings, _contract = lint_task_contract(workdir)
        if contract_errors:
            blockers.append(f"check_failed:task_contract_conflict(count={len(contract_errors)})")
        else:
            passed.append("check:task_contract_clean")
    else:
        passed.append("check:task_contract_mode=off")

    return len(blockers) == 0, passed, blockers


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
    _project_config, active_task, prompt_mode, task_contract_mode, runtime_policy = _load_runtime_settings(base)
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
    print(f"📐 TASK 契约模式: {task_contract_mode}")
    print(f"🔐 运行策略: {format_runtime_policy_audit(runtime_policy)}")
    print(f"🧷 当前任务: {active_task}")
    print(f"♻️ 自动恢复状态：第 {state['cycle']} 轮，下一角色 {ROLE_LABELS[state['next_step']]}")

    active_cycle: int | None = None
    while True:
        _project_config, active_task, prompt_mode, task_contract_mode, runtime_policy = _load_runtime_settings(base)
        headless_exec_args = build_codex_exec_permission_args(runtime_policy) if headless else None
        cycle = int(state["cycle"])
        next_step = str(state["next_step"])
        if active_cycle != cycle:
            append_event(base, cycle=cycle, event_type="cycle_start")
            active_cycle = cycle

        print(f"\n{'█' * 60}")
        print(f"🔄 第 {cycle} 轮开发周期 | 当前阶段: {ROLE_LABELS[next_step]}")
        print(f"🔐 运行策略: {format_runtime_policy_audit(runtime_policy)}")
        print("█" * 60)

        if next_step == "supervisor":
            if cycle > 1:
                enforce_next_cycle_git_worktree_guard(base, next_cycle=cycle)
            _start_role_transaction(state, role="supervisor", cycle=cycle)
            save_state(base, state)
            run_id = str(state.get("run_id") or "")
            started_at = state.get("started_at")
            run_agent(
                "Supervisor",
                "SUPERVISOR.md",
                base,
                prompt_mode,
                cycle=cycle,
                headless=headless,
                headless_exec_args=headless_exec_args,
            )
            completion_failures = _verify_supervisor_real_completion(
                base,
                cycle=cycle,
                started_at=started_at,
            )
            if completion_failures:
                _fail_dual_gate_and_stop(
                    workdir=base,
                    state=state,
                    cycle=cycle,
                    role="supervisor",
                    run_id=run_id,
                    active_task=active_task,
                    failures=completion_failures,
                )
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
            if runtime_policy.human_gate_policy == HUMAN_GATE_POLICY_ALWAYS:
                _audit_human_gate_decision(
                    cycle=cycle,
                    policy=runtime_policy.human_gate_policy,
                    decision="enter_gate",
                    evidence=["trigger:policy=always"],
                )
                human_gate()
            elif runtime_policy.human_gate_policy == HUMAN_GATE_POLICY_RISK:
                should_gate, evidence = _evaluate_risk_policy(base, task_contract_mode)
                _audit_human_gate_decision(
                    cycle=cycle,
                    policy=runtime_policy.human_gate_policy,
                    decision="enter_gate" if should_gate else "auto_pass",
                    evidence=evidence,
                )
                if should_gate:
                    human_gate()
                else:
                    print("🟢 risk 模式信号全部通过，自动放行 Worker。")
            else:
                safe_to_skip, passed_checks, blockers = _evaluate_off_policy(base, state, task_contract_mode)
                if safe_to_skip:
                    _audit_human_gate_decision(
                        cycle=cycle,
                        policy=runtime_policy.human_gate_policy,
                        decision="skip_gate",
                        evidence=passed_checks,
                    )
                    print("🟢 off 模式前置安全条件满足，已跳过 Human Gate。")
                else:
                    _audit_human_gate_decision(
                        cycle=cycle,
                        policy=runtime_policy.human_gate_policy,
                        decision="enter_gate_fail_closed",
                        evidence=blockers + passed_checks,
                    )
                    print("⚠️ off 模式前置安全条件不满足，已回退进入 Human Gate（Fail-Closed）。")
                    human_gate()
            state["next_step"] = "worker"
            save_state(base, state)
            sync_task_bus_to_active(base, active_task)
            continue

        if next_step == "worker":
            if task_contract_mode != TASK_CONTRACT_MODE_OFF:
                contract_errors, contract_warnings, _contract = lint_task_contract(base)
                for warning in contract_warnings:
                    print(f"⚠️ [TASK_CONTRACT] {warning}")
                if contract_errors:
                    if task_contract_mode == TASK_CONTRACT_MODE_WARN:
                        print("⚠️ [TASK_CONTRACT] 检测到契约冲突，当前模式=warn，继续执行。")
                        for reason in contract_errors:
                            print(f"   - {reason}")
                    else:
                        _route_blocked_spec_and_stop(
                            workdir=base,
                            state=state,
                            cycle=cycle,
                            active_task=active_task,
                            reasons=contract_errors,
                        )
            _start_role_transaction(state, role="worker", cycle=cycle)
            save_state(base, state)
            run_id = str(state.get("run_id") or "")
            try:
                run_agent(
                    "Worker",
                    "WORKER.md",
                    base,
                    prompt_mode,
                    cycle=cycle,
                    headless=headless,
                    headless_exec_args=headless_exec_args,
                )
            except SystemExit:
                outcome, reasons = _classify_worker_outcome(base, cycle=cycle, run_id=run_id)
                if outcome in {WORKER_RESULT_FAILED, WORKER_RESULT_BLOCKED}:
                    _route_worker_non_success_and_stop(
                        workdir=base,
                        state=state,
                        cycle=cycle,
                        run_id=run_id,
                        active_task=active_task,
                        outcome=outcome,
                        reasons=reasons,
                    )
                raise
            append_task_completion_evidence(base, cycle=cycle, role="worker", run_id=run_id)
            outcome, reasons = _classify_worker_outcome(base, cycle=cycle, run_id=run_id)
            if outcome != WORKER_RESULT_SUCCESS:
                _route_worker_non_success_and_stop(
                    workdir=base,
                    state=state,
                    cycle=cycle,
                    run_id=run_id,
                    active_task=active_task,
                    outcome=outcome,
                    reasons=reasons,
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
        run_agent(
            "Validator",
            "VALIDATOR.md",
            base,
            prompt_mode,
            cycle=cycle,
            headless=headless,
            headless_exec_args=headless_exec_args,
        )
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
        enforce_next_cycle_git_worktree_guard(base, next_cycle=cycle + 1)
        checkpoint_sha = try_create_validator_checkpoint(base, cycle=cycle, run_id=run_id)
        if checkpoint_sha:
            state["last_checkpoint_sha"] = checkpoint_sha
            save_state(base, state)
        sync_task_bus_to_active(base, active_task)
