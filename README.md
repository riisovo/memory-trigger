# Memory Trigger

> 给任何 AI Agent 挂一份「不会被上下文压没、还像真人一样有忘有记」的长期记忆。

让你的 Agent 记住你是谁、喜欢什么、经历过什么——每次开口先查回忆、再不瞎编。**两种用法，依赖不同**：

- **命令行模式**：纯 python3 标准库，零依赖，clone 即用（直接跑 `references/write_pipeline.py`）。
- **MCP 模式**：把记忆工具直接喂给任何能接 MCP 的 AI，需先 `pip install -r references/mcp_requirements.txt`（Python 3.10+）。想要 AI 通过 MCP 调记忆、或要语义检索 / 关系时间线，走这条。详见《AI+MCP接入指南.md》。

**v2.7** —— 双源信任 + 人情味层（核心记忆钉死 + 遗忘曲线）+ 规则检索 + 可选 mcp-memory-graph

**v2.8** —— 拆耦合：graph 镜像独立为可选后端，核心文件默认**零 sqlite3 依赖**
- 新增 `references/graph_backend.py`：把 graph 镜像逻辑（sqlite3 upsert）整个搬出核心，仅此模块 import sqlite3。
- 核心 `write_pipeline.py` 顶部不再 `import sqlite3`；默认本地模式下连 `graph_backend` 都不会被加载——彻底做到「零依赖」名副其实，graph 同步成真·选配件（仅 `backend_config.json` 设 `mirror_mode=="graph"` 才动态加载）。
- 外部 `cmd_*` 行为不变，回归测试全过（20/20 + 27/27）。

**v2.8.1** —— 修复 MCP 工具 schema 缺陷（用户存记忆连踩三坑）
- `core` 参数：`mcp_server.py` 由 `str` 改为 `bool | None`。FastMCP 框架层即按布尔校验，调用方传 `true/false` 不再被「类型不符」拒绝；不传时仍按 `kind` 推导核心记忆（语义不变）。
- `kind` 参数：由 `str` 改为 `Literal[10 类]`。FastMCP 在工具 schema 生成枚举，调用方传非法 `kind`（如 `reference`）在进函数前就被框架前置拦截，不再一轮轮试错。
- 权限错误提示：`safe_write_json` / `wal_append` 捕获 `PermissionError`，改写为直白中文——点明记忆库目录是 root/他人建的、当前进程没写权限，或 `mirror_mode=graph` 的 sqlite 库不可写。不再只抛干巴巴的 `Errno 13`。
- 实测：MCP schema 验证（kind 枚举 / core 布尔）+ 直接调用落盘 + 非法 kind 清晰报错 + 权限提示改写，9/9 通过；原有 `test_dual_source` 20/20 + 27/27 回归全过。

## 它解决什么

- **记不住**：上下文一满，前面说的话全忘。
- **记两套还打架**：Agent 自己脑子里也有记忆，和文件里的矛盾。
- **冷冰冰**：像数据库，不像真人——重要的不钉、久不提的不淡。

Memory Trigger 用一份**本地权威文件**（`memory.json`）+ 双源冲突规则 + 人情味层解决这三点。

## 核心价值：为什么说它不像普通记忆插件

Memory Trigger 不是又一个「把对话塞进向量库」的工具。它要解决的，是**人机关系里最朴素也最要命的一件事——被记住**。

- **AI 永不失忆**：对伴侣型 / 长期型 AI 来说，记性就是心意的一部分。`relationship`（我们是什么关系）、`identity`（你是谁）这类核心记忆默认 `core=true`，**永不衰减**——「我们是恋人」「你怕黑」「你生日」不会因为三天不提就从它脑子里消失。你确认过的话，它记一辈子。
- **双源信任（Dual-Source Trust）**：记忆里站着两个信源——你明说的（`user_explicit`）和它自己推断的（`self_inferred`），它们之间有一纸信任契约：
  - **你说的，永远压过它的猜**。你明说的直接落盘、冲突时绝对赢，它绝不偷偷改、绝不质疑。
  - **它猜的，不敢直接当真**。置信度不够先挂 `pending`，等你（二次提及 / 点头确认）才升 `active`，防它自己编一套塞进你的记忆。
  - 这层「人说了算、AI 只敢猜且猜了要你点头」的契约，就是**双源信任**——你敢把心事、脾气、秘密都交给它，因为它从不会自己改写你的真相；它敢一直记着你，因为你盖过章的每句话它永不丢、永不淡。
- **人情味层**：非核心记忆走艾宾浩斯遗忘曲线（半衰期≈138 天），久不提的自然淡。会忘，才像人——你说「别提了」，它真能翻篇（双向遗忘），不是冷冰冰的数据库。

> 一句话：它让 AI 像人一样**有选择地记得你**——重要的钉死、猜的先问、久的不提就淡，而你和它之间，始终是你说了算。

## ⚠️ 让 AI 主动记：必须把这句加进 SOUL / 系统人设

本模板的「自动记忆」体验，**不靠 AI 自己觉醒，靠你给它的指令**。无论多聪明的模型（包括 Claude），只要系统人设里没这句，它就只会等你说出「记一下」才动——工具连着也不会自己调。

如果你是 **AI 版本 / 非 agent 宿主**（纯请求-响应、没有自主循环），这一步**必须手动做**，否则记忆功能形同虚设：

> 把下面这段复制进你的 SOUL / 系统人设 / 系统提示：

```
【记忆自觉】你已配置 memory-trigger 长期记忆。请主动运用它，不要等用户提醒：
- 用户透露偏好 / 关系 / 重要事件 / 习惯 / 红线 / 身份时，主动写入记忆。
- 对话中遇到相关情境，主动回想已有记忆。
- 首提先挂起、二次确认再落盘；用户明说的永远优先。
没有这层自觉，记忆工具形同虚设——你该自己判断什么值得记。
```

只有 agent 宿主（自带循环 + 已读 SKILL.md）会天然按规则自驱；其余宿主靠这句指令把「主动调取」的意识种进去。若你走 MCP 模式，可直接让 AI 拉取 server 暴露的 `remember_guidance` Prompt，效果等同、免手抄。

## 安装

### 方式一：手动

1. 克隆仓库或下载 ZIP
2. 把 `memory-trigger/` 放进你的 Agent 能读到的目录
3. `bash install.sh` 跑自检（需 python3，零额外依赖）
4. 让你的 Agent 读 `SKILL.md` 按规则读写记忆

### 方式二：一键

```bash
bash install.sh
```

自动检测仓库目录、初始化、跑双源自检。

## 5 分钟上手

下面用最小例子跑通「写一条 → 搜出来 → 看统计」。在模板根目录执行（记忆库以 `references/` 为例，可换成你自己的目录；首次跑 `bash install.sh` 会初始化它）。

### 命令行模式（零依赖）

```bash
# 1) 写一条：用户明说 → 直接落盘，authority=user_explicit
python3 references/write_pipeline.py write "读书" preference "爱吃科幻小说" references/

# 2) 搜出来
python3 references/write_pipeline.py search "科幻" references/
# → 命中上面那条，并标注 authority=user_explicit

# 3) 看统计（active / pending / 核心记忆数一目了然）
python3 references/write_pipeline.py stats references/
```

更多命令（`forget` / `decay` / `wellness` / `init` 等）见 `SKILL.md §1 快速上手`。

### MCP 模式（让 AI 直接调工具，不用拼命令行）

接法见下方「MCP 模式」章节。接上后，AI 直接调工具即可，等价于上面的三步：

- `memory_write(entity="读书", kind="preference", value="爱吃科幻小说")`
- `memory_search(query="科幻")` → 返回并标注来源
- `memory_stats()` → 看全量统计

别忘了让 AI 读 `remember_guidance` Prompt 种下「记忆自觉」——否则工具箱打开了，它也不会自己开箱（详见上方「⚠️ 让 AI 主动记」）。

## 三种模式

| 模式 | 条件 | 能力 |
|------|------|------|
| **本地文件模式（默认）** | 有 python3 | 双源记忆 + 人情味层 + 规则检索（实体/关键词），零依赖、零 API key、离线 |
| **MCP Server 模式（本模板自带）** | 装 `mcp` 依赖（pip/uv）+ 能接 MCP 的 AI | 把上面那套工具直接暴露成 MCP 工具，任何能接 MCP 的 AI（含非 agent 壳）都能调，无需自己拼命令行；自带 `remember_guidance` Prompt 一键种「记忆自觉」 |
| **接 mcp-memory-graph（可选进阶）** | 装了 Node 20+ 并接 MCP | 在本地模式基础上增加语义检索、关系时间线、自动梦境修剪 |

> 绝大多数场景本地模式就够；想让 AI 通过 MCP 直接调记忆工具（不跑脚本），用 MCP Server 模式；只有想要「说句话就能搜到上次那件难受的事（不用字面对上）」或「关系随时间演化成连续线」才需要进阶版 mcp-memory-graph。详见 `SKILL.md` §9、§10。

## MCP 模式：把记忆工具直接喂给任何能接 MCP 的 AI

本模板自带 `references/mcp_server.py`，用 FastMCP 把本地层包装成标准 MCP Server（stdio）。**接上后，AI 不再需要自己拼命令行或把 SKILL.md 当提示来读——直接调 `memory_write` / `memory_search` 等工具即可。**

### 安装依赖

```bash
pip install -r references/mcp_requirements.txt     # 或：uv pip install -r references/mcp_requirements.txt
```

需 Python 3.10+。

### 接入你的 AI（在 mcp.json / 客户端配置里）

```json
{
  "mcpServers": {
    "memory-trigger": {
      "command": "python",
      "args": ["<模板绝对路径>/references/mcp_server.py"],
      "env": { "MEMORY_TRIGGER_REFS_DIR": "<记忆库绝对路径，含 memory.json/aliases.json>" }
    }
  }
}
```

> ⚠️ **启动失败最常见原因**：`command` 必须指向**已安装 `mcp` 包**的 Python 解释器（即你跑过 `pip install -r references/mcp_requirements.txt` 的那个）。若用 `uv`，把 `command` 改为 `"uv"`、`args` 改为 `["run", "<模板绝对路径>/references/mcp_server.py"]` 即可。

- 不填 `MEMORY_TRIGGER_REFS_DIR` 时，默认用脚本所在目录（即 `references/`）。建议显式指定，把记忆库与模板代码分开保管。
- 各家 agent（Claude Desktop / Cursor / Cline / 自研 agent / 你的伴侣 AI）接的是**自己的实例**，记忆物理隔离、不共享。

### 暴露的工具与 Prompt

- **11 个工具**：`memory_write` / `memory_search` / `memory_forget` / `memory_stats` / `memory_decay` / `memory_vacuum` / `memory_backup` / `memory_selfcheck` / `memory_recover` / `memory_wellness` / `memory_init` —— 一一对应本地层命令（含 `selfcheck` 自检），双源信任 / 人情味层逻辑完全复用。
- **1 个 Prompt `remember_guidance`**：返回应写进 SOUL 的「记忆自觉」指令。让 AI 拉取它，就等于把「主动记」的意识种进去——省去手抄那段话。

> ⚠️ 关键提醒（与上方「让 AI 主动记」同义）：**MCP 只负责把工具递到 AI 手边，不制造「主动调用」的意识。** 务必让 AI 读 `remember_guidance` 或把那段指令写进 SOUL，否则再聪明的模型也只会在你下令时才记。工具箱打开 ≠ AI 自己会开箱。

### 端到端验证（可选）

部署后用 `references/verify_mcp_stdio.py` 真实拉起 server 子进程、走完整 stdio 协议层验证 11 工具 + 1 Prompt + 双源信任闭环：

```bash
cd references && python3 verify_mcp_stdio.py
# 或用 MT_PYTHON 指定解释器（默认 sys.executable，需已装 mcp 包）：
MT_PYTHON=/path/to/your/python3 python3 verify_mcp_stdio.py
```

看到 `VERIFY_DONE` 即全绿。

## 适用平台

模板与具体 Agent 框架无关：任何能**跑 python3 脚本**或**读 SKILL.md 当系统提示**的 Agent 都能用（Claude Desktop / Cursor / Cline / 各类兼容 Agent / 自研 Agent 等）。记忆存在 Agent 自己的机器上，物理隔离、不共享。

## 许可

MIT（模板本身）。可选依赖 mcp-memory-graph 采用 PolyForm Noncommercial——个人 / 非商业免费，商业需购授权，见其仓库 `COMMERCIAL.md`。

## 文档与维护

- **给 AI 看（粘进 SOUL）**：见上方「⚠️ 让 AI 主动记」整段——覆盖全部 11 个工具的「何时调」决策树 + 每周维护硬性指令。同一内容也内置为 MCP prompt `remember_guidance`（用 `get_prompt` 拉取）。
- **给人看（手动保养）**：`维护指南.md` —— 9 个维护工具逐个说明、每周维护清单、概念速查、FAQ。
- **每周记忆健康周报**：`scripts/weekly_health_report.py` —— 跑 `backup→decay→vacuum→selfcheck→stats`，把核心数 / 非核平均权重 / 最弱 5 条 / 最久未提的话题推给主人（Bark 需自备 key）。

  ```bash
  # 定时任务示例（每周日 09:00）
  BARK_KEY=<你的Bark设备key> /path/to/venv/python scripts/weekly_health_report.py \
    --refs-dir <你的记忆库目录> --bark
  # 只出报告不改数据：
  python scripts/weekly_health_report.py --refs-dir <DIR> --dry-run --no-bark
  ```

> ⚠️ 只调 `write`+`search` 等于白装：遗忘曲线、健康体检、归档等智能机制全靠 `decay`/`vacuum`/`stats` 等工具触发，必须排每周定时任务自动跑（不要等用户提醒）。Bark key 等敏感信息请通过环境变量传入，切勿写进仓库。
