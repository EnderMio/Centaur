from __future__ import annotations

import json
from importlib.resources import files
import subprocess
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
PROJECT_SCHEMA_VERSION = 1
PROMPT_SET_VERSION = "2026-03-02"
RUNTIME_DIR = ".centaur"
STATE_FILE = "state.json"
PROJECT_FILE = "project.json"
LEGACY_STATE_FILE = ".centaur_state.json"
LEGACY_PROJECT_FILE = ".centaur_project.json"
ROLE_LABELS = {
    "supervisor": "Supervisor",
    "human_gate": "Human Gate",
    "worker": "Worker",
    "validator": "Validator",
}


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


def _project_path(workdir: Path) -> Path:
    return _runtime_dir(workdir) / PROJECT_FILE


def _legacy_project_path(workdir: Path) -> Path:
    return workdir / LEGACY_PROJECT_FILE


def infer_prompt_mode_from_workspace(workdir: Path) -> str:
    if any((workdir / name).exists() for name in ROLE_TEMPLATE_FILES):
        return PROMPT_MODE_FROZEN
    return PROMPT_MODE_GLOBAL


def default_project_config(prompt_mode: str | None = None) -> dict[str, Any]:
    mode = prompt_mode if prompt_mode in PROMPT_MODES else PROMPT_MODE_GLOBAL
    return {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "centaur_version": __version__,
        "prompt_set_version": PROMPT_SET_VERSION,
        "prompt_mode": mode,
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

    return {
        "schema_version": schema_version,
        "centaur_version": centaur_version,
        "prompt_set_version": prompt_set_version,
        "prompt_mode": prompt_mode,
    }


def save_project_config(workdir: Path, config: dict[str, Any]) -> None:
    ensure_runtime_dir(workdir)
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
    config = load_project_config(workdir)
    if config is not None:
        return config
    inferred_mode = infer_prompt_mode_from_workspace(workdir)
    config = default_project_config(prompt_mode=inferred_mode)
    save_project_config(workdir, config)
    print(f"ℹ️ 已创建 {_project_path(workdir)} (prompt_mode={inferred_mode})")
    return config


def validate_prompt_mode_env(workdir: Path, prompt_mode: str) -> None:
    if prompt_mode == PROMPT_MODE_FROZEN:
        missing_local = [name for name in CORE_FILES if not (workdir / name).exists()]
        if missing_local:
            print(f"❌ 启动失败：frozen 模式缺少项目角色提示词文件 {missing_local}")
            print("👉 请执行 `centaur migrate --prompts frozen --force` 修复。")
            raise SystemExit(1)
        return

    missing_packaged = [name for name in CORE_FILES if not template_exists(name)]
    if missing_packaged:
        print(f"❌ 启动失败：global 模式缺少安装包模板 {missing_packaged}")
        print("👉 请重新安装 Centaur CLI，或切换到 frozen: `centaur migrate --prompts frozen`")
        raise SystemExit(1)

    ignored_local = [name for name in CORE_FILES if (workdir / name).exists()]
    if ignored_local:
        print("ℹ️ 当前为 global 模式，已忽略项目内角色提示词文件: " + ", ".join(ignored_local))
        print("ℹ️ 如需使用项目提示词，请执行 `centaur migrate --prompts frozen`。")


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
    ensure_runtime_dir(workdir)
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


def run_workflow(workdir: Path | None = None, start_step: str | None = None) -> None:
    base = (workdir or Path.cwd()).resolve()

    print("🤖 Codex Agent 2.0 (红蓝对抗版) 已启动！")
    check_env(base)
    init_memory_files(base)
    project_config = load_or_init_project_config(base)
    prompt_mode = str(project_config.get("prompt_mode", PROMPT_MODE_GLOBAL))
    validate_prompt_mode_env(base, prompt_mode)
    state = load_state(base)
    state = _resolve_start_step(state, start_step)
    state = _ensure_supervisor_bootstrap(base, state)
    save_state(base, state)
    print(f"🧭 Prompt 模式: {prompt_mode}")
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
            continue

        if next_step == "human_gate":
            human_gate()
            state["next_step"] = "worker"
            save_state(base, state)
            continue

        if next_step == "worker":
            run_agent("Worker", "WORKER.md", base, prompt_mode)
            state["next_step"] = "validator"
            save_state(base, state)
            continue

        print("\n🔍 Validator 正在审查 Worker 的代码与数据契约...")
        run_agent("Validator", "VALIDATOR.md", base, prompt_mode)
        state["cycle"] = cycle + 1
        state["next_step"] = "supervisor"
        save_state(base, state)
