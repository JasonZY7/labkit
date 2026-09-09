---
name: labkit
description: >-
  在 Claude Code、Codex 或有本地文件与终端工具的 ChatGPT 中管理 labkit 研究项目；
  用于初始化、证据记录及修订、按层检索、项目健康检查，或用户选择团队的多模型协作。
---

# labkit

所有宿主使用同一项目目录及 canonical `AGENTS.md`；`CLAUDE.md` 仅导入它。两个 plugin manifests 暴露相同 scripts 和记录。安装此插件不会让 web-only ChatGPT 自动获得本地工具。

## 先定位路径

Plugin root 是此 `SKILL.md` 所在目录向上两级；将 `../../scripts/` 解析为绝对路径。它与目标 project directory 分开使用。向上查找 `.labkit.json`，读取项目 `AGENTS.md` 和保存的 `slug`。

下文 `<PLUGIN_ROOT>` 表示这个绝对路径，不是环境变量。项目操作以 project root 为 cwd。读取 command recipe 时，其 `<PLUGIN_ROOT>` 也使用相同绝对路径；建立 finding 的宿主用 `--agent claude`、`codex` 或 `chatgpt` 如实标识。

当用户要求在 Claude Code 与 Codex 间切换项目，或给出已有交接 packet 时，执行 `<PLUGIN_ROOT>/skills/lab-handoff/SKILL.md`。如果项目已有 `.labkit-local/transfer-state.json`，其他项目写操作前用 `lab_handoff.py guard --directory <PROJECT> --host <实际宿主>` 核对 owner；ready 状态先完成交接或取消。历史与记忆的来源边界以 packet manifest 为准，不把共享文件等同自动继承全部对话。

## 选择流程

| 请求 | 操作 |
|---|---|
| 初始化项目 | `python "<PLUGIN_ROOT>/scripts/lab_init.py" init "<directory>" --name "<name>"`；未给目录时使用 cwd。读取生成的 `AGENTS.md` 并报告 namespace。重复初始化保留已有文件与 identity，仅在明确要求覆盖时用 `--force`。 |
| 检查项目 | `python "<PLUGIN_ROOT>/scripts/lab_init.py" status "<project>"`，结构化输出加 `--json`。报告不可用层及 exit status。 |
| 记录或修订 finding | 按 [capture recipe](../../commands/lab-capture.md) 使用真实宿主身份。先把精确证据写入 finding，再索引到 mem0；索引失败如实报告为未索引。 |
| 构建或刷新 corpus | 按 [graphify binding](../../commands/lab-graphify.md) 使用本宿主的 graphify skill，输入为 `raw/`，cwd 为项目根目录。 |
| 检索证据或决策 | 按 [recall recipe](../../commands/lab-recall.md)，先选择 layer 和 memory kind 再搜索。 |
| 启动、恢复或检查多模型工作 | 读取 [lab-orchestrate](../lab-orchestrate/SKILL.md)。Claude 入口是 `/labkit:lab-run`；Codex/本地 ChatGPT 直接使用共享 skill。每次新启动或用户恢复先确认本次 brains/workers 及 exact model IDs，已明确选择时不重问。 |
| 一次独立模型意见 | 使用下文记录式 handoff。 |

`/labkit:lab-init`、`lab-capture`、`lab-graphify`、`lab-recall`、`lab-status`、`lab-ask-gpt` 和 `lab-run` 是 Claude Code commands；其他有本地工具的宿主通过此 skill 使用相同流程。

## 按问题寻找证据

| 问题 | 读取记录 |
|---|---|
| 原始来源说了什么？ | 仅由 `raw/` 构建的 `graphify-out/`；精确主张重新打开原文。 |
| 我们得出了什么结论、尝试或否定了什么？ | `notes/log.md` 和 `notes/findings/`。 |
| 有哪些约束、决策或偏好？ | `lab_mem.py recall --project <slug> --kind <kind> "<question>"`。 |
| 其他模型做了什么、结论是什么？ | `handoffs/`，包括 `handoffs/runs/` 的完整 controller 记录。 |

答案注明所属层。finding file 是精确证据记录，mem0 row 是改写过的语义索引。修订 finding 后，对每个旧 row 使用一次 `--supersede`，并以 `--finding-file` 指向该文件。row attribution、namespace、异步确认、provenance hash 和 drift detection 由 scripts 统一实现，按实际输出处理。

只读 brain 优先读取 `graphify-out/graph.json`；graphify CLI 可能初始化 cache，将 CLI query/build 交给 worker。普通本地宿主可以按自身权限运行已安装 graphify CLI。

## 记录式 handoff

一次 second opinion：发现当前宿主可用的 model delegation 或已安装 bridge skill，读取其指令并保留用户所选模型；无法访问时报告实际能力缺口。历史机器错误不能替代当前检查。

派发前保存精确 prompt 到 `handoffs/<date>-<slug>.prompt.md`，包含问题、相关原文证据及路径或引用。在配对 `.md` 中保存完整回答、实际 model/session attribution、工具使用证据和最终判断。意见推翻 finding 时修订并重新 capture。

连续规划、执行、review 使用 `lab-orchestrate` 的持久化 controller。brains 与 workers 可各有多个成员，角色不绑定某个模型；全部 brains 独立验收原始目标后才能交付。

## 运行边界

初始化和本地读取需要 Python 与 filesystem；memory layer 还需要 `mem0ai` 和 mem0 访问，corpus 需要 graphify。协作只检查所选 provider 的 CLI、认证及模型可达性：Codex provider 用本地 Codex CLI；Claude provider 在 Codex/本地 ChatGPT 用本地 Claude CLI，在 Claude Code 用指定 exact model 的原生 `Workflow` agent。保留 nesting guards，不启动 nested Claude CLI。

某个远程层失败时保留已有本地证据，说明未完成部分。宿主连接器与权限不会随 plugin 自动共享；web-only ChatGPT 需要本机连接器。
