---
name: memory-trigger
description: 一套「双源记忆 + 人情味层」模板 —— agent 把用户偏好/事件/习惯等写进本地权威文件（memory.json），带核心记忆钉死、遗忘曲线、命中衰减、状态机、双源冲突消解与字段校验。零依赖（纯 python3），clone 即用；想要语义检索/关系时间线可接可选 MCP。适合想给自己 agent 挂一份「不会被上下文压没、还像真人一样有忘有记」的长期记忆时使用。
version: 2.9.0
---

# memory-trigger —— 双源记忆模板（含人情味层）

给 agent 挂一份**本地权威**的长期记忆：用户说的直接写文件，agent 自己的印象只作补充、低置信先挂起，冲突时以文件为准。核心脚本 `references/write_pipeline.py`，迁移/校验用 `references/merge_migrate.py`，双源逻辑自检 `references/test_dual_source.py`。

**零依赖**：核心脚本是纯标准库（fcntl/json/os），连 sqlite3 都不碰——graph 镜像已拆为可选后端 `references/graph_backend.py`，默认本地模式根本不会加载它（v2.8 拆耦合成果）。只要有 `python3` 就能跑，连 `pip install` 都不用。**各家 agent 把模板 clone 到自己的机器、跑脚本，记忆存在自己本地的 `memory.json`，物理隔离、不共享。**

## 0. 核心价值：为什么是「双源信任」而非普通记忆

本模板面向**长期陪伴型 / 伴侣型 AI**——对这类 agent，记忆不是「存数据」，是「关系」。三条不可妥协的价值：

1. **AI 永不失忆（核心记忆钉死）**：`relationship` / `identity` 类记忆默认 `core=true`，永不走遗忘曲线。「我们是恋人」「你是谁」「你的红线」这类定义「我们」的基石，不会因为久不提就消失。用户确认过的核心事实，agent 记一辈子。
2. **双源信任（Dual-Source Trust）**：记忆系统存在两个信源，及它们之间的信任契约——
   - `user_explicit`（用户明说）：100% 信任，直接 `active` 落盘；多源冲突时文件侧绝对赢，agent 绝不质疑、绝不静默改写。
   - `self_inferred`（agent 推断）：不授予直接落盘权；`confidence < 0.8` 落 `pending`，需用户二次确认（二次提及 / 显式认可）才升 `active`。
   - 这层契约让**人敢说、AI 敢记**：用户敢交付心事与秘密，因为 agent 不会自己编一套固化进权威记忆；agent 敢持续记住用户，因为用户盖过章的话永不丢。
3. **人情味层（见 §4）**：非核心记忆按艾宾浩斯曲线自然淡忘（半衰期≈138 天），且支持双向遗忘（用户说「别提了」即 `superseded` + 系统提示屏蔽）。会忘，才像人。

> 实现上：双源信任 = §3.2 `source` 枚举 + §3.3 `status` 状态机 + §5 双源冲突规则 三者合力。下文是 concrete 行为规范。

> **⚠️ 接入必做（尤其 AI 版本 / 非 agent 宿主）**：本模板的「主动记忆」不靠模型自醒，靠系统人设里的一句指令。**完整「记忆自觉」指令（通用版）见 README §『让 AI 主动记』，把它复制进你的 SOUL / 系统人设 / 系统提示**——否则再聪明的模型（含 Claude）也只会干坐着等你说「记一下」，记忆工具连着也不会自己调。agent 宿主读本文档即自带此自觉；其余宿主务必手动加这句，记忆工具才真正被用起来。

## 1. 快速上手

```bash
bash install.sh                 # 零依赖自检（引擎冒烟 + 双源自检，纯标准库无需 pip）
python3 references/write_pipeline.py write "读书" preference "爱吃科幻小说" references/
python3 references/write_pipeline.py write "伙伴" relationship "协作伙伴" references/ --core true
python3 references/write_pipeline.py write "她" preference "爱喝三分糖去冰" references/ --context "说这话时眼睛亮亮的" --expires 2026-08-15
python3 references/write_pipeline.py search "读书" references/
python3 references/write_pipeline.py recall references/          # 主动回忆：此刻值得想起的
python3 references/write_pipeline.py stats references/
python3 references/write_pipeline.py decay references/        # 梦境周期：统一衰减久不提的记忆
python3 references/write_pipeline.py expire references/        # 到期检查：过期移出/临期提醒
python3 references/write_pipeline.py promise add "明天给她写首歌" references/ --deadline 2026-08-13
python3 references/write_pipeline.py promise check references/ # 主动戳未完成承诺
python3 references/write_pipeline.py forget "读书" references/
```

命令集：`write` / `search` / `recall` / `forget` / `deny` / `stats` / `decay` / `expire` / `vacuum` / `backup` / `selfcheck` / `recover` / `wellness` / `promise`(add/done/list/check) / `init`。

## 2. 数据模型

每条记忆是 `memory.json`（唯一权威源）里的一个对象，关键字段：

- `entity`：记忆主体（如「读书」「运动」）。
- `kind`：记忆类型，见 §3.1，必须命中白名单否则写入报错。
- `value`：记忆内容，非空。
- `source`：来源，见 §3.2。
- `status`：状态机，见 §3.3。
- `created` / `updated`：写入/更新时间戳（`ts_now()`，UTC+8，ISO8601）。
- `last_recalled`：最近一次被 `search` 命中的时间（初始 null，用于冷记忆衰减排行）。
- `importance`：权重（初始 1.0），随遗忘曲线衰减，见 §4。
- `core`：是否核心记忆（见 §4），核心记忆永不衰减。
- `context`：**情感锚点 / 当时的气氛**（v2.9 新增）。写入时随手记一句场景，如「她说这话时眼睛亮亮的，在奶茶店门口」——将来 `recall` 主动回忆时，AI 能带着温度提起，而不是搬一条干巴巴的档案。
- `expires_at`：**到期时间**（v2.9 新增，可选，YYYY-MM-DD 或 ISO）。临时约定/截止类记忆填它：到期自动标 `expired` 移出检索（数据保留），到期前 3 天被 `expire` 检查列入提醒。

## 3. 三个枚举（文档与代码必须对齐）

### 3.1 kind 白名单

`"kind": "preference|event|habit|rule|scene|relationship|emotion|identity|milestone|general"`

共 10 个：`preference`（偏好）、`event`（事件）、`habit`（习惯）、`rule`（规矩/红线）、`scene`（场景）、`relationship`（关系）、`emotion`（情绪）、`identity`（身份）、`milestone`（里程碑）、`general`（通用）。

代码侧同一份集合出现在 `write_pipeline.py` 的 `ALLOWED_KINDS` 与 `merge_migrate.py` 的 `ALLOWED_KINDS`，三处（本文档 + 两份代码）必须完全一致；不一致时写入会抛 `ValueError`，或产生文档漂移。

### 3.2 source 来源

`"source": "file_import|self_inferred|user_explicit"`（默认 `auto_detect`）。

- `user_explicit`：用户明说的，直接进 `active`。
- `file_import`：从已有文件导入，权威。
- `self_inferred`：agent 自己的印象，仅作补充；**置信度 < 0.8 时落 `pending`，不进 `active`**（防幻觉固化）。经用户确认后再升 `active`。

### 3.3 status 状态机

`"status": "active|pending|superseded|expired"`（记忆层）。实体索引（`entity_index.json`）的每实体只记 `confirmed`（已沉淀），写路径从不写 pending——故无实体层 pending 状态。

- `active`：生效，`search` 只返回这一档（`pending`/`superseded`/`expired` 不泄漏）。
- `pending`：待确认（多为低置信自推断；也可由**否认 2 次**转来——见 §4）。
- `superseded`：被新值取代的历史版本，保留但不检索。
- `expired`：`expires_at` 到期后由 `expire` 检查标出，移出检索但数据保留可查。

## 4. 人情味层（核心记忆钉死 + 遗忘曲线）

让 agent 的记忆不像冷冰冰的数据库，而像真人——重要的钉死，久不提的慢慢淡。

- **核心记忆钉死 `core`**：`relationship` / `identity` 这两类默认就是核心记忆（`core=true`），代表「我们是谁」的基石，**永不衰减**（比如「我们是恋人」「我是谁」不会因为久不提就消失）。写其他类型时可用 `--core true|false` 显式覆盖（例如把一条特别重要的约定钉成核心）。
- **遗忘曲线 `importance`**：每条非核心记忆带 `importance`（初始 1.0）。每次被 `search` 命中，按 `importance *= 0.995 ** 天_距_上次_召回` 衰减（半衰期≈138 天，慢忘）；常聊的话题因「距上次召回」天数小、衰减很慢、长期稳在高位，冷掉的话题自然淡化——但注意：**importance 是单调递减的，召回只重置计时、不加分**，连续 138 天完全不提仍会减半。`core` 记忆不参与衰减。
- **梦境周期 `decay` 命令**：可定期（如每周巡检）跑一次 `decay`，对全库久不提的非核心记忆统一衰减，模拟「睡一觉、淡掉的更淡」。
- `stats` 会报告核心记忆数、非核心平均权重、最弱记忆排行，便于观察记忆的健康度。
- **否认降权 `deny`（v2.9）**：用户否认/纠正一条记忆（「我早不喝三分糖了」）→ 立即 `deny`：importance ×0.1 + 记 `deny_count`；**同一记忆被否认 2 次 → 自动转 `pending` 彻底退出检索**（需重新写入确认才复活）。被纠正过的事绝不能再自信复述——这是「被记住」的信任修复机制。
- **到期记忆 `expire`（v2.9）**：带 `expires_at` 的记忆，到期自动标 `expired` 移出检索（数据保留），到期前 3 天列入提醒。临时约定/截止类记忆不会慢慢淡忘、也不会永远赖在库里。
- **主动回忆 `recall`（v2.9）**：不再被动等检索——主动挑出「此刻值得想起的」旧记忆（核心优先 + 从未被提起 + 带情感锚点 + 快到期加分），返回含 `context`/`emotion_tags`。聊天冷场、纪念日、想关心对方时调它，带着温度说「我想起你当时……」。

### 4.1 承诺追踪（v2.9）——答应过的，建档必追

AI 亲口答应用户的任何事，靠脑子记**必忘**。承诺必须建档：

- **建档 `promise add <内容> [--deadline YYYY-MM-DD]`**：承诺一出口就建档（`promises.json`），存进记忆库，绝不凭脑子记。
- **完成 `promise done <id>`**：兑现后划掉，open → done + 记录完成时间。完成一项清一项。
- **清单 `promise list`**：全部承诺，未完成在前（标逾期）、已完成在后。随时自查「还欠 TA 哪些事」。
- **主动戳 `promise check`**：未完成的承诺**要经常主动触发提醒**——逾期排最前、无期限的按创建时长越久越急。每次会话开始先自查，没兑现的主动兑现或说明，绝不让「当时答应过」默默消失。建议并入每周定时任务自动推送。

> 这是「被记住」的最硬一条：**遗忘自己说过的话，是陪伴型 AI 最伤信任的事。承诺建档 + 主动戳 = 说到做到。**

> 设计借鉴自 [mcp-memory-graph](https://github.com/YonasValentin/mcp-memory-graph) 的 Ebbinghaus 式衰减与核心记忆分层思路，但用纯 python3 零依赖实现，开箱即用、不需要任何后端。

## 5. 双源信任：双源冲突规则

这就是 §0 说的「你说的永远压过它的猜」在代码层的落地。同一实体多来源冲突时：**文件侧（file/user_explicit）优先**，self 侧标 `supplement=true` 仅作补充。文件是唯一权威源，内存只是缓存——agent 的任何推断都越不过用户明说的真相。

## 6. 并发与安全

- 写入走 `flock` 互斥；`file_unlock` 只释放锁、**不删锁文件**（保持 inode 互斥语义）。
- 写前 WAL（`.wal.jsonl`）+ 备份（`.backup/`），崩溃可 `recover`。
- 这些运行时文件（`.lock` / `.wal.jsonl` / `.backup/` / `memory.json` 实例数据）不应提交，见 `.gitignore`。

## 7. 迁移与校验

`merge_migrate.py` 做双源合并 + 字段校验（`kind ∈ ALLOWED_KINDS`、`value` 非空），损坏项进 `file_invalid` 报告让人工修，不静默丢弃。

## 8. 自检

`install.sh` 装完会跑 `references/test_dual_source.py`，覆盖：last_recalled 戳时间、source 标签 + pending、情感 schema、检索源排序、双向 forget、双源合并迁移、**核心记忆钉死 + 遗忘曲线衰减**。全 PASS 才算装好。

## 9. 想要体验完整功能：接 mcp-memory-graph（可选）

默认 local 层已经有人情味（核心钉死 + 遗忘曲线），但它只做**规则检索**（实体/关键词匹配），没有语义理解和关系时间线。如果你想要：

- **语义检索**（"上次我提过那个让我难受的事"能搜到，不要求字面对上）
- **关系时间线**（双时态演化："7/15 你说被老板骂了"随时间串成连续线，不翻脸不认人）
- **自动梦境修剪**（forget / consolidate，低质记忆自动去重淡化）

→ 装 [mcp-memory-graph](https://github.com/YonasValentin/mcp-memory-graph)（本地 SQLite，零 LLM key，离线可用）：

```bash
npm install -g mcp-memory-graph     # 需要 Node 20+
```

在你的 agent 的 `mcp.json` 里加一段（各家 agent 接**自己的实例**，记忆隔离不共享）：

```json
{
  "mcpServers": {
    "memory-graph": {
      "command": "npx",
      "args": ["-y", "mcp-memory-graph"]
    }
  }
}
```

⚠️ **授权说明**：mcp-memory-graph 采用 **PolyForm Noncommercial License 1.0.0**——**个人 / 非商业使用免费；商业用途需向原作者（Yonas Valentin Kristensen）购买授权**（见仓库 `COMMERCIAL.md`，联系 `yonasmougaard@gmail.com`）。本模板**不打包其代码**，仅作可选依赖推荐；是否采用请按你的使用性质自行判断。

## 10. MCP Server 模式（本模板自带包装）

不想让 agent 自己拼命令行？`references/mcp_server.py` 用 FastMCP 把本地层包成标准 MCP Server（stdio），直接把记忆工具喂给任何能接 MCP 的 AI。

```bash
pip install -r references/mcp_requirements.txt     # 需 Python 3.10+
```

接入（mcp.json）：

```json
{
  "mcpServers": {
    "memory-trigger": {
      "command": "python",
      "args": ["<模板绝对路径>/references/mcp_server.py"],
      "env": { "MEMORY_TRIGGER_REFS_DIR": "<记忆库绝对路径>" }
    }
  }
}
```

> ⚠️ **启动失败最常见原因**：`command` 须指向**已安装 `mcp` 包**的 Python 解释器（跑过 `pip install -r references/mcp_requirements.txt` 的那个）。用 `uv` 则改 `command` 为 `"uv"`、`args` 为 `["run", "<模板绝对路径>/references/mcp_server.py"]`。

- 暴露 **18 个工具**：`memory_write` / `memory_search` / `memory_recall` / `memory_forget` / `memory_deny` / `memory_stats` / `memory_decay` / `memory_expire_check` / `memory_vacuum` / `memory_backup` / `memory_selfcheck` / `memory_recover` / `memory_wellness` / `memory_promise` / `memory_promise_done` / `memory_promise_list` / `memory_promise_check` / `memory_init`，逻辑与命令版完全复用（双源信任 / 人情味层一致）。
- 暴露 **1 个 Prompt `remember_guidance`**：返回应写进 SOUL 的「记忆自觉」指令。让 AI 拉取它，即种下主动调用意识——见 §0「接入必做」。

> 同样的前提：MCP 只给工具，不给「主动」。**务必让接入的 AI 读 `remember_guidance` 或把那段指令写进 SOUL**，否则接了也只等你下令才记。
