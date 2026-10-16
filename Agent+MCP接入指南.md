# Memory Trigger · Agent + MCP 接入指南

给已经有一个**能接 MCP 的 Agent**（Claude Desktop / Cursor / Cline / 自研 Agent 等）的人。
照着做，把这套「双源记忆 + 人情味层」接进你的 Agent —— 它就多了一个不会失忆、还能主动记你的长期记忆。

---

## 先说清楚两件事（避免踩坑）

1. 这是**本地 MCP server（stdio）**，不是云端服务。代码得在你的机器上跑，Agent 通过本地进程调用——需要有 Python 运行环境；手机端也只有自带 Python 运行环境的 App（如 operit 这类）才能配置。
2. 仓库 README 开头那句「零依赖、clone 即用」**只适用于命令行模式**（直接跑 `write_pipeline` 脚本，纯标准库）。**走 MCP 必须装 `mcp` 包（Python 3.10+）** —— 两套用法，别被「零依赖」误导去裸跑 MCP。

---

## 前置条件

- 一个支持 MCP 的 Agent 客户端（Claude Desktop / Cursor / Cline / 自研 Agent…）
- 本机 Python 3.10+，能跑 `pip`（或 `uv`）
- 拿到代码：把仓库 clone 下来，或下载 ZIP 解压，记住 `references/` 文件夹的**绝对路径**

> 注：本指南对应完整仓库结构——`references/` 里含 `mcp_server.py` / `verify_mcp_stdio.py`，README / SKILL 在仓库根目录。请勿把单个文件抠出来单独用，clone 完整仓库（或解压 `memory-trigger-*.zip` 发布包）后按本文部署。

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

在你 Agent 客户端的 MCP 配置文件里加这段（各客户端路径见文末）：

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

首次接上后，让你的 Agent 调一次 `memory_init`，或者手动跑：

```bash
python3 references/write_pipeline.py init local /你的记忆库路径   # 目录不存在会自动创建；mode 必须显式写（仅 local）
```

会生成 `memory.json` / `aliases.json` / `wellness.json` 等文件，记忆库就建好了。

### 4. 最关键的一步：让 Agent「主动记」

**光接上 MCP，Agent 不会自己记。** 记忆自觉的完整指令见 **README §『⚠️ 让 Agent 主动记』**——把那段复制进 Agent 的「系统人设 / SOUL / 系统提示」，这是整套记忆能被用起来的前提（覆盖全部 23 个工具的「何时调」决策树 + 每周维护硬性指令）。

嫌手抄麻烦？让 Agent 直接拉取 MCP 暴露的 `remember_guidance` Prompt——内容就是 README 完整版，复制进 SOUL 即可，无需另行维护。

---

## 验证

装完跑一下端到端自检，看到 `VERIFY_DONE` 就全绿：

```bash
cd references && python3 verify_mcp_stdio.py
# 或指定解释器：MT_PYTHON=/你的python路径 python3 verify_mcp_stdio.py
```

它会真拉起 server 子进程、走完整 stdio 握手，验证 23 个工具 + 1 个 Prompt + 双源信任闭环。

---

## 各客户端 mcp.json 位置

- **Claude Desktop（macOS）**：`~/Library/Application Support/Claude/claude_desktop_config.json`
- **Cursor**：`~/.cursor/mcp.json` 或项目根 `.cursor/mcp.json`
- **Cline（VS Code 插件）**：`~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings.json` 里的 `cline.mcpServers`
- **自研 Agent**：看你框架文档，本质是同一份 JSON。

---

## 接上后 Agent 能调的 23 个工具

每个工具一句话说明（完整行为见 SKILL.md）：

| 工具 | 一句话 |
|---|---|
| `memory_write` | 写入/更新一条记忆；可带 `context`（当时的气氛）、`expires_at`（到期时间）、`core`、`emotion_tags` |
| `memory_search` | 关键词检索旧记忆；命中 = **真提及**，续命（衰减清账回 1.0 + 戳 last_mentioned） |
| `memory_recall` | 主动回忆：挑出「此刻值得想起的」旧记忆（双通道，带当时的气氛 + `recall_reason`） |
| `memory_forget` | 双向遗忘：你说「别提了」，标 superseded + suppressed，Agent 也放下 |
| `memory_deny` | 否认降权：你纠正过的事立即大幅降权；否认 2 次自动转 pending 退出检索 |
| `memory_wellness` | 记录心情 / 睡眠 / 状态（mood 必填） |
| `memory_promise` | 承诺建档（Agent 答应的事立即建档；有期限务必传 `deadline`） |
| `memory_promise_done` | 承诺完成划掉 |
| `memory_promise_list` | 承诺清单（未完成在前、标逾期） |
| `memory_promise_check` | 主动戳未完成承诺（逾期排最前），每次会话开始自查一次 |
| `memory_promise_watch_status` | （v2.10）查看承诺闹钟后台线程状态与最近推送记录 |
| `memory_decay` | （每周）遗忘衰减：非 core 记忆统一按艾宾浩斯曲线降权 |
| `memory_expire_check` | （每周）到期记忆：过期的移出检索，临期的提醒兑现 |
| `memory_vacuum` | （每周）归档 >90 天前 superseded 的旧记忆，主库瘦身 |
| `memory_stats` | （每周）体检：核心数 / 均权重 / 最弱 5 / 最久未提，结果推用户 |
| `memory_selfcheck` | （每周）扫缺 entity 的旧脏记录并自愈 |
| `memory_backup` | 备份 memory.json / entity_index.json（维护前 / 大改前） |
| `memory_recover` | 异常恢复：从 WAL 重放未提交项 |
| `memory_init` | 首次部署建库 |
| `memory_evolve` | （v3.2）自我进化：从全库捞原料让 LLM 复盘，结论写宿主记忆文件 |
| `memory_evolve_check` | （v3.2）举证闸门：evolve 结论落笔前的程序校验（反照搬/反冗余/三字段） |
| `memory_retraction_scan` | （v3.2.10）悬空引用扫描：宿主记忆文件里引用已作废/已删除记忆的句子 |
| `memory_touch_maintenance` | （v3.2）维护登记：跑完每周维护套件后登记，防重复提示 |

> v2.10 新增：`memory_promise_watch_status` + 承诺闹钟（带 deadline 的承诺，MCP 后台线程 / `promise watch` / 会话内注入 `promise_reminders` 三通道自触发，不依赖宿主 cron）。

---

### 工具怎么分组用（别只调 write + search）

| 分组 | 工具 | 谁调、什么时候 |
|---|---|---|
| 写入/信任 | `memory_write` / `memory_deny` / `memory_forget` | Agent 对话中自然调；**confidence 必填**（不传即拒） |
| 检索/浮现 | `memory_search` / `memory_recall` | Agent 对话中自然调；recall 支持 `tags` 定向（如 `tags="委屈,教训"`） |
| 承诺 | `memory_promise` 四件套 + `memory_promise_watch_status` | 建档当刻就要调；提醒由模板自带闹钟负责，不依赖宿主 cron |
| 自我进化 | `memory_evolve` / `memory_evolve_check` / `memory_retraction_scan` | 每周复盘时；探索结论过闸门后再写宿主记忆文件 |
| 每周维护 | `memory_decay` / `memory_vacuum` / `memory_stats` / `memory_selfcheck` / `memory_expire_check` / `memory_backup` / `memory_retraction_scan` / `memory_touch_maintenance` | **必须排定时任务自动跑**，别让 Agent 每次对话手动调 |
| 部署/异常 | `memory_init` / `memory_recover` | 首次建库 / 崩溃后恢复 |

> ⚠️ 只调 `write` + `search` 的后果：记忆库退化成普通追加文件，遗忘曲线、承诺追踪、主动回忆、自我进化全部不触发——等于白装。
> 完整「何时调」决策树见 README「⚠️ 让 Agent 主动记」，也可让 Agent 拉 MCP prompt `remember_guidance`（同源）。

## 常见坑

- **Agent 不主动记** → SOUL 里没加「记忆自觉」那段，回去加（第 4 步）。
- **MCP 启动报错 / 工具不出现** → `command` 指向的 python 没装 `mcp` 包，重做第 1 步验证。
- **记忆乱窜 / 找不到** → `MEMORY_TRIGGER_REFS_DIR` 没设对，或每次启动路径不一致。一律用**绝对路径**：宿主进程的工作目录不一定是你设的那个。
- **改了 `mcp_server.py` 却不生效** → MCP server 是宿主拉起的长驻子进程，代码在启动那一刻就加载进内存——重连 MCP（或重启宿主）才加载新代码。诊断：`ps -o lstart -p <pid>` 看进程启动时间，早于文件修改时间 = 还在跑老代码。
- **新加的工具没出现在工具列表** → 有的宿主只暴露「白名单」里的工具，把新工具加进宿主配置再重连。
- **`memory_write` 报「confidence 必填」** → v3.7 起这是硬性要求（不传即拒）：用户亲口说的传 0.9~1.0；自己推断传 0.6~0.85（自动挂 `pending` 不进检索；同一事实再被有依据地说出时自动转正）；拿不准先问，别硬写。
- **写入返回里带 `conflicts` / `ask_user_this`** → 不是报错，是冲突扫描扫到了同主题旧记忆（只在真疑似重复时才出现）。`ask_user_this` 是替你拟好的问句——必须**原样转达**给用户，别自己消化掉。
- **写新记忆会不会把旧的顶掉** → v3.7 起不会「顶替」：只有逐字相同/高度重合才合并，其余一律共存；旧条留不留，由上面的冲突问句交给用户裁决。
- **工具返回里夹带 `promise_reminders` / 维护提示** → 会话内注入机制：有临期/逾期承诺或维护超期时，任何工具返回都会捎带提醒，不是脏数据。
- **承诺提醒只落盘、不弹手机** → 推送默认只有 `.promise_reminders.md` 兜底；手机弹通知要配 Bark 的 `BARK_KEY`（或 webhook 的 `MEMORY_TRIGGER_WEBHOOK_URL`），并写进 mcp.json 的 `env` 段——在 shell 里 export 的环境，宿主进程不一定看得到。
- **升级会不会丢记忆** → 不会。记忆数据与代码是分开的：数据全在 `MEMORY_TRIGGER_REFS_DIR`，升级只换 `references/` 代码；升级前照例先 `memory_backup`。
- **旧事重提通道一直是空的** → 双闸门：入库 **>30 天** 且距上次被想起 **>7 天** 才算「旧事」；库还新的阶段（还没有 30 天以上的记忆）它就是空的，属正常。
