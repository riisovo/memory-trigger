"""graph 镜像后端（可选插件，v2.8 拆耦合后独立）。

仅当 backend_config.json 设 mirror_mode=='graph' 且 graph_db_path 存在时，
才由 write_pipeline 动态加载本模块。本模块完全自包含——不反向依赖
write_pipeline 的任何全局符号，从而让核心在默认本地模式下彻底不接触 sqlite3。

- mirror_to_graph(): 把 trigger 写入的记忆 upsert 进 graph 的 memory.db
  （memories 表 + core_memory 表），FTS 同步 rebuild。
- mirror 失败只告警、不阻断主写入（trigger 文件仍是权威源）。
"""
import os
import json
import sys
import sqlite3
from datetime import datetime, timezone

# 与 write_pipeline.CORE_KINDS 保持一致：核心记忆种类永不衰减、必镜像
CORE_KINDS = {"relationship", "identity"}


def _to_utc_z(iso_local):
    """把 trigger 的 +08:00 ISO 转 UTC Z 字符串，供 graph db 存储。无效则回退 now UTC。"""
    if not iso_local:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        dt = datetime.fromisoformat(iso_local)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def mirror_to_graph(entry, refs_dir, cfg=None):
    """把 trigger 写入的记忆镜像到 graph 的 memory.db（实现两套记忆打通）。
       读 backend_config.json 的 mirror_mode=='graph' 才生效；否则跳过（零侵入）。
       - memories 表：upsert（按 id），填满 NOT NULL 列，FTS rebuild 同步。
       - core=true 或 CORE_KINDS：同步写 core_memory 表。
       mirror 失败只告警、不阻断主写入（trigger 文件仍是权威源）。
       参数 cfg 可选：调用方已读过 backend_config 可传入，避免重复读。"""
    try:
        if cfg is None:
            cfg_path = os.path.join(refs_dir, "backend_config.json")
            if not os.path.exists(cfg_path):
                return
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        if cfg.get("mirror_mode") != "graph":
            return
        gdb = cfg.get("graph_db_path")
        if not gdb or not os.path.exists(gdb):
            return
        mid = entry.get("id")
        if not mid:
            return
        title = f'{entry.get("entity", "?")}:{entry.get("kind", "general")}'
        content = entry.get("value", "")
        tags = json.dumps(
            [entry.get("entity", ""), entry.get("kind", ""), entry.get("source", "")],
            ensure_ascii=False,
        )
        created = _to_utc_z(entry.get("created"))
        updated = _to_utc_z(entry.get("updated") or entry.get("created"))
        is_core = bool(entry.get("core")) or (entry.get("kind") in CORE_KINDS)
        g = sqlite3.connect(gdb)
        try:
            cur = g.execute("SELECT rowid FROM memories WHERE id=?", (mid,)).fetchone()
            if cur:
                g.execute(
                    """UPDATE memories SET title=?,content=?,tags=?,importance_score=?,confidence_score=?,updated_at=? WHERE id=?""",
                    (title, content, tags, float(entry.get("importance", 1.0)),
                     float(entry.get("confidence", 1.0)), updated, mid),
                )
            else:
                g.execute(
                    """INSERT INTO memories(id,scope,namespace,title,content,document_type,source,author,tags,access_level,language,version,created_at,updated_at,access_count,importance_score,confidence_score,condensation_level,provenance,stability,volatility,verification_tier)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (mid, 'user', None, title, content, 'memory', entry.get("source"), 'agent',
                     tags, 'public', 'zh', 1, created, updated, 0,
                     float(entry.get("importance", 1.0)), float(entry.get("confidence", 1.0)),
                     'full', 'manual', 1.0, 'normal', 'source_verified'),
                )
            if is_core:
                g.execute(
                    """INSERT OR REPLACE INTO core_memory(scope,namespace,content,char_limit,updated_at) VALUES(?,?,?,?,?)""",
                    ('user', None, content, 2000, updated),
                )
            # 外部内容 FTS 表需手动同步：rebuild 全表（数据量小，毫秒级）
            g.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
            g.commit()
        finally:
            g.close()
    except Exception as e:
        sys.stderr.write(f"[mirror_to_graph] skipped: {e}\n")
