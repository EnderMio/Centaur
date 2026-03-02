from __future__ import annotations

import subprocess
from pathlib import Path

CORE_FILES = ("SUPERVISOR.md", "WORKER.md", "VALIDATOR.md", "AGENTS.md")
MEMORY_FILES = ("DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md")


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


def run_workflow(workdir: Path | None = None) -> None:
    base = (workdir or Path.cwd()).resolve()

    print("🤖 Codex Agent 2.0 (红蓝对抗版) 已启动！")
    check_env(base)
    init_memory_files(base)

    cycle = 1
    while True:
        print(f"\n{'█' * 60}")
        print(f"🔄 第 {cycle} 轮开发周期开始")
        print("█" * 60)

        run_agent("Supervisor", base / "SUPERVISOR.md", base)
        human_gate()
        run_agent("Worker", base / "WORKER.md", base)

        print("\n🔍 Validator 正在审查 Worker 的代码与数据契约...")
        run_agent("Validator", base / "VALIDATOR.md", base)
        cycle += 1
