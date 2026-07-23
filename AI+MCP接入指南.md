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

> 注：本 testkit 文件夹里平铺的 `mcp_server.py` / `verify_mcp_stdio.py` / `README` / `SKILL` 只是同源副本，供你速览核对。**真正部署请用完整仓库**——clone，或解压 `memory-trigger-v2.7-changed.zip`，解压后里面的 `references/` 才是 MCP server 与依赖所在。

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
python references/write_pipeline_v2.6.py init /你的记忆库路径
```

会生成 `memory.json` / `aliases.json` / `wellness.json` 等文件，记忆库就建好了。

### 4. 最关键的一步：让 AI「主动记」

**光接上 MCP，AI 不会自己记。** 必须把下面这段话写进 AI 的「系统人设 / SOUL / 系统提示」里 —— 这是整套记忆能被用起来的前提：

```
【记忆自觉】你已配置 memory-trigger 长期记忆（MCP 工具 memory_write / memory_search / memory_forget 等）。请主动运用它，不要等用户提醒：
- 用户透露偏好 / 关系 / 重要事件 / 习惯 / 红线 / 身份时，主动调用 memory_write 写入。
- 对话中遇到相关情境，主动调用 memory_search 回想已有记忆。
- 首提先挂起（source=self_inferred 且 confidence<0.8 自动 pending）、二次确认再落盘；用户明说的（source=user_explicit）永远优先、冲突时绝对赢。
- 关系(relationship) / 身份(identity) 类记忆默认 core=true 永不衰减，重要的事大胆钉死。
- 用户说「别提了 / 忘了 X」，调用 memory_forget 做双向遗忘。
```

嫌手抄麻烦？让 AI 直接拉取 MCP 暴露的 `remember_guidance` Prompt，内容就是上面这段，复制进 SOUL 即可。

---

## 验证

装完跑一下端到端自检，看到 `VERIFY_DONE` 就全绿：

```bash
cd references && python3 verify_mcp_stdio.py
# 或指定解释器：MT_PYTHON=/你的python路径 python3 verify_mcp_stdio.py
```

它会真拉起 server 子进程、走完整 stdio 握手，验证 10 个工具 + 1 个 Prompt + 双源信任闭环。

---

## 各客户端 mcp.json 位置

- **Claude Desktop（macOS）**：`~/Library/Application Support/Claude/claude_desktop_config.json`
- **Cursor**：`~/.cursor/mcp.json` 或项目根 `.cursor/mcp.json`
- **Cline（VS Code 插件）**：`~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings.json` 里的 `cline.mcpServers`
- **自研 agent**：看你框架文档，本质是同一份 JSON。

---

## 接上后 AI 能调的 10 个工具

`memory_write` / `memory_search` / `memory_forget` / `memory_stats` / `memory_decay` / `memory_vacuum` / `memory_backup` / `memory_recover` / `memory_wellness` / `memory_init` —— 具体行为见 SKILL.md。

---

## 常见坑

- **AI 不主动记** → SOUL 里没加「记忆自觉」那段，回去加（第 4 步）。
- **MCP 启动报错 / 工具不出现** → `command` 指向的 python 没装 `mcp` 包，重做第 1 步验证。
- **记忆乱窜 / 找不到** → `MEMORY_TRIGGER_REFS_DIR` 没设对，或每次启动路径不一致。
