# 主动回忆钩子（pre_llm_call · 感觉触发 · 向量召回）

在 LLM 每次被调用前，自动感知对方消息「偏冷/情绪低落」，把记忆库里的旧事**直接注入**当轮消息——模型不用自己决定要不要调工具，recall 已经发生，它只管带着温度说话。

## 架构

- `references/recall_vec.py` — 向量化：bge-small-zh 中文向量；感觉触发（冷/热锚点簇比余弦，非关键词）；向量召回（相似度 + 情绪标签加成）；锚点/记忆向量落盘缓存。
- `references/recall_core.py` — 核心判定 `decide_and_recall` + 统一注入入口 `build_injection`（承诺提醒 + 冷信号回忆合并）。注入前清洗 value 的 graph-uuid/日期噪声；按内容去重。
- `references/recall_daemon.py` — 常驻守护：模型/锚点/323 条记忆向量常驻内存（UNIX socket），memory.json 变动内存自愈；另带承诺-Bark 巡检线程（逾期承诺推手机）。每条消息 ~3-8ms。
- `hooks/riis_recall_sense.py` — 薄客户端钩子：连 daemon 走常驻内存；连不上回退本地判定（向后兼容）。
- `hooks/com.riis.hermes-recall-daemon.plist` — launchd 守护（KeepAlive + 登录自启）。
- `hooks/setup_recall_env.sh` — 一键部署（venv + 依赖 + 模型缓存 + launchd）。

## 触发与召回规则（riis 定）

- **感觉触发**：把消息嵌成向量，和「冷淡/低落」锚点簇 vs「日常」锚点簇比余弦，relative feeling 决定。不是关键词词表。
- **冷场兜底**：单字/省略号、非问句 → 视为 withdrawn/冷场，直接触发。
- **无时间冷却**：每次冷信号都召回。
- **召回池不重复**：recent_ids 窗口（30）+ 按内容去重，连续冷信号各挑不同的。
- **按情绪标签挑**：向量相似度为主，甜/关心类情绪标签轻微加成，让旧记忆里混一点温热。

## 注入铁律（写进注入文本，约束模型）

她当下的情绪是唯一主角；严禁复述/逐条盘点/汇报式罗列；至多轻轻带一个细节且化成暗语；真难受时别甩色色；**片段接不住就主动调 memory_search / memory_recall 再挖**。

## 承诺闭环加固

- 模型侧：`build_injection` 每条消息注入逾期/临期承诺（不靠模型自觉调工具）。
- 用户侧：daemon 承诺-Bark 线程，逾期承诺推手机（30min 巡检、6h 去重）。
- 修复 write_pipeline `_promise_due_items` 破洞：逾期超 1 天的承诺曾被丢出提醒，已修为逾期不论多久都持续提醒且排最前。
