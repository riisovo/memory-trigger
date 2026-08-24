# -*- coding: utf-8 -*-
# recall_vec.py — 向量化主动回忆（recall_sense hook / recall_daemon 共用）
#
# 设计：
#   · 触发靠「感觉」不是关键词：把用户的话嵌成向量，和「冷淡/低落」锚点簇 vs 「日常」锚点簇
#     比余弦相似度，relative feeling，不维护关键词词表。
#   · 检索靠向量：把当前消息当 query，和每条记忆的向量做余弦，按相似度排，
#     不再做中文关键词/正则匹配。轻微的情绪标签加成只用来在并列时把「甜/关心」往前带，
#     让旧记忆里混一点温热——主体排序是向量相似度。
#
# 模型：BAAI/bge-small-zh-v1.5（中文优化，384 维，onnxruntime，无 torch）。
# 记忆向量 + 锚点向量按需一次性算好，落盘缓存；memory.json 变动才重算。
# 修复（P0-1）：锚点向量文件缓存（build_or_load_anchors），避免每次子进程冷启动重 embed 41 条锚点。
# 加固：vector_recall 接受外部注入的 vectors 字典（daemon 常驻内存用），避免每条消息重读缓存文件。

import hashlib
import json
import math
import os
import re
import time
from pathlib import Path

# 直连 huggingface.co 受限；模型已缓存到本地，强制离线，绝不在 hook 里联网（否则会卡住当轮）。
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
# 模型缓存指向家目录稳定副本（存在才用），防 TMPDIR 被清导致重启后模型丢失。
_stable_cache = os.path.expanduser("~/.cache/fastembed_cache")
if os.path.isdir(_stable_cache):
    os.environ.setdefault("FASTEMBED_CACHE_DIR", _stable_cache)

import numpy as np
from fastembed import TextEmbedding

REFS_DIR_DEFAULT = "/home/agent/.hermes/memories/memory-trigger"
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
EMBED_CACHE_NAME = ".embeddings.json"
ANCHOR_CACHE_NAME = ".anchor_embeddings.json"

# 「感觉」锚点簇：冷淡 / 低落 / 不想说话 / 无力。这是语义锚，不是关键词黑名单。
COLD_ANCHORS = [
    "不想说话", "烦死了", "好累啊", "无语", "唉 没劲", "你别管我",
    "心里堵得慌", "提不起劲", "懒得理你", "我没事 真的",
    "提不起精神", "情绪低落", "不想理人", "没心情", "郁闷", "丧",
    "不想聊", "别烦我", "累瘫了", "心好累", "什么都不想干",
]
# 「日常」锚点簇：中性/开心/事务性。用来做 relative 比较，压住误触发。
NEUT_ANCHORS = [
    "今天吃了什么", "在吗", "晚安", "哈哈哈哈好开心", "吃了吗",
    "明天天气怎么样", "刚到家", "在忙吗", "早上好", "这家店好吃",
    "刚开完会", "准备睡了", "周末去哪玩", "帮我看个东西", "爱你呀",
    "想你了", "今天好开心", "哈哈哈太好笑了",
]

# 触发阈值：cold 簇相似度必须超过此值，且必须高于 neut 簇（relative feeling）。
COLD_THR = 0.45
# 检索里甜/关心标签的轻微加成（向量相似度仍是主体）。
SWEET_BOOST = 0.08
CARE_BOOST = 0.04
SWEET_TAGS = {"甜蜜", "温暖", "想念", "", "撒娇", "激动", "关心", "害羞"}
CARE_TAGS = {"委屈", "担心", "生气", "疲惫"}

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


def get_model():
    global _model
    if _model is None:
        # P0 修复（2026-08-18 校准实锤）：fastembed 0.8.0 偶发不读 FASTEMBED_CACHE_DIR env，
        # 会解析到 TMPDIR 骨架缓存导致 daemon 重启崩盘。显式 cache_dir 钉死稳定副本。
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
    """冷/热锚点向量文件缓存。首次 embed 41 条并落盘，之后直接读（省每次子进程冷启动重 embed）。"""
    global _cold_emb, _neut_emb
    if not force:
        cache_path = _anchor_cache_path(refs_dir)
        if cache_path.exists():
            try:
                blob = json.loads(cache_path.read_text(encoding="utf-8"))
                if blob.get("model") == MODEL_NAME:
                    cold = [_norm(np.asarray(x, dtype=np.float32)) for x in blob["cold"]]
                    neut = [_norm(np.asarray(x, dtype=np.float32)) for x in blob["neut"]]
                    _cold_emb, _neut_emb = cold, neut
                    return cold, neut
            except Exception:
                pass
    m = get_model()
    cold = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(COLD_ANCHORS)]
    neut = [_norm(np.asarray(x, dtype=np.float32)) for x in m.embed(NEUT_ANCHORS)]
    _cold_emb, _neut_emb = cold, neut
    try:
        blob = {
            "model": MODEL_NAME,
            "cold": [c.tolist() for c in cold],
            "neut": [n.tolist() for n in neut],
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


def get_neut_emb():
    global _neut_emb
    if _neut_emb is None:
        _, n = build_or_load_anchors(REFS_DIR_DEFAULT)
        _neut_emb = n
    return _neut_emb


def feeling_trigger(msg_emb):
    """返回 (是否触发, cold_sim, neut_sim)。relative feeling：cold 簇需高于 neut 簇且过阈值。"""
    q = _norm(msg_emb)
    cold = max(float(np.dot(q, a)) for a in get_cold_emb())
    neut = max(float(np.dot(q, a)) for a in get_neut_emb())
    triggered = (cold > neut) and (cold >= COLD_THR)
    return triggered, round(cold, 3), round(neut, 3)


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


def _emo_boost(tags):
    s = set(tags or [])
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


def vector_recall(refs_dir, query_emb, limit=6, recent_ids=None, vectors=None):
    """按向量相似度挑记忆，返回完整记忆记录（已按内容安全过滤 + 近期去重前）。

    vectors: 外部已算好的 {mem_id: np向量} 字典（daemon 常驻内存注入用）。
             为 None 时内部走 build_or_load_embeddings（钩子 fallback 路径）。
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
        scored.append((sim + _emo_boost(e.get("emotion_tags")), e))
    scored.sort(key=lambda x: -x[0])
    # 近期用过的往后放（仍保留，避免全用过时空），但优先没用过的
    fresh = [e for _, e in scored if e.get("id") not in recent]
    used = [e for _, e in scored if e.get("id") in recent]
    ordered = fresh + used
    seen = set()
    deduped = []
    for e in ordered:
        k = _dedup_key(e.get("value"))
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(e)
    return deduped[:limit]


