# Validation — labkit 0.7.0

## 已验证

2026-09-09，Windows / Python 3.12 环境的 **148 项 offline regressions 全部通过**，耗时 694.207 秒。其中包括 21 项 handoff、19 项原生 history、4 项 context integration 和 104 项原有功能检查。

核心 scripts、commands 和 skills 从该版本的已核验发布文件复制；本次公开整理调整分发结构、安装说明与发布 metadata。内部会话、模型 prompts/events、机器路径和历史审查文件未发布。

| 验证 | 已观察到的结果 |
|---|---|
| CLI 多模型团队 | lead 与 advisor/reviewer 各自验收，worker 完成有边界的任务，输出 delivery |
| Claude 原生 Workflow | planner、worker、reviewer 经实际 transcript/model/completion 校验后完成 |
| Codex → Claude → Codex | 三次真实 CLI turn、两次接收回执、连续的交接记录 |
| 历史连续性 | 最后接手者恢复两个分别仅出现在两侧原生用户对话中的测试名称 |
| 项目记忆 | 接手者从项目 memory 文件恢复格式约定；5 个受保护文件的 SHA256 不变 |
| 安装内容 | 原本两端本地安装各 40 个文件与验证源码一致；Codex generated wrappers 也核验 |
| mem0 边界 | 本次 namespace/read-only capture/unavailable 路径由 mocks 与 fixtures 覆盖 |

真实往返使用请求模型 `gpt-5.6-sol` / high 和观察到的 `claude-sonnet-4-6`。Codex JSONL 未提供独立 served-model attestation，因此请求的 model ID 不作为独立身份证明。

## 可复现的检查

在本仓库运行：

```text
python -m pip install -r requirements.txt
python -X utf8 -B plugins/labkit/scripts/lab_selftest.py --offline
```

测试使用隔离 fixtures，无需模型账户或 API key；Node.js 可执行一项额外的 Workflow script 检查，缺少时会跳过。非 Windows 会跳过 Windows 特定的 callback 检查，并需要 `trash` 或 `gio` 来回收临时目录。

## 限制

- 完整 native 模型与跨宿主往返在 Windows 实测；不能将这些结果扩展为所有 macOS/Linux 环境均通过。
- native history 是可见消息导出，不是跨产品原生 resume。tool payloads、hidden reasoning 和 system/developer messages 不导出。
- Claude 分支按原文件顺序保留，不能据此认定只有一个活动分支；storage key 有 cwd collision 或无法验证范围时不复制 auto-memory。
- Codex global memories 没有可靠的 exact-project 边界，未自动复制。cloud memory 不可用会记录缺口。
- 交接 owner guard 约束 labkit 参与者，不能阻止外部编辑器。源端停工和目标 receipt 均需实际 agent 执行；receipt 不证明模型理解了所有历史。
- live mem0 历史检查曾通过 79 项，但此次公开分发没有重新执行该服务测试，也不把它作为本次 live handoff 的 cloud memory 证明。

该文档是脱敏验证摘要；自动测试源码随仓库提供，个人原生会话与项目记录留在本机。
