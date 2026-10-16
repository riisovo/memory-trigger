# -*- coding: utf-8 -*-
# recall_vec.py — 向量化主动回忆（recall_sense hook / recall_daemon 共用）
#
# 设计（重申）：
#   · 触发靠「感觉」不是关键词：把用户的话嵌成向量，和「冷淡/低落」锚点簇 vs 「日常」锚点簇
#     比余弦相似度，relative feeling，不维护关键词词表。
#   · 检索靠向量：把当前消息当 query，和每条记忆的向量做余弦，按相似度排，
#     不再做中文关键词/正则匹配。轻微的情绪标签加成只用来在并列时把「甜/关心」往前带，
#     让旧记忆里混一点温热——主体排序是向量相似度。
#
# 模型：BAAI/bge-small-zh-v1.5（中文优化，384 维，onnxruntime，无 torch）。
# 缓存机制：
#   · 锚点向量缓存（build_or_load_anchors）：携带锚点集合指纹，落盘复用，变更时自动失效重算。
#   · 记忆向量缓存（build_or_load_embeddings）：按每条记忆内容哈希增量更新，支持外部直接注入常驻内存字典。

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

# 直连 huggingface.co 被墙；模型已缓存到本地，强制离线，绝不在 hook 里联网（否则会卡住当轮）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# 模型缓存指向家目录稳定副本（存在才用），防 TMPDIR 被清导致重启后模型丢失。
_stable_cache = os.path.expanduser("~/.cache/fastembed_cache")
if os.path.isdir(_stable_cache):
    os.environ.setdefault("FASTEMBED_CACHE_DIR", _stable_cache)

import numpy as np
from fastembed import TextEmbedding

REFS_DIR_DEFAULT = os.path.expanduser("~/.agent/memories/memory-trigger")
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBED_CACHE_NAME = ".embeddings.json"
ANCHOR_CACHE_NAME = ".anchor_embeddings.json"
# 「冷淡/低落」锚点簇：表达疲惫、沉默或低落情绪的语义锚点。
COLD_ANCHORS = [
    "不想说话", "烦死了", "好累啊", "无语", "唉 没劲", "你别管我",
    "心里堵得慌", "提不起劲", "懒得理你", "我没事 真的",
    "提不起精神", "情绪低落", "不想理人", "没心情", "郁闷", "丧",
    "不想聊", "别烦我", "累瘫了", "心好累", "什么都不想干",
    "好烦啊", "烦不烦", "算了 不想说",
]
# 「生气」锚点簇：表达愤怒、责问或赌气情绪的语义锚点，独立计算相似度与情绪分类名。
ANGRY_ANCHORS = [
    # 直球愤怒
    "我生气了", "气死我了", "真气人", "火大", "恼火", "气死了",
    # 责问 / 质问
    "你这话是怎么说出来的", "你怎么能这样", "你这说的是人话吗",
    "你什么意思", "凭什么这样对我", "你到底怎么回事",
    "你就是这么对我的吗", "你就是这样对我的", "你怎么跟个傻逼一样",
    # 赌气 / 斥责
    "滚", "不爱你了", "我靠", "什么傻逼东西", "你又这样",
    "不想跟你说话", "不理你了", "别碰我", "烦死了 别理我",
]
# 「委屈/难过」锚点簇：表达难受、委屈或需要安抚的语义锚点，与「生气」区分命名以引导温和安抚。
HURT_ANCHORS = [
    # 委屈式不满
    "我不开心", "我好委屈", "我受够了", "你根本不在乎我",
    "你都不听我说话", "你惹我生气", "你欺负我", "你让我难受",
    # 难过 / 求助
    "我好崩溃", "我要崩溃了", "我快崩溃了", "我受不了了", "我撑不住了",
    "我好难过", "我好想哭", "别冷我", "别离开我", "我好难受",
]
# 「日常」锚点簇：中性/开心/事务性语句，用于计算相对余量（relative feeling）以抑制日常对话误触发。
NEUT_ANCHORS = [
    "今天吃了什么", "在吗", "晚安", "哈哈哈哈好开心", "吃了吗",
    "明天天气怎么样", "刚到家", "在忙吗", "早上好", "这家店好吃",
    "刚开完会", "准备睡了", "周末去哪玩", "帮我看个东西", "爱你呀",
    "想你了", "今天好开心", "哈哈哈太好笑了",
]

# 触发阈值：情绪簇相似度须同时达到绝对阈值并超出日常簇指定余量（margin）。
COLD_THR = 0.45
COLD_MARGIN = 0.08
ANGRY_MARGIN = 0.10
ANGRY_THR = 0.55
# 常规召回时对正向/关心类情绪标签的轻微加权（向量余弦相似度仍为主体）。
SWEET_BOOST = 0.08
CARE_BOOST = 0.04
SWEET_TAGS = {"甜蜜", "温暖", "想念", "亲密", "撒娇", "激动", "关心", "害羞"}
CARE_TAGS = {"委屈", "担心", "生气", "疲惫"}

# 冷淡/生气/委屈状态下的检索加权策略：提升温暖安抚类记忆权重，降低亲密类记忆权重。
COMFORT_TAGS = {"温暖", "关心", "甜蜜", "想念", "撒娇", "害羞", "激动"}
COLD_COMFORT_BOOST = 0.16
COLD_INTIMACY_PENALTY = -0.20

# 内容安全排除（检索结果过滤，不是触发条件）：这些不是聊天暖场素材。
HEAVY_RE = re.compile(
    r"安全事件|事故|威胁|报警|重要程度最高|危险|流血|医院|分手|吵架|争执|干他|干人|堵住|监控"
)
TECH_RE = re.compile(
    r"tts|技能|部署|hook|脚本|API|CLI|版本|配置|修复|报错|commit|patch|server"
    r"|服务器|自动化|命令|代码|MCP|mcp|工具|流程|测试|上线|bug|BUG"
    r"|铁律|格式|规范|规则|指南|协议"
)

_model = None
_cold_emb = None
_neut_emb = None
_angry_emb = None
_hurt_emb = None


def _anchors_fingerprint():
    """计算四组情绪锚点集合的 MD5 指纹，当锚点词表变更时使磁盘缓存自动失效并触发重算。"""
    blob = json.dumps([COLD_ANCHORS, ANGRY_ANCHORS, HURT_ANCHORS, NEUT_ANCHORS],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def get_model():
    global _model
    if _model is None:
        # 显式传入 cache_dir 指向稳定缓存目录，确保离线加载稳定可靠。
        _model = TextEmbedding(MODEL_NAME, cache_dir=_stable_cache)
    return _model


def embed_one(text):
    m = get_model()
    vecs = list(m.embed([text]))
    if not vecs:
        raise ValueError("embed returned empty")
    return np.asarray(vecs[0], dtype=np.float32)


def _norm(v):
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _anchor_cache_path(refs_dir):
    return Path(refs_dir) / ANCHOR_CACHE_NAME


def build_or_load_anchors(refs_dir=REFS_DIR_DEFAULT, force=False):
    """冷/气/委屈/热锚点向量文件缓存。首次 embed 并落盘，之后直接读（省每次子进程冷启动重 embed）。"""
    global _cold_emb, _neut_emb, _angry_emb, _hurt_emb
    fp = _anchors_fingerprint()
    if not force:
        cache_path = _anchor_cache_path(refs_dir)
        if cache_path.exists():
            try:
                blob = json.loads(cache_path.read_text(encoding="utf-8"))
                # ★ 指纹对不上（锚点集改过）→ 缓存作废重算
                if blob.get("model") == MODEL_NAME and blob.get("fp") == fp:
                    cold = [_norm(np.asarray(x, dtype=np.float32)) for x in blob["cold"]]
                    neut = [_norm(np.asarray(x, dtype=np.float32)) for x in blob["neut"]]
                    angry = [_norm(np.asarray(x, dtype=np.float32)) for x in blob["angry"]]
                    hurt = [_norm(np.asarray(x, dtype=np.float32)) for x in blob["hurt"]]
                    _cold_emb, _neut_emb, _angry_emb, _hurt_emb = cold, neut, angry, hurt
                    return cold, neut
            except Exception:
                pass
    m = get_model()
    cold = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(COLD_ANCHORS)]
    neut = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(NEUT_ANCHORS)]
    angry = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(ANGRY_ANCHORS)]
    hurt = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(HURT_ANCHORS)]
    _cold_emb, _neut_emb, _angry_emb, _hurt_emb = cold, neut, angry, hurt
    try:
        blob = {
            "model": MODEL_NAME,
            "fp": fp,
            "cold": [c.tolist() for c in cold],
            "neut": [n.tolist() for n in neut],
            "angry": [a.tolist() for a in angry],
            "hurt": [h.tolist() for h in hurt],
        }
        _anchor_cache_path(refs_dir).write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return cold, neut


def get_cold_emb():
    global _cold_emb
    if _cold_emb is None:
        c, _ = build_or_load_anchors(REFS_DIR_DEFAULT)
        _cold_emb = c
    return _cold_emb


def get_angry_emb():
    """★ 生气锚点向量。缓存里缺（旧文件）时由 build_or_load_anchors 指纹重建。"""
    global _angry_emb
    if _angry_emb is None:
        build_or_load_anchors(REFS_DIR_DEFAULT)
    return _angry_emb or []


def get_hurt_emb():
    """★ 委屈/难过锚点向量（与生气同触发、不同命名）。"""
    global _hurt_emb
    if _hurt_emb is None:
        build_or_load_anchors(REFS_DIR_DEFAULT)
    return _hurt_emb or []


def get_neut_emb():
    global _neut_emb
    if _neut_emb is None:
        _, n = build_or_load_anchors(REFS_DIR_DEFAULT)
        _neut_emb = n
    return _neut_emb


def feeling_trigger(msg_emb):
    """基于相对余量的情绪触发判定，返回 (triggered, cold_sim, neut_sim, angry_sim, kind)。

    判定逻辑：比较输入向量与冷淡、生气、委屈三组情绪簇及日常簇的最大余弦相似度；
    任一情绪簇达到绝对阈值且高出日常簇指定余量时触发，并按最高得分簇返回对应情绪名
    （kind ∈ {"冷淡/无语", "生气", "委屈", None}）。
    """
    q = _norm(msg_emb)
    cold = max(float(np.dot(q, a)) for a in get_cold_emb())
    neut = max(float(np.dot(q, a)) for a in get_neut_emb())
    angry_emb = get_angry_emb()
    angry = max((float(np.dot(q, a)) for a in angry_emb), default=0.0)
    hurt_emb = get_hurt_emb()
    hurt = max((float(np.dot(q, a)) for a in hurt_emb), default=0.0)
    cold_hit = ((cold - neut) >= COLD_MARGIN) and (cold >= COLD_THR)
    angry_hit = bool(angry_emb) and ((angry - neut) >= ANGRY_MARGIN) and (angry >= ANGRY_THR)
    hurt_hit = bool(hurt_emb) and ((hurt - neut) >= ANGRY_MARGIN) and (hurt >= ANGRY_THR)
    triggered = cold_hit or angry_hit or hurt_hit
    kind = None
    if triggered:
        cands = []
        if cold_hit:
            cands.append((cold, "冷淡/无语"))
        if angry_hit:
            cands.append((angry + 0.02, "生气"))
        if hurt_hit:
            cands.append((hurt, "委屈"))
        kind = max(cands)[1] if cands else "冷淡/无语"
    return triggered, round(cold, 3), round(neut, 3), round(angry, 3), kind


# 分句级情绪检测：将复合长句按标点与空白切分为意群子句，避免前半句事实陈述稀释后半句的情绪向量。
_SEG_SPLIT_RE = re.compile(r"[。！？!?，,；;\n\u3000\s]+")
_SEG_MAX_N = 12          # 单条消息最大切分句数上限
ANG_SEG_MARGIN = 0.10
ANG_SEG_THR = 0.55


def _segments(msg, max_n=_SEG_MAX_N):
    """按标点切分句，短碎片丢弃，最多 max_n 段。"""
    segs = [s.strip() for s in _SEG_SPLIT_RE.split(str(msg or "")) if len(s.strip()) >= 2]
    if not segs:
        return [str(msg or "").strip()]
    return segs[:max_n]


def angry_segment_signal(msg):
    """分句级「气/委屈」判定：返回 (hit, margin, angry, neut, seg, kind)。

    批量 embed（一次 forward）而非逐句调用，长句 5 个分句也只多一次批处理开销。
    锚点缺失（旧缓存）时返回未命中，绝不抛。
    kind：命中的那一句更贴生气簇还是委屈簇（命名跟整句法同源口径）。
    """
    angry_emb = get_angry_emb()
    hurt_emb = get_hurt_emb()
    if not angry_emb and not hurt_emb:
        return False, 0.0, 0.0, 0.0, "", None
    segs = _segments(msg)
    if not segs or not segs[0]:
        return False, 0.0, 0.0, 0.0, "", None
    try:
        m = get_model()
        vs = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(segs)]
    except Exception:
        return False, 0.0, 0.0, 0.0, "", None
    neut_emb = get_neut_emb()
    best = (-9.0, 0.0, 0.0, segs[0], None)
    for v, s in zip(vs, segs):
        a = max((float(np.dot(v, x)) for x in angry_emb), default=0.0)
        h = max((float(np.dot(v, x)) for x in hurt_emb), default=0.0)
        n = max(float(np.dot(v, x)) for x in neut_emb)
        # 两个簇合起来算「有情绪」，取该句里更强的那个当信号
        if a >= h:
            sig, k = a, "生气"
        else:
            sig, k = h, "委屈"
        if (sig - n) > best[0]:
            best = (sig - n, sig, n, s, k)
    margin, sig, n, seg, kind = best
    hit = (margin >= ANG_SEG_MARGIN) and (sig >= ANG_SEG_THR)
    return hit, round(margin, 3), round(sig, 3), round(n, 3), seg, (kind if hit else None)


# 复用本地 memory.json 的读取，daemon 也走同一份，避免重复实现。
def load_memory(refs_dir):
    return json.loads((Path(refs_dir) / "memory.json").read_text(encoding="utf-8"))


def memory_mtime(refs_dir):
    return (Path(refs_dir) / "memory.json").stat().st_mtime


def _embed_cache_path(refs_dir):
    return Path(refs_dir) / EMBED_CACHE_NAME


def _mem_text(e):
    """记忆向量化用的正文：value + 标签 + context（去空白拼接）。"""
    val = str(e.get("value") or "")
    tags = " ".join(str(t) for t in (e.get("emotion_tags") or []))
    ctx = str(e.get("context") or "")
    return " ".join(x for x in (val, tags, ctx) if x).strip()


def _mem_hash(text):
    """内容指纹：仅看正文，id 变动/顺序变动不影响。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def build_or_load_embeddings(refs_dir, force_full=False):
    """返回 {mem_id: 归一化向量(np)}；memory.json 变动时【增量】重算。

    增量：缓存里存每条记忆的内容哈希，只重算 新增/变更 的条目，未动的条目直接
    复用旧向量 → 库越大重算越快（全量 30~77s 降到 <1s）。
    无哈希（首次/force_full）走全量，但调用方（daemon）已在后台线程跑，不阻塞请求。
    """
    cache_path = _embed_cache_path(refs_dir)
    mem_path = Path(refs_dir) / "memory.json"
    mem_mtime = mem_path.stat().st_mtime
    old_vectors = {}
    old_hashes = {}
    if cache_path.exists() and not force_full:
        try:
            blob = json.loads(cache_path.read_text(encoding="utf-8"))
            if blob.get("model") == MODEL_NAME and blob.get("source_mtime", 0) >= mem_mtime:
                # 缓存仍新鲜，直接返回（不触碰模型，最快路径）
                return {k: np.asarray(v, dtype=np.float32) for k, v in blob["vectors"].items()}
            old_vectors = blob.get("vectors", {}) or {}
            old_hashes = blob.get("hashes", {}) or {}
        except Exception:
            pass
    memory = load_memory(refs_dir)
    # 收集当前 active 记忆：id / 正文 / 哈希
    cur_ids, cur_texts, cur_hashes = [], [], {}
    for e in memory:
        if not isinstance(e, dict) or e.get("status") != "active":
            continue
        iid = e.get("id")
        if not iid:
            continue
        text = _mem_text(e)
        if not text:
            continue
        cur_ids.append(iid)
        cur_texts.append(text)
        cur_hashes[iid] = _mem_hash(text)
    # 增量：复用未变条目向量，只 embed 新增/变更
    vectors = {}
    to_embed_ids, to_embed_texts = [], []
    for iid, text in zip(cur_ids, cur_texts):
        if iid in old_vectors and old_hashes.get(iid) == cur_hashes[iid]:
            vectors[iid] = old_vectors[iid]   # 沿用旧向量（已是 list）
        else:
            to_embed_ids.append(iid)
            to_embed_texts.append(text)
    if to_embed_texts:
        m = get_model()
        new_vecs = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(to_embed_texts)]
        for iid, v in zip(to_embed_ids, new_vecs):
            vectors[iid] = v.tolist()
    blob = {
        "model": MODEL_NAME,
        "source_mtime": mem_mtime,
        "generated_at": time.time(),
        "count": len(vectors),
        "incremental": True,
        "vectors": vectors,
        "hashes": cur_hashes,
    }
    try:
        cache_path.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return {k: np.asarray(v, dtype=np.float32) for k, v in vectors.items()}


def _emo_boost(tags, cold=False):
    """计算情绪标签加权值。cold=True（冷淡/生气/委屈状态）时提升温暖安抚类标签权重，降低亲密类标签权重。"""
    s = set(tags or [])
    if cold:
        if s & {"亲密"}:
            return COLD_INTIMACY_PENALTY
        if s & COMFORT_TAGS:
            return COLD_COMFORT_BOOST
        return 0.0
    if s & SWEET_TAGS:
        return SWEET_BOOST
    if s & CARE_TAGS:
        return CARE_BOOST
    return 0.0


def _dedup_key(val):
    """按内容去重键：取 ｜ 后正文，去标点空白，用于合并同内容不同 id 的重复记忆。"""
    v = str(val or "")
    if "｜" in v:
        v = v.split("｜", 1)[1]
    v = re.sub(r"\s+", "", v)
    v = re.sub(r"[，。、·：:（）()\-—…~～!！?？;；'\"']", "", v)
    return v[:48]


def vector_recall(refs_dir, query_emb, limit=6, recent_ids=None, vectors=None, cold=False):
    """按向量余弦相似度与情绪标签加成检索 active 记忆，执行内容安全过滤、72h 展示冷却与正文去重。

    vectors: 外部传入的 {mem_id: np向量} 字典（daemon 常驻内存模式）；为 None 时从本地缓存加载。
    cold:    为 True 时启用冷情绪安抚加权策略（提升温暖/关心标签，压低亲密标签）。
    """
    memory = load_memory(refs_dir)
    if vectors is None:
        vectors = build_or_load_embeddings(refs_dir)
    vecs = vectors
    q = _norm(query_emb)
    recent = set(recent_ids or [])
    scored = []
    for e in memory:
        if not isinstance(e, dict) or e.get("status") != "active":
            continue
        iid = e.get("id")
        v = vecs.get(iid)
        if v is None:
            continue
        val = str(e.get("value") or "")
        if e.get("kind") == "rule" or HEAVY_RE.search(val) or TECH_RE.search(val):
            continue
        sim = float(np.dot(q, v))
        scored.append((sim + _emo_boost(e.get("emotion_tags"), cold=cold), e))
    scored.sort(key=lambda x: -x[0])
    # 72 小时展示冷却：读取 .shown_log.json 与 recent_ids，优先返回未在冷却期内的记忆；
    # 若全部处于冷却期，则按相似度顺序回填兜底。
    shown = {}
    try:
        import json as _json, os as _os
        _sp = _os.path.join(refs_dir, ".shown_log.json")
        _b = _json.loads(open(_sp, encoding="utf-8").read())
        if isinstance(_b, dict):
            shown = _b
    except Exception:
        shown = {}
    _now = time.time()
    _CD = 72 * 3600
    def _cooled(eid):
        if eid in recent:
            return True
        ts = shown.get(eid)
        return isinstance(ts, (int, float)) and (_now - ts) < _CD
    fresh = [e for _, e in scored if not _cooled(e.get("id"))]
    used = [e for _, e in scored if _cooled(e.get("id"))]
    ordered = fresh if fresh else used
    seen = set()
    deduped = []
    for e in ordered:
        k = _dedup_key(e.get("value"))
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(e)
    return deduped[:limit]
