---
description: 选择一个或多个 brains 和 workers，执行原始目标并由全部 brains 独立验收
---

从本 command/skill 文件所在目录向上查找包含 `.claude-plugin/plugin.json` 或 `.codex-plugin/plugin.json` 的最近祖先目录，作为 plugin root；读取该目录下的 `skills/lab-orchestrate/SKILL.md` 并执行。不要按项目 cwd 或固定的父目录层数解析：Codex 会把 command 转成更深目录中的 skill。该流程由 Claude Code、Codex 和本地 ChatGPT 共用。

每次新启动或用户主动恢复时，先确认本次使用一个还是多个 brains、一个还是多个 workers，以及每个成员的 provider、exact model ID 和用户指定的 effort。用户已在本次请求中明确选择时直接沿用；内部 `submit` 循环不重复询问。此命令不固定当前会话模型。

保留用户的目标项目、原始目标、验收标准、run directory、预算和明确指定的 `--windows-sandbox`。Claude provider 在 Claude Code 内将 controller 生成的整个 `.workflow.json` 原样交给原生 `Workflow`；新请求的 `scriptPath` 指向已内嵌 exact prompt、model、effort 和 agentType 的 `.workflow.js`，无需重构长 prompt。已有 pending input 原样沿用。等待同一 Workflow 完成后提交原始最终文本与真实 transcript。Codex provider 使用本地 Codex CLI。

提交 report 时保留完整最终原文，包括 `Plan` 可能附加的说明/footer，不能只摘 JSON 或加包装；controller 核验原文后再提取结构。`--timeout` 只硬限制本地 CLI 调用，原生 Workflow 的停止与恢复按共享 skill 执行。

保留 `CLAUDECODE` 和 nesting guards，不启动 nested Claude CLI。具体启动、恢复、`submit --artifact --report-file --transcript-file`、取消和交付条件均以共享 skill 为准。
