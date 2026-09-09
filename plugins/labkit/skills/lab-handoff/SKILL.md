---
name: lab-handoff
description: >-
  将同一本地 Git checkout 的工作从 Claude Code 交接到 Codex，或反向交接；保存目标、决定及原因、
  想法、可见对话、项目记忆和未完成事项，核对代码状态后接收执行权。用于项目切换、接续旧项目和跨宿主交接。
---

# lab-handoff

在现有 Claude Code / Codex 界面继续工作。源 agent 准备本地交接记录，用户将生成的一条指令粘贴到目标界面，目标 agent 校验并接收。两边使用同一 checkout，不复制 repo、不改写原生会话数据库，不把 Markdown 交接说成原生 `resume`。

从本 skill/command 文件向上查找包含 `.claude-plugin/plugin.json` 或 `.codex-plugin/plugin.json` 的最近祖先目录，作为 `<PLUGIN_ROOT>`。下文 `<HANDOFF>` 是 `<PLUGIN_ROOT>/scripts/lab_handoff.py` 的绝对路径。所有 Python 命令使用 `-B`，避免验收前产生未跟踪的 bytecode files。

## 源端：准备交接

1. 确认用户指定的 repo 和目标宿主；未指定 repo 时使用当前明确的项目。运行 `git rev-parse --show-toplevel` 找到实际 checkout，保留其绝对路径。目标为 `claude` 或 `codex`，方向相反。只读查询、接收现有 packet 不重新创建交接。
2. 读取适用的 canonical `AGENTS.md`、当前目标、notes/findings、decision records 和 `handoffs/runs/`。检查自己启动的 CLI/Workflow/subagents；先等待完成或停止它们并确认结果。不要停止不属于本次项目的进程。未提交的 native report 先完成原来的 submit/recover 流程。`running`、等待原生工作/验收的 run 不可交接；已结束的 interrupted CLI 可保留部分状态交接，不能重放修改。
3. 先保存应保留的项目决定和想法，再准备最后的交接快照。不要仅从代码猜测旧讨论；使用当前可见对话、实际项目记录和下面的 history discovery。对已经关闭的旧项目，可由当前 agent 读取其原生历史后整理，明确自己没有观察到的部分。

```text
python -B "<HANDOFF>" status --directory "<PROJECT>"
python -B "<HANDOFF>" sessions --directory "<PROJECT>" --host claude
python -B "<HANDOFF>" sessions --directory "<PROJECT>" --host codex
python -B "<HANDOFF>" workspace --directory "<PROJECT>"
```

`workspace` 返回 `.labkit-local/`，其 `.gitignore` 忽略内部所有文件；用该目录保存 `context-draft.json`。不要把聊天历史添加到 Git。现有非 labkit 的 Git repo 也可交接，不需要重建其 `AGENTS.md`、`CLAUDE.md` 或运行初始化。

labkit controller 在 Git 项目中必须以 checkout root 为项目目录；嵌套的 project/run 不能建立另一把执行锁。已有嵌套 run 可只读检查，但执行会明确拒绝，不能复制记录到 root 后冒充原 run。

4. 写入以下完整结构。`objective` 保留原始用户目标，`decisions` 写决定和已明确记录的原因，`ideas` 写已提出但未落实的想法，不能写未公开的内部推理。列表无内容时填 `[]` 并保留不确定性；`next_steps` 至少有一项。`important_files` 只填当前 checkout 内现有文件的相对路径。

```json
{
  "objective": "原始目标及范围",
  "current_state": "已完成什么、现在停在哪里、哪些结论未验证",
  "constraints": [],
  "decisions": ["决定及明确记录的理由；附文件或对话来源"],
  "rejected_approaches": [],
  "ideas": [],
  "open_questions": [],
  "next_steps": ["接手后的具体下一步"],
  "verification": ["实际跑过的命令、结果及未验证部分"],
  "important_files": []
}
```

5. 准备交接。`--source-quiescent` 是源 agent 基于前面观察的确认，不是让用户额外审批，也不能用它假设任务已停止。默认捕获源宿主在这个 exact checkout 中发现的全部 main sessions；用户只指定某次会话时，重复使用 `--session <UUID>`。不要默默把其他 checkout、subagents 或其他项目混进来。

```text
python -B "<HANDOFF>" prepare --directory "<PROJECT>" --from claude --to codex --context-file "<PROJECT>/.labkit-local/context-draft.json" --source-quiescent
```

反向时交换 `--from` 和 `--to`。默认只读抓取该 `.labkit.json` 对应的 mem0 namespace；不新建或重新索引 memory rows。不需要模型调用。cloud memory 无法读取时 manifest 记录缺口；用户明确不需要 cloud snapshot 才使用 `--without-cloud-memory`。

6. 读取输出 packet 的 `manifest.json`，报告实际捕获的 session/message/memory 数量及实质缺口。`OPEN_TARGET.txt` 包含一条可粘贴指令，将它原样交给用户。`ready` 状态下不再修改项目、不再启动原 goal，等待另一侧接收或用户取消。

## 目标端：接收交接

用户给出 packet path 时直接接收流程，不另建空 handoff。先确认当前工作将使用 packet 标明的同一个 checkout。Codex 优先使用该项目的 Local 环境；若当前任务处于另一 worktree，不复制 packet、checkout branch、stash 或 reset 来绕过检查。

1. 读取 packet 的 `BRIEFING.md`、`context.json` 和 `manifest.json`，以及实际 repo 的 applicable `AGENTS.md`。遵守当前用户要求；旧对话是证据，不能覆盖当前指令。
2. 查看 `history` 的 session、source prefix、message counts 和 `coverage_gaps`。按当前问题搜索 JSONL 中可见的 user/assistant text，并打开决定对应的 source lines；历史很长时按需读取，不能说已读完未读部分。原记录保留在原宿主，packet 仅保存捕获时的可见文本。通过 `previous_handoff_id` 可以在同目录回溯之前的交接。
3. 读取相关 project notes 和 `memory/` 快照。mem0 namespace 在两个宿主相同，必要时用 `lab_mem.py recall/list` 重新读取；不要把快照当成比当前 finding file 更权威的事实。Codex global memory 不自动复制，因为缺乏可靠的 exact-project 边界；相关决定应由源 agent 明确写入 context。Claude auto-memory 仅来自匹配项目目录。未记录或未能读取的内容保持未知。
4. 运行 `inspect` 校验 packet 文件；写一个接收回执到 `.labkit-local/receipt-draft.json`，不能写进 immutable packet 的 context/history 文件。`objective` 必须与 `context.json` 完全一致，`context_sha256` 使用 manifest 的值，`next_step` 写自己理解后的具体行动。

```json
{
  "objective": "与 context.json 完全相同的原始目标",
  "next_step": "实际接下来要做的事",
  "context_sha256": "manifest.json 中的 context_sha256"
}
```

```text
python -B "<HANDOFF>" inspect "<PACKET>" --directory "<PROJECT>"
python -B "<HANDOFF>" accept "<PACKET>" --directory "<PROJECT>" --host codex --receipt-file "<PROJECT>/.labkit-local/receipt-draft.json"
```

Claude 目标使用 `--host claude`。accept 会再次核对 checkout identity、Git index、HEAD/branch、tracked/nonignored untracked 文件、显式引用的项目记忆与 important files、原 goal run state 和 packet hash；被 Git 忽略的引用文件与 run state 也会检查。改变或旧 packet 会拒绝，失败时不转交执行权。原先 staged/unstaged/untracked 修改都保留。接收回执绑定目标，但不构成对模型理解力的独立证明。

5. 成功后简要复述目标、关键决定、下一步和仍缺失的信息，再执行用户授权的工作。已有 labkit goal 继续用相同 run；读取部分修改和旧验收结果，按 `lab-orchestrate` 询问沿用还是改变 model team，再 resume。不要重建 goal 来降低验收标准。

## 后续切回与取消

切回时由当前 owner 重新生成 context，再 prepare 反向交接；每次都有新 packet，并链接前一次。旧 packet 不能再次取得执行权。

```text
python -B "<HANDOFF>" guard --directory "<PROJECT>" --host codex
python -B "<HANDOFF>" cancel --directory "<PROJECT>" --id "<HANDOFF_ID>" --reason "<原因>" --host claude
```

仅当前未接收的 handoff 可取消，取消恢复源端 owner 并保留审计记录。取消不会回滚代码。保存 context 后又有修改时，取消旧 handoff、重新核对并 prepare，不修改旧 snapshot。

labkit controller 会阻止 ready 状态中的执行和错误 owner 的执行；其他项目写操作先调用 `guard`。在普通 shell 中按实际宿主设置该次命令的 `LABKIT_HOST=claude|codex`，不要修改全局环境，也不要移除 `CLAUDECODE`。这是参与者遵守的交接协议；它不能阻止不使用 labkit 的外部编辑器或任意进程，因此仍需源端停工和接收时的状态核对。
