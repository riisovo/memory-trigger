# references/ —— 记忆引擎代码

这是 memory-trigger 的**引擎本体**（纯 python3 标准库，零依赖）。完整文档在仓库根目录，这里只放代码 + 一页地图，免得打开看到一堆 `.py` 不知道从哪下手。

## 从哪开始

- **只想用（命令行）** —— 跑 `write_pipeline.py`：
  ```bash
  python3 references/write_pipeline.py write "读书" preference "爱吃科幻小说" references/ --confidence 0.95
  python3 references/write_pipeline.py search "科幻" references/
  python3 references/write_pipeline.py stats references/
  ```
  命令集见根目录 `SKILL.md §1 快速上手`。
- **让 Agent 通过 MCP 调** —— 起 `mcp_server.py`（需先 `pip install -r references/mcp_requirements.txt`），接法见根目录《Agent+MCP接入指南.md》。
- **要「不等你开口就想起你」** —— `hooks/recall_sense.py` + `recall_*.py`（挂宿主 `pre_llm_call`），见根目录 `RECALL.md`。

## 三层接入方式（代码分别属于哪层）

| 层 | 靠什么 | 宿主要求 | 需要改宿主源码吗 |
|---|---|---|---|
| **① 命令行** | 跑 `write_pipeline.py` | 能执行 shell + 有 python3 | 否 |
| **② MCP** | 起 `mcp_server.py`（stdio） | 支持 MCP | 否（只加客户端 `mcp.json` 配置） |
| **③ 主动回忆/情绪检测** | `hooks/recall_sense.py` 挂 `pre_llm_call` | 支持钩子注入 | 是（宿主没有钩子机制就挂不上） |

## 文件地图

| 文件 | 是什么 | 层 | 怎么用 |
|---|---|---|---|
| `write_pipeline.py` | 主引擎 + CLI 入口：写入 / 检索 / 遗忘 / 衰减 / 承诺 / 体检 / vacuum / 自我进化 | ① | `python3 write_pipeline.py <命令> ...`（命令集见 SKILL.md §1） |
| `mcp_server.py` | 把引擎包成 MCP Server（stdio），暴露 23 个工具 + 1 个 Prompt | ② | 客户端 `mcp.json` 指向它；需 `mcp` 依赖 |
| `recall_vec.py` | 主动回忆·向量层：中文向量比「冷 / 热」锚点簇、向量召回 | ③ | 由 `recall_core` / daemon 调用，不直接跑 |
| `recall_core.py` | 主动回忆·判定 + 注入文案（`decide_and_recall` / `build_injection`） | ③ | 同上，不直接跑 |
| `recall_daemon.py` | 常驻守护进程（UNIX socket，模型 / 锚点 / 记忆向量内存常驻） | ③ | 由 `hooks/setup_recall_env.sh` 拉 launchd 启动 |
| `merge_migrate.py` | 一次性「双源合并迁移」：接入时把已有文件记忆 + Agent 自述记忆并成一套干净库 | 工具 | `python3 merge_migrate.py --file-memory A --self-memory B --out DIR` |
| `test_dual_source.py` | 双源信任回归自检（49 例） | 测试 | `python3 test_dual_source.py` |
| `verify_mcp_stdio.py` | MCP 端到端自检（真起 server 走 stdio，验证 23 工具 + Prompt） | 测试 | `cd references && python3 verify_mcp_stdio.py` |
| `aliases.json` | 实体别名表（示例，可改） | 配置 | 被引擎读取 |
| `backend_config.json` | 后端模式配置（默认 `local`，纯本地文件模式） | 配置 | 被引擎读取 |
| `mcp_requirements.txt` | MCP 模式依赖（`mcp[cli]` 带上限） | 配置 | `pip install -r mcp_requirements.txt` |
| `promises.json` | 承诺账本（运行时生成，初始为空） | 运行时 | 引擎自动维护 |
| `__init__.py` | 包标记（空） | — | — |

## 不在本目录的

- 钩子部署脚本 + launchd plist：`../hooks/`（`recall_sense.py` / `setup_recall_env.sh` / `com.agent-recall-daemon.plist`）
- 文档：`../README.md`、`../SKILL.md`、`../RECALL.md`、`../维护指南.md`、`../Agent+MCP接入指南.md`、`../MEMORY_RULES.md`、`../SHARE.md`
- 自检入口：`../install.sh`（一键跑引擎冒烟 + `test_dual_source.py`）

## 数据放哪

**引擎代码（本目录）与记忆数据是分开的。** 数据写在传入的 `refs_dir`（建议单独目录，MCP 模式用 `MEMORY_TRIGGER_REFS_DIR` 指定），核心文件 `memory.json` 是唯一权威源。本目录下的 `memory.json` / `entity_index.json` / `wellness.json` / `preferences.json` / `.wal.jsonl` / `.backup/` / `.archive/` 等均为**运行时产物**，已在根目录 `.gitignore` 忽略，不要提交。
