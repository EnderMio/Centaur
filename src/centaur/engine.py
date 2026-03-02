from __future__ import annotations

import json
from importlib.resources import files
import subprocess
import shutil
import re
from pathlib import Path
from typing import Any

from centaur import __version__

ROLE_TEMPLATE_FILES = ("AGENTS.md", "SUPERVISOR.md", "WORKER.md", "VALIDATOR.md")
PROJECT_TEMPLATE_FILES = ("PROPOSAL.md",)
CORE_FILES = ROLE_TEMPLATE_FILES
REQUIRED_WORKSPACE_FILES = ("PROPOSAL.md",)
MEMORY_FILES = ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md")
ROLE_ORDER = ("supervisor", "human_gate", "worker", "validator")
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
DEFAULT_TASK_NAME = "default"
TASK_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ROLE_LABELS = {
    "supervisor": "Supervisor",
    "human_gate": "Human Gate",
    "worker": "Worker",
    "validator": "Validator",
}


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


def run_agent(role: str, prompt_filename: str, workdir: Path, prompt_mode: str) -> None:
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

    print(f"\n[🚀] 正在唤醒 {role}... (提示词来源: {source})")
    try:
        subprocess.run(["codex", "--full-auto", prompt_content], check=True, cwd=workdir)
        print(f"[✅] {role} 运行结束。")
    except FileNotFoundError:
        print("❌ 未找到 `codex` 命令，请先安装并配置 Codex CLI。")
        raise SystemExit(1)
    except subprocess.CalledProcessError as exc:
        print(f"[❌] {role} 异常退出 (RC={exc.returncode})，请检查日志。")
        raise SystemExit(1)
    except KeyboardInterrupt:
        print(f"\n[⚠️] 手动中止 {role}。")
        raise SystemExit(1)


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


def _default_state() -> dict[str, Any]:
    return {"cycle": 1, "next_step": "supervisor"}


def _state_path(workdir: Path) -> Path:
    return _runtime_dir(workdir) / STATE_FILE


def _legacy_state_path(workdir: Path) -> Path:
    return workdir / LEGACY_STATE_FILE


def _normalize_state(raw: dict[str, Any]) -> dict[str, Any] | None:
    cycle = raw.get("cycle")
    next_step = raw.get("next_step")
    if isinstance(cycle, int) and cycle > 0 and next_step in ROLE_ORDER:
        return {"cycle": cycle, "next_step": next_step}
    return None


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
            return {"cycle": cycle, "next_step": "human_gate"}
        return _default_state()
    if worker_pos > validator_pos:
        return {"cycle": cycle, "next_step": "validator"}
    if validator_pos > worker_pos:
        return {"cycle": cycle, "next_step": "supervisor"}
    return _default_state()


def load_state(workdir: Path) -> dict[str, Any]:
    path = _state_path(workdir)
    legacy_path = _legacy_state_path(workdir)

    for candidate in (path, legacy_path):
        if not candidate.exists():
            continue
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
            state = _normalize_state(raw)
        except (OSError, json.JSONDecodeError):
            print(f"⚠️ 状态文件读取失败，自动改为 TASK 推断：{candidate.name}")
            continue
        if state is None:
            print(f"⚠️ 状态文件格式异常，自动改为 TASK 推断：{candidate.name}")
            continue
        if candidate == legacy_path:
            save_state(workdir, state)
            print(f"ℹ️ 已迁移旧状态文件到 {_state_path(workdir)}")
        return state

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


def _ensure_supervisor_bootstrap(workdir: Path, state: dict[str, Any]) -> dict[str, Any]:
    if (workdir / "TASK.md").exists():
        return state
    if state.get("next_step") != "supervisor" or state.get("cycle") != 1:
        print("ℹ️ 检测到 TASK.md 缺失，已强制从 Supervisor 开始首轮建模。")
    return {"cycle": 1, "next_step": "supervisor"}


def run_workflow(
    workdir: Path | None = None,
    start_step: str | None = None,
    allow_repo_root: bool = False,
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
    state = load_state(base)
    state = _resolve_start_step(state, start_step)
    state = _ensure_supervisor_bootstrap(base, state)
    save_state(base, state)
    sync_task_bus_to_active(base, active_task)
    print(f"🧭 Prompt 模式: {prompt_mode}")
    print(f"🧷 当前任务: {active_task}")
    print(f"♻️ 自动恢复状态：第 {state['cycle']} 轮，下一角色 {ROLE_LABELS[state['next_step']]}")

    while True:
        cycle = int(state["cycle"])
        next_step = str(state["next_step"])

        print(f"\n{'█' * 60}")
        print(f"🔄 第 {cycle} 轮开发周期 | 当前阶段: {ROLE_LABELS[next_step]}")
        print("█" * 60)

        if next_step == "supervisor":
            run_agent("Supervisor", "SUPERVISOR.md", base, prompt_mode)
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
            run_agent("Worker", "WORKER.md", base, prompt_mode)
            state["next_step"] = "validator"
            save_state(base, state)
            sync_task_bus_to_active(base, active_task)
            continue

        print("\n🔍 Validator 正在审查 Worker 的代码与数据契约...")
        run_agent("Validator", "VALIDATOR.md", base, prompt_mode)
        state["cycle"] = cycle + 1
        state["next_step"] = "supervisor"
        save_state(base, state)
        sync_task_bus_to_active(base, active_task)
