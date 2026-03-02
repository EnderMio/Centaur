import subprocess
import os
import sys

def check_env():
    core_files = ["SUPERVISOR.md", "WORKER.md", "VALIDATOR.md", "AGENTS.md"]
    missing = [f for f in core_files if not os.path.exists(f)]
    if missing:
        print(f"❌ 启动失败：缺少核心配置文件 {missing}")
        sys.exit(1)

def run_agent(role, file_path):
    print(f"\n[🚀] 正在唤醒 {role}...")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            prompt_content = f.read()
        subprocess.run(["codex", "--full-auto", prompt_content], check=True)
        print(f"[✅] {role} 运行结束。")
    except subprocess.CalledProcessError as e:
        print(f"[❌] {role} 异常退出 (RC={e.returncode})，请检查日志。")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n[⚠️] 手动中止 {role}。")
        sys.exit(1)

def human_gate():
    """人类验收门：设立在 Supervisor 规划之后，拦截发散。"""
    while True:
        print("\n" + "="*60)
        print("🚦 [人类验收门 / Human-in-the-Loop]")
        print("Supervisor 已更新全局状态并生成了新 TASK.md。")
        print("="*60)
        
        choice = input("👉 操作: [回车]放行 Worker | [e]去 VSCode 微调 TASK.md | [q]退出 > ").strip().lower()
        if choice in ['', 'y']:
            print("🟢 审查通过，放行！")
            return
        elif choice == 'e':
            print("📝 请在 VSCode 中手动编辑 TASK.md。")
            input("编辑完成后按回车返回验收门继续 > ")
        elif choice == 'q':
            print("👋 已安全退出。")
            sys.exit(0)

def init_memory_files():
    """初始化长短期记忆文件，如果不存在则创建为空文件"""
    mem_files = ["DESIGN.md", "LESSONS.md", "CODE_MAP.md", "PLAN.md", "PROJECT_STATUS.md"]
    for f in mem_files:
        if not os.path.exists(f):
            open(f, 'w', encoding='utf-8').close()

def main():
    print("🤖 Codex Agent 2.0 (红蓝对抗版) 已启动！")
    check_env()
    init_memory_files()
    
    cycle = 1
    while True:
        print(f"\n" + "█"*60)
        print(f"🔄 第 {cycle} 轮开发周期开始")
        print("█"*60)

        # 1. 规划阶段 (Supervisor)
        run_agent("Supervisor", "SUPERVISOR.md")

        # 2. 决策与干预阶段 (Human)
        human_gate()

        # 3. 执行阶段 (Worker)
        run_agent("Worker", "WORKER.md")

        # 4. 审查阶段 (Validator)
        print("\n🔍 Validator 正在审查 Worker 的代码与数据契约...")
        run_agent("Validator", "VALIDATOR.md")
        
        # 此时循环结束。下一轮 Supervisor 醒来时，会直接看到 Validator 的结论。
        cycle += 1

if __name__ == "__main__":
    main()
