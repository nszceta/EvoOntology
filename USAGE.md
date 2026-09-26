# EvoOntology 产品化使用指南

第 1–9 节的安装和默认路径说明面向 Claude Code / Codex。OMP 的用户级安装、
共享工作区和会话内自动维护见第 10 节；请勿套用前文的项目本地默认路径。

本文档说明如何实际使用 EvoOntology。产品化把散落在三个 benchmark 里的通用能力抽取为
一个**核心包** `evoontology/`（确定性能力），并配两个自包含插件：

- `evoontology/` —— 与 benchmark 无关的产品运行时：ontology store / runtime(MCP) /
  trajectory / trigger / evaluation / evolution 生命周期 / validate 门禁。
- `plugins/` —— Claude Code 插件（`/evo-build`、`/evo-evolve`、`/evo-visualize` 命令 +
  对应 skills + MCP + Session Start 提醒）与 Codex 插件（`evo-build` / `evo-evolve` /
  `evo-visualize` skills + `AGENTS.md` + MCP）。两者内置同一份 core 副本。

产品最终形态 = 一个核心包（含 validate 门禁）+ 两个 skill 命令，无 CLI。智能分析全在
skill，Python 只做「运行时 + 最小确定性校验 + 进化生命周期状态机」。默认**零配置**：
不要求用户填写 workspace 路径、Evaluation Mode、Judge 模型或 Trigger 参数。

---

## 1. 安装

请选择正在使用的客户端，通过 Marketplace 安装；无需 clone 仓库、创建虚拟环境或单独运行
`pip install`。

### Claude Code

```bash
claude plugin marketplace add MeiduoChong/EvoOntology
claude plugin marketplace list
claude plugin install evoontology@evoontology
claude plugin list
```

### Codex

```bash
codex plugin marketplace add MeiduoChong/EvoOntology
codex plugin marketplace list
codex plugin add evoontology-codex@evoontology
codex plugin list
```

Marketplace 添加成功不等于插件已安装；请以最后一条 `plugin list` 显示 installed/enabled
为准。安装或更新后新建会话，再运行 `/evo-build`。

---

## 2. 一个 workspace 长什么样

workspace 默认是项目根的 `.evoontology/`，首次 `/evo-build` 时自动创建：

```
.evoontology/
├── project.json         # mode / data source / workload / evaluator / boundary
├── active.json          # {"active_version": "ontology_v0"}
├── versions/            # 所有版本（正式 ontology_vN + 候选 vN-cK），每版本 5 个 JSON
├── trajectories/        # 每个任务一条 JSON trajectory
├── evolution/           # 每个进化 run 一个目录 run_N/
│   └── run_N/
│       ├── run.json                 # 状态 / Parent / 当前 Candidate / 轮次 / 冻结预算
│       ├── trajectory-sources.json  # 用户确认的轨迹来源记录
│       ├── rounds.jsonl             # 每轮一行摘要
│       └── evaluations/             # 正式 Parent/Candidate 评估摘要
└── state.json           # Trigger checkpoint 与阈值
```

每版本下是 5 个记录文件，对应五类对象：Term / Mapping / Relation / Constraint /
Evidence。轨迹由 Data Agent 运行时（benchmark adapter 侧）在每次任务结束时追加到
`trajectories/`。

Workspace 分阶段初始化：Step 0 确认后写 `project.json`；初始版本保存并通过
语义 MCP 的 `validate_semantics`（`version` 传 `ontology_v0`）后，才写 `active.json` 和
`state.json`。Core 默认解析 `<project-root>/.evoontology/`，benchmark 可显式传入其他路径。

### 选择 mode

- `fixed_split`：用于已有固定问题集、GT 和评测边界的 benchmark。Construction Pool 用于
  Build/诊断，Validation Reserve 只用于最终 Gate。优先复用官方划分，不额外生成随机 Fold。
- `rolling_trajectory`：用于真实业务或冷启动。先用 seed workload 初始化，之后按 checkpoint
  持续收集新的 task trajectory；没有 GT 时通过独立任务抽样和 LLM Judge 比较 Parent/Candidate，
  不需要强行划分 Fold A/B。

mode 在 Step 0 确认后写入 `project.json`，后续 Build 和 Evolve 共用，避免每轮重新判断。

---

## 3. 触发指令

| 指令 | 语义 | 执行者 |
| --- | --- | --- |
| `/evo-build` | 构建 ontology_v0：读数据、探索 schema、生成五类记录 | agent 按 build skill |
| `/evo-evolve` | 触发进化：诊断→归因→补丁→Parent/Candidate gate→发布 | agent 按 evolve skill |

两者都是**触发指令**，不是 Python 确定性操作；真正的构建 / 进化由 agent 按 skill 执行。
版本命名与切换约定见 `plugins/claude-code/docs/versioning.md`（正式 `ontology_vN`、
候选 `vN-cK`，accept 映射 `vN-cK` → `ontology_vN+1`）。

---

## 4. 进化闭环：EvolutionSession

每次 `/evo-evolve` 对应一个 Run，由核心包的 `EvolutionSession` 状态机托管。Skill 决定
「改什么、为什么改」，Session 保证 run 不会以错误方式结束：

```
running ──Reject──▶ running（同一 run 内设计下一个 Candidate）
running ──Accept──▶ accepted（发布新版本、推进 checkpoint）
running ──预算耗尽/用户中断/数据缺失/评估不可靠──▶ incomplete
```

### 新 run 开始时

1. **恢复优先**：若已有未结束的 run，resume 它而不是新开；
2. **冻结数据**：fixed_split 复用已持久化的训练/验证子集；rolling_trajectory 从
   checkpoint 之后收集合格轨迹、冻结批次并切分 Evolution Pool / Validation Reserve；
3. **确认预算**：向用户说明本次计划使用的轮数（默认 8），确认后冻结进 `run.json`；
   resume 同一 run 沿用已确认预算；预算耗尽后如需加轮数，必须再次确认；
4. **确认轨迹来源**：来源或范围未定时，向用户说明每条来源的路径、内容范围、时间与
   用途并确认，确认后写入 `run_N/trajectory-sources.json`。新 run 默认复用最近一次 run
   的来源记录并验证路径仍有效，仅当来源新增、失效或范围变化时重新确认。找不到轨迹时，
   先跑 Parent baseline，再据评测结果、错误和反例开始诊断。

### 循环内

- 诊断 → 归因 → 补丁：沿 **Content / Tool / Schema** 三个维度选择主要机制，一个
  Candidate 验证一个主要假设；改动必须可溯源到目标维度、可回滚到 Parent；
- 评估：Candidate 以自己的存储版本参评（`--semantic-version`），比较期间不修改
  `active.json`；有 GT 走绝对评分，无 GT 走 LLM Judge 匿名 A/B；
- **Reject 不是终点**：写 `rounds.jsonl` 摘要、更新归因与 problem map，然后设计下一个
  Candidate；不推进 checkpoint、不结束 run；
- **Accept 结束搜索**：进入收尾。

### 收尾（Finalize）

Accept 后：确定性校验 → 发布为 `ontology_vN+1`（不覆盖已有正式版本）→ 更新
`active.json` → 推进一次 checkpoint → run 标记 `accepted`。
Incomplete 不发布、不推进；同一批次在下次 run 重试。`missing_data` /
`unreliable_evaluation` / `external_block` 这类判断性停止，需先在同一 run 内正式 Reject
至少 `min_rejects_before_incomplete`（默认 2）个候选；`user_interrupted` 与
`missing_permissions` 才立即停止。最终报告必须基于 session 终态与落盘记录，不依赖对话记忆。

---

## 5. 配置（零配置）

产品默认零配置，无 `config.yaml`。用户需要调整时直接告诉 Claude / Codex（例如「以后每
60 个任务提醒我一次」），由 agent 更新 `state.json` 内部状态，不改配置文件。

- 进化触发默认：checkpoint 后新增 ≥ 30 个 task，或距 checkpoint ≥ 7 天。首次 checkpoint
  是 `ontology_v0` 发布时间；**只有正式 Gate 的 Accept 推进 checkpoint**（Reject 在同一
  run 内继续循环，Incomplete 不推进）。
- 评估协议自动选择：benchmark 提供 Evaluator（Ground Truth）时走 GT；否则走 LLM Judge
  （见 `plugins/claude-code/docs/evaluation-protocol.md`）。

---

## 6. MCP 接入

插件通过 `.mcp.json` 以模块形式 spawn 服务，client 自动拉起、无需手动起服。默认
workspace 为当前项目的 `.evoontology/`（零配置）。

接入后 Data Agent 可见：

- 工具 `browse_semantics(query, kind, limit)` —— 发现相关概念；
- 工具 `resolve_semantics(mentions, context)` —— 解析概念到 grounding 的 mapping +
  关联的 relation / constraint / evidence；
- 资源 `evo-semantic://session-manifest` —— 会话开始时读取的简洁说明。

同一个 `evo-semantic` 服务还向 Build / Evolve / Visualize 暴露确定性操作
（`validate_semantics`、`visualize_ontology`、`evolution_status`、版本辅助与进化会话
工具），因此插件-only 安装无需在用户项目里运行 `python -m evoontology...`。

这两个工具返回的是元数据与指引，数据库查询与 Python 执行仍由 benchmark 原生工具负责。

---

## 7. validate 门禁（agent 自动）

`/evo-build`、`/evo-evolve` 发布新版本前，agent 会自动调用语义 MCP 的
`validate_semantics` 工具做确定性门禁（JSON 合法 / 引用完整 / 可加载），用户无需手动执行。
validate 只做结构校验，不做数据库语义校验（表字段存在 / Mapping 可执行 / Evidence 可复现
是 Builder 探索阶段已做的事）。

---

## 8. 一个最小端到端流程

```bash
# 1. 按第 1 节通过 Claude Code 或 Codex Marketplace 安装插件

# 2. 触发构建 ontology_v0（在客户端会话里输入）
/evo-build

# 3. Data Agent 通过 MCP 接入（.mcp.json 声明，client 自动 spawn，无需手动起服）

# 4. 触发进化（或等待轨迹达到阈值后的提醒）
/evo-evolve        # agent 用语义 MCP 的进化工具循环 Candidate；
                   # Accept 时经 accept_evolution 校验、发布、
                   # 更新 active.json 并推进 checkpoint
```

agent 发布前会自动调用 `validate_semantics` 做门禁。

---

## 9. 边界（一期不做；以下指非 OMP 插件流程）

Web UI / SaaS / 多租户 / 消息队列 / 常驻 worker / 多 Candidate 并行 / 自动循环 / 高频改
schema 均不在本版范围。非 OMP 钩子下的无人值守全自动进化需要常驻后台 worker，一期只做
「检测 + 提醒」，由人触发。OMP 用户全局集成不受此限：其自动生命周期（采集、门禁、认领、
同会话维护 turn）见 §10，全程跑在存活会话内，不设常驻 daemon。

---

## 10. OMP 用户全局集成（共享根 + 项目 lanes）

给 oh-my-pi（OMP）用户：EvoOntology 从本 clone 以用户全局方式运行——所有项目共用
一个外部 shared root，不写任何项目本地状态，内部按项目分 lane；每个完成的真实用户
turn 自动追加一条 lane 内 trajectory，数据任务按需走 grounded build，普通 coding 只采集；
会话存活时调度器可认领并启动 build / evolve / resume 维护任务。本节是与英文 README 中
“OMP User-Global Integration” 对应的中文契约。实现位置：
安装器 [`scripts/install_omp.py`](scripts/install_omp.py)、服务端
[`evoontology/runtime/omp_server.py`](evoontology/runtime/omp_server.py)、调度器
[`evoontology/omp_automation.py`](evoontology/omp_automation.py)、采集与调度扩展
[`integrations/omp/evo-capture.ts`](integrations/omp/evo-capture.ts)。

### 10.1 安装：checkout 内一条命令

前置要求：已安装 `uv`（在 `PATH` 上，或经 `--uv` / `$EVO_ONTOLOGY_UV` 指定并验证可执行），
且 OMP 已配置可用的认证模型（含 `@tiny` 模型——每 turn 门禁调用它判断是否同时给出可识别
data source **和** analytical goal）。

```bash
cd /path/to/EvoOntology
uv run python scripts/install_omp.py
# 可选覆盖：--agent-dir <dir> --store <dir> --uv <path>
```

装完重启 OMP。安装器幂等，可反复运行：

- 在 `<agent-dir>/mcp.json`（默认 `~/.omp/agent`）只合并 `evo-semantic` 一个条目，
  保留所有无关字段与服务；
- 向 `<agent-dir>/extensions/` 写 `evo-capture.ts` wrapper：文件内以文件 URI 导入本
  checkout 的真实扩展，并以安装时解析出的 `{store, uv}` 作为默认值调用其默认导出；
- 把三个 ontology skill（`build-ontology`、`evolve-ontology`、`explore-ontology`）
  链接进 `<agent-dir>/skills/`（无 symlink 权限时复制并跟踪归属）；
- 记录安装 manifest；只确保 store 目录存在，不写任何 ontology 状态
 （不写 `state.json` / `project.json` / 版本）。
- 安装解析出的 store 与 `uv` 永远持久化进 server 条目（`--store` 参数与 launcher
  `command` 中的解析后可执行文件）与 wrapper 默认值；store 覆盖分两条路径一致：
  server 与扩展进程 env 中的绝对路径 `$EVO_ONTOLOGY_STORE` 优先于安装值，安装值优先于
  当前 `HOME` 派生默认（`~/.omp/ontologies/shared`）。`uv` 的覆盖面更窄：事后改
  `$EVO_ONTOLOGY_UV` 不会改变 OMP 已 baked 的 MCP 启动器，只影响扩展 spawn 的子进程
  （采集与调度器调用：env 优先于安装默认值，安装默认值优先于 `PATH` 查找）。
  要换 MCP 启动器用的 `uv`，用新的 `--uv`（或 `$EVO_ONTOLOGY_UV`）重跑安装器并重启 OMP。
  本节不写任何个人绝对路径。
- 任何冲突在改动前报错且不做任何变更：无关的 `evo-semantic` 条目、占位的无关文件、
  不可读的 `mcp.json` 都会中止安装；本 clone 名下旧的 `mcp_server` 注册与旧的同目标
  symlink 在 rerun 时安全迁移为新形态。

### 10.2 布局：外部共享 + 内部 lanes

| 部件 | 路径 |
| --- | --- |
| Clone（改这里） | `<checkout>`（安装命令中的 `--project` 指向它） |
| Shared root（外部，永远是它） | `~/.omp/ontologies/shared`（当前用户；可被 `--store` / 绝对路径 `$EVO_ONTOLOGY_STORE` 覆盖） |
| Lane（内部，每项目一条） | `<shared root>/projects/<sha256(canonical project_root)[:16]>/` |
| MCP 服务 | `evoontology.runtime.omp_server --store <shared root>`（永远 project-aware；见 10.3） |
| 采集与调度扩展 | 本 clone 的 [`integrations/omp/evo-capture.ts`](integrations/omp/evo-capture.ts)，用户全局加载 |
| Recorder | `python -m evoontology.trajectory.omp_capture --store <shared root>`（stdin/stdout 走 JSON） |
| 调度器 | `python -m evoontology.omp_automation --store <shared root> --project-root <cwd>`（stdin JSON 包，stdout 恰一个 JSON 结果） |

```
<shared root>/
└── projects/
    └── <sha256(canonical project_root)[:16]>/
        ├── project.json         # lane 身份 project_root + 真实 data_source / workload / evaluator / boundary
        ├── active.json          # {"active_version": "ontology_v0"}
        ├── versions/            # 正式 ontology_vN + 候选 vN-cK，每版本 5 个 JSON
        ├── trajectories/        # 本 lane 每个任务一条 JSON trajectory
        ├── evolution/           # 本 lane 每个进化 run 一个目录 run_N/
        ├── state.json           # 本 lane Trigger checkpoint 与阈值
        └── automation.json      # 本 lane 维护租约 / seed / 冷却状态（调度器读写）
```

任何项目下都不会创建 `.evoontology/`。内部路由由
`workspace.resolve_project_workspace(workspace, project_root)` 决定：外部
`workspace` 为 shared 时按 `project_root` 落到对应 lane；显式传入其他 workspace
时保留为所选 base。`project_root` 缺省时保持 legacy flat 行为不变。Helper 为只读
（不创建 lane）；带 `project_root` 时要求显式 workspace，否则抛错。

空 lane 是合法状态：没有 `project.json`、没有 versions 时 trajectory 先攒着；数据任务
给出可识别的数据源与分析目标后 agent 才走 grounded workflow，缺证据时 build 中止，
lane 保持为空。

Legacy flat root（`project.json` 直接落在 shared root 下）仅当其持久化的
`project_root` 或 `data_source` 能无歧义匹配当前 canonical project root 时才复用；
否则走新 lane，绝不混用别项目的 active 版本。
`configure_ontology_project` 把 `project_root`（lane 身份）与真实 `data_source`
（数据来源）分开持久化。

### 10.3 MCP 设置（`~/.omp/agent/mcp.json`，安装器代写）

安装器注册的 `evo-semantic` 服务形如（`<checkout>`、`<shared root>`、`<uv>` 均为安装时解析值）：

```json
"evo-semantic": {
  "type": "stdio",
  "command": "<uv>",
  "args": [
    "run", "--offline", "--no-sync",
    "--project", "<checkout>",
    "python", "-m", "evoontology.runtime.omp_server",
    "--store", "<shared root>"
  ],
  "env": { "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1" },
  "enabled": true,
  "timeout": 120000
}
```

- `--project` 指向 clone，`--store` 为安装时解析的 shared root。
- `omp_server` 永远 project-aware：服务启动 cwd 绑定为默认 `project_root`；每个工具的显式
  `project_root` 参数覆盖该默认值。运行时绝对路径 `$EVO_ONTOLOGY_STORE` 优先于 `--store`。
- 不带 `project_root` 且显式指定其他 workspace 时，保留所选 base（legacy flat 兼容）。

### 10.4 扩展设置（用户全局）

扩展 wrapper 由安装器写入 agent extensions 目录，内容为导入本 checkout 真实扩展并传入
`{store, uv}` 安装默认值；扩展子进程运行时绝对路径 `$EVO_ONTOLOGY_STORE` 优先于安装默认值，
`$EVO_ONTOLOGY_UV` 优先于安装默认值（只影响扩展 spawn 的采集与调度器子进程，不改变
`mcp.json` 已 baked 的 MCP 启动器——换启动器用新 `--uv` 重跑安装器，见 10.1）。
全局启用二选一（之后重启 OMP；不需要在任何项目的 `.omp/extensions` 里声明）：

```bash
# 安装器已代写 wrapper；手工等价形态仅示意（<checkout> 换成实际 clone 路径）：
ls ~/.omp/agent/extensions/evo-capture.ts
```

```yaml
# 或在 ~/.omp/agent/config.yml 里显式声明 wrapper 路径：
extensions:
  - ~/.omp/agent/extensions/evo-capture.ts
```

### 10.5 自动采集（lane-aware，只采真实用户 turn）

每个完成的真实用户 OMP turn 向对应 lane 的 `trajectories/` 追加一条 trajectory
（`agent_end` 且仅当 `!willContinue` 时；自主维护 turn 自身不被采集、不递归）：

- `question`（用户文本，必填非空）、`final_answer`（助手文本）、`status`
  （`completed` | `failed` | `interrupted`），以及按执行顺序的工具 `calls`
  （`tool` 必填非空，`arguments` 缺省 `{}`，`is_error` 缺省 `false`），
  均从 `ctx.sessionManager.getBranch()` 重建；
- 来源：`project_root`（绝对路径，取自 `ctx.cwd`）、`session_id` 和 `turn_id`
  （均必填非空）；`--store` 同样必须是绝对路径；
- `task_id` 由 project + session + turn 确定性派生，重试幂等——stdout 为
  `{task_id, status: recorded | already_recorded}`。输入非法、或同一 id
  重试但内容变化时，以非零退出码结束并往 stderr 写 `{"error": ...}`。

采集复用 `from_message_trace` + `TrajectoryStore`，与构建工作流无关：不需要
`project.json`、不需要准备好的问题集、不需要 active ontology 版本。落盘的
`ontology_version` 缺省记为 `"uninitialized"`：只有被证明适用于当前 lane 的
active 版本才会被记录，别 lane 的版本不会混用。

### 10.6 按需门禁与合格种子（YES 才留种，前台仍走 skill）

全局 `before_agent_start` hook 对每个真实非空用户 prompt 做静默 per-turn 门禁：
用 isolated 无工具会话调用已配置 `@tiny` 模型（分类器本身无工具、不写任何状态），独立判断
该 prompt 是否同时给出可识别 data source **和** analytical goal。门禁本身不写 ontology
版本状态（`project.json` / `versions/` / `evolution/`）；仅 YES 分支把脱敏截断后的 prompt
经调度器持久化为 automation seed（只进 `automation.json`，不碰 project 上下文与版本）。

- 仅肯定（YES）结论才注入隐藏（`display: false`）的 build/use 指引，指示前台 agent
  按 skill 处理 lane 检查（`list_versions`）/ 未初始化时的 grounded build 并在最终回复中
  使用其结果；普通请求无消息、无 UI 打扰。
- YES 的 prompt 文本经脱敏、截断后作为 automation seed 经调度器持久化（见 10.7），
  供失败初建的唤醒与 build 任务使用；NO / unknown 不留种。
- 未知模型 / 出错 / 超时则 fail-open，返回隐藏的条件安全指引。Hook 永不切换会话模型，
  绝不从代码里自动 build、发布或进化；采集仍在每个完成的 turn 运行。代价：一 turn 一次小
  分类往返；若 `@tiny` 配置为云模型，有界 prompt 文本会出本机（见 10.10）。
- 手工 skill 调用始终允许：门禁只是前台指引，不是唯一入口。

### 10.7 自动维护调度（会话内存活，无 daemon）

OMP 关闭时无任何维护；会话存活期间扩展在三个时机尝试推进：会话启动、每次完成采集后、
约 60s 的受管 idle timer。扩展按 lane 向调度器认领 durable 租约任务
（`build` / `evolve` / `resume`，跨会话持久化在 lane 内），一次最多持有一个 job：
认领成功后用原生同会话 custom turn（`pi.sendMessage` + `triggerTurn`，
`MAINTENANCE_CUSTOM_TYPE`）**真正启动**该任务——执行者是前台主 agent 本人，
不是独立 worker，不存在权限绕过。

- 租约约 15 分钟并定时心跳续约；扩展侧本 job turn 执行超时约 10 分钟（只 abort
  扩展自己的维护 turn，用户工作永不 abort）；自动预算 2 轮。
- 用户优先：idle / 用户活动中 / 有待发送消息 / 已持 job / 冷却期内均不派发；
  plan 与 paused-plan 下跳过；认领后复检被抢占则直接释放租约。
- 维护 turn 结束时以 `finish` 按 lane 实际落盘状态结算（调用方不传结果标志，
  调度器从 store 推断 completed / cooldown）；过期与重启恢复同样按持久化结果结算，
  不捏造调度事实。受阻 / 未完成的尝试冷却约 24h；新的、不同的合格 seed 可唤醒失败的初建。
- 调度器入口只认 stdin 单个 JSON 包（`op` 为 `status` / `seed` / `claim` /
  `heartbeat` / `finish` / `release`），stdout 恰一个 JSON 结果。

### 10.8 进化就绪与执行纪律

- 就绪：checkpoint 后新增 ≥ 30 个 trajectory，或距 checkpoint ≥ 7 天；且任务另需有效
  active parent 与已配置的 project 上下文。无 source / 无 goal 时只采集、不推断语义，
  绝不编造。
- 无可访问的数据源、目标或评测路径时，任务报告真实 blocker 并安全停止，不发布空版本。
- 源数据只读；provider 与工具审批 fail-closed：被拒即安全结束并在收尾消息中报告 blocker，
  不吞拒、不绕行，不承诺无人值守必定成功。
- Gate 评判由宿主侧独立 agent / subagent 评估器执行并记录真实结论——不暗示 MCP 自己
  发明或执行 judge；缺证据或证据失败时保留 parent。
- 每次 run 在启动时冻结 `trajectory_checkpoint`，只推进到该截止（含该截止）的批次，
  run 期间新到的 trajectory 留给下一批。

### 10.9 永不自动做的事与退出

- 采集不写 `project.json`、`active.json`、`versions/`、`evolution/`。
- 调度代码负责触发与状态管理，原生 agent 自动按 skill 完成构建/进化；
  初始发布仍须源证据与 `validate_semantics`，进化发布另须 Parent/Candidate gate。
- 未通过 source + goal 分类的通用 coding turn 不会新建种子；无可识别来源和目标时
  不得凭空构建语义事实。
- `EVO_ONTOLOGY_AUTOMATION=0` 关闭维护派发与 YES 留种；前台指引与采集继续运行。

### 10.10 有界本地持久化、隐私与费用

- 全部采集数据只落在 shared root 本地（各 lane 内）；采集本身不上传。例外有二：
  10.6 的 `@tiny` 门禁往返（有界 prompt 文本），以及维护 turn 内前台 agent 为执行任务
  调用已配置模型时发送的相关 prompt / 数据预览——若配置为云模型，这些内容会出本机
  并产生模型费用。
- Native 工具结果截断为有界预览；agent 的 prose 和 chain-of-thought 不落盘——只保留
  可观察的工具输入/结果和 final answer。
- 写入前对可疑 secret 值做 best-effort 脱敏。这是本地卫生措施，不是加密 vault，
  不要把密钥写进 prompt 或工具参数。

### 10.11 状态排查（走 CLI，不翻 lane 内部）

排查只用调度器 `status`，不要手工浏览 lane 内部文件：

```bash
echo '{"op":"status"}' \
  | uv run --project <checkout> python -m evoontology.omp_automation \
    --store <ABS-STORE> --project-root <ABS-PROJECT-ROOT>
```

`<ABS-STORE>` 与 `<ABS-PROJECT-ROOT>` 均为绝对路径。返回 active 版本、project 配置、
就绪原因、运行中 run、seed、租约、冷却与下次检查时间。

### 10.12 Smoke test（沙箱，不碰线上 shared root）

```bash
SMOKE_STORE=$(mktemp -d)
SMOKE_PROJ=$(mktemp -d)
echo "{\"project_root\":\"$SMOKE_PROJ\",\"session_id\":\"smoke\",\"turn_id\":\"t1\",\"question\":\"q\",\"final_answer\":\"a\",\"status\":\"completed\",\"calls\":[]}" \
  | uv run --project <checkout> \
    python -m evoontology.trajectory.omp_capture --store "$SMOKE_STORE"
ls "$SMOKE_STORE"/projects/*/trajectories/
rm -rf "$SMOKE_STORE" "$SMOKE_PROJ"
```

Smoke 永远用临时 store 与临时 project 目录：不要把演示 trajectory 写进线上 shared root。
安装器试装用全新的临时 `HOME`（如 `HOME=$(mktemp -d)` 配合 `--agent-dir` / `--store`
指向临时目录）验证，不碰真实 `~/.omp`。扩展另支持测试专用的
`EVO_ONTOLOGY_STORE` 绝对路径覆盖 `--store`（线上默认仍是 shared root，
非绝对路径会被忽略并告警）。清理只删上面自己创建的临时目录，不做宽泛删除。

### 10.13 局限（明确声明）

- OMP 关闭时无维护；正常退出释放租约，异常退出的残留租约过期后按实际状态结算，
  下次会话可恢复未完成的 run。
- 无自动审批：任何 provider / 工具审批弹窗都需要人工原生确认。
- 没有可访问的数据源、分析目标或评测路径就建不出 ontology：此时只报告真实 blocker，
  不编造版本与分数。
