# PROPOSAL: Centaur Framework (自举与工程化重构)

项目定位: 基于共享内存与文件驱动的无状态 AI 研发智能体框架
文档属性: 框架自举重构与 CLI 化提案 (Bootstrapping & CLI Packaging Proposal)
当前基线: `v0.1.0-script-baseline` (依赖单文件 `run_workflow.py` 与分散的 `.md` 模板)

---

## 1. 意图与愿景 (Intent & Vision)

Centaur 的核心哲学是“文件驱动、无状态执行、红蓝对抗、人类把关”。
当前的实现（单点 Python 脚本 + 手动拷贝 Markdown 文件）虽然验证了核心工作流的巨大威力，但在多项目复用和版本管理上存在瓶颈。

本次重构的愿景是进行 Dogfooding（吃自己的狗粮）：利用现有的 Centaur 流程，将 Centaur 自身重构为一个标准化的、可全局安装的 Python CLI 工具。
目标形态：开发者只需在一个空目录敲击 `centaur init` 即可生成完整环境，敲击 `centaur run` 即可进入红蓝对抗研发循环。

## 2. 问题背景与痛点 (Problem Statement)

1. 脚手架缺乏自动化: 每次新开项目需要手动拷贝 `SUPERVISOR.md`、`WORKER.md`、`VALIDATOR.md` 和 `AGENTS.md`，极易造成模板版本碎裂。
2. 调度引擎不够内聚: `run_workflow.py` 作为孤立脚本，难以进行单元测试和模块化扩展（如未来增加更多 Agent 角色或切换底层 LLM CLI 工具）。
3. 长期记忆缺乏初始化辅助: `DESIGN.md`、`LESSONS.md` 和 `CODE_MAP.md` 需要在引擎级别有更好的存在性校验和默认占位符生成。

## 3. 目标架构与演进切分 (Target Architecture & Phases)

为实现 CLI 化与工程化，Centaur 自身需要完成以下三个维度的演进（请 Supervisor 按此划分 Phase）：

### Phase A: 核心 CLI 骨架与打包 (Scaffolding & Packaging)
- 目标: 将项目转换为标准的 Python Package (`pyproject.toml` 或 `setup.py`)。
- 任务: 引入 `Click` 或 `Argparse` 构建基础命令路由。实现 `centaur init` 命令，该命令能够将内置的 `SUPERVISOR.md`, `WORKER.md`, `VALIDATOR.md`, `AGENTS.md` 静态模板释放到当前执行目录，并创建空的长期记忆文件（`DESIGN.md`, `LESSONS.md`, `CODE_MAP.md`）。

### Phase B: 调度引擎模块化重构 (Engine Refactoring)
- 目标: 拆解 `run_workflow.py`。
- 任务: 将原本的面条代码重构为高内聚的模块（例如：`engine/runner.py` 负责子进程调用与异常捕获，`engine/human_gate.py` 负责终端交互拦截）。实现 `centaur run` 命令来拉起并接管标准的 `Supervisor -> Human -> Worker -> Validator` 循环。

### Phase C: 状态边界保护 (State Guardrails)
- 目标: 增强运行时的健壮性。
- 任务: 在引擎启动前增加严格的“起飞前检查 (Pre-flight Check)”。如果缺失核心文件，引导用户执行 `init`；在 `centaur run` 执行期间，捕获 Codex 进程卡死、`Ctrl+C` 中断等边界情况，确保所有 stdout/stderr 日志不丢失，且 Markdown 状态文件不损坏。

## 4. 毕业与验收标准 (Graduation Criteria)

本次自举重构达到 `v1.0.0-cli-stable` 的标志是：
1. 可安装性: 在当前虚拟环境中执行 `pip install -e .` 后，可在任意目录直接调用 `centaur` 命令。
2. 业务无损: 重构后的引擎必须 100% 保持原有逻辑（严格的子进程唤醒、`TASK.md` 覆写/追加语义、人类验收门阻塞），不能为了模块化而丢失原有的错误容忍度。
3. 自我证明: 重构完成后，使用新的 `centaur run` 能够成功调度它自己，完成一个微小 Feature 的开发（例如增加一个 `--version` flag）。

## 5. 执行纪律 (Strict Guardrails for Agents)
- 绝对静默原则: 在重构调度引擎时，不要改变原本的 Markdown 核心心智模型。Centaur 的核心竞争力就是“不依赖内存”，严禁引入任何基于内存的历史对话上下文传递机制。
- 平滑过渡: 第一步先搭建外壳和拷贝模板逻辑，最后再动现有的 `run_workflow.py` 的核心循环逻辑。