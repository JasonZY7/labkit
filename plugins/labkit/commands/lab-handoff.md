---
description: 在 Claude Code 和 Codex 间交接同一本地项目，保存历史、记忆和决定并核对接收状态
---

从本 command/skill 文件向上查找包含 `.claude-plugin/plugin.json` 或 `.codex-plugin/plugin.json` 的最近祖先目录，读取并执行该 plugin root 下的 `skills/lab-handoff/SKILL.md`。不要使用固定的父目录层数；Codex 会将 command 转成更深目录中的 skill。

用户要求切到另一宿主时，按源端流程保存交接记录并输出一条可粘贴指令；用户给出已有 packet path 时，按目标端流程读取、核对并接收。保留实际 checkout、未提交改动、原始目标、已有 goal runs、决定及理由、想法、历史覆盖范围和 memory namespace。ready 后停止源端工作，目标端接收后先复述理解再继续。

不要重写原生 session 数据库、自动创建 worktree、切换 branch、stash/reset、重复执行已完成的修改，或把交接简报称为完整原生会话迁移。
