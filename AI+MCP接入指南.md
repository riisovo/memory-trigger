# Memory Trigger · AI + MCP 接入指南

给已经有一个**能接 MCP 的 AI**（Claude Desktop / Cursor / Cline / 自研 agent 等）的人。
照着做，把这套「双源记忆 + 人情味层」接进你的 AI —— 它就多了一个不会失忆、还能主动记你的长期记忆。

---

## 先说清楚两件事（避免踩坑）

1. 这是**本地 MCP server（stdio）**，不是云端服务。代码得在你的机器上跑，AI 通过本地进程调用，不存在「甩个链接就自动连上」这种事。
2. 仓库 README 开头那句「零依赖、clone 即用」**只适用于命令行模式**（直接跑 `write_pipeline` 脚本，纯标准库）。**走 MCP 必须装 `mcp` 包（Python 3.10+）** —— 两套用法，别被「零依赖」误导去裸跑 MCP。

---

## 前置条件

- 一个支持 MCP 的 AI 客户端（Claude Desktop / Cursor / Cline / 自研 agent…）
- 本机 Python 3.10+，能跑 `pip`（或 `uv`）
- 拿到代码：把仓库 clone 下来，或下载 ZIP 解压，记住 `references/` 文件夹的**绝对路径**

> 注：本 testkit 文件夹里平铺的 `mcp_server.py` / `verify_mcp_stdio.py` / `README` / `SKILL` 只是同源副本，供你速览核对。**真正部署请用完整仓库**——clone，或解压最新的 `memory-trigger-*.zip` 发布包，解压后里面的 `references/` 才是 MCP server 与依赖所在。

---

## 四步接上

### 1. 装依赖

```bash
pip install -r references/mcp_requirements.txt
# 或用 uv：uv pip install -r references/mcp_requirements.txt
```

装完确认一下，不报错就行：

```bash
python -c "import mcp; print('ok')"
```

### 2. 配 mcp.json

在你 AI 客户端的 MCP 配置文件里加这段（各客户端路径见文末）：

```json
{
  "mcpServers": {
    "memory-trigger": {
      "command": "python",
      "args": ["/你的绝对路径/references/mcp_server.py"],
      "env": { "MEMORY_TRIGGER_REFS_DIR": "/你想存记忆的绝对路径" }
    }
  }
}
```

三个值都得改成你自己的绝对路径：

- **`command`**：必须指向**装了 `mcp` 包的那个 python**。用虚拟环境或 uv 的话，直接写 `uv`、`args` 改成 `["run", "/绝对路径/mcp_server.py"]` 更稳。
- **`args`**：填 `mcp_server.py` 的绝对路径。
- **`env.MEMORY_TRIGGER_REFS_DIR`**：记忆库存哪。不填默认用 `references/` 目录（代码和记忆混一起，不推荐）。建议单独建个文件夹，把记忆和代码分开保管。

> 启动失败 90% 是因为 `command` 指向的 python 没装 `mcp` 包。第一步那个 `import mcp` 验证就是为这个。

### 3. 初始化记忆库

首次接上后，让你的 AI 调一次 `memory_init`，或者手动跑：

```bash
python references/write_pipeline.py init local /你的记忆库路径   # 目录不存在会自动创建；mode 必须显式写（local 或 graph）
```

会生成 `memory.json` / `aliases.json` / `wellness.json` 等文件，记忆库就建好了。

### 4. 最关键的一步：让 AI「主动记」

**光接上 MCP，AI 不会自己记。** 记忆自觉的完整指令见 **README §『⚠️ 让 AI 主动记』**——把那段复制进 AI 的「系统人设 / SOUL / 系统提示」，这是整套记忆能被用起来的前提（覆盖全部 19 个工具的「何时调」决策树 + 每周维护硬性指令）。

嫌手抄麻烦？让 AI 直接拉取 MCP 暴露的 `remember_guidance` Prompt——内容就是 README 完整版，复制进 SOUL 即可，无需另行维护。

---

## 验证

装完跑一下端到端自检，看到 `VERIFY_DONE` 就全绿：

```bash
cd references && python3 verify_mcp_stdio.py
# 或指定解释器：MT_PYTHON=/你的python路径 python3 verify_mcp_stdio.py
```

它会真拉起 server 子进程、走完整 stdio 握手，验证 19 个工具 + 1 个 Prompt + 双源信任闭环。

---

## 各客户端 mcp.json 位置

- **Claude Desktop（macOS）**：`~/Library/Application Support/Claude/claude_desktop_config.json`
- **Cursor**：`~/.cursor/mcp.json` 或项目根 `.cursor/mcp.json`
- **Cline（VS Code 插件）**：`~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings.json` 里的 `cline.mcpServers`
- **自研 agent**：看你框架文档，本质是同一份 JSON。

---

## 接上后 AI 能调的 19 个工具

每个工具一句话说明（完整行为见 SKILL.md）：

| 工具 | 一句话 |
|---|---|
| `memory_write` | 写入/更新一条记忆；可带 `context`（当时的气氛）、`expires_at`（到期时间）、`core`、`emotion_tags` |
| `memory_search` | 关键词检索旧记忆；命中即触发遗忘衰减（戳 last_recalled + 降权重） |
| `memory_recall` | 主动回忆：挑出「此刻值得想起的」旧记忆（双通道，带当时的气氛 + `recall_reason`） |
| `memory_forget` | 双向遗忘：你说「别提了」，标 superseded + suppressed，AI 也放下 |
| `memory_deny` | 否认降权：你纠正过的事立即大幅降权；否认 2 次自动转 pending 退出检索 |
| `memory_wellness` | 记录心情 / 睡眠 / 状态（mood 必填） |
| `memory_promise` | 承诺建档（AI 答应的事立即建档；有期限务必传 `deadline`） |
| `memory_promise_done` | 承诺完成划掉 |
| `memory_promise_list` | 承诺清单（未完成在前、标逾期） |
| `memory_promise_check` | 主动戳未完成承诺（逾期排最前），每次会话开始自查一次 |
| `memory_promise_watch_status` | （v2.10）查看承诺闹钟后台线程状态与最近推送记录 |
| `memory_decay` | （每周）遗忘衰减：非 core 记忆统一按艾宾浩斯曲线降权 |
| `memory_expire_check` | （每周）到期记忆：过期的移出检索，临期的提醒兑现 |
| `memory_vacuum` | （每周）归档 >90 天前 superseded 的旧记忆，主库瘦身 |
| `memory_stats` | （每周）体检：核心数 / 均权重 / 最弱 5 / 最久未提，结果推主人 |
| `memory_selfcheck` | （每周）扫缺 entity 的旧脏记录并自愈 |
| `memory_backup` | 备份 memory.json / entity_index.json（维护前 / 大改前） |
| `memory_recover` | 异常恢复：从 WAL 重放未提交项 |
| `memory_init` | 首次部署建库 |

> v2.10 新增：`memory_promise_watch_status` + 承诺闹钟（带 deadline 的承诺，MCP 后台线程 / `promise watch` / 会话内注入 `promise_reminders` 三通道自触发，不依赖宿主 cron）。

---

## 常见坑

- **AI 不主动记** → SOUL 里没加「记忆自觉」那段，回去加（第 4 步）。
- **MCP 启动报错 / 工具不出现** → `command` 指向的 python 没装 `mcp` 包，重做第 1 步验证。
- **记忆乱窜 / 找不到** → `MEMORY_TRIGGER_REFS_DIR` 没设对，或每次启动路径不一致。
