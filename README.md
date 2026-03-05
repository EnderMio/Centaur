# 🐎 Centaur Framework (半人马架构)

> **A File-Driven, Stateless Multi-Agent Framework for Real-World Software Engineering.**
> 一个基于文件驱动、无状态交互、专注于真实世界软件工程的“人机协同”智能体框架。

## 💡 核心哲学：为什么我们需要 Centaur？

在高强度 AI 辅助研发中（如基于内存的 Multi-Agent 框架），团队常见问题是上下文漂移、任务边界扩散、失败难以复盘。
Centaur 回归 **UNIX + GitOps** 思路，用可观察、可回放、可治理的方式组织协作流程：

1. **半人马模式 (Human + AI)**：人类掌握战略方向，AI 负责战术执行，流程内置人类验收门。
2. **Markdown 即状态 (State is Text)**：核心状态与任务证据全部文件化，支持随时审阅与人工干预。
3. **绝对无状态 (Stateless Execution)**：每次角色唤醒都从文件快照重建上下文，避免隐式记忆污染。
4. **红蓝对抗 (Triad Checks)**：Supervisor / Worker / Validator 三角制衡，压制“伪成功”路径。

---

## 🏗️ 核心架构与角色 (The Triad)

Centaur 由三个职责隔离的角色协同推进，通过共享文件系统通信：

- 🧠 **Supervisor (主管)**：阅读全局、维护计划、下发任务，聚焦调度与风险收敛。
- 🛠️ **Worker (执行者)**：按 `TASK.md` 边界实现与验证，提交命令输出和证据。
- 🕵️ **Validator (审查者)**：独立复核契约一致性、回归风险与证据完整性。

标准循环：`Supervisor -> Human Gate -> Worker -> Validator`。

---

## 📂 共享内存矩阵 (Memory Matrix)

Centaur 使用“工作区 + 运行态目录”管理项目上下文。

### 业务协作文档

- `PROPOSAL.md`：北极星目标与边界。
- `PLAN.md`：阶段任务树与进度看板。
- `PROJECT_STATUS.md`：里程碑、风险与阻塞。
- `TASK.md`：当前任务总线。
- `DESIGN.md` / `LESSONS.md` / `CODE_MAP.md`：长期知识沉淀。

### 运行态目录（`<workspace>/.centaur/`）

- `project.json`：项目元数据（`schema_version`、`prompt_mode`、`active_task`、`controller_version`、`target_*`）。
- `state.json`：调度状态（`cycle`、`next_step`）。
- `tasks/`：任务总线快照。
- `logs/`：运行证据与日志。

---

## 🚀 快速启动 (Quick Start)

### 1. 安装 CLI

要求：`Python >= 3.9`，`centaur run` 依赖系统可调用 `codex`。

开发安装（editable）：

```bash
git clone https://github.com/EnderMio/Centaur.git
cd Centaur
python -m pip install -U pip setuptools wheel
python -m pip install -e . --no-build-isolation
centaur version
```

Conda 示例：

```bash
conda create -n centaur python=3.12 -y
conda activate centaur
python -m pip install -U pip setuptools wheel
python -m pip install -e . --no-build-isolation
centaur version
```

升级（editable）：

```bash
git pull
python -m pip install -e . --no-build-isolation
centaur version
```

常见安装报错 `Cannot import 'setuptools.build_meta'`：

```bash
python -m pip install -U setuptools wheel
python -m pip install -e . --no-build-isolation
```

命令入口排查（`centaur: command not found`）：

```bash
python -m centaur.cli version
```

### 2. 初始化工作区

推荐创建独立工作区：

```bash
centaur workspace create selfhost --root ./workspaces
```

也可以在现有目录初始化：

```bash
centaur init --workspace ./workspaces/my-project
```

如需将角色提示词冻结到项目目录：

```bash
centaur init --workspace ./workspaces/my-project --freeze-prompts
```

### 3. 定义北极星目标

编辑 `PROPOSAL.md`，明确目标、约束和验收标准。

### 4. 体检并启动引擎

```bash
centaur doctor --workspace ./workspaces/selfhost
centaur run --workspace ./workspaces/selfhost
```

默认不建议在 Centaur 框架源码根目录直接运行；确需放行时：

```bash
centaur run --allow-repo-root
```

如需手动覆盖入口角色：

```bash
centaur run --workspace ./workspaces/selfhost --from-role supervisor
```

---

## 🧰 命令地图 (Command Map)

工作区控制面：

```bash
centaur workspace create <name> --root ./workspaces [--freeze-prompts] [--force]
centaur workspace list --root ./workspaces
```

任务控制面：

```bash
centaur task list --workspace <path>
centaur task new <task_name> --workspace <path> [--switch] [--from-current] [--force]
centaur task switch <task_name> --workspace <path>
```

迁移控制面：

```bash
centaur migrate --workspace <path> --schema
centaur migrate --workspace <path> --prompts global
centaur migrate --workspace <path> --prompts global --keep-local-prompts
centaur migrate --workspace <path> --prompts frozen
centaur migrate --workspace <path> --prompts frozen --force
```

常用辅助：

```bash
centaur doctor --workspace <path>
centaur version
```

---

## 🔁 Prompt 模式与迁移策略

- `global`：运行时读取安装包模板；项目内同名角色提示词默认归档到 `.centaur_prompts_backup/`。
- `frozen`：运行时读取项目内 `AGENTS.md/SUPERVISOR.md/WORKER.md/VALIDATOR.md`。

`centaur migrate --prompts frozen --force`：

- 使用当前安装版本模板覆盖项目角色提示词。
- 适用于将项目提示词对齐到当前版本基线。

若要保留项目内自定义提示词：

- 迁移到 `frozen` 且不添加 `--force`。

---

## 🛡️ 安全与恢复策略

- 随时可用 `Ctrl+C` 中止流程。
- 重新执行 `centaur run --workspace <path>` 即可按 `state.json` 断点续跑。
- 需要强制角色入口时使用 `--from-role`。

Centaur 的价值在于把 AI 协作纳入工程治理：流程可审计，状态可恢复，决策可追踪。
