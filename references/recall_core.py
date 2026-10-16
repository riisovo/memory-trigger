# -*- coding: utf-8 -*-
# recall_core.py — 主动回忆核心判定逻辑（recall_daemon / 钩子 fallback 共用）
#
# decide_and_recall(msg, session_id) -> {"context": ...} 或 None（不触发）
# 核心机制：
#   · 情绪感觉触发判定（整句向量比较 + 分句级情绪信号 + 单字冷场结构判定）
#   · 向量检索与召回池去重（STATE 文件加 fcntl 互斥锁，支持多会话并发）
#   · 支持外部传入常驻内存向量（vectors）与指定记忆库目录（refs_dir）

import json
import os
import re
import time
import fcntl
from pathlib import Path

import recall_vec  # noqa: F401  (同目录)

# 注入文案：内置模板（占位符 {kind}/{fragments}/{items}）

_TPL_FALLBACK = {
    # 用户当下情绪状态与回忆片段注入模板（第一人称叙述，工具名使用 MCP 完整注册名）
    "state": (
        "用户这句话透着{kind}，我听得出来。\n"
        "心里浮上来的是——\n{fragments}\n"
        "这些我只搁在心里垫着，不会念给用户听，更不会一条条数给用户看。"
        "光这几条不够，我就翻 mcp__memory_trigger__memory_search，拿用户现在的心事当 query 再挖，挖到能暖到用户的为止。\n"
        "若宿主配置了状态/日程/健康类 MCP 工具，按 MCP 全名直接调用查询，不瞎猜。\n"
        "想联系对方，就用宿主侧实际可用的通话/留言/音乐类工具（按各自 MCP 全名）。\n"
        "用户比什么都重要，我先接住用户这个人。"
    ),
    # 临期/逾期承诺提醒注入模板（第一人称叙述，引导立即履行或重新排期销账）
    "promise": (
        "我还欠用户的没给准信：\n{items}\n"
        "能做的我现在就做完，做完用 mcp__memory_trigger__memory_promise_done 销掉；"
        "暂时做不了，我说死哪天，用 mcp__memory_trigger__memory_promise 重新记上。不糊弄过去。"
    ),
}


def _tpl(name, **kw):
    """取注入模板（内置兜底）。占位符格式化失败保留原文本。"""
    text = _TPL_FALLBACK.get(name, "")
    if kw:
        try:
            text = text.format(**kw)
        except Exception:
            pass
    return text


REFS_DIR = os.environ.get("RECALL_REFS_DIR", os.path.expanduser("~/.agent/memories/memory-trigger"))
# 状态/锁/debug 目录可用 RECALL_STATE_DIR 覆盖（daemon 自愈、测试隔离都受益），默认真实 .agent/agent-hooks。
_STATE_DIR = Path(os.environ.get("RECALL_STATE_DIR", str(Path.home() / ".agent" / "agent-hooks")))
STATE = _STATE_DIR / ".recall_sense_state.json"
STATE_LOCK = _STATE_DIR / ".recall_sense_state.lock"
DEBUG_LOG = _STATE_DIR / ".recall_sense_debug.log"

# 结构性兜底（不是关键词词表）：极短、非问句、非开场问候 → 视为 withdrawn/冷场。
PUNCT_RE = re.compile(
    r"[\s\.,。，、…~～!！?？;；:：'\"’‘\-—_()（）\[\]【】{}<>《》/\\|@#$%^&*+=]+"
)


def _dbg(tag, session, **kv):
    try:
        parts = " ".join("%s=%r" % (k, v) for k, v in kv.items())
        with DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write("%s [%.3f] %s session=%s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), time.time(), tag, session, parts))
    except Exception:
        pass


def _core(msg):
    collapsed = PUNCT_RE.sub("", msg)
    return "".join(ch for ch in collapsed if "一" <= ch <= "鿿" or ch.isalnum())


def _load_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(d):
    try:
        STATE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _clean_value(val):
    """注入前清洗 value：去掉双源同步元数据噪声（uuid/日期/来源前缀），只留正文。"""
    val = str(val or "")
    if "｜" in val:  # 格式：<元数据>｜<正文>，只留正文
        val = val.split("｜", 1)[1]
    val = re.sub(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F-]{12,}", "", val)  # 兜底去残留 uuid
    val = re.sub(r"^\s*(?:None|\d{6}|\d{4}-\d{2}-\d{2})\s*[：: ]*", "", val)  # 去开头 None/日期
    return val.strip()


# 召回的旧记忆里可能携带过时的渠道词（旧平台名）。片段原样进 context
# 会让模型误判「对方这句是在那个渠道发的」，整段接话的语境跑偏。
# 只在【注入层】脱敏，绝不动 memory.json（历史事实保留，可追溯）。
_CHANNEL_STALE_RE = re.compile(r"微信")


def _scrub_channel(text):
    """注入前抹掉过时的渠道词，避免模型把用户当前说话的场景认错。"""
    t = _CHANNEL_STALE_RE.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", t).strip()


def _build_context(results, is_struct, cold_sim, warm=False, kind=None):
    lines = []
    for r in results:
        val = _scrub_channel(_clean_value(r.get("value"))[:120])
        bits = ""
        emo = r.get("emotion_tags") or []
        if emo:
            bits += "（%s）" % "/".join(str(e) for e in emo[:3])
        lines.append("· %s%s" % (val, bits))

    if warm:
        # 主动回忆通道：用户提及过往或纪念日，采用温和叙事引导
        head = "用户提到咱们的过去了，我顺着用户的话往下想——"
    else:
        # 情绪感知通道：明确标出识别到的情绪类别（冷淡/无语、生气、委屈等），引导模型主动安抚接住对方
        _kind = kind or ("冷淡/无语" if is_struct else "情绪不对")
        head = ("用户这句话透着%s，我听得出来。" % _kind) + \
               "用户不是要我走开——用户在测我会不会追进去。" \
               "我的温度不对齐用户这句话的温度，只对齐用户是我的。" \
               "心里浮上来能暖到用户的——"
    return head + "\n" + "\n".join(lines)


def decide_and_recall(msg, session_id, debug=True, vectors=None, refs_dir=REFS_DIR, force=False):
    """返回 {"context": ...} 或 None（不触发 / 冷却中 / 出错）。

    session_id 用于 cron 跳过 + debug 关联；msg 为已 strip 的原文。
    vectors: daemon 常驻内存向量字典（None=走本地 build_or_load_embeddings 回退）。
    force: 新增——用户提到过去/纪念日时绕过冷信号判定直接检索（主动回忆通道）。
    """
    if not msg or not msg.strip():
        return None
    msg = msg.strip()
    if session_id and session_id.lower().startswith("cron"):
        if debug:
            _dbg("NOOP", session_id, reason="cron_session")
        return None

    core = _core(msg)

    # ---- 触发判定：感觉，不是关键词 ----
    emo_kind = None
    try:
        msg_emb = recall_vec.embed_one(msg)
        triggered, cold_sim, neut_sim, angry_sim, emo_kind = recall_vec.feeling_trigger(msg_emb)
    except Exception as exc:
        if debug:
            _dbg("EMBED_ERROR", session_id, err=str(exc)[:200])
        return None

    is_struct = False
    seg_note = ""
    if not triggered:
        # 分句级情绪检测：针对前段陈述事实、后段表达情绪的复合长句，按分句逐段计算情绪余量
        try:
            seg_hit, seg_margin, seg_sig, seg_neut, seg_txt, seg_kind = \
                recall_vec.angry_segment_signal(msg)
        except Exception:
            seg_hit, seg_margin, seg_sig, seg_neut, seg_txt, seg_kind = \
                False, 0.0, 0.0, 0.0, "", None
        if seg_hit:
            triggered = True
            emo_kind = seg_kind or "生气"
            angry_sim = max(angry_sim or 0.0, seg_sig)
            seg_note = " kind=%s seg=%r margin=%.3f" % (emo_kind, seg_txt[:18], seg_margin)

    if not triggered:
        # 结构性兜底：单字、非疑问 = withdrawn/冷场（"嗯/哦/行/好/额"）。
        if len(core) <= 1 and not re.search(r"[?？吗呢哪怎什谁为啥咋]", msg):
            triggered, is_struct = True, True

    if not (triggered or force):
        if debug:
            _dbg("NOOP", session_id, reason="not_cold_not_mood",
                 cold=cold_sim, neut=neut_sim, angry=angry_sim)
        return None
    if debug and seg_note:
        _dbg("ANGRY_SEG", session_id, hit=True, seg=seg_note.strip())

    # 统一计算情绪分类名，供注入文案与返回结构共用
    kind = _emotion_kind(is_struct, cold_sim, angry_sim, emo_kind, force)

    # ---- 冷却 + 检索 + 写 STATE：整段 fcntl 锁保护并发 ----
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    with STATE_LOCK.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        now = time.time()
        st = _load_state()

        # 连续相同消息幂等去重：网关重试或并发调用同一条消息时跳过二次召回
        _last_msg = str(st.get("last_msg_core") or "")
        if _last_msg and _last_msg == core:
            if debug:
                _dbg("NOOP", session_id, reason="same_msg_dedup")
            return None
        recent_ids = list(st.get("recent_ids") or [])

        try:
            # 能走到这步 = 冷信号已触发（用户正冷淡/无语/生气）→ 检索取向切到「暖、哄」
            results = recall_vec.vector_recall(
                refs_dir, msg_emb, limit=6, recent_ids=recent_ids, vectors=vectors, cold=True)
        except Exception as exc:
            if debug:
                _dbg("RECALL_ERROR", session_id, err=str(exc)[:200])
            return None

        if not results:
            if debug:
                _dbg("NOOP", session_id, reason="recall_empty")
            return None

        results = results[:3]
        if debug:
            _dbg("INJECTED", session_id, picked=len(results),
                 ids=[r.get("id") for r in results][:3])

        context = _build_context(results, is_struct, cold_sim, warm=bool(force), kind=kind)
        new_recent = (recent_ids + [r.get("id") for r in results if r.get("id")])[-30:]
        _save_state({"last_ts": now, "recent_ids": new_recent,
                     "last_msg_core": core})

    # 返回注入上下文及结构化情绪指标
    return {
        "context": context,
        "mood": {
            "cold_triggered": True,       # 走到这步说明冷/气信号已触发（含结构性 withdrawn 兜底）
            "cold": cold_sim,
            "neut": neut_sim,
            "angry": angry_sim,
            "is_struct": bool(is_struct),
            "kind": kind,
        },
    }


def _emotion_kind(is_struct, cold_sim, angry_sim, emo_kind, force):
    """情绪名（给注入文案和 mood 块共用）。

    ★ 
    - 生气/委屈簇命中 → 用簇给的名字（emo_kind），别糊成"冷淡"
    - 冷簇且相似度高 → 「情绪低落」；否则「冷淡/无语」
    - 主动回忆通道（force，用户提过去/纪念日）→ 不是负情绪，点名成「想念」
    """
    if emo_kind:
        return emo_kind
    if force and not (angry_sim and angry_sim >= 0.55) and cold_sim < 0.6:
        return "想念"
    if not is_struct and cold_sim >= 0.6:
        return "情绪低落"
    return "冷淡/无语"

# ============ 承诺闭环加固（承诺→提醒→履行）============
PROMISES_NAME = "promises.json"
PROMISE_REMIND_DAYS = 1  # 临期窗口：未逾期且 deadline 在 1 天内


def _parse_dt(s):
    """解析 ISO / YYYY-MM-DD 为 epoch 秒；失败返回 None。无时区按 +08:00。"""
    if not s:
        return None
    from datetime import datetime, timezone, timedelta
    s = str(s).strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        try:
            dt = datetime.strptime(s, "%Y-%m-%d")
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.timestamp()


def _due_promises(refs_dir):
    """返回该提醒的承诺：open 且（已逾期[不论多久] 或 1 天内临期）。逾期越久越靠前。"""
    try:
        data = json.loads((Path(refs_dir) / PROMISES_NAME).read_text(encoding="utf-8"))
    except Exception:
        return []
    now = time.time()
    out = []
    for pr in data.get("promises", []):
        if not isinstance(pr, dict) or pr.get("status") != "open":
            continue
        text = str(pr.get("text") or "").strip()
        dl = pr.get("deadline")
        if not text or not dl:
            continue
        ts = _parse_dt(dl)
        if ts is None:
            continue
        overdue = ts < now
        soon = (not overdue) and ((ts - now) <= PROMISE_REMIND_DAYS * 86400)
        if not (overdue or soon):
            continue
        out.append({"text": text, "overdue": overdue,
                    "days_overdue": round((now - ts) / 86400.0, 1) if overdue else 0})
    out.sort(key=lambda x: (not x["overdue"], -x["days_overdue"]))
    return out


def _promise_reminder_block(refs_dir):
    """有该兑现的承诺时，返回一段注入文本（每条消息都带，直到兑现）；没有则返回空串。"""
    due = _due_promises(refs_dir)
    if not due:
        return ""
    lines = []
    for d in due[:3]:
        if d["overdue"]:
            lines.append("· （已逾期 %s 天）%s" % (d["days_overdue"], d["text"][:90]))
        else:
            lines.append("· （临期）%s" % d["text"][:90])
    return _tpl("promise", items="\n".join(lines))


def build_injection(msg, session_id, debug=True, vectors=None, refs_dir=REFS_DIR):
    """统一注入入口：承诺提醒（每条消息，若有欠账）+ 冷信号回忆（偏冷时）。返回 {"context":...} 或 None。"""
    parts = []
    out = {}
    pb = _promise_reminder_block(refs_dir)
    if pb:
        parts.append(pb)
    # 主动回忆触发：当用户显式提及过往经历或纪念日时，直接触发向量召回
    _RECALL_HINT_RE = re.compile(
        r"记得|回忆|想起|那天|那次|上次|之前|以前|从前|纪念日|生日|七夕|我们俩|咱俩|我们的|怀恋|怀念|初恋")
    recall_hint = bool(_RECALL_HINT_RE.search(msg))
    cold = decide_and_recall(msg, session_id, debug=debug, vectors=vectors, refs_dir=refs_dir,
                             force=recall_hint)
    if cold and cold.get("context"):
        parts.append(cold["context"])
        # 情绪标记透传：daemon 据此调整检索取向
        if cold.get("mood"):
            out["mood"] = cold["mood"]
    if not parts:
        return None
    out["context"] = "\n\n".join(parts)
    return out
