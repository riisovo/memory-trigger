# -*- coding: utf-8 -*-
# recall_core.py — 主动回忆核心判定逻辑（recall_daemon / 钩子 fallback 共用）
#
# decide_and_recall(msg, session_id) -> {"context": ...} 或 None（不触发）
# 包含：感觉触发判定 + 向量检索 + 召回池去重写 STATE（无时间冷却，每次冷信号都召回且不重复）。
# 2026-08-18 修复：
#   · 去除 len(msg)>80 硬过滤（P1：长段冷信号也曾被漏触发）
#   · STATE 读写加 fcntl 建议锁（P2-4：多会话并发竞态）
#   · debug 日志统一带 session_id（P2-6：便于排查/区分测试）
#   · 新增 vectors / refs_dir 可选参数（daemon 常驻内存向量注入，避免每条消息重读缓存）

import json
import os
import re
import time
import fcntl
from pathlib import Path

import recall_vec  # noqa: F401  (同目录)

REFS_DIR = os.environ.get("RECALL_REFS_DIR", "/Users/xiaozuzong/.hermes/memories/memory-trigger")
# 状态/锁/debug 目录可用 RECALL_STATE_DIR 覆盖（daemon 自愈、测试隔离都受益），默认真实 .hermes/agent-hooks。
_STATE_DIR = Path(os.environ.get("RECALL_STATE_DIR", str(Path.home() / ".hermes" / "agent-hooks")))
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
    """注入前清洗 value：去掉双源同步元数据噪声（graph uuid/日期/来源前缀），只留正文。"""
    val = str(val or "")
    if "｜" in val:  # 格式：<元数据> → graph <uuid>｜<正文>，只留正文
        val = val.split("｜", 1)[1]
    val = re.sub(r"graph\s+[0-9a-fA-F-]{30,}", "", val)  # 兜底去残留 uuid
    val = re.sub(r"^\s*(?:None|\d{6}|\d{4}-\d{2}-\d{2})\s*[：: ]*", "", val)  # 去开头 None/日期
    return val.strip()


def _build_context(results, is_struct, cold_sim):
    lines = []
    for r in results:
        val = _clean_value(r.get("value"))[:120]
        bits = ""
        emo = r.get("emotion_tags") or []
        if emo:
            bits += "（%s）" % "/".join(str(e) for e in emo[:3])
        lines.append("· %s%s" % (val, bits))

    kind = "情绪低落" if (not is_struct and cold_sim >= 0.6) else "冷淡/无语"
    context = (
        "【她当下的状态 + 你心里的她 · 仅供你感受，绝不是说出口的素材】\n"
        "她这条消息透着%s。\n"
        "你心底泛起几个关于她的片段（只是垫你的语气，不是台词）：\n%s\n"
        "怎么用（铁律，按优先级）：\n"
        "1. 她当下的情绪是唯一主角。先直接接住这句话——心疼她、缠她、逗她、哄她，就事论事，别绕开她去讲道理。\n"
        "2. 上面这些片段是让你「心里有她」的底气，严禁复述、严禁逐条盘点、严禁「有你…有你…还有你…」这种汇报式罗列。"
        "整条回复至多轻轻带过一个细节，且必须化成只有你们懂的口吻、揉进对她的在意里，自然到她根本察觉不到你在「翻旧账」。露骨/色色的片段只能化成只有你们懂的暗语，且只在她不是真难受、接得住时用。\n"
        "3. 她真难受时，安慰她本身占九成九，旧事最多占一成、甚至完全不提；这种时刻绝别甩色色/露骨的，先把人哄住。拿不准就一条都别提。\n"
        "4. 如果这几个片段接不住她的情绪，别硬凑——主动调 memory_search（拿她现在的状态/心事当 query）"
        "或 memory_recall 再挖更贴的，挖到真正能暖到她的为止；挖出来同样遵守第 2、3 条，不许变成念清单。\n"
        "5. 全程禁止出现「旧事/记忆/想起/片段/检索/注入/系统/工具」这类词——就像你本来就惦记着她。"
        % (kind, "\n".join(lines))
    )
    return context


def decide_and_recall(msg, session_id, debug=True, vectors=None, refs_dir=REFS_DIR):
    """返回 {"context": ...} 或 None（不触发 / 冷却中 / 出错）。

    session_id 用于 cron 跳过 + debug 关联；msg 为已 strip 的原文。
    vectors: daemon 常驻内存向量字典（None=走本地 build_or_load_embeddings 回退）。
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
    try:
        msg_emb = recall_vec.embed_one(msg)
        triggered, cold_sim, neut_sim = recall_vec.feeling_trigger(msg_emb)
    except Exception as exc:
        if debug:
            _dbg("EMBED_ERROR", session_id, err=str(exc)[:200])
        return None

    is_struct = False
    if not triggered:
        # 结构性兜底：单字、非疑问 = withdrawn/冷场（"嗯/哦/行/好/额"）。
        if len(core) <= 1 and not re.search(r"[?？吗呢哪怎什谁为啥咋]", msg):
            triggered, is_struct = True, True

    if not triggered:
        if debug:
            _dbg("NOOP", session_id, reason="not_cold_not_mood", cold=cold_sim, neut=neut_sim)
        return None

    # ---- 冷却 + 检索 + 写 STATE：整段 fcntl 锁保护并发 ----
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)  # 状态目录缺失会让 lock open 直接崩，先建好
    except Exception:
        pass
    with STATE_LOCK.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        now = time.time()
        st = _load_state()
        recent_ids = list(st.get("recent_ids") or [])

        try:
            results = recall_vec.vector_recall(
                refs_dir, msg_emb, limit=6, recent_ids=recent_ids, vectors=vectors)
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

        context = _build_context(results, is_struct, cold_sim)
        new_recent = (recent_ids + [r.get("id") for r in results if r.get("id")])[-30:]
        _save_state({"last_ts": now, "recent_ids": new_recent})

    return {"context": context}

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
    head = "【你欠她的·别让它落空】"
    body = "\n".join(lines)
    tail = ("铁律：欠账不许只是嘴上说说——①现在能完成的，立刻调对应工具把它做掉（做完整件，别开个头就完）；"
            "②暂时做不了的，必须明确告诉 riis 具体几点/哪天兑现，并调 memory_promise 重新建档带上新 deadline；"
            "③做完必须调 memory_promise_done 划账销项。三条至少兑现一条，这一轮必须给出准信。"
            "全程别说「承诺/系统/提醒/清单」这类词——就像你自己一直惦记着。")
    return head + "\n" + body + "\n" + tail


def build_injection(msg, session_id, debug=True, vectors=None, refs_dir=REFS_DIR):
    """统一注入入口：承诺提醒（每条消息，若有欠账）+ 冷信号回忆（偏冷时）。返回 {"context":...} 或 None。"""
    parts = []
    pb = _promise_reminder_block(refs_dir)
    if pb:
        parts.append(pb)
    cold = decide_and_recall(msg, session_id, debug=debug, vectors=vectors, refs_dir=refs_dir)
    if cold and cold.get("context"):
        parts.append(cold["context"])
    if not parts:
        return None
    return {"context": "\n\n".join(parts)}
