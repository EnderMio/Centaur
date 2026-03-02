from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

CORE_FILES = ("SUPERVISOR.md", "WORKER.md", "VALIDATOR.md", "AGENTS.md")
MEMORY_FILES = ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md")
ROLE_ORDER = ("supervisor", "human_gate", "worker", "validator")
STATE_FILE = ".centaur_state.json"
ROLE_LABELS = {
    "supervisor": "Supervisor",
    "human_gate": "Human Gate",
    "worker": "Worker",
    "validator": "Validator",
}


def check_env(workdir: Path) -> None:
    missing = [name for name in CORE_FILES if not (workdir / name).exists()]
    if missing:
        print(f"❌ 启动失败：缺少核心配置文件 {missing}")
        print("👉 请先运行 `centaur init` 初始化模板。")
        raise SystemExit(1)


def run_agent(role: str, prompt_file: Path, workdir: Path) -> None:
    print(f"\n[🚀] 正在唤醒 {role}...")
    try:
        prompt_content = prompt_file.read_text(encoding="utf-8")
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


def _default_state() -> dict[str, Any]:
    return {"cycle": 1, "next_step": "supervisor"}


def _state_path(workdir: Path) -> Path:
    return workdir / STATE_FILE


def _normalize_state(raw: dict[str, Any]) -> dict[str, Any] | None:
    cycle = raw.get("cycle")
    next_step = raw.get("next_step")
    if isinstance(cycle, int) and cycle > 0 and next_step in ROLE_ORDER:
        return {"cycle": cycle, "next_step": next_step}
    return None


def save_state(workdir: Path, state: dict[str, Any]) -> None:
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
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            state = _normalize_state(raw)
            if state is not None:
                return state
            print(f"⚠️ 状态文件格式异常，自动改为 TASK 推断：{path.name}")
        except (OSError, json.JSONDecodeError):
            print(f"⚠️ 状态文件读取失败，自动改为 TASK 推断：{path.name}")

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


def run_workflow(workdir: Path | None = None, start_step: str | None = None) -> None:
    base = (workdir or Path.cwd()).resolve()

    print("🤖 Codex Agent 2.0 (红蓝对抗版) 已启动！")
    check_env(base)
    init_memory_files(base)
    state = load_state(base)
    state = _resolve_start_step(state, start_step)
    save_state(base, state)
    print(f"♻️ 自动恢复状态：第 {state['cycle']} 轮，下一角色 {ROLE_LABELS[state['next_step']]}")

    while True:
        cycle = int(state["cycle"])
        next_step = str(state["next_step"])

        print(f"\n{'█' * 60}")
        print(f"🔄 第 {cycle} 轮开发周期 | 当前阶段: {ROLE_LABELS[next_step]}")
        print("█" * 60)

        if next_step == "supervisor":
            run_agent("Supervisor", base / "SUPERVISOR.md", base)
            state["next_step"] = "human_gate"
            save_state(base, state)
            continue

        if next_step == "human_gate":
            human_gate()
            state["next_step"] = "worker"
            save_state(base, state)
            continue

        if next_step == "worker":
            run_agent("Worker", base / "WORKER.md", base)
            state["next_step"] = "validator"
            save_state(base, state)
            continue

        print("\n🔍 Validator 正在审查 Worker 的代码与数据契约...")
        run_agent("Validator", base / "VALIDATOR.md", base)
        state["cycle"] = cycle + 1
        state["next_step"] = "supervisor"
        save_state(base, state)
