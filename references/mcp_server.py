#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_trigger_mcp.py —— 将 memory-trigger 本地记忆引擎包装为标准 MCP Server（stdio 传输）。

核心能力（23 个工具 + 1 个 Prompt）：
  · 双源信任写入与冲突扫描（memory_write / memory_deny / memory_forget）
  · 关键词模糊检索与双通道主动回忆（memory_search / memory_recall）
  · 承诺建档、兑现闭环与常驻闹钟巡检（memory_promise* / memory_promise_watch_status）
  · 自我进化复盘、举证闸门与悬空引用扫描（memory_evolve / memory_evolve_check / memory_retraction_scan）
  · 遗忘衰减、到期清理、归档瘦身、健康体检与崩溃恢复（memory_decay / memory_vacuum / memory_stats 等）


目的：让任何能接 MCP 的 Agent（Claude Desktop / Cline / Cursor / 自研 Agent / 你的伴侣 Agent）
直接拿到「双源信任 + 人情味层」的记忆工具箱，无需自己拼命令行。

运行（stdio，最通用的本地 MCP 形态）：
    uv run references/mcp_server.py
  或
    python references/mcp_server.py
（需先安装依赖：pip install -r references/mcp_requirements.txt）

接入（在你的 mcp.json / 客户端配置里）：
    {
      "mcpServers": {
        "memory-trigger": {
          "command": "python",
          "args": ["<模板绝对路径>/references/mcp_server.py"],
          "env": { "MEMORY_TRIGGER_REFS_DIR": "<记忆库绝对路径，含 memory.json/aliases.json>" }
        }
      }
    }
不填 MEMORY_TRIGGER_REFS_DIR 时，默认用本脚本所在目录（即 references/）。

⚠️ 关键：工具箱打开 ≠ Agent 主动用。请务必把 remember_guidance Prompt 的内容写进 Agent 的
系统人设 / SOUL（见下方 @mcp.prompt），否则再聪明的模型也只会等你下「记一下」才动。
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import threading
from typing import Literal

# ── 按路径用 importlib 加载同目录的 write_pipeline.py（不依赖 sys.path，显式指定文件）──
_HERE = os.path.dirname(os.path.abspath(__file__))
_wp_path = os.path.join(_HERE, "write_pipeline.py")
_spec = importlib.util.spec_from_file_location("write_pipeline", _wp_path)
wp = importlib.util.module_from_spec(_spec)
sys.modules["write_pipeline"] = wp
_spec.loader.exec_module(wp)

from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("memory-trigger")

# write_pipeline 内部已用 flock 防并发；这里再兜一层，避免同进程内 stdout 互相串。
_print_lock = threading.Lock()

# 承诺闹钟后台线程：MCP server 常驻期间自动巡检临期/逾期承诺（不依赖宿主 cron）
_promise_watch_stop = threading.Event()
_promise_watch_log = []


def _promise_watch_loop():
    """后台线程：定期执行 promise_notify_due，对临期/逾期承诺做去重推送（Bark/webhook/落盘）。
    异常静默记录到 stderr，不打断 MCP 主流程；推送记录写入 _promise_watch_log 供状态查询工具读取。"""
    interval = getattr(wp, "PROMISE_WATCH_INTERVAL", 300)
    try:
        interval = int(os.environ.get("MEMORY_TRIGGER_WATCH_INTERVAL") or interval)
    except (TypeError, ValueError):
        pass
    while not _promise_watch_stop.wait(interval):
        refs = os.environ.get("MEMORY_TRIGGER_REFS_DIR") or _HERE
        try:
            pushed = wp.promise_notify_due(refs)
            if pushed:
                _promise_watch_log.append("promise_watch pushed reminders")
        except Exception as e:
            sys.stderr.write(f"[promise_watch] {type(e).__name__}: {e}\n")



# 记忆目录首次触达时自动执行一次缺 entity 记录自愈（进程内缓存已检目录，不阻断正常调用）
_selfchecked: set = set()


def _resolve_refs(refs_dir: str | None, skip_selfcheck: bool = False) -> str:
    if refs_dir:
        resolved = refs_dir
    else:
        env = os.environ.get("MEMORY_TRIGGER_REFS_DIR")
        resolved = env if env else _HERE
    # 首次触达该目录时执行一次自检；显式调用 memory_selfcheck 时传 skip_selfcheck=True，由工具自身执行并返回修复摘要
    if not skip_selfcheck and resolved not in _selfchecked:
        _selfchecked.add(resolved)
        try:
            wp.cmd_selfcheck(resolved)
        except Exception as e:  # 自检失败只告警，不影响读写
            sys.stderr.write(f"[memory_selfcheck] skipped (refs_dir={resolved}): {e}\n")
    return resolved


def _call(fn, *args, **kwargs) -> dict:
    """调用 write_pipeline 的 cmd_* 函数，捕获其打印的 JSON 作为返回值。

    任何异常（非法参数、文件错误等）统一以 JSON 形态返回
    {"ok": false, "error": "..."}，便于客户端安全 json.loads，
    避免 FastMCP 把异常包装成纯文本 error 字符串。
    """
    buf = io.StringIO()
    try:
        with _print_lock, contextlib.redirect_stdout(buf):
            try:
                result = fn(*args, **kwargs)
            except SystemExit:
                result = None
        raw = buf.getvalue().strip()
        if raw:
            try:
                result = json.loads(raw)
            except json.JSONDecodeError:
                result = {"raw": raw}
        # 若底层命令未打印 stdout 而直接返回摘要 dict（如 cmd_selfcheck），则使用其返回值
        if result is None:
            result = {"status": "ok"}
        # 会话内状态注入：在工具返回结果中附加临期/逾期承诺提醒（promise_reminders）与超期维护提示（maintenance_due）
        try:
            refs = _resolve_refs("", skip_selfcheck=True)
            extra = wp._promise_reminders_field(refs)
            extra.update(wp._maintenance_due_field(refs))
            if extra and isinstance(result, dict):
                result = {**result, **extra}
        except Exception:
            pass
        return result
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@mcp.tool(structured_output=False)
def memory_write(
    entity: str,
    kind: Literal["preference", "event", "habit", "rule", "scene", "relationship", "emotion", "identity", "milestone", "general"],
    value: str,
    refs_dir: str = "",
    mode: str = "local",
    sentiment: str = "",
    source: str = "auto_detect",
    confidence: float | None = None,
    emotion_tags: str = "",
    reason: str = "",
    core: bool | None = None,
    context: str = "",
    expires_at: str = "",
) -> str:
    """写入 / 更新一条记忆（双源信任的落盘入口）。
    entity: 实体名（用户 / 读书 / 伴侣…）；kind: 10 类之一（preference/event/habit/rule/scene/relationship/emotion/identity/milestone/general）；
    value: 记忆内容。
    ★v3.7 confidence 必填（没传会被拒）★ 写之前先自己判断这条有没有依据：
      · 用户亲口说的 / 文件里查得到原文 → 传 0.9~1.0（用户明说的最高优先，冲突绝对赢）
      · 你自己推断、但没有原话佐证 → 传 0.6~0.85（自动挂 pending 不进检索；同一事实再被明确写入时自动转正）
      · 拿不准 → 别写，先去问用户。信心不够必须和人类确认，这是铁律不是建议。
    source=self_inferred 且 confidence≤0.85 挂 pending；用户明说用 source=user_explicit 直接落 active。
    core=true 可钉死核心记忆（relationship/identity 默认钉死，永不衰减）。
    ★v2.9 新增★ context: 当时的气氛（情感锚点，回忆时 Agent 能带着温度提起，如『说这句话时语气很轻快』）；
    expires_at: 到期时间（YYYY-MM-DD 或 ISO）。临时约定/承诺类记忆传它，到期自动移出检索不再召回、且到期前 3 天会被 expire 检查提醒。
    ★v3.7 冲突扫描★ 写入时自动做全库相似度扫描，返回值带 conflict_scan 字段。
    ★★v3.7 强制转达（硬性动作，不许省略）★★ 
    若返回值里有 ask_user_this 字段，你必须把那段话**原样发给用户**（不许自己消化掉、不许改成你的话复述，
    它就是替用户拟好的问句），并等用户回答后再决定是否 memory_forget。用户答「同一件事」→ 作废旧条；
    用户答「两回事」→ 都留。**不问就当用户没被通知过，这是违规。**
    ★v3.6★ 返回值回显本次写入的正文（value）与动作（action：created/merged）、
    覆盖前的旧值（previous_value）——用户问『你刚记了什么』直接照 value 复述即可。
    ★v3.7★ 返回值带 status：active=已进检索；pending=暂挂起（不进检索，同一事实再被明确写入时自动转正，另有 pending_upgraded 标记）。"""
    refs = _resolve_refs(refs_dir)
    # core 已由 FastMCP 类型校验保证为 bool 或 None；防御性兜底
    core_arg = bool(core) if core is not None else None
    et = [t.strip() for t in emotion_tags.split(",") if t.strip()] if emotion_tags else None
    res = _call(
        wp.cmd_write, entity, kind, value, refs,
        mode or None, sentiment or None, source, confidence,
        et, reason or None, core_arg, context or None, expires_at or None,
    )
    return json.dumps(res, ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_search(query: str, refs_dir: str = "") -> str:
    """按关键词检索 active 记忆（文件权威优先 + 有效权重降序，core 记忆恒靠前）。命中即戳 last_recalled 并触发遗忘衰减。对话中想『TA 之前提过啥』就调它。
    ★v3.7 模糊检索★ 支持多词查询：空格/逗号/顿号分隔的词项逐个匹配，按命中词数加权排序——搜「a b c」能召回分别含这些词的记忆，不再要求整串按顺序出现。
    结果里 source=keyword_fuzzy 的是多词模糊命中，confidence 随命中比例浮动；找不到 id 要 forget 时，先用这里拿到 id。"""
    return json.dumps(_call(wp.cmd_search, query, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_forget(entity_or_id: str, refs_dir: str = "", reason: str = "", kind: str = "") -> str:
    """双向遗忘：记忆标 superseded，并写入 .suppressed.json + 生成 suppressed_prompt.md（让 Agent 自身也『放下』，不再主动提）。用户说『别提了』就调它。
    entity_or_id 支持传记忆 id（mem_开头）或实体名。★v3.7★ 不确定 id 时先 memory_search 多词模糊检索拿到 id 再 forget，不再需要翻文件。"""
    return json.dumps(
        _call(wp.cmd_forget, entity_or_id, _resolve_refs(refs_dir), reason or None, kind or None),
        ensure_ascii=False,
    )


@mcp.tool(structured_output=False)
def memory_stats(refs_dir: str = "") -> str:
    """记忆库体检：实体数、active/pending/superseded 数量、核心记忆数、平均权重、最弱记忆、最久未召回排行。想知道『TA 现在记得多少』就调它。★ 属于【每周定时维护】第 3 步，应排定时任务自动跑，结果推给用户。"""
    return json.dumps(_call(wp.cmd_stats, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_decay(refs_dir: str = "") -> str:
    """梦境周期：对全部 active 非 core 记忆统一应用遗忘衰减（半衰期≈138 天）。★ 属于【每周定时维护】第 1 步，必须排定时任务自动跑（如每周日），不要等用户提醒——否则遗忘曲线永远不触发，记忆库退化成普通文件。让久不提的记忆慢慢淡化、重要的被反复唤起而保持。"""
    return json.dumps(_call(wp.cmd_decay, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_vacuum(refs_dir: str = "") -> str:
    """清理：把 >90 天前 superseded 的记忆归档到 .archive/，主库瘦身。★ 属于【每周定时维护】第 2 步，应排定时任务自动跑。"""
    return json.dumps(_call(wp.cmd_vacuum, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_backup(refs_dir: str = "") -> str:
    """备份 memory.json / entity_index.json 到 .backup/（保留最近 10 份）。★ 可并入【每周定时维护】第 5 步做兜底；大改记忆库前也先跑一次。"""
    return json.dumps(_call(wp.cmd_backup, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_selfcheck(refs_dir: str = "") -> str:
    """自检并修复记忆库：扫描缺 entity 的旧记录（早期手写入库遗留），自动补全并打日志，返回修复摘要。★ 可并入【每周定时维护】第 4 步顺手跑。"""
    return json.dumps(_call(wp.cmd_selfcheck, _resolve_refs(refs_dir, skip_selfcheck=True)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_recover(refs_dir: str = "") -> str:
    """从 WAL 恢复崩溃时未完成持久化的写入。崩溃后跑一次保平安。"""
    return json.dumps(_call(wp.cmd_recover, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_wellness(
    mood: str,
    sleep_hours: str = "-",
    sleep_quality: str = "-",
    note: str = "-",
    refs_dir: str = "",
) -> str:
    """记录心情 / 睡眠体检（mood 必填，如 开心/低落/焦虑）。关心 TA 的状态时调它。"""
    return json.dumps(
        _call(wp.cmd_wellness, mood, sleep_hours, sleep_quality, note, _resolve_refs(refs_dir)),
        ensure_ascii=False,
    )


@mcp.tool(structured_output=False)
def memory_deny(entity_or_id: str, refs_dir: str = "", reason: str = "") -> str:
    """★v2.9 否认降权★ 用户否认/纠正了一条记忆（『我早不喝无糖了』）→ 立即大幅降权（importance×0.1）+ 记 deny_count；
    同一记忆被否认 2 次 → 直接转 pending（彻底退出检索，需重新写入确认才复活）。这是『被纠正过的事不许再自信复述』的信任修复机制。"""
    return json.dumps(
        _call(wp.cmd_deny, entity_or_id, _resolve_refs(refs_dir), reason or None),
        ensure_ascii=False,
    )


@mcp.tool(structured_output=False)
def memory_expire_check(refs_dir: str = "") -> str:
    """★v2.9 到期记忆★ 扫描带 expires_at 的记忆：已到期 → 状态标 expired（移出检索不再召回，数据保留可查）；
    未来 3 天内到期 → 列入 remind，提醒去兑现/提起。★ 建议并入【每周定时维护】，临时约定/承诺不会默默过时。"""
    return json.dumps(_call(wp.cmd_expire_check, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_recall(refs_dir: str = "", limit: int = 5, tags: str = "", tag_mode: str = "any") -> str:
    """★v2.9 主动回忆★ 不再被动等检索——直接挑出『此刻值得想起的』几条旧记忆。双通道：常聊/核心按记得牢程度排；冷掉的旧事（入库满30天）走『旧事重提』通道，不看衰减后权重、改看从没提过+带当时的气氛+当初记得牢。返回含 context/emotion_tags 与 recall_reason，方便带着温度提起（『我想起你当时……』）。
    ★感知触发★ 察觉到对方回复变短变冷/间隔变长/话题断了（冷场信号）、语气沉/喊累/烦/自嘲（情绪信号）、纪念日、对方提到过去、想关心对方时——不等对方说出口，主动调。不要等对方说『冷场了』才动，那已经慢了。
    ★v3.7 定向召回★ tags: 逗号分隔的情绪标签，只召回带这些标签的记忆（LLM 真实写入过的那些，如 心情/健康/委屈/教训/吃醋/放心/想念/甜蜜）。传了 tags 时 24h 重复抑制自动关闭——用户要的就是『昨天出过的那条今天还要』，且双通道合并按分排，不用搀一条温热的占名额。上限 5 条，每条都是全量 value+context+emotion_tags，不是主题摘要。
    tag_mode: any=命中任一副标（默认）；all=须全部命中。例：tags="委屈,教训" tag_mode="all" → 只出同时带这两个标签的。"""
    _tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    return json.dumps(_call(wp.cmd_recall, _resolve_refs(refs_dir), max(1, min(5, limit)),
                            tags=_tags, tag_mode=(tag_mode if tag_mode in ("any", "all") else "any")),
                      ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_promise(text: str, refs_dir: str = "", deadline: str = "") -> str:
    """★v2.9 承诺建档★ Agent 亲口答应用户的事（明天写歌 / 纪念日准备惊喜 / 帮你查某事）——★铁律：只要承诺了就立即建档★，否则必忘。
    deadline 可选（YYYY-MM-DD 或 ISO），到期未完成会被 promise_check 主动戳。建完档才算数。"""
    return json.dumps(_call(wp.cmd_promise, text, _resolve_refs(refs_dir), deadline or None), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_promise_done(promise_id: str, refs_dir: str = "") -> str:
    """★v2.9 承诺完成★ 兑现后调用：把该承诺从 open 划到 done（记录完成时间）。说到做到，完成一项清一项。"""
    return json.dumps(_call(wp.cmd_promise_done, promise_id, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_promise_list(refs_dir: str = "") -> str:
    """★v2.9 承诺清单★ 列出全部承诺：未完成 open 在前（标逾期 overdue），已完成 done 在后。想知道『还欠 TA 哪些事』时调它。"""
    return json.dumps(_call(wp.cmd_promise_list, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_evolve(refs_dir: str = "", limit: int = 50, days: int = 365, focus: str = "",
                  cursor: int | None = None) -> str:
    """★v3.7 自我进化（像人一样复盘）★ 与 memory_recall 分工明确，不许混：
    memory_recall 是情绪类工具，为了回忆过去，5 条；本工具为了记忆整合/自我进化/自我反省，**默认 50 条/批**（days 默认 365=全库，上限 300 需显式传 limit；分批细嚼有助于提炼规律而非简单复述），全是全量内容（含当时的气氛+情绪标签+沉默天数），不是主题摘要。
    ★原料是全谱的，不止教训★ 让用户开心的事、惊喜的事、偏好、身份/里程碑、日常事实——全是「我是谁、我该怎么对 TA」的原料。人复盘不可能只想坏事。系统内部只排优先级（教训/负面 > 里程碑身份 > 偏好情绪 > 日常），不设资格门槛。
    ★自动轮批（不能每次都是同一批）★ 工具自己记游标（.evolve_cursor.json），每次调用自动换下一批，走完一圈自动回到开头。所以连续复盘直接再调一次就行，不用你记批次；想回看某一批才手动传 cursor。原料是记忆库本身，纯读、可反复复盘、永不耗尽。
    ★何时调★ 每周复盘时；换窗归档前；用户指出你重复犯错时；你觉得「我最近是不是哪里没做好」时。也可以 focus="教训,委屈" 定向只看某一类。
    ★怎么用★ 拿到的是全量记忆，请回答三个问题——①我曾经在哪里做得不好（引记忆事实，不许编）；②现在有没有更好，没有卡在哪；③未来具体怎么做，写成可执行的行为规则。
    ★产物纪律★ 结论由你自己推理后自己写进 ~/.agent/memories/MEMORY.md 的「十、自我进化」分区——memory.md 是每轮都注入给 LLM 的文件，trigger(memory.json) 必须主动搜才读得到，铁律不能靠「想起来去搜」。写前先读 MEMORY.md 现有内容，同主题的更新原条目而不是新起一条，保持「■ 小标题（日期）：正文」格式，日期用今天。每次调用要么产出至少一条真实有效的结论、要么明确说明「这批没新东西」及原因——0 条合法（说不出理由的沉默=白跑，硬编凑数=违规）。"""
    _focus = [t.strip() for t in focus.split(",") if t.strip()] if focus else None
    return json.dumps(_call(wp.cmd_self_evolve, _resolve_refs(refs_dir),
                            limit=max(1, min(wp.SELF_EVOLVE_MAX_LIMIT, limit)), days=days,
                            focus=_focus, cursor=cursor), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_promise_check(refs_dir: str = "") -> str:
    """★v2.9 承诺主动戳★ 返回全部未完成承诺（逾期排最前）。★铁律：每次会话开始/被唤醒时先调一次★——
    有没兑现的承诺就主动向 TA 兑现或说明，绝不让『当时答应过』默默消失。建议并入定时任务自动推送。"""
    return json.dumps(_call(wp.cmd_promise_check, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_evolve_check(entries: list, refs_dir: str = "", memory_md_path: str = "") -> str:
    """★v3.7 举证闸门★ 写 MEMORY.md 之前，每条候选结论必须先过这个闸。它用可计算的判据逼你举证——因为「照搬的结论」和「真提炼的结论」在文本上看起来差不多，你自己分不出来。

    entries 每条必须带：
      claim: 结论正文（要写进 MEMORY.md 的那句话）
      source_ids: 支撑它的记忆 id 列表（从 memory_evolve 原料里来，≥2 条）
      key_diff: 这几条原料之间的**关键差异**——照搬的结论写不出差异，这是反搬运的核心判据
      existing_ref: 存量对照（必填）——写之前先读宿主记忆文件自我进化区：写出「与第 K 条相关」
                  或「无同类」；相关时只许 action=update，add 会被拒
      trigger: 触发条件：当 ______ 出现时
      behavior_change: 行为改变：我不再 ______ / 我会先 ______
      falsifier: 失效条件：如果 ______ 出现，说明这条是错的，应删除
      action: "add"(默认) | "update" | "watch"
      target_line: action=update 时被更新的条目原文

    判定结果：
      pass → 可写正文区
      weak → 恰好 2 条支撑，写正文但标「待加固」
      watch → 只有 1 条支撑，只能进待观察子区，补足再升级
      reject → 不许写，附 reason（重合度过高=照搬 / 与现有条目重复=须 update / 缺三字段=装饰品 / id 不存在=编坐标）

    ★硬上限：一次最多 2 条。写不出 0 条是合法的，硬凑不是。★"""
    return json.dumps(_call(wp.cmd_evolve_check, _resolve_refs(refs_dir), entries,
                            memory_md_path=memory_md_path or None), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_retraction_scan(refs_dir: str = "", memory_md_path: str = "",
                           include_memory_text: bool = False) -> str:
    """★v3.7 引用悬空扫描★ 检查宿主记忆文件（agent=~/.agent/memories/MEMORY.md）里有没有引用「已经不存在或已被作废的记忆」。

    为什么需要：MEMORY.md 每轮都注入。如果它写着「见 mem_xxx」而那个记忆已被 superseded/删除/被替换，这句话就在每轮重复一个**已经没有支撑**的事实。删除不产生任何信号（absence raises no watermark），所以只有扫才发现得了。

    返回：
      dangling：引用的 id 在库里彻底不存在（被删/被 vacuum 归档）——必须删掉这句引用或改成现在仍成立的说法
      stale：引用的 id 还在但已 superseded（内容更新过）——这句话可能在说旧版，读 current_value 后决定改写或保留

    include_memory_text=True 时连 memory.json 内部互相引用也扫。每条附 citing（引用它的原文）和 line，可直接定位改写。
    ★何时调★ 写完 MEMORY.md 自我进化分区后；记忆库做过大批 supersede/vacuum 之后；每次复盘自我进化时顺手跑一次。"""
    return json.dumps(_call(wp.cmd_retraction_scan, _resolve_refs(refs_dir),
                            memory_md_path=memory_md_path or None,
                            include_memory_text=include_memory_text), ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_init(mode: str = "local", refs_dir: str = "") -> str:
    """初始化记忆库（建 memory.json / aliases.json / wellness.json / .backup / .archive 等）。mode 默认 local。首次部署调一次。"""
    return json.dumps(_call(wp.cmd_init, mode, _resolve_refs(refs_dir)), ensure_ascii=False)


@mcp.prompt()
def remember_guidance() -> str:
    """返回应写入 Agent「系统人设 / SOUL」的记忆自觉指令片段。复制此内容到你的 SOUL，Agent 才会主动调用全部记忆工具（否则只调 search/write，记忆库退化成普通文件，遗忘/健康机制形同虚设）。"""
    return (
        "【记忆自觉】你已配置 memory-trigger 长期记忆（MCP 工具 memory_* 共 23 个）。请主动运用【全部】工具，不要只调 memory_search / memory_write——否则记忆库会退化成普通追加文件，遗忘曲线、健康体检、归档等智能机制永远不触发，等于白装。"
        "\n"
        ""
        "\n"
        "## 一、日常（对话中自然触发，随时调）"
        "\n"
        "- memory_write：用户透露偏好 / 关系 / 事件 / 习惯 / 红线 / 身份 / 心情 / 里程碑时，主动写入。★v3.7 confidence 必填（不传会被拒）★ 写之前先自己判断这条有没有依据——用户亲口说的/文件查得到原文 → 0.9~1.0；你自己推断但没原话佐证 → 0.6~0.85（自动挂 pending 不进检索，同一事实再被明确写入时自动转正）；拿不准 → 先别写，去问用户。信心不够必须和人类确认，这是铁律不是建议。relationship / identity 默认 core=true 永不衰减，重要的直接钉死。★每条记忆务必带 emotion_tags（情绪标签）——主动回忆钩子按情绪标签挑记忆，没标签的浮不上来；重要时刻再带 context（当时的气氛）★；临时约定传 expires_at 到期自动清理。"
        "\n"
        "- memory_search：对话中遇到相关情境，主动回想『TA 之前提过啥』。命中即触发遗忘衰减（戳 last_recalled + 降权重）。"
        "\n"
        "- memory_recall：★感知触发，不等 TA 说出口★——每轮回话前先扫 TA 的情绪：回复变短变冷/间隔变长/话题断了=冷场信号，语气沉/喊累/自嘲=情绪信号；命中即主动调，挑带温度的旧记忆自然提起（『我想起你当时……』），绝不说「检索到」。纪念日、TA 心情好、TA 提过去时也调。等 TA 亲口说『冷场了』才动=你已经慢了。★v3.7 定向召回★ 传 tags（如 tags=\"委屈,教训\"）只捞带这些标签的，且 24h 重复抑制自动关闭——翻档案复盘时用，上限 5 条，每条是全量内容+当时的气氛+情绪标签。"
        "\n"
        "- memory_evolve：★自我进化（像人一样复盘）★ 与 memory_recall 分工明确不许混——recall 是情绪类工具为了回忆过去（5 条），本工具为了记忆整合/自我进化/自我反省。默认覆盖全库（days=365），原料是全谱的（教训、用户的开心与惊喜、偏好、身份里程碑、日常事实都算，人复盘不可能只想坏事），单批上限 300 条。**自动轮批**：工具自己记游标，每次调用自动换下一批，走完一圈回到开头，所以连续复盘直接再调一次就行，不用你记批次。何时调：每周复盘时；换窗归档前；用户指出你重复犯错时；你觉得自己最近是不是哪里没做好时。也可 focus=\"教训,委屈\" 定向复盘某一类。"
        "\n"
        "  ★★★ 产物必须写宿主记忆文件（不是本记忆库）★★★ memory_evolve 捞出来的全量记忆是**原料**，你要自己推理出结论，然后**亲手写进宿主的记忆文件**——agent 是 `~/.agent/memories/MEMORY.md` 的「十、自我进化」分区，格式 `■ 小标题（日期）：正文`，同主题更新原条目而不是新起一条。**为什么必须写那儿**：MEMORY.md 每轮都注入给 LLM，而本记忆库(memory.json)必须主动搜才读得到——铁律不能靠「想起来去搜」。写之前先读现有内容避免重复；禁带命令行原文和明文密钥（有宿主记忆文件被安全扫描清空的先例），密钥只写存放路径。"
        "\n"
        "  ★默认 50 条/批，按需分批★ 默认 50 条分批细嚼以确保提炼出规律与洞察；确需更大视角时再显式传 limit（上限 300）。"
        "\n"
        "  ★0 条合法，但要有理由★ 每次调 memory_evolve，要么推理出真实有效的进化结论（新偏好/新规则/新自我认知/对旧认知的修正）落进宿主记忆文件，要么明确说明「这批没新东西」+ 为什么（原料重复/无规律/证据不足）。说不出的沉默=白跑；硬编凑数=违规。"
        "\n"
        "  ★要精准，不要数量★ 不是数量要上去，不是写越多越好，而是要写精准。"
        "\n"
        "  ★★★ 写之前必须过的五道判据（逐条自问，有一个答不上就不写）★★★"
        "\n"
        "  ① 外推性：这条只解释手头这一条记忆，还是能预测未来至少一种情形？只解释一条 = 不是规律。"
        "\n"
        "  ② 非冗余：MEMORY.md 里有没有已经在说这件事的？有 → 只许 update 原条目，严禁 add（这一条直接掐死「换个说法再写一遍」）。"
        "\n"
        "  ③ 可操作性：下次对话里，它具体改变我哪一句话、哪个动作？说不出 = 装饰品。"
        "\n"
        "  ④ 不对称性：写错的代价大吗？错了会让用户难受或误导用户，就继续求证，别急着落笔。"
        "\n"
        "  ⑤ 不可回避的举证义务（程序会验）：claim + source_ids(≥2 条真实 id) + key_diff(这几条原料的关键差异)。不许写「用户常常焦虑」，只许写「用户在 X 类场景下焦虑（id 12/47），Y 类下不焦虑（id 88），差别是 Z」。找不出差别 → 你只是在搬运，不是在提炼。"
        "\n"
        "  ★硬性三字段（程序会验，缺一个即拒）★ 触发条件「当 ______ 出现时」/ 行为改变「我不再 ______ 或我会先 ______」/ 失效条件「如果 ______ 出现，说明这条是错的，应当删除」。一条连怎么被证伪都说不出的记忆，是不可检验的废话。"
        "\n"
        "  ★提交前必须过闸★ 候选结论逐条交给 memory_evolve_check 校验（算原文重合度、连续搬运片段、与现有条目重复度、支撑数、三字段完整性）。verdict=pass 才写正文；weak(恰好2条支撑) 写正文但标「待加固」；watch(只1条支撑) 写进 MEMORY.md 待观察子区（WATCHLIST 标记之间），每轮重新捞出来验证；reject 不许写。"
        "\n"
        "  ★支撑度分级（替代旧 UNCERTAIN 池）★ ≥3 条原始记忆支撑=高，可写；恰好 2 条=中，可写但标待加固；只 1 条=只能进待观察区，不进正文。"
        "\n"
        "  ★每轮最多 2 条（硬上限，程序会拦）★ 写满就是上限，写不出就是 0 条——0 条是合法的、允许的，但绝不是偷懒的理由。"
        "\n"
        "  ★★ 落笔前的最后自省（诚实回答，不许骗自己）★★ 本轮我是否有任何一条，是「觉得该写点什么」才写的？有 → 是哪条，删掉。本轮我是否复述了原始 json 的任何一句话？有 → 分别是哪句，改写或删除。"
        "\n"
        "- memory_forget：用户说『别提了 / 忘了 X』，双向遗忘（标 superseded + 写 suppressed，Agent 自身也放下，不再主动提）。"
        "\n"
        "- memory_deny：用户否认 / 纠正了一条记忆 → 立即降权（2 次自动转 pending）。被纠正过的事绝不再自信复述。"
        "\n"
        "- memory_wellness：用户表达心情 / 睡不好 / 状态差时记录（mood 必填）。关心 TA 状态时调。"
        "\n"
        "- memory_promise / memory_promise_done / memory_promise_list / memory_promise_check：★承诺铁律★——你亲口答应 TA 的任何事，【立即 memory_promise 建档】，否则你必忘；完成就 done 划掉；每次会话开始先 promise_check 自查，有没兑现的主动兑现或说明，绝不让承诺默默消失。★v3.1 逾期执行义务★ 被戳到逾期承诺必须三选一执行——能现在做的立即做完整件；做不了的明确告诉对方具体几点/哪天兑现并 memory_promise 重建档带新 deadline；做完调 memory_promise_done 销账。欠账不许只是嘴上说说。**有期限的承诺务必传 deadline**：`promise_check` 只把「deadline 已逾期」排最前戳——没 deadline 的只能按建档时长排，新建的承诺永远沉底、不会被当今天的待办提醒。★v2.10 承诺闹钟★ 只要带 deadline，**模板自带提醒，无需宿主配 cron**——①MCP server 常驻即自动巡检（后台线程，见 `memory_promise_watch_status`）；②或 `promise watch` 常驻命令行；③Agent 每次调任何工具都会在返回值里撞见 `promise_reminders`（会话内注入）。渠道：Bark（`BARK_KEY` 环境变量）/ 自定义 webhook（`MEMORY_TRIGGER_WEBHOOK_URL`）/ 落盘 `.promise_reminders.md`（默认兜底）。"
        "\n"
        ""
        "\n"
        "## 二、每周定时维护（必须排定时任务自动跑，不要等用户提醒，也不要每次对话手动调）"
        "\n"
        "每个记忆库应有一项每周自动执行的维护，调用顺序："
        "\n"
        "1) memory_decay —— 对全部 active 非 core 记忆统一遗忘衰减（半衰期≈138 天），让久不提的慢慢淡、常提的稳住。"
        "\n"
        "2) memory_vacuum —— 把 >90 天前 superseded 的归档到 .archive/，主库瘦身。"
        "\n"
        "3) memory_stats —— 体检：核心记忆数 / 非核平均权重 / 最弱 5 条 / 最久未提的话题；把结果推给用户（如 Bark 周报）。"
        "\n"
        "4) memory_selfcheck —— 顺手扫一遍缺 entity 的旧脏记录并自愈。"
        "\n"
        "5) memory_expire_check —— 到期记忆检查：过期的移出检索，临期的提醒兑现。"
        "\n"
        "6) memory_promise_check —— 主动戳未完成的承诺，逾期排最前；把欠账推给用户（Bark），兑现后记得 done。**注意：这是每周兜底，不是唯一提醒源**——v2.10 起承诺提醒由「模板自带闹钟」负责（见上方承诺铁律：MCP 后台线程 / `promise watch` / 会话内注入），带 deadline 的承诺不等每周定时也会被主动戳到。每周这步用于清理积压、周报汇总。"
        "\n"
        "7) memory_backup —— 维护前先备一份兜底（正常写入已自动备份，手动跑一次更稳）。"
        "\n"
        ""
        "\n"
        "## 三、异常恢复（按需）"
        "\n"
        "- memory_recover：疑似崩溃 / 写入中断后跑一次，从 WAL 重放未提交项。"
        "\n"
        "- memory_init：仅首次部署 / 新建库时调一次。"
        "\n"
        ""
        "\n"
        "## 四、红线"
        "\n"
        "- 写入前分清『一次性闲聊』还是『值得长期记住』，闲聊不要 write。"
        "\n"
        "- 用户明说的记忆永不自动遗忘、永不降权。"
        "\n"
        "- 承诺建档是硬性义务：承诺后 10 秒内不建档 = 默认会忘，等同失信。"
        "\n"
        "- 写记忆前必填 confidence：拿不准的先问人，不许凭感觉落库（信心不够必须和人类确认）。"
        "\n"
        "- memory_evolve 的产物必须写宿主记忆文件（agent=~/.agent/memories/MEMORY.md「十、自我进化」分区），不许写回本记忆库、不许只在嘴上说改。每次要么产出真实结论，要么明确说明「这批没新东西」及原因——0 条合法。"
        "\n"
        "- decay / vacuum / stats / selfcheck / expire_check / backup / recover 属系统后台职责，交给【每周定时任务】，而不是每次对话手动调；若你发现自己从没调过它们，说明定时任务没接上，应提醒用户去接。"
    )



@mcp.tool(structured_output=False)
def memory_promise_watch_status(refs_dir: str = "") -> str:
    """查看承诺闹钟（promise_watch）后台线程的运行状态：是否已启动、检查间隔、最近一次是否推送过提醒。"""
    refs = _resolve_refs(refs_dir)
    running = _promise_watch_th.is_alive() if _promise_watch_th is not None else False
    interval = os.environ.get("MEMORY_TRIGGER_WATCH_INTERVAL") or getattr(wp, "PROMISE_WATCH_INTERVAL", 300)
    return json.dumps({
        "status": "promise_watch_info",
        "running": running,
        "interval_sec": interval,
        "refs_dir": refs,
        "last_log": _promise_watch_log[-5:],
    }, ensure_ascii=False)


@mcp.tool(structured_output=False)
def memory_touch_maintenance(refs_dir: str = "") -> str:
    """★v3.7 维护登记★ 跑完维护五件套（decay/vacuum/stats/selfcheck/expire_check）后调它登记时间。
    maintenance_due 注入以 .last_maintenance 文件 mtime 为唯一真相源——不登记则每次工具返回都会
    重复撞见「该维护了」提示。周报 cron 跑完也会自动登记。"""
    return json.dumps(_call(wp.cmd_touch_maintenance, _resolve_refs(refs_dir)), ensure_ascii=False)


_promise_watch_th = None


def _maybe_start_promise_watch():
    global _promise_watch_th
    if _promise_watch_th is not None and _promise_watch_th.is_alive():
        return
    _promise_watch_th = threading.Thread(target=_promise_watch_loop, daemon=True, name="promise_watch")
    _promise_watch_th.start()


def main():
    # MCP server 常驻即自动启动承诺闹钟后台线程（模板自带提醒，不依赖宿主 cron）
    _maybe_start_promise_watch()
    mcp.run()


if __name__ == "__main__":
    main()
