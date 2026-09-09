---
name: lab-orchestrate
description: >-
  在 labkit 项目启动、恢复或检查用户选择的多模型协作；一个或多个 brains 负责规划和独立验收，
  一个或多个 workers 逐个执行任务。Claude Code 通过 /labkit:lab-run 进入，Codex 和本地 ChatGPT 直接使用此 skill。
---

# lab-orchestrate

`scripts/lab_run.py` 保存团队选择、原始目标、所有模型调用和验收结果。`brains[0]` 是 lead，其余 brains 先给建议；lead 每次只给一个 worker 分配任务；随后所有 brains 独立检查整个原始目标。不能把 worker 自报完成、局部任务成功或预算用完当作交付。

## 1. 定位项目与确认本次团队

Plugin root 是此 `SKILL.md` 所在目录向上两级。将 `../../scripts/lab_run.py` 解析为绝对路径，用作下文 `<CONTROLLER>`。找到目标 `.labkit.json` 项目并读取 canonical `AGENTS.md`；plugin path 和 project cwd 分开使用。

若用户提供跨宿主交接 packet，先执行同插件的 `lab-handoff` skill，完成上下文读取、checkout 校验和接收，再恢复 goal。controller 会拒绝交接仍为 ready 或实际宿主不是当前 owner 的执行；不要删除交接状态来绕过。

仅检查 `status`、`doctor` 或 `catalog` 时直接运行。每次新启动或用户主动恢复协作前，先询问本次需要一个还是多个 brains、一个还是多个 workers，以及各成员的 provider、exact model ID 和需要指定的 effort。用户已在本次请求中明确给出选择时不重问；恢复时明确选择沿用上一团队也有效。控制器内部等待、提交和继续同一 run 不触发再次询问。

将用户本次回答原文记入 `team.json` 的 `selection`，按 [README 示例](../../README.md#启动示例) 写入 `brains` 和 `workers`。每组至少一个成员，所有 `id` 唯一；provider 为 `codex` 或 `claude`，model 使用完整 ID，不用 `opus`、`sonnet`、`default`、`inherit` 等别名。省略 `effort` 使用原生默认设置；用户指定时原样保存。`catalog` 是配置提示，不是完整模型清单或账户 entitlement 证明。

## 2. 保存原始目标并启动

将用户原始目标及限制完整保存在 `goal.json` 的 `objective`，提取可核查的 `acceptance: [{"id", "text"}]`，并列出非空的 `deliverables`。交付物必须是项目内文件的相对路径，不能指向 `handoffs/runs/`。沿用已有目标文件时先检查它是否保留了用户要求。run 创建后目标冻结，后续计划和团队变化不能降低验收标准；需要改变 scope 时保留原 run 并另建目标。

```text
python "<CONTROLLER>" doctor
python "<CONTROLLER>" catalog
python "<CONTROLLER>" run --directory "<PROJECT>" --goal-file "<GOAL_FILE>" --team-file "<TEAM_FILE>"
python "<CONTROLLER>" status "<RUN_DIRECTORY>"
python "<CONTROLLER>" resume "<RUN_DIRECTORY>" --reuse-team
python "<CONTROLLER>" resume "<RUN_DIRECTORY>" --team-file "<TEAM_FILE>"
```

开始真实协作前说明这些调用会消耗所选模型的 usage，然后继续已授权任务。`doctor` 只检查 CLI 和登录状态，不调用模型。只需满足当前团队所用 provider 的依赖；未选 provider 的失败不证明当前团队不可用。

`run` / `resume` 支持 `--max-rounds`、`--max-calls`、`--timeout`。新 run 默认最多 4 个规划/验收 cycle；默认 call budget 为 `max_rounds * (2 * brains数量 + 2)`。`--timeout` 是每次本地 CLI 调用的 hard timeout，默认 1,200 秒；原生 `Workflow` 的公开 agent 契约没有 timeout/AbortSignal，不受该参数硬限制，需要停止时由宿主使用 `TaskStop`，再按第 4 步恢复。调用预算在 dispatch 前计入，包含中断的调用。新 run 的 `--stall-limit` 默认 2，连续 cycle 没有项目进展会暂停。保留用户明确给定的限制。

Windows 的 `--windows-sandbox elevated|unelevated` 保存到 run；省略则使用宿主配置。Codex brains 保持 `read-only`，Codex workers 使用 `workspace-write`。遇到 Windows 1385 时可说明官方 `unelevated` 兼容选项及其较弱隔离，但不自动换实现、禁用 sandbox 或改 ACL。项目仍需在所选 sandbox 下可读。

保存 controller 打印的 `handoffs/runs/<run-id>/`。若命令工具返回正在运行的 session，等待同一 invocation 完成，不启动第二个 controller。所有 workers 共用项目目录，controller 逐个派发；不能自行并发修改同一目录。

## 3. 按 provider 和宿主执行

| 所选 provider / 当前宿主 | 执行方式 |
|---|---|
| `codex` / 任一本地宿主 | 本地 Codex CLI，指定所选完整 model ID 与 effort。 |
| `claude` / Codex 或本地 ChatGPT | 本地 Claude CLI，指定所选完整 model ID 与 effort。 |
| `claude` / Claude Code | 原生 `Workflow` 单个 agent；不依赖当前主会话模型。 |

保留 `CLAUDECODE`、`LABKIT_ROLE` 和 nesting guards。Claude 内不能清除环境变量后启动 nested Claude CLI。非 worker 的 Claude agent 使用 `Plan`；worker 使用 `general-purpose`。这些是宿主权限模式，不是独立 OS sandbox。连接器是否可用以实际宿主为准。

### Claude 原生请求

Exit `4` 且 `status: awaiting_host` 表示一个确切的原生请求，可能是 advisor、planner、worker 或 reviewer。

1. 读取 controller 指定的整个 `.workflow.json`，将其 JSON object 原样作为原生 `Workflow` 的输入。新请求只含 `scriptPath`，指向已内嵌 exact prompt、pending token、完整 model ID、effort 和 agentType 的 `.workflow.js` 绝对路径；无需读取、复制或重构长 prompt。已有 pending 即使仍含旧 `script` + `args` 也原样使用，不替换它，不改为当前主会话直接执行。
2. 保存返回的 Workflow ID，等待同一 Workflow 完成。恢复或等待期间不能重新派发同一 prompt。工具不可用或拒绝所选模型时报告实际阻碍，保留 pending request。
3. 将该 agent 的完整最终 assistant text 原样保存为 run 内 report file，不总结、不增加包装文字。原生 `Plan` 可能在 JSON 外加入说明和 `Critical Files` footer，全部保留，不能只摘 JSON。controller 先精确核验完整原文，再从直接 JSON 或唯一标记为 json 的代码块提取结构，执行相同 strict schema/criterion gates，并记录原始 `text` 与 `structured_output`；多个 JSON 块或模糊结构会被拒绝。找到此 Workflow 实际写出的 agent transcript，通常位于 `~/.claude/projects/<project>/<session>/subagents/workflows/<workflow>/agent-*.jsonl`；配置了 `CLAUDE_CONFIG_DIR` 时使用对应目录。同目录的 agent `.meta.json` 和 `journal.jsonl` 也必须保留，供 controller 核验角色和完成记录。不得复制或改写 runtime 文件来构造证据。
4. 提交同一 run、同一 artifact、该 report 与真实 transcript：

```text
python "<CONTROLLER>" submit "<RUN_DIRECTORY>" --artifact "<ARTIFACT>" --report-file "<REPORT_FILE>" --transcript-file "<TRANSCRIPT_FILE>"
```

`ARTIFACT` 必须与 `state.json` 的 `pending.artifact` 相同。controller 校验 transcript 路径、pending token、实际 `assistant.message.model`、metadata 的模型/角色、journal 的完成事件及最终文本；模型自报身份或旧版 `--host-model` 不是证据。拒绝过期或不匹配的报告时，不给旧报告换 artifact。`submit` 自动推进同一 run；若返回新的 exit `4`，处理新请求，不重新询问团队。

## 4. 恢复、暂停与取消

用户恢复时先执行第 1 步的团队选择，再使用 `--reuse-team` 或 `--team-file`。pending request 尚未解决时必须保持原团队；同一团队文件可以重用。

- `awaiting_host`：`resume` 返回相同 request，不重复 dispatch，也不增加预算。先确认旧 Workflow 的状态，成功时提交原最终文本与真实 transcript。失败或中断时，先观察它已结束/失败，或用原生 `TaskStop` 停止它，确认没有并行执行，再使用下列 `recover`。`reason` 如实记录已确认的部分改动和实际错误；controller 保留原 pending 与原因、保留已消耗预算，让 brains 检查部分状态后重新规划。不能把部分日志包装成完成 report，也不能盲目重派同一任务。
- `interrupted` CLI turn：恢复保留已消耗预算，brains 先检查原 prompt、保存输出和实际部分改动，再决定新任务；不要手动重放 worker 的修改。
- `paused` / `blocked`：用户恢复会授予新的 cycle/call budget。`no_progress` 暂停需先读取未通过的 reviews，确定新做法后使用 `--reset-stall`，不能无思考地反复重置。
- 放弃 run：先停止仍在运行的 Workflow 或 CLI invocation，再执行下列命令。取消释放项目保留状态并保存所有证据，已取消 run 不可恢复。

```text
python "<CONTROLLER>" recover "<RUN_DIRECTORY>" --artifact "<PENDING_ARTIFACT>" --reason "<部分改动和实际中断原因>" --workflow-stopped
python "<CONTROLLER>" cancel "<RUN_DIRECTORY>" --reason "<具体取消原因>"
```

`recover` 仅用于 `awaiting_host` 且 artifact 完全匹配的请求；`--workflow-stopped` 必须基于实际观察。恢复或取消前停止任务是宿主的职责，controller 不能替宿主终止 Workflow。

同一项目的未结束调用可能阻止新 run；应恢复或取消原 run，不能删除 lock/state 来绕过。format 1 历史 run 保留旧协议，用 `--reuse-team` 恢复固定团队；其原生 report 使用 `lab_run_legacy.py submit`，不要与 format 2 的新提交参数混用。

## 5. 交付

只有所有当前 brains 都独立覆盖全部 acceptance ID、逐项给出通过证据、没有 unresolved issues，且所列文件实际存在并在 review 期间通过项目稳定性检查，controller 才写入 `delivery.md` 和 `delivery.json`。未通过则回到建议/规划和修复；外部前提缺失为 `blocked`，预算或无进展为 `paused`。

向用户报告交付物、相关验证、实质限制和 run path，并以保存的 state 为准：`run` / `resume` / `submit` 的 exit `0` 完成、`1` 错误或中断、`2` 阻塞、`3` 暂停、`4` 等待原生请求。`status` 的 exit `0` 仅代表成功读出状态。独立验收是对当前证据的判断，不能表述为零 bug 保证。

## 共享研究工具

所有角色使用同一 `AGENTS.md`、`lab_mem.py`、`lab_init.py`、notes 和 handoffs。读取 source claims 时使用 corpus 并重开原文；精确证据保存在 findings，mem0 只作语义索引。brains 在只读环境先读 `graphify-out/graph.json`；需要 graphify CLI query/build 时交给 worker，因为 CLI 可能初始化 cache。插件不会复制账户连接器；web-only ChatGPT 需要到本机的连接器才能运行 controller。
