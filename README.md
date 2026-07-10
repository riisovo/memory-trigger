---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 98ae5fdf984949056840134e4af3a50e_ddd132a37c5811f1938f5254006c9bbf
    ReservedCode1: s9zqq6/tIf1MWSpmmNmklOsegXfSPvEAekp2qh2vy32Ln/lXa1F6HtE3krUhmxpjqZ7c1BYVvkHHApGcMfjF1q+9EcooUYICDhb1e030f5Qaz/D9TMYDW2eaSM0M4t1wc6o3BN5fnU3MIoh4gpWdOpx/Wg0khh1/1sru+tA8TScE8Vew4LmhnJjQMjI=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 98ae5fdf984949056840134e4af3a50e_ddd132a37c5811f1938f5254006c9bbf
    ReservedCode2: s9zqq6/tIf1MWSpmmNmklOsegXfSPvEAekp2qh2vy32Ln/lXa1F6HtE3krUhmxpjqZ7c1BYVvkHHApGcMfjF1q+9EcooUYICDhb1e030f5Qaz/D9TMYDW2eaSM0M4t1wc6o3BN5fnU3MIoh4gpWdOpx/Wg0khh1/1sru+tA8TScE8Vew4LmhnJjQMjI=
---

# Memory Trigger

> AI 恋爱 · 人机恋 · AI 伴侣记忆引擎

让你的 Agent 装上一个不会失忆的大脑——记住你是谁、喜欢什么、经历过什么，每次开口先对时、先查回忆、再不瞎编。

**v2.6** — 双源信任优先级 + 情感 schema + 双向遗忘 + 召回追踪 + 合并迁移工具

## 安装

### 方式一：手动安装（推荐，当前可用）

1. 克隆仓库或下载 ZIP
2. 将 `memory-trigger/` 目录复制到 Agent 的 skills 目录
3. 参见 SKILL.md 第九章「首次部署指南」完成记忆后端安装（三选一）和文件初始化

### 方式二：一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/riisovo/memory-trigger/main/install.sh | bash
```

自动检测 skills 目录（Marvis / Claude Desktop），clone 仓库 → 初始化 → 跑自检，一条命令搞定。

## 适用平台

当前路径文档针对 **Marvis**。其他 Agent 框架（Claude Desktop / Cursor / Cline）的 skills 目录和 MCP 配置文件路径不同，请自行调整为对应路径。

## 记忆后端方案

| 方案 | 条件 | 推荐度 |
|------|------|--------|
| Marvis 内置 memorious | Marvis 用户 | ⭐⭐⭐ 零配置 |
| @qianjue/mcp-memory-server | 任何支持 MCP 的 Agent | ⭐⭐ 自托管免费 |
| 纯本地文件模式 | 无法安装 npm | ⭐ 零依赖 |

## 许可

MIT
*（内容由AI生成，仅供参考）*
