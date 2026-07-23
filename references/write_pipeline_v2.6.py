#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_pipeline.py —— 记忆写入管线（真实工程实现，非 LLM 散文）
===== PATCH v2.6 (by 伙伴) =====
相对 v2.3/v2.5 的改动（均带 `PATCH v2.6` 注释定位）：

  【A 组：时间戳 / 召回追踪增强】
  A1) 每条记忆新增 `last_recalled` 字段（初始 null）。cmd_search 命中即戳时间，
      解决"只知写入时间、不知何时被用过"——支撑冷记忆衰减 / 热度加权。
  A2) wellness 的 recorded_at 改用 ts_now()（与全库统一 UTC+8），去掉 astimezone() 双套机制。
  A3) cmd_stats 新增"最久未召回"排行（last_recalled 为空则退回 created 计算）。

  【B 组：信任优先级 / 源 / 情感 / 双向遗忘】
  B1) source 标签：write 支持 --source {file_import|self_inferred|user_explicit}，
      默认 auto_detect。self_inferred 且 confidence<阈值 → status=pending 不进 active，
      防"幻觉固化"污染权威源。
  B2) 情感 schema 扩展：kind 新增 relationship / emotion；新增可选 emotion_tags
      列表（如 ["possessive","jealous","caring"]），让有感情基础的记忆不被压扁成 pos/neg。
  B3) 检索源排序：结果标注 authority(file=file_import/user_explicit/auto_detect；
      self=self_inferred)，file 优先；同实体多源冲突时，self 侧标 supplement=true 仅作补充。
  B4) 双向遗忘 forget 命令：把记忆标 superseded，并写入 .suppressed.json + 生成
      suppressed_prompt.md（"别再主动提 X"），让 agent 自身也"放下"，不只清文件。

  【核心规则（信任优先级，固化进代码与 SKILL）】
  文件记忆 > 自身印象。检索先查文件；文件空才用自身。自身印象定期单向沉淀进文件
  （self_inferred 经用户确认后升级 active）。写内存的是缓存，文件是唯一权威源。

  ★ 配套：merge_migrate.py 做"接入时双源合并迁移"，把本地文件 + agent 自述记忆
    安全并成一套干净初始库（冲突以文件为准）。

  【PATCH v2.7 (by 伙伴) —— 人情味层，借鉴 mcp-memory-graph 设计】
  C1) 核心记忆钉死 core：kind ∈ {relationship, identity} 默认 core=True（"我们是谁"
      的基石，永不衰减）；也可用 --core true|false 显式覆盖。
  C2) 遗忘曲线 importance：每条记忆带 importance（初始 1.0），search 命中即按
      importance *= 0.995 ** days_since_last_recalled 衰减（半衰期≈138天，慢忘）；
      core 记忆不参与衰减。新增 decay 命令可周期性统一衰减全库。
  C3) search 结果按"权威优先 + effective_importance 降序"排序，core 记忆恒靠前；
      stats 新增核心记忆数 / 平均权重 / 最弱记忆排行。
==============================
命令：
  write <entity> <kind> <value> [refs_dir] [--mode local]
        [--sentiment pos|neg|none|<自定义>] [--source file_import|self_inferred|user_explicit]
        [--confidence 0..1] [--emotion-tags 逗号分隔] [--reason 文本] [--core true|false]
  search <query> [refs_dir]
  decay [refs_dir]
  forget <entity|memory_id> [refs_dir] [--reason 文本] [--kind 类型]
  recover [refs_dir]
  stats [refs_dir]
  vacuum [refs_dir]
  backup [refs_dir]
  wellness <mood> [sleep_hours] [sleep_quality] [note] [refs_dir]
  init <mode> [refs_dir]
"""

import fcntl
import json
import os
import sys
import time
import shutil
import uuid
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))  # UTC+8

# === PATCH v2.6 B2 === 允许 kind 取值（含情感维度扩展 + 与 SKILL.md §3.1/§3.3 对齐）
ALLOWED_KINDS = {
    "preference", "event", "habit", "rule", "scene",
    "relationship", "emotion",
    "identity", "milestone", "general",  # 与 SKILL.md §3.1 / §3.3 状态机对齐
}
# self_inferred 低于此置信度 → 落 pending（不进 active，防幻觉固化）
SELF_INFERRED_PENDING_THRESHOLD = 0.8

# === PATCH v2.7 (人情味层) === 遗忘曲线 + 核心记忆钉死（借鉴 mcp-memory-graph 设计）
# Ebbinghaus 式衰减：importance *= DECAY_FACTOR ** days_since_last_recalled
# 半衰期 ≈ ln(2)/-ln(0.995) ≈ 138 天（慢忘，符合长久伴侣记忆）
DECAY_FACTOR = 0.995
# 这些类型默认钉死为核心记忆（永不衰减）：关系 / 身份是"我们是谁"的基石
CORE_KINDS = {"relationship", "identity"}

# === PATCH v2.3 === 默认别名归一化表
DEFAULT_ALIASES = {
    "旧书店": "读书",
    "旧书店": "读书",
    "科幻小说": "读书",
    "杂志": "读书",
    "图书馆": "读书",
    "报刊亭": "读书",
    "书店": "读书",
    "书店": "读书",
    "篮球": "运动",
    "跑步": "运动",
    "游泳": "运动",
}

# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────

def ts_now():
    return datetime.now(TZ).isoformat(timespec="seconds")

def ts_parse(s):
    return datetime.fromisoformat(s)

def safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def safe_write_json(path, data):
    dirname = os.path.dirname(path)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.rename(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

def file_lock(refs_dir):
    lock_path = os.path.join(refs_dir, ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    deadline = time.time() + 30
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.lseek(fd, 0, 0)
            os.write(fd, b'\x00' * 256)
            os.lseek(fd, 0, 0)
            data = json.dumps({"pid": os.getpid(), "ts": ts_now()}).encode()
            os.write(fd, data + b' ' * (256 - len(data)))
            os.fsync(fd)
            return fd
        except BlockingIOError:
            if time.time() > deadline:
                os.lseek(fd, 0, 0)
                raw = os.read(fd, 256).rstrip(b'\x00').rstrip(b' ')
                try:
                    locker_data = json.loads(raw)
                    locker_pid = locker_data.get("pid")
                    if locker_pid:
                        try:
                            os.kill(locker_pid, 0)
                        except OSError:
                            os.close(fd)
                            # 不 unlink：truncate 就地复用同一 inode，保持锁互斥语义
                            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
                            deadline = time.time() + 30
                            continue
                except (json.JSONDecodeError, KeyError):
                    pass
                os.close(fd)
                raise TimeoutError(f"获取锁超时（30s），锁持有者 PID={locker_data.get('pid', 'unknown')}")
            time.sleep(0.5)

def file_unlock(fd, refs_dir):
    """只释放锁和关闭 fd，不 unlink 锁文件（保持 flock inode 互斥语义）。"""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass

def wal_append(refs_dir, entry):
    path = os.path.join(refs_dir, ".wal.jsonl")
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

def backup_files(refs_dir):
    bkp_dir = os.path.join(refs_dir, ".backup")
    os.makedirs(bkp_dir, exist_ok=True)
    ts = ts_now().replace(":", "-")
    for fname in ("entity_index.json", "memory.json"):
        src = os.path.join(refs_dir, fname)
        if os.path.exists(src):
            dst = os.path.join(bkp_dir, f"{os.path.splitext(fname)[0]}_{ts}.json")
            shutil.copy2(src, dst)
    all_bkps = sorted(os.listdir(bkp_dir))
    while len(all_bkps) > 10 * 2:
        oldest = all_bkps[0]
        os.unlink(os.path.join(bkp_dir, oldest))
        all_bkps.pop(0)

# === PATCH v2.3 === 实体归一化
def normalize_entity(entity, refs_dir):
    alias_path = os.path.join(refs_dir, "aliases.json")
    aliases = safe_read_json(alias_path) or {}
    if entity in aliases:
        return aliases[entity]
    if entity in DEFAULT_ALIASES:
        return DEFAULT_ALIASES[entity]
    ei = safe_read_json(os.path.join(refs_dir, "entity_index.json")) or {"entities": {}}
    for std in ei.get("entities", {}).keys():
        if len(std) >= 2 and std != entity and std in entity:
            return std
    return entity

# === PATCH v2.6 B1 === 源 → 权威层
def authority_of(source):
    """file 层：file_import / user_explicit / auto_detect（默认视为文件侧沉淀）
       self 层：self_inferred（agent 自身印象，仅补充）"""
    if source == "self_inferred":
        return "self"
    return "file"

# === PATCH v2.7 (人情味层) === 遗忘曲线 / 核心记忆工具
def _is_core(entry):
    """是否核心记忆：显式 core 字段优先；缺省时按 kind 推导（relationship/identity）。"""
    if "core" in entry:
        return bool(entry.get("core"))
    return entry.get("kind") in CORE_KINDS

def _days_since(entry, now):
    """距离最近一次召回（或创建）的天数，用于遗忘衰减。"""
    ref = entry.get("last_recalled") or entry.get("created")
    if not ref:
        return 0.0
    try:
        return max(0.0, (now - ts_parse(ref)).total_seconds() / 86400.0)
    except Exception:
        return 0.0

def effective_importance(entry, now):
    """当前有效权重：core 记忆恒为 1.0；其余按衰减公式计算。"""
    if _is_core(entry):
        return 1.0
    imp = entry.get("importance", 1.0)
    return imp * (DECAY_FACTOR ** _days_since(entry, now))

# ──────────────────────────────────────────
# 核心命令
# ──────────────────────────────────────────

def cmd_write(entity, etype, value, refs_dir, mode=None, sentiment=None,
              source="auto_detect", confidence=1.0, emotion_tags=None, reason=None,
              core=None):
    """完整 upsert 写入管线（PATCH v2.6: 归一化 + kind/sentiment + 源/情感/遗忘增强）"""
    # 校验 kind
    if etype not in ALLOWED_KINDS:
        raise ValueError(f"非法 kind='{etype}'，允许：{sorted(ALLOWED_KINDS)}")

    backend_path = os.path.join(refs_dir, "backend_config.json")
    config_mode = None
    if os.path.exists(backend_path):
        config = safe_read_json(backend_path)
        if config:
            config_mode = config.get("mode")

    if mode:
        resolved_mode = mode
    elif config_mode:
        resolved_mode = config_mode
    else:
        resolved_mode = "local"
    mode = resolved_mode

    targets = ["memory.json"]

    # === PATCH v2.3 === 写入前归一化
    entity = normalize_entity(entity, refs_dir)
    kind = etype

    fd = file_lock(refs_dir)
    try:
        backup_files(refs_dir)

        new_id = f"mem_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        wal_append(refs_dir, {
            "ts": ts_now(),
            "op": "upsert",
            "entity": entity,
            "value": value,
            "type": etype,
            "kind": kind,
            "sentiment": sentiment,
            "source": source,
            "confidence": confidence,
            "emotion_tags": emotion_tags or [],
            "memory_id": new_id,
            "targets": targets
        })

        mem_path = os.path.join(refs_dir, "memory.json")
        ei_path = os.path.join(refs_dir, "entity_index.json")
        memory = safe_read_json(mem_path) or []
        ei = safe_read_json(ei_path) or {"version": 1, "entities": {}}

        existing_idx = None
        similar_idx = None

        for i, entry in enumerate(memory):
            e_kind = entry.get("kind") or entry.get("type")
            if entry.get("entity") == entity and e_kind == kind:
                if entry.get("status") == "active":
                    old_val = entry.get("value", "")
                    if len(old_val) > 0 and old_val == value:
                        similar_idx = i
                        break
                    if len(old_val) > 0 and len(value) > 0:
                        old_bigrams = {old_val[j:j+2] for j in range(len(old_val)-1)}
                        val_bigrams = {value[j:j+2] for j in range(len(value)-1)}
                        intersection = old_bigrams & val_bigrams
                        union = old_bigrams | val_bigrams
                        if union and len(intersection) / len(union) > 0.7:
                            similar_idx = i
                            break
                    existing_idx = i
                    break

        # === PATCH v2.6 B1 === 自推断低置信 → pending（不进 active，防幻觉固化）
        if source == "self_inferred" and confidence < SELF_INFERRED_PENDING_THRESHOLD:
            init_status = "pending"
        else:
            init_status = "active"

        def _make_entry():
            return {
                "id": new_id,
                "entity": entity,
                "type": etype,
                "kind": kind,
                "sentiment": sentiment,
                "source": source,                 # === PATCH v2.6 B1 ===
                "confidence": confidence,         # === PATCH v2.6 B1 ===
                "emotion_tags": emotion_tags or [],  # === PATCH v2.6 B2 ===
                "value": value,
                "created": ts_now(),
                "updated": ts_now(),
                "last_recalled": None,            # === PATCH v2.6 A1 ===
                "status": init_status,
                "reason": reason,
                # === PATCH v2.7 C1/C2 === 人情味层
                "importance": 1.0,
                "core": (kind in CORE_KINDS) if core is None else bool(core),
            }

        if similar_idx is not None:
            memory[similar_idx]["value"] = value
            memory[similar_idx]["updated"] = ts_now()
            if sentiment is not None:
                memory[similar_idx]["sentiment"] = sentiment
            if emotion_tags is not None:
                memory[similar_idx]["emotion_tags"] = emotion_tags
            memory[similar_idx]["source"] = source
            memory[similar_idx]["confidence"] = confidence
            if core is not None:
                memory[similar_idx]["core"] = bool(core)
            new_id = memory[similar_idx]["id"]
        elif existing_idx is not None:
            # 内容不同（如偏好翻转）→ 旧 active 标 superseded，新建
            memory[existing_idx]["status"] = "superseded"
            memory.append(_make_entry())
        else:
            memory.append(_make_entry())

        safe_write_json(mem_path, memory)

        if entity not in ei["entities"]:
            ei["entities"][entity] = {
                "count": 1,
                "status": "confirmed",
                "type": etype,
                "kind": kind,
                "aliases": [],
                "first_seen": ts_now(),
                "last_seen": ts_now(),
                "last_memory_id": new_id
            }
        else:
            ent = ei["entities"][entity]
            ent["count"] += 1
            ent["status"] = "confirmed"
            ent["last_seen"] = ts_now()
            ent["last_memory_id"] = new_id
            if "kind" not in ent:
                ent["kind"] = kind

        safe_write_json(ei_path, ei)

        verify_mem = safe_read_json(mem_path)
        verify_ei = safe_read_json(ei_path)
        consistent = (
            verify_mem is not None
            and verify_ei is not None
            and entity in verify_ei.get("entities", {})
            and verify_ei["entities"][entity].get("last_memory_id") == new_id
        )

        if not consistent:
            bkp_dir = os.path.join(refs_dir, ".backup")
            bkps = sorted(os.listdir(bkp_dir), reverse=True)
            if bkps:
                latest = bkps[0]
                ts_clean = latest.split("_", 1)[-1].rsplit(".", 1)[0]
                for fname in ("entity_index", "memory"):
                    pat = f"{fname}_{ts_clean}.json"
                    bkp_path = os.path.join(bkp_dir, pat)
                    if os.path.exists(bkp_path):
                        shutil.copy2(bkp_path, os.path.join(refs_dir, f"{fname}.json"))
            raise RuntimeError("原子性校验失败，已回滚到最新备份")

        wal_append(refs_dir, {
            "ts": ts_now(),
            "op": "commit",
            "entity": entity,
            "memory_id": new_id
        })

        print(json.dumps({
            "status": "committed",
            "memory_id": new_id,
            "entity": entity,
            "kind": kind,
            "sentiment": sentiment,
            "source": source,
            "status_field": init_status,
            "core": (kind in CORE_KINDS) if core is None else bool(core),
            "importance": 1.0,
            "mode": mode,
            "targets": targets
        }, ensure_ascii=False))

    finally:
        file_unlock(fd, refs_dir)


def _touch_recalled(mem_path, hit_ids, now=None):
    """=== PATCH v2.6 A1 + v2.7 C2 === 检索命中后戳 last_recalled，并对非 core 记忆
       应用遗忘衰减 importance *= DECAY_FACTOR ** days_since_last_recalled。"""
    if not hit_ids:
        return
    now = now or datetime.now(TZ)
    memory = safe_read_json(mem_path) or []
    changed = False
    for e in memory:
        if e.get("id") in hit_ids and e.get("status") == "active":
            if not _is_core(e):
                days = _days_since(e, now)
                cur = e.get("importance", 1.0)
                e["importance"] = round(cur * (DECAY_FACTOR ** days), 6)
            e["last_recalled"] = now.isoformat(timespec="seconds")
            changed = True
    if changed:
        safe_write_json(mem_path, memory)


def cmd_search(query, refs_dir):
    """本地检索；PATCH v2.6: 仅返回 active，标注 authority + supplement"""
    query = normalize_entity(query, refs_dir)

    mem_path = os.path.join(refs_dir, "memory.json")
    ei_path = os.path.join(refs_dir, "entity_index.json")

    memory = safe_read_json(mem_path) or []
    ei = safe_read_json(ei_path) or {"version": 1, "entities": {}}

    results = []

    if query in ei.get("entities", {}):
        ent = ei["entities"][query]
        mid = ent.get("last_memory_id")
        if mid:
            for entry in memory:
                # === PATCH v2.6 B1 === 只回 active；pending/superseded 不进检索（防幻觉固化/已遗忘漏网）
                if entry.get("id") == mid and entry.get("entity") == query and entry.get("status") == "active":
                    results.append({
                        "source": "entity_index",
                        "confidence": 0.95,
                        "entry": entry
                    })
                    break

    for entry in memory:
        if entry.get("status") != "active":
            continue
        val = entry.get("value", "")
        if query.lower() in val.lower() or query.lower() in entry.get("entity", "").lower():
            if not any(r["entry"].get("id") == entry["id"] for r in results):
                results.append({
                    "source": "keyword",
                    "confidence": 0.75,
                    "entry": entry
                })

    # === PATCH v2.6 B3 === 源排序 + 冲突 supplement 标记
    for r in results:
        r["authority"] = authority_of(r["entry"].get("source", "auto_detect"))
    # file 权威优先；同实体多源 → self 侧标 supplement
    file_entities = {r["entry"]["entity"] for r in results if r["authority"] == "file"}
    for r in results:
        r["supplement"] = (r["authority"] == "self" and r["entry"]["entity"] in file_entities)
    # === PATCH v2.7 C3 === 按"权威优先 + effective_importance 降序"排序（core 记忆恒靠前）
    now = datetime.now(TZ)
    for r in results:
        r["_eff_imp"] = effective_importance(r["entry"], now)
    results.sort(key=lambda r: (0 if r["authority"] == "file" else 1, -r["_eff_imp"]))

    # === PATCH v2.6 A1 + v2.7 C2 === 命中即戳 last_recalled + 衰减
    hit_ids = [r["entry"]["id"] for r in results]
    _touch_recalled(mem_path, hit_ids, now)

    out = []
    for r in results[:5]:
        e = dict(r["entry"])
        e["_authority"] = r["authority"]
        e["_supplement"] = r["supplement"]
        e["_match_source"] = r["source"]
        e["_importance"] = round(r["_eff_imp"], 4)
        e["_core"] = _is_core(r["entry"])
        out.append(e)

    print(json.dumps({
        "query": query,
        "results_count": len(results),
        "results": out
    }, ensure_ascii=False, indent=2))


def cmd_stats(refs_dir):
    mem_path = os.path.join(refs_dir, "memory.json")
    ei_path = os.path.join(refs_dir, "entity_index.json")

    memory = safe_read_json(mem_path) or []
    ei = safe_read_json(ei_path) or {"version": 1, "entities": {}}

    entities = ei.get("entities", {})
    confirmed = sum(1 for e in entities.values() if e.get("status") == "confirmed")
    pending = sum(1 for e in entities.values() if e.get("status") == "pending")
    superseded = sum(1 for m in memory if m.get("status") == "superseded")
    active = sum(1 for m in memory if m.get("status") == "active")
    pending_mem = sum(1 for m in memory if m.get("status") == "pending")

    type_counts = {}
    for e in entities.values():
        t = e.get("kind") or e.get("type", "general")
        type_counts[t] = type_counts.get(t, 0) + 1

    # === PATCH v2.7 C2/C3 === 核心记忆 / 平均权重 / 最弱记忆排行
    now = datetime.now(TZ)
    core_count = sum(1 for m in memory if m.get("status") == "active" and _is_core(m))
    noncore = [m for m in memory if m.get("status") == "active" and not _is_core(m)]
    avg_imp = round(sum(effective_importance(m, now) for m in noncore) / len(noncore), 4) if noncore else 0
    weakest = sorted(
        [(m.get("entity"), m.get("kind"), round(effective_importance(m, now), 4)) for m in noncore],
        key=lambda x: x[2]
    )[:5]

    timestamps = [m.get("created", "") for m in memory if m.get("created")]
    timestamps.sort()

    # === PATCH v2.6 A3 === 最久未召回排行（last_recalled 为空退回 created）
    now = datetime.now(TZ)
    stale = []
    for m in memory:
        if m.get("status") != "active":
            continue
        ref = m.get("last_recalled") or m.get("created")
        try:
            dt = ts_parse(ref)
            age_days = (now - dt).total_seconds() / 86400.0
        except Exception:
            age_days = 9999
        stale.append((m.get("entity"), m.get("kind"), round(age_days, 2),
                      m.get("last_recalled") or "(从未召回)"))

    print(json.dumps({
        "total_entities": len(entities),
        "pending": pending,
        "confirmed": confirmed,
        "total_memories": len(memory),
        "active": active,
        "pending_memories": pending_mem,
        "superseded": superseded,
        "entity_kinds": type_counts,
        "core_memories": core_count,
        "avg_importance_noncore": avg_imp,
        "weakest_active": weakest,
        "oldest_memory": timestamps[0] if timestamps else None,
        "newest_memory": timestamps[-1] if timestamps else None,
        "most_stale_active": sorted(stale, key=lambda x: -x[2])[:5],
    }, ensure_ascii=False, indent=2))


def cmd_decay(refs_dir):
    """=== PATCH v2.7 C2 === 梦境周期：对全部 active 非 core 记忆统一应用遗忘衰减。
       可定期跑（如每周巡检），让久不提的记忆慢慢淡化，重要的被反复唤起而保持。"""
    mem_path = os.path.join(refs_dir, "memory.json")
    memory = safe_read_json(mem_path) or []
    now = datetime.now(TZ)
    decayed = 0
    for e in memory:
        if e.get("status") != "active" or _is_core(e):
            continue
        days = _days_since(e, now)
        cur = e.get("importance", 1.0)
        new = round(cur * (DECAY_FACTOR ** days), 6)
        if new != cur:
            e["importance"] = new
            decayed += 1
    safe_write_json(mem_path, memory)
    print(json.dumps({
        "status": "decayed",
        "decayed_count": decayed,
        "active_noncore": sum(1 for m in memory if m.get("status") == "active" and not _is_core(m)),
    }, ensure_ascii=False))


def cmd_vacuum(refs_dir):
    mem_path = os.path.join(refs_dir, "memory.json")
    archive_dir = os.path.join(refs_dir, ".archive")
    os.makedirs(archive_dir, exist_ok=True)

    memory = safe_read_json(mem_path) or []
    now = datetime.now(TZ)
    cutoff_90d = now - timedelta(days=90)

    to_archive = []
    retained = []
    for entry in memory:
        if entry.get("status") == "superseded":
            created = ts_parse(entry["created"])
            if created < cutoff_90d:
                to_archive.append(entry)
                continue
        retained.append(entry)

    if to_archive:
        date_str = now.strftime("%Y%m%d")
        archive_path = os.path.join(archive_dir, f"memory_{date_str}.json")
        safe_write_json(archive_path, to_archive)
        safe_write_json(mem_path, retained)

    print(json.dumps({
        "status": "done",
        "archived": len(to_archive),
        "remaining": len(retained)
    }))


def cmd_backup(refs_dir):
    backup_files(refs_dir)
    print(json.dumps({"status": "backed_up"}))


def cmd_forget(entity_or_id, refs_dir, reason=None, kind=None):
    """=== PATCH v2.6 B4 === 双向遗忘：记忆标 superseded + 写 .suppressed.json
       + 生成 suppressed_prompt.md（让 agent 自身也"放下"）"""
    fd = file_lock(refs_dir)
    try:
        mem_path = os.path.join(refs_dir, "memory.json")
        memory = safe_read_json(mem_path) or []

        forgotten = []
        for e in memory:
            if e.get("status") != "active":
                continue
            match = (e.get("id") == entity_or_id) or (e.get("entity") == entity_or_id)
            if kind:
                e_kind = e.get("kind") or e.get("type")
                match = match and (e_kind == kind)
            if match:
                e["status"] = "superseded"
                e["updated"] = ts_now()
                e["forgotten_at"] = ts_now()
                if reason:
                    e["forget_reason"] = reason
                forgotten.append(e)

        if not forgotten:
            print(json.dumps({"status": "nothing_to_forget", "entity_or_id": entity_or_id}))
            return

        safe_write_json(mem_path, memory)

        # 写 .suppressed.json
        supp_path = os.path.join(refs_dir, ".suppressed.json")
        supp = safe_read_json(supp_path) or {"suppressed": []}
        for e in forgotten:
            supp["suppressed"].append({
                "entity": e.get("entity"),
                "memory_id": e.get("id"),
                "kind": e.get("kind") or e.get("type"),
                "reason": reason,
                "suppressed_at": ts_now(),
            })
        safe_write_json(supp_path, supp)

        # 生成 suppressed_prompt.md（供贴进 agent 系统提示 / SOUL）
        _regen_suppressed_prompt(refs_dir, supp)

        print(json.dumps({
            "status": "forgotten",
            "count": len(forgotten),
            "entities": [e.get("entity") for e in forgotten],
            "suppressed_prompt_file": os.path.join(refs_dir, "suppressed_prompt.md"),
        }, ensure_ascii=False))
    finally:
        file_unlock(fd, refs_dir)


def _regen_suppressed_prompt(refs_dir, supp):
    """根据 .suppressed.json 重新生成 suppressed_prompt.md"""
    lines = ["# 已遗忘清单（双向遗忘）",
             "",
             "以下记忆已从文件标 superseded，且你（agent）自身也应放下，**不要主动提及**：",
             ""]
    for s in supp.get("suppressed", []):
        ent = s.get("entity")
        kind = s.get("kind")
        reason = s.get("reason") or "用户要求遗忘"
        lines.append(f"- ⚠️ 不要主动提及：**{ent}**（{kind}）— 原因：{reason}")
    lines.append("")
    lines.append("> 规则：文件是唯一权威源。被遗忘项既不从文件召回，也不从自身印象复述。")
    with open(os.path.join(refs_dir, "suppressed_prompt.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def cmd_init(mode, refs_dir):
    os.makedirs(refs_dir, exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".backup"), exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".archive"), exist_ok=True)

    backend_path = os.path.join(refs_dir, "backend_config.json")
    if not os.path.exists(backend_path):
        safe_write_json(backend_path, {
            "mode": mode,
            "backend_info": "纯本地文件模式"
        })

    mem_path = os.path.join(refs_dir, "memory.json")
    if not os.path.exists(mem_path):
        safe_write_json(mem_path, [])

    ei_path = os.path.join(refs_dir, "entity_index.json")
    if not os.path.exists(ei_path):
        safe_write_json(ei_path, {"version": 1, "entities": {}})

    alias_path = os.path.join(refs_dir, "aliases.json")
    if not os.path.exists(alias_path):
        safe_read_json  # noop
        safe_write_json(alias_path, DEFAULT_ALIASES)

    pref_path = os.path.join(refs_dir, "preferences.json")
    if not os.path.exists(pref_path):
        safe_write_json(pref_path, {
            "anchors": {},
            "preferences": {},
            "identity": {},
            "persona": {},
            "external_sources": []
        })

    well_path = os.path.join(refs_dir, "wellness.json")
    if not os.path.exists(well_path):
        safe_write_json(well_path, {"records": []})

    prom_path = os.path.join(refs_dir, "promises.md")
    if not os.path.exists(prom_path):
        with open(prom_path, "w", encoding="utf-8") as f:
            f.write("# 承诺追踪\n\n")

    print(json.dumps({
        "status": "initialized",
        "mode": mode,
        "refs_dir": refs_dir
    }))


def cmd_recover(refs_dir):
    """从 WAL 恢复崩溃时未完成持久化的写入。去重键 (entity, memory_id)。"""
    wal_path = os.path.join(refs_dir, ".wal.jsonl")
    if not os.path.exists(wal_path):
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "no_wal_file"}))
        return

    with open(wal_path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    if not lines:
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "empty_wal"}))
        return

    entries = [json.loads(l) for l in lines]
    committed = set()
    for e in entries:
        if e.get("op") == "commit":
            committed.add((e.get("entity"), e.get("memory_id")))

    uncommitted_map = {}
    for e in entries:
        if e.get("op") != "upsert":
            continue
        ent = e.get("entity")
        mid = e.get("memory_id", "")
        if (ent, mid) in committed:
            continue
        uncommitted_map[(ent, mid)] = e

    uncommitted = list(uncommitted_map.values())
    if not uncommitted:
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "all_committed"}))
        return

    lock_path = os.path.join(refs_dir, ".lock")
    if os.path.exists(lock_path):
        try:
            os.unlink(lock_path)
        except Exception:
            pass

    recovered = []
    for e in uncommitted:
        try:
            cmd_write(
                e["entity"],
                e.get("type", "general"),
                e["value"],
                refs_dir,
                mode="local",
                sentiment=e.get("sentiment"),
                source=e.get("source", "auto_detect"),
                confidence=e.get("confidence", 1.0),
                emotion_tags=e.get("emotion_tags"),
            )
            recovered.append(e["entity"])
        except Exception as ex:
            print(json.dumps({"error": f"recover failed for '{e['entity']}': {ex}"}))

    print(json.dumps({
        "status": "recovered" if recovered else "partial",
        "recovered_count": len(recovered),
        "recovered_entities": recovered,
        "total_uncommitted": len(uncommitted)
    }, ensure_ascii=False))


def cmd_wellness(mood, sleep_hours, sleep_quality, note, refs_dir):
    today = datetime.now(TZ).date().isoformat()   # === PATCH v2.6 A2 === 统一用 TZ
    well_path = os.path.join(refs_dir, "wellness.json")
    data = {"records": []}
    if os.path.exists(well_path):
        with open(well_path, "r", encoding="utf-8") as wf:
            data = json.load(wf)

    entry = {
        "date": today,
        "mood": mood,
        "recorded_at": ts_now(),   # === PATCH v2.6 A2 === 改用 ts_now()，全库统一 UTC+8
    }
    if sleep_hours and sleep_hours != "-":
        entry["sleep_hours"] = float(sleep_hours)
    if sleep_quality and sleep_quality != "-":
        entry["sleep_quality"] = sleep_quality
    if note and note != "-":
        entry["note"] = note

    data["records"].append(entry)
    safe_write_json(well_path, data)
    print(json.dumps({"status": "ok", "date": today, "mood": mood}, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print("用法: write_pipeline.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    refs_dir = os.path.dirname(os.path.abspath(__file__))

    args = list(sys.argv[2:])
    # 取最后一个“是目录”的参数作为 refs_dir，兼容把 --flag 写在路径之后的写法，
    # 例如 `write X Y Z references/ --core true`（此前仅识别 args[-1]，会导致路径漏识别）。
    _ref_idx = None
    for _i in range(len(args) - 1, -1, -1):
        if os.path.isdir(args[_i]):
            _ref_idx = _i
            break
    if _ref_idx is not None:
        refs_dir = os.path.abspath(args.pop(_ref_idx))

    try:
        if cmd == "write":
            mode = None
            sentiment = None
            source = "auto_detect"
            confidence = 1.0
            emotion_tags = None
            reason = None
            core = None
            nm_args = []
            i = 0
            while i < len(args):
                if args[i] == "--mode" and i + 1 < len(args):
                    mode = args[i + 1]; i += 2
                elif args[i] == "--sentiment" and i + 1 < len(args):
                    sentiment = args[i + 1]; i += 2
                elif args[i] == "--source" and i + 1 < len(args):
                    source = args[i + 1]; i += 2
                elif args[i] == "--confidence" and i + 1 < len(args):
                    confidence = float(args[i + 1]); i += 2
                elif args[i] == "--emotion-tags" and i + 1 < len(args):
                    emotion_tags = [t.strip() for t in args[i + 1].split(",") if t.strip()]; i += 2
                elif args[i] == "--reason" and i + 1 < len(args):
                    reason = args[i + 1]; i += 2
                elif args[i] == "--core" and i + 1 < len(args):
                    core = args[i + 1].lower() in ("true", "1", "yes", "y"); i += 2
                else:
                    nm_args.append(args[i]); i += 1
            if len(nm_args) < 3:
                print(json.dumps({"error": "用法: write <entity> <kind> <value> [refs_dir] [--mode local] [--sentiment ...] [--source file_import|self_inferred|user_explicit] [--confidence 0..1] [--emotion-tags 占有,吃醋] [--reason 文本] [--core true|false]"}), file=sys.stderr)
                sys.exit(1)
            cmd_write(nm_args[0], nm_args[1], nm_args[2], refs_dir, mode, sentiment,
                      source, confidence, emotion_tags, reason, core)
        elif cmd == "search":
            nm_args = [a for a in args if not a.startswith("--")]
            if not nm_args:
                print(json.dumps({"error": "用法: search <query> [refs_dir]"}), file=sys.stderr)
                sys.exit(1)
            cmd_search(nm_args[0], refs_dir)
        elif cmd == "forget":
            reason = None
            kind = None
            nm_args = []
            i = 0
            while i < len(args):
                if args[i] == "--reason" and i + 1 < len(args):
                    reason = args[i + 1]; i += 2
                elif args[i] == "--kind" and i + 1 < len(args):
                    kind = args[i + 1]; i += 2
                else:
                    nm_args.append(args[i]); i += 1
            if not nm_args:
                print(json.dumps({"error": "用法: forget <entity|memory_id> [refs_dir] [--reason 文本] [--kind 类型]"}), file=sys.stderr)
                sys.exit(1)
            cmd_forget(nm_args[0], refs_dir, reason, kind)
        elif cmd == "stats":
            cmd_stats(refs_dir)
        elif cmd == "decay":
            cmd_decay(refs_dir)
        elif cmd == "vacuum":
            cmd_vacuum(refs_dir)
        elif cmd == "backup":
            cmd_backup(refs_dir)
        elif cmd == "recover":
            cmd_recover(refs_dir)
        elif cmd == "wellness":
            if len(args) < 1:
                print(json.dumps({"error": "用法: wellness <mood> [sleep_hours] [sleep_quality] [note] [refs_dir]"}), file=sys.stderr)
                sys.exit(1)
            mood = args[0]
            sleep_hours = args[1] if len(args) > 1 else "-"
            sleep_quality = args[2] if len(args) > 2 else "-"
            note = args[3] if len(args) > 3 else "-"
            cmd_wellness(mood, sleep_hours, sleep_quality, note, refs_dir)
        elif cmd == "init":
            if not args:
                print(json.dumps({"error": "用法: init <mode> [refs_dir]  mode: local"}), file=sys.stderr)
                sys.exit(1)
            cmd_init(args[0], refs_dir)
        else:
            print(json.dumps({"error": f"未知命令: {cmd}"}), file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
