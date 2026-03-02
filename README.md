# 🐎 Centaur Framework (半人马架构)

> **A File-Driven, Stateless Multi-Agent Framework for Real-World Software Engineering.**
> 一个基于文件驱动、无状态交互、专注于真实世界软件工程的“人机协同”智能体框架。



## 💡 核心哲学：为什么我们需要 Centaur？

在当前主流的 AI 辅助开发中（如基于内存的 Multi-Agent 框架），极易陷入“上下文爆炸”、“死循环重试”和“代码静默破坏”的泥潭。
Centaur 抛弃了黑盒式的内存图网络，回归最纯粹的 **UNIX 哲学与 GitOps 理念**：

1. **半人马模式 (Human + AI)**：人类掌握战略方向（导航员），AI 负责战术执行（引擎）。核心设置 `[人类验收门]`，系统决不擅自越过安全护栏。
2. **Markdown 即状态 (State is Text)**：没有复杂的数据库，所有的规划、状态、任务和长期记忆全部具象化为 `.md` 文件。极其透明，人类可随时干预篡改。
3. **绝对无状态 (Stateless Execution)**：每次 Agent 唤醒都是全新的冷启动，依靠读取文件快照重建上下文，彻底消灭 LLM 幻觉累积。
4. **红蓝对抗 (Triad Checks)**：引入独立的 Validator（审查官），强制基于业务契约（而非覆盖率）进行审查，阻断 AI 的“伪成功”欺骗。

---

## 🏗️ 核心架构与角色 (The Triad)

Centaur 由三个严格隔离的 AI 角色组成，通过共享文件系统进行通信：

- 🧠 **Supervisor (主管)**：负责阅读大局，维护项目进度（`PLAN.md`），并向员工下发带有 TDD 约束的具体工单（`TASK.md`）。**绝对不写业务代码**。
- 🛠️ **Worker (员工)**：绝对专注的执行者。只看眼前的 `TASK.md`，执行编码、命令行测试，并强制要求返回**真实数据切片**作为验收证据。
- 🕵️ **Validator (审查官)**：冷酷的红队。审查 Worker 的输出，专挑硬编码、环境耦合与契约违背的毛病。拥有极其严厉的一票否决权。

---

## 📂 共享内存矩阵 (Memory Matrix)

Centaur 通过不同生命周期的 Markdown 文件管理庞大的项目上下文：

### 短期通信与状态（动态流转）
- `TASK.md`: Agent 之间通信的唯一总线（覆写与追加）。
- `PLAN.md`: 树状任务清单与进度打钩（看板）。
- `PROJECT_STATUS.md`: 当前里程碑、技术栈宏观状态与残余风险。

### 长期组织记忆（沉淀防腐）
- `PROPOSAL.md`: 项目的北极星与终极目标（只读）。
- `DESIGN.md`: 长期架构设计草图与数据结构规范。
- `LESSONS.md`: 避坑指南、已知的环境约束与历史教训。
- `CODE_MAP.md`: 核心模块索引，防止 AI 全局盲搜。

---

## 🚀 快速启动 (Quick Start)

### 1. 安装 CLI

方式 A：通用 Python `venv`（推荐，适用于没有 `dev` 环境的机器）

```bash
git clone <your-centaur-repo-url>
cd Centaur

python3 -m venv .venv
source .venv/bin/activate

python -m pip install -U pip setuptools wheel
python -m pip install -e . --no-build-isolation

centaur version
```

方式 B：Conda（环境名可自定义，不必叫 `dev`）

```bash
conda create -n centaur python=3.12 -y
conda activate centaur

python -m pip install -U pip setuptools wheel
python -m pip install -e . --no-build-isolation

centaur version
```

如果提示 `centaur: command not found`，先用：

```bash
python -m centaur.cli version
```

如果这条能输出版本号，说明包已安装，问题是当前 shell 没激活对应环境。

在当前仓库内快速安装（已有可用 Python 环境时）：

```bash
python -m pip install -e .
centaur --help
```

### 2. 初始化脚手架
在目标项目目录中运行初始化命令，生成 Centaur 基础配置文件：

```bash
mkdir my-project && cd my-project
centaur init

```

默认是全局提示词模式：项目目录不会复制 `AGENTS.md/SUPERVISOR.md/WORKER.md/VALIDATOR.md`，运行时直接使用已安装的 Centaur 模板。

如需把当前提示词快照冻结到项目中（便于长期锁定行为）：

```bash
centaur init --freeze-prompts
```

### 3. 定义北极星目标

打开 `PROPOSAL.md`，清晰地写入你的项目需求和边界约束。

### 4. 启动引擎

运行调度脚本，开始人机协同开发：

```bash
centaur run

```

*在每次 Supervisor 下发任务后，系统会暂停在 `🚦 [人类验收门]`，等待你的放行或微调。*
*运行状态会写入当前目录的 `.centaur_state.json`，重启后会自动续跑到下一角色。*
*项目元数据会写入 `.centaur_project.json`（记录 prompt 模式与版本）。*

如需手动覆盖起点角色，可使用：
```bash
centaur run --from-role supervisor
```

如需升级后迁移项目（切换提示词模式 / 刷新版本记录）：
```bash
centaur migrate --prompts global
centaur migrate --prompts frozen --force
```

---

## 🛡️ 安全与回滚策略

Centaur 深度绑定 Git。如果 AI 将代码改坏或陷入混乱路线：

1. 随时按 `Ctrl+C` 中止脚本。
2. 运行 `git reset --hard` 回溯到上一个完美节点。
3. 重新运行 `centaur run`，AI 将基于正确的文件快照瞬间“失忆并重生”。
