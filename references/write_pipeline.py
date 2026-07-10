#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_pipeline.py —— 记忆写入管线（真实工程实现，非 LLM 散文）
===== PATCH v2.3 (by Plato) =====
相对 v2.2 的改动（均带 `PATCH v2.3` 注释，逐函数定位）：
  1) 新增 DEFAULT_ALIASES + normalize_entity()：写入前实体归一化，解决
     「拉面」vs「那家拉面店」名字对不上→两条都 active→上下文打架。
  2) cmd_write 支持 kind(=原 type) + sentiment(pos/neg/none) 字段；
     upsert 去重维度从「仅 entity」改为「(entity, kind)」——偏好与事件
     分层共存，不再互相覆盖。
  3) 偏好翻转：同一 (entity, kind=preference) 的新值写入时，旧 active 自动
     标 superseded（保留历史情绪，不删），检索只返回 active → 不污染。
  4) cmd_recover 去重键从 entity 改为 (entity, memory_id)，修复重复写场景
     静默丢数据。
其余（锁/WAL/原子写/备份/崩溃恢复）沿用 v2.2 已验证实现。
==============================
命令：
  write <entity> <kind> <value> [refs_dir] [--mode local|memorious] [--sentiment pos|neg|none]
  search <query> [refs_dir]
  recover [refs_dir]
  ...(其余命令同 v2.2)
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

# === PATCH v2.3 === 默认别名归一化表（运行时优先读 aliases.json，兜底用此表）
DEFAULT_ALIASES = {
    "那家拉面店": "拉面",
    "那家拉面": "拉面",
    "豚骨拉面": "拉面",
    "日式拉面": "拉面",
    "一兰拉面": "拉面",
    "味千拉面": "拉面",
    "拉面馆": "拉面",
    "拉面店": "拉面",
    "波霸奶茶": "奶茶",
    "珍珠奶茶": "奶茶",
    "一点点奶茶": "奶茶",
    "冰美式": "咖啡",
    "美式咖啡": "咖啡",
    "拿铁": "咖啡",
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
                            os.unlink(lock_path)
                            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
                            deadline = time.time() + 30
                            continue
                except (json.JSONDecodeError, KeyError):
                    pass
                os.close(fd)
                raise TimeoutError(f"获取锁超时（30s），锁持有者 PID={locker_data.get('pid', 'unknown')}")
            time.sleep(0.5)

def file_unlock(fd, refs_dir):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass
    lock_path = os.path.join(refs_dir, ".lock")
    try:
        os.unlink(lock_path)
    except FileNotFoundError:
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

# === PATCH v2.3 === 实体归一化：别名表精确命中 → 默认表 → 反向子串（entity 含已有标准名）
def normalize_entity(entity, refs_dir):
    """把口语化/变体实体名映射到标准名，避免同一事物分裂成多条 active 记忆。"""
    alias_path = os.path.join(refs_dir, "aliases.json")
    aliases = safe_read_json(alias_path) or {}
    if entity in aliases:
        return aliases[entity]
    if entity in DEFAULT_ALIASES:
        return DEFAULT_ALIASES[entity]
    # 反向子串：若本实体包含某个已存在的标准名（长度>=2，避免「面」过短误匹配），归一到它
    ei = safe_read_json(os.path.join(refs_dir, "entity_index.json")) or {"entities": {}}
    for std in ei.get("entities", {}).keys():
        if len(std) >= 2 and std != entity and std in entity:
            return std
    return entity

# ──────────────────────────────────────────
# 核心命令
# ──────────────────────────────────────────

def cmd_write(entity, etype, value, refs_dir, mode=None, sentiment=None):
    """完整 upsert 写入管线（PATCH v2.3: 归一化 + kind/sentiment 分层）"""
    # ── 0. 解析后端模式 ──
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
    if mode == "memorious":
        targets.append("memorious")

    # === PATCH v2.3 === 写入前归一化：这是「记忆不打架」的总闸门
    entity = normalize_entity(entity, refs_dir)
    kind = etype  # kind 语义 = 原 type 字段（preference / event / habit ...）

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
            "memory_id": new_id,
            "targets": targets
        })

        mem_path = os.path.join(refs_dir, "memory.json")
        ei_path = os.path.join(refs_dir, "entity_index.json")
        memory = safe_read_json(mem_path) or []
        ei = safe_read_json(ei_path) or {"version": 1, "entities": {}}

        # === PATCH v2.3 === 去重维度改为 (entity, kind)：偏好与事件分层，互不覆盖
        # 兼容旧 entry（只有 type 无 kind）用 entry.get("kind") or entry.get("type")
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
                    elif len(old_val) > 20 and (value in old_val or old_val in value):
                        similar_idx = i
                        break
                    else:
                        existing_idx = i
                        break

        def _make_entry():
            return {
                "id": new_id,
                "entity": entity,
                "type": etype,
                "kind": kind,            # === PATCH v2.3 ===
                "sentiment": sentiment,  # === PATCH v2.3 ===
                "value": value,
                "created": ts_now(),
                "updated": ts_now(),
                "status": "active",
                "source": "auto_detect",
                "confidence": 1.0
            }

        if similar_idx is not None:
            memory[similar_idx]["value"] = value
            memory[similar_idx]["updated"] = ts_now()
            if sentiment is not None:
                memory[similar_idx]["sentiment"] = sentiment
            new_id = memory[similar_idx]["id"]
        elif existing_idx is not None:
            # 内容不同（如偏好翻转：喜欢→讨厌）→ 旧 active 标 superseded，新建
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
                "kind": kind,                 # === PATCH v2.3 ===
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
            "mode": mode,
            "targets": targets
        }, ensure_ascii=False))

    finally:
        file_unlock(fd, refs_dir)

def cmd_search(query, refs_dir):
    """本地检索（不依赖 memorious）；PATCH v2.3: 仅返回 active，结果含 kind/sentiment"""
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
                if entry.get("id") == mid and entry.get("entity") == query:
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

    print(json.dumps({
        "query": query,
        "results_count": len(results),
        "results": results[:5]
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

    type_counts = {}
    for e in entities.values():
        t = e.get("kind") or e.get("type", "general")
        type_counts[t] = type_counts.get(t, 0) + 1

    timestamps = [m.get("created", "") for m in memory if m.get("created")]
    timestamps.sort()

    print(json.dumps({
        "total_entities": len(entities),
        "pending": pending,
        "confirmed": confirmed,
        "total_memories": len(memory),
        "active": active,
        "superseded": superseded,
        "entity_kinds": type_counts,
        "oldest_memory": timestamps[0] if timestamps else None,
        "newest_memory": timestamps[-1] if timestamps else None
    }, ensure_ascii=False, indent=2))

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

def cmd_init(mode, refs_dir):
    os.makedirs(refs_dir, exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".backup"), exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".archive"), exist_ok=True)

    backend_path = os.path.join(refs_dir, "backend_config.json")
    safe_write_json(backend_path, {
        "mode": mode,
        "backend_info": "Marvis 内置 memorious MCP" if mode == "memorious" else "纯本地文件模式"
    })

    mem_path = os.path.join(refs_dir, "memory.json")
    if not os.path.exists(mem_path):
        safe_write_json(mem_path, [])

    ei_path = os.path.join(refs_dir, "entity_index.json")
    if not os.path.exists(ei_path):
        safe_write_json(ei_path, {"version": 1, "entities": {}})

    # === PATCH v2.3 === 初始化时一并建立别名表（可被 runtime 改写）
    alias_path = os.path.join(refs_dir, "aliases.json")
    if not os.path.exists(alias_path):
        safe_write_json(alias_path, DEFAULT_ALIASES)

    pref_path = os.path.join(refs_dir, "preferences.json")
    if not os.path.exists(pref_path):
        safe_write_json(pref_path, {
            "anchors": {},
            "preferences": {},
            "identity": {},
            "persona": {}
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
    """从 WAL 恢复崩溃时未完成持久化的写入。
    === PATCH v2.3 === 去重键从 entity 改为 (entity, memory_id)，修复重复写场景丢数据
    """
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

    # === PATCH v2.3 === 按 (entity, memory_id) 精确去重，不再按 entity 名去重
    uncommitted_map = {}
    for e in entries:
        if e.get("op") != "upsert":
            continue
        ent = e.get("entity")
        mid = e.get("memory_id", "")
        if (ent, mid) in committed:
            continue
        if not mid and any(c[0] == ent for c in committed):
            continue
        if not mid:
            if (ent, None) in committed or (ent, "") in committed:
                continue
        # 精确键：每条未提交 upsert 各保留一次，重复写不再被吞
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
                sentiment=e.get("sentiment")
            )
            recovered.append(e["entity"])
        except Exception as ex:
            print(json.dumps({
                "error": f"recover failed for '{e['entity']}': {ex}"
            }))

    print(json.dumps({
        "status": "recovered" if recovered else "partial",
        "recovered_count": len(recovered),
        "recovered_entities": recovered,
        "total_uncommitted": len(uncommitted)
    }, ensure_ascii=False))


def cmd_wellness(mood, sleep_hours, sleep_quality, note, refs_dir):
    import datetime as _dt
    today = _dt.date.today().isoformat()

    well_path = os.path.join(refs_dir, "wellness.json")
    data = {"records": []}
    if os.path.exists(well_path):
        with open(well_path, "r", encoding="utf-8") as wf:
            data = json.load(wf)

    entry = {
        "date": today,
        "mood": mood,
        "recorded_at": _dt.datetime.now().astimezone().isoformat()
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
    if args and os.path.isdir(args[-1]):
        refs_dir = os.path.abspath(args.pop())

    try:
        if cmd == "write":
            mode = None
            sentiment = None
            nm_args = []
            i = 0
            while i < len(args):
                if args[i] == "--mode" and i + 1 < len(args):
                    mode = args[i + 1]
                    i += 2
                # === PATCH v2.3 === 解析 --sentiment
                elif args[i] == "--sentiment" and i + 1 < len(args):
                    sentiment = args[i + 1]
                    i += 2
                else:
                    nm_args.append(args[i])
                    i += 1
            if len(nm_args) < 3:
                print(json.dumps({"error": "用法: write <entity> <kind> <value> [refs_dir] [--mode local|memorious] [--sentiment pos|neg|none]"}), file=sys.stderr)
                sys.exit(1)
            cmd_write(nm_args[0], nm_args[1], nm_args[2], refs_dir, mode, sentiment)
        elif cmd == "search":
            mode = "local"
            nm_args = []
            i = 0
            while i < len(args):
                if args[i] == "--mode" and i + 1 < len(args):
                    mode = args[i + 1]
                    i += 2
                else:
                    nm_args.append(args[i])
                    i += 1
            if not nm_args:
                print(json.dumps({"error": "用法: search <query> [refs_dir]"}), file=sys.stderr)
                sys.exit(1)
            cmd_search(nm_args[0], refs_dir)
        elif cmd == "stats":
            cmd_stats(refs_dir)
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
                print(json.dumps({"error": "用法: init <mode> [refs_dir]  mode: local 或 memorious"}), file=sys.stderr)
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
