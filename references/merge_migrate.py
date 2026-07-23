#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
merge_migrate.py —— 双源记忆合并迁移（PATCH v2.6 by 伙伴）
============================================
用途：agent "接入记忆技能"时，把两套记忆安全并成一套干净初始库：
  源 A（文件，权威）：已有的本地 memory.json（如 友商 产物 / 历史沉淀）
  源 B（自身，缓存）：agent 自述记忆——让它先把自己的印象 dump 成一个文件

合并规则（信任优先级固化）：
  1) 实体归一化（别名表 + 反向子串）。
  2) 按 (entity, kind) 合并去重；同一 (entity,kind) 文件侧 active 优先，自身侧被丢弃
     （除非文件侧为空 → 自身侧保留，但标 source=self_inferred，
      低置信 → status=pending 不进 active，等用户确认再升级）。
  3) 冲突检测：若文件侧与自身侧都 active 且 value 不同 → 以文件为准，自身侧记为
     "被覆盖"（写入报告，不进库），避免"两套记忆互相矛盾"。
  4) 字段校验（防 示例式字段错位）：kind 必须 ∈ ALLOWED_KINDS；value 非空且
     不等于某个 kind 关键字（如误把 "event" 写进 value）。不合规项跳过并报告。
  5) 不删文件侧任何历史（superseded 保留）；只补自身侧沉淀。

输出：
  <out_dir>/memory.json        合并后干净库（覆盖写，先备份原文件）
  <out_dir>/entity_index.json  重建索引
  <out_dir>/merge_report.json  合并明细（冲突/跳过/新增/覆盖）

用法：
  merge_migrate.py --file-memory <path> --self-memory <path> --out <dir>
                   [--aliases <aliases.json>] [--auto-approve-self]
                   [--self-format json|lines]

  --self-format lines：每行 "entity | kind | value [| sentiment] [| confidence]"
  --self-format json：JSON 数组，元素 {entity,kind,value,sentiment?,confidence?,emotion_tags?}
  --auto-approve-self：自身侧高置信也直接落 active（默认进 pending，最安全）
"""

import argparse
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))

ALLOWED_KINDS = {
    "preference", "event", "habit", "rule", "scene",
    "relationship", "emotion",
    "identity", "milestone", "general",   # 与 SKILL.md §3.1 / §3.3 状态机对齐
}
# 这些词若出现在 value 里，基本是"字段错位"（应写在 kind），视为损坏
KIND_KEYWORDS = ALLOWED_KINDS
SELF_PENDING_THRESHOLD = 0.8

DEFAULT_ALIASES = {
    "旧书店": "读书", "旧书店": "读书", "科幻小说": "读书",
    "杂志": "读书", "图书馆": "读书", "报刊亭": "读书",
    "书店": "读书", "书店": "读书",
    "篮球": "运动", "跑步": "运动", "游泳": "运动",
}


def ts_now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def normalize_entity(entity, aliases):
    if entity in aliases:
        return aliases[entity]
    for std in aliases.keys():
        if len(std) >= 2 and std != entity and std in entity:
            return std
    return entity


def parse_self_memory(path, fmt):
    """返回 list of dict: {entity, kind, value, sentiment, confidence, emotion_tags}"""
    items = []
    if fmt == "json":
        data = safe_read_json(path)
        if not isinstance(data, list):
            raise ValueError("--self-format json 要求顶层是数组")
        for d in data:
            items.append({
                "entity": d.get("entity", "").strip(),
                "kind": (d.get("kind") or d.get("type") or "").strip(),
                "value": (d.get("value") or "").strip(),
                "sentiment": d.get("sentiment"),
                "confidence": float(d.get("confidence", 0.6)),
                "emotion_tags": d.get("emotion_tags") or [],
            })
    else:  # lines
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3:
                    continue
                conf = 0.6
                sent = None
                if len(parts) >= 4 and parts[3]:
                    sent = parts[3]
                if len(parts) >= 5 and parts[4]:
                    try:
                        conf = float(parts[4])
                    except ValueError:
                        pass
                items.append({
                    "entity": parts[0],
                    "kind": parts[1],
                    "value": parts[2],
                    "sentiment": sent,
                    "confidence": conf,
                    "emotion_tags": [],
                })
    return items


def validate(entry):
    """返回 (ok, reason)。防 示例式字段错位。"""
    ent = entry.get("entity", "")
    kind = entry.get("kind", "")
    val = entry.get("value", "")
    if not ent:
        return False, "entity 为空"
    if kind not in ALLOWED_KINDS:
        return False, f"kind='{kind}' 非法（字段错位？应写 value 的内容写进了 kind）"
    if not val:
        return False, "value 为空"
    if val in KIND_KEYWORDS:
        return False, f"value='{val}' 是 kind 关键字（字段错位：应写在 kind 的内容写进了 value）"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file-memory", required=True)
    ap.add_argument("--self-memory", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--aliases", default=None)
    ap.add_argument("--auto-approve-self", action="store_true")
    ap.add_argument("--self-format", default="json", choices=["json", "lines"])
    args = ap.parse_args()

    aliases = DEFAULT_ALIASES
    if args.aliases:
        a = safe_read_json(args.aliases)
        if a:
            aliases = {**DEFAULT_ALIASES, **a}

    # 载入文件侧（权威）
    file_mem = safe_read_json(args.file_memory) or []
    if not isinstance(file_mem, list):
        file_mem = []

    # 载入自身侧
    self_items = parse_self_memory(args.self_memory, args.self_format)

    report = {
        "file_count_raw": len(file_mem),
        "self_count_raw": len(self_items),
        "kept_file": 0,
        "file_invalid": [],
        "self_approved_active": 0,
        "self_pending": 0,
        "self_overridden": 0,
        "self_skipped_invalid": [],
        "self_deduped": 0,
        "merged_at": ts_now(),
    }

    merged = []  # 最终 memory.json 内容
    file_index = {}  # (entity, kind) -> entry（仅 active）

    # 1) 先放文件侧（权威，损坏也保留历史，但标记 file_invalid 让人修）
    for e in file_mem:
        ent = normalize_entity(e.get("entity", ""), aliases)
        kind = e.get("kind") or e.get("type") or ""
        e["entity"] = ent
        e["kind"] = kind
        ok, reason = validate({"entity": ent, "kind": kind, "value": e.get("value", "")})
        if not ok:
            report["file_invalid"].append({"entity": ent, "reason": reason})
        if e.get("status", "active") == "active":
            file_index[(ent, kind)] = e
        merged.append(e)
        report["kept_file"] += 1

    # 2) 合并自身侧
    seen_self = set()
    for it in self_items:
        ent = normalize_entity(it["entity"], aliases)
        kind = it["kind"]
        val = it["value"]
        cand = {"entity": ent, "kind": kind, "value": val,
                "sentiment": it["sentiment"], "confidence": it["confidence"],
                "emotion_tags": it["emotion_tags"]}
        ok, reason = validate(cand)
        if not ok:
            report["self_skipped_invalid"].append({"item": cand, "reason": reason})
            continue

        key = (ent, kind)
        # 去重：自身侧同 (entity,kind,value) 只处理一次
        dedupe_key = (ent, kind, val)
        if dedupe_key in seen_self:
            report["self_deduped"] += 1
            continue
        seen_self.add(dedupe_key)

        if key in file_index:
            # 冲突：文件侧已 active → 以文件为准，自身侧被覆盖（不进库）
            file_val = file_index[key].get("value", "")
            if file_val != val:
                report["self_overridden"] += 1
                # 记录到报告即可，不污染库
            continue  # 不进库

        # 仅自身侧有 → 沉淀为 self_inferred
        init_status = "active" if (args.auto_approve_self and it["confidence"] >= SELF_PENDING_THRESHOLD) else "pending"
        new_id = f"mem_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        entry = {
            "id": new_id,
            "entity": ent,
            "type": kind,
            "kind": kind,
            "sentiment": it["sentiment"],
            "source": "self_inferred",
            "confidence": it["confidence"],
            "emotion_tags": it["emotion_tags"] or [],
            "value": val,
            "created": ts_now(),
            "updated": ts_now(),
            "last_recalled": None,
            "status": init_status,
        }
        merged.append(entry)
        if init_status == "active":
            report["self_approved_active"] += 1
            file_index[key] = entry
        else:
            report["self_pending"] += 1

    # 3) 重建 entity_index.json
    entities = {}
    for e in merged:
        ent = e.get("entity", "")
        kind = e.get("kind") or e.get("type")
        if ent not in entities:
            entities[ent] = {
                "count": 0, "status": "confirmed", "type": kind, "kind": kind,
                "aliases": [], "first_seen": e.get("created", ts_now()),
                "last_seen": e.get("updated", ts_now()), "last_memory_id": e.get("id"),
            }
        ent_obj = entities[ent]
        ent_obj["count"] += 1
        ent_obj["last_seen"] = e.get("updated", ent_obj["last_seen"])
        ent_obj["last_memory_id"] = e.get("id")

    # 4) 写出（先备份原 out 的 memory.json）
    os.makedirs(args.out, exist_ok=True)
    out_mem = os.path.join(args.out, "memory.json")
    out_ei = os.path.join(args.out, "entity_index.json")
    out_rep = os.path.join(args.out, "merge_report.json")
    if os.path.exists(out_mem):
        shutil.copy2(out_mem, out_mem + ".bak")
    with open(out_mem, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    with open(out_ei, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "entities": entities}, f, ensure_ascii=False, indent=2)
    with open(out_rep, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "status": "merged",
        "out_dir": os.path.abspath(args.out),
        "summary": report,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
