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

  【PATCH v2.7 (by 伙伴) —— 人情味层】
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
        [--context 当时的气氛] [--expires YYYY-MM-DD]
  search <query> [refs_dir]
  decay [refs_dir]
  forget <entity|memory_id> [refs_dir] [--reason 文本] [--kind 类型]
  deny <entity|memory_id> [refs_dir] [--reason 文本]      # 否认降权（用户纠正过的）
  expire [refs_dir]                                       # 到期记忆检查（到期移除/临期提醒）
  recall [refs_dir] [--limit N]                           # 主动回忆建议（值得此刻想起的）
  promise add <内容> [refs_dir] [--deadline YYYY-MM-DD]   # 承诺建档
  promise done <promise_id> [refs_dir]                    # 完成划掉
  promise list [refs_dir]                                 # 承诺清单
  promise check [refs_dir]                                # 主动戳未完成承诺
  recover [refs_dir]
  stats [refs_dir]
  vacuum [refs_dir]
  backup [refs_dir]
  wellness <mood> [sleep_hours] [sleep_quality] [note] [refs_dir]
  init <mode> [refs_dir]
"""

try:
    import fcntl
except ImportError:
    fcntl = None  # 非 POSIX 平台（如 Windows）无 fcntl：降级为无文件锁，功能正常，仅失去跨进程并发保护
import json
import math
import os
import sys
import time
import shutil
import uuid
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))  # UTC+8

# === PATCH v2.8.3 (加固) === 数值合法区间
CONFIDENCE_MIN, CONFIDENCE_MAX = 0.0, 1.0
SLEEP_HOURS_MIN, SLEEP_HOURS_MAX = 0.0, 24.0

# === v2.8.4 === wellness 心情记录条数上限（截断保留最近 N 条，防无界增长）
WELLNESS_MAX_RECORDS = 365

# === v2.9 人情味增强 === 到期记忆 / 否认降权 / 主动回忆 / 承诺追踪
# 到期记忆：expires_at 到期后自动移出 active（不再被检索召回），数据保留可查
EXPIRY_REMIND_DAYS = 3            # 到期前多少天列为"即将到期"提醒
# 否认降权：用户否认一条记忆 → importance ×DENY_FACTOR 且记录 deny_count；
# 累积 2 次否认 → 直接转 pending（彻底退出检索，需重新确认才复活）
DENY_FACTOR = 0.1
DENY_TO_PENDING_AFTER = 2
# 主动回忆：recall 建议的评分加分（让"从未被提起 / 带情感 / 快到期"的记忆更容易被想起）
RECALL_BONUS_NEVER = 0.15
RECALL_BONUS_EMOTION = 0.10
RECALL_BONUS_EXPIRING = 0.20

# === PATCH v2.6 B2 === 允许 kind 取值（含情感维度扩展 + 与 SKILL.md §3.1/§3.3 对齐）
ALLOWED_KINDS = {
    "preference", "event", "habit", "rule", "scene",
    "relationship", "emotion",
    "identity", "milestone", "general",  # 与 SKILL.md §3.1 / §3.3 状态机对齐
}
# self_inferred 低于此置信度 → 落 pending（不进 active，防幻觉固化）
SELF_INFERRED_PENDING_THRESHOLD = 0.8

# === PATCH v2.7 (人情味层) === 遗忘曲线 + 核心记忆钉死
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

def _parse_expiry(v):
    """到期时间解析：接受 YYYY-MM-DD 或 ISO 时间，返回 UTC+8 ISO 秒级字符串；空值返回 None。
    无法解析明确拒绝（防脏数据），避免到期记忆永远不触发。"""
    if v is None or v == "" or v == "-":
        return None
    s = _safe_str(v).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return dt.isoformat(timespec="seconds")
    except ValueError:
        raise ValueError(f"expires_at 无法解析（应为 YYYY-MM-DD 或 ISO 时间）：{s!r}")

def safe_read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, json.JSONDecodeError, UnicodeDecodeError):
        return None

# === PATCH v2.8.3 (加固) === 类型兜底：脏数据（None / 字符串 / 缺字段）不得让运算崩溃
def _safe_str(v, default=""):
    """任何值安全转字符串：None / 数字 / dict 都不炸。"""
    if v is None:
        return default
    if isinstance(v, str):
        return v
    try:
        return str(v)
    except Exception:
        return default

def _safe_float(v, default=0.0, allow_nonfinite=False):
    """任何值安全转有限浮点：None / 'abc' / NaN / Inf 一律退回 default。"""
    if isinstance(v, bool):
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if not allow_nonfinite and not math.isfinite(f):
        return default
    return f

def _require_finite(name, v, lo, hi):
    """入口校验：必须是 [lo, hi] 区间内的有限数，否则明确拒绝（防 NaN/Inf 污染 JSON）。"""
    if isinstance(v, bool):
        raise ValueError(f"{name} 必须是数值，收到布尔值 {v!r}")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} 必须是数值，收到 {v!r}")
    if not math.isfinite(f):
        raise ValueError(
            f"{name}={v!r} 非有限数（NaN/Infinity）。写入会让 JSON 出现裸 NaN 字面量，"
            f"非 Python 客户端（JS/Go/Rust）将无法解析该记忆库，故拒绝。"
        )
    if not (lo <= f <= hi):
        raise ValueError(f"{name}={f} 超出合法范围 [{lo}, {hi}]")
    return f

def _quarantine(path, why):
    """把损坏文件另存为 .corrupt.<ts>，绝不原地丢弃——数据可人工找回。

    === PATCH v2.8.3 === 时间戳精确到微秒 + 同名兜底加序号。
    原来用秒级 ts，同一秒内连续损坏会 copy2 同名覆盖，只剩最后一份证据；
    而"反复损坏"恰恰是坏客户端/坏磁盘的典型症状，前几份才是诊断关键。
    """
    if not os.path.exists(path):
        return None
    stamp = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S-%f")
    dst = f"{path}.corrupt.{stamp}"
    seq = 1
    while os.path.exists(dst):
        dst = f"{path}.corrupt.{stamp}.{seq}"
        seq += 1
    try:
        shutil.copy2(path, dst)
        sys.stderr.write(f"[memory] 检测到损坏文件 {os.path.basename(path)}（{why}），"
                         f"已隔离副本 → {os.path.basename(dst)}\n")
        return dst
    except Exception as e:
        sys.stderr.write(f"[memory] 隔离损坏文件失败 {path}: {e}\n")
        return None

def _restore_from_backup(refs_dir, stem):
    """从 .backup 取最近一份快照恢复（stem: 'memory' / 'entity_index'）。找不到返回 None。"""
    bkp_dir = os.path.join(refs_dir, ".backup")
    if not os.path.isdir(bkp_dir):
        return None
    for path in _backup_candidates(bkp_dir, stem):
        data = safe_read_json(path)
        if data is not None:
            sys.stderr.write(f"[memory] 已从备份 {os.path.basename(path)} 恢复 {stem}.json\n")
            return data
    return None

def read_json_typed(path, expect, refs_dir=None, stem=None):
    """读 JSON 并保证类型正确（expect=list|dict）。

    这是 v2.8.3 的核心加固点：此前 `safe_read_json(...) or []` 会把
    「文件损坏」和「文件不存在」混为一谈，导致损坏后写入直接用空列表覆盖，
    **静默清空全部历史记忆**。现在的策略：
      1. 文件不存在        → 返回空容器（正常冷启动）
      2. 文件损坏/类型不符 → 先隔离 .corrupt 副本，再尝试从 .backup 恢复；
                            恢复不到才以空容器继续（此时原始数据仍在 .corrupt 里）
    """
    empty = [] if expect is list else {}
    if not os.path.exists(path):
        return empty
    data = safe_read_json(path)
    why = None
    if data is None:
        why = "JSON 解析失败"
    elif not isinstance(data, expect):
        why = f"类型应为 {expect.__name__}，实际为 {type(data).__name__}"
    if why is None:
        return data
    _quarantine(path, why)
    if refs_dir and stem:
        restored = _restore_from_backup(refs_dir, stem)
        if isinstance(restored, expect):
            return restored
    return empty

def safe_write_json(path, data):
    dirname = os.path.dirname(path)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            # allow_nan=False：最后一道防线，宁可写失败也不产出非法 JSON（裸 NaN/Infinity）
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        # os.replace 跨平台原子替换：Windows 上目标已存在时 os.rename 会抛
        # FileExistsError，导致每次改写已有文件都失败；os.replace 在 POSIX/Windows
        # 均覆盖式替换（见 #11）。
        os.replace(tmp_path, path)
    except PermissionError as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise PermissionError(
            f"无写权限：无法写入 {path}。常见于记忆库目录由 root/其他用户创建、"
            f"当前 MCP 进程无写权限，或 backend_config 开启 mirror_mode=graph 但其 sqlite 库不可写。"
            f"原错误：{e}"
        )
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
            if fcntl is not None:
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
                # === PATCH v2.8.3 === locker_pid 先给默认值：此前 .lock 内容损坏时
                # json.loads 抛错走 except pass，下面却仍引用 locker_data →
                # UnboundLocalError 掩盖了真正的 TimeoutError，调用方无从判断是超时。
                locker_pid = "unknown"
                try:
                    locker_data = json.loads(raw)
                    if isinstance(locker_data, dict):
                        locker_pid = locker_data.get("pid", "unknown")
                    if isinstance(locker_pid, int):
                        try:
                            os.kill(locker_pid, 0)
                        except OSError:
                            os.close(fd)
                            # 不 unlink：truncate 就地复用同一 inode，保持锁互斥语义
                            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_TRUNC)
                            deadline = time.time() + 30
                            continue
                except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                    pass  # 锁文件内容损坏：无法判断持有者，按超时处理
                os.close(fd)
                raise TimeoutError(f"获取锁超时（30s），锁持有者 PID={locker_pid}")
            time.sleep(0.5)

def file_unlock(fd, refs_dir):
    """只释放锁和关闭 fd，不 unlink 锁文件（保持 flock inode 互斥语义）。"""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except Exception:
        pass

def wal_append(refs_dir, entry):
    path = os.path.join(refs_dir, ".wal.jsonl")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    except PermissionError as e:
        raise PermissionError(
            f"无写权限：无法追加 WAL {path}（记忆库目录 {refs_dir} 当前进程不可写）。原错误：{e}"
        )

def _wal_prune(refs_dir, keep):
    """=== 修复 (finding #3) === WAL 重放后就地截断，只保留 keep（重放失败）的条目，
    其余清空，彻底阻断 WAL 的无限增长。

    此前 cmd_recover 只重放未提交行、从不截断 .wal.jsonl：每跑一次磁盘里就多攒一批
    upsert+commit 行（实测 1→3→5→7… 只增不减）。虽然已落盘的 memory_id 会被跳过不
    重放，但 WAL 文件本身永不缩小，长期运行等同于缓慢泄漏磁盘。

    注意：重放时 cmd_write 会向 WAL 追加它自己的新 upsert+commit，这些代表**已成功
    落盘**的记录，不需要再被恢复——截断时一并清除是正确行为。整个重写在文件锁内完成，
    避免与并发写入竞争。
    """
    path = os.path.join(refs_dir, ".wal.jsonl")
    fd = file_lock(refs_dir)
    try:
        lines = []
        for e in keep:
            if isinstance(e, dict):
                try:
                    lines.append(json.dumps(e, ensure_ascii=False))
                except (TypeError, ValueError):
                    continue
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(ln + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        file_unlock(fd, refs_dir)


def _backup_candidates(bkp_dir, stem):
    """该 stem 的备份快照路径列表，从新到旧。

    === PATCH v2.8.3 === 按文件 mtime 排序而不是文件名：v2.8.3 起备份时间戳
    改为微秒格式（旧格式带 '+08-00' 时区后缀），两种格式混在一起做字符串
    排序会错判新旧（'+' < '-'），导致恢复时取到更旧的快照。mtime 不受
    命名格式影响，且升级前后都成立。
    """
    try:
        names = [f for f in os.listdir(bkp_dir)
                 if f.startswith(stem + "_") and f.endswith(".json")]
    except OSError:
        return []
    paths = [os.path.join(bkp_dir, n) for n in names]
    def _key(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0
    return sorted(paths, key=_key, reverse=True)

def _latest_backup_path(bkp_dir, stem):
    """取 .backup 里该 stem 最新一份快照的路径，没有则 None。"""
    cands = _backup_candidates(bkp_dir, stem)
    return cands[0] if cands else None

_BACKUP_EXPECT = {"entity_index": dict, "memory": list}

def backup_files(refs_dir):
    """快照 entity_index.json + memory.json 到 .backup。

    === PATCH v2.8.3 (3.3a) === 四处改动，缺一不可：
      1. **拒绝备份损坏文件**（最关键）：备份前先校验 JSON 可解析且类型正确。
         此前会把已损坏的 memory.json 原样拍进 .backup，等于亲手毁掉最后的
         救命快照——恢复时读到的全是坏数据，历史记忆真正永久丢失。
      2. **时间戳带微秒**：原来是秒级，同一秒内的两次备份会 copy2 同名覆盖。
         实测「写入→损坏→再写入」全发生在 1 秒内时，好快照被坏快照顶掉。
      3. **内容去重**：与最新快照逐字节相同则整体跳过，避免 write-through 后
         下一次写入前又拍一张一模一样的快照，把保留窗口（20 份）迅速冲掉。
      4. **配对写入**：只要任一文件有变化，两个文件就用同一个 ts 一起备份。
         回滚逻辑靠文件名里的 ts 配对取回两个文件，若只备份其中一个，会导致
         回滚后 entity_index 与 memory 版本错位（索引指向不存在的记忆）。
    """
    bkp_dir = os.path.join(refs_dir, ".backup")
    os.makedirs(bkp_dir, exist_ok=True)

    targets = []
    changed = False
    for fname in ("entity_index.json", "memory.json"):
        src = os.path.join(refs_dir, fname)
        if not os.path.exists(src):
            continue
        stem = os.path.splitext(fname)[0]
        # 1. 损坏/类型不符的文件绝不进备份，保住上一份可用快照
        data = safe_read_json(src)
        if data is None or not isinstance(data, _BACKUP_EXPECT[stem]):
            sys.stderr.write(f"[memory] {fname} 当前不可解析，已跳过备份以保住上一份可用快照\n")
            continue
        targets.append((src, stem))
        prev = _latest_backup_path(bkp_dir, stem)
        if prev is None:
            changed = True
            continue
        try:
            with open(src, "rb") as f1, open(prev, "rb") as f2:
                if f1.read() != f2.read():
                    changed = True
        except OSError:
            changed = True
    if not targets or not changed:
        return

    # 2. 微秒级时间戳，杜绝同秒覆盖
    ts = datetime.now(TZ).strftime("%Y-%m-%dT%H-%M-%S-%f")
    for src, stem in targets:
        shutil.copy2(src, os.path.join(bkp_dir, f"{stem}_{ts}.json"))

    _prune_backups(bkp_dir, keep_groups=10)

def _prune_backups(bkp_dir, keep_groups=10):
    """按「快照组」淘汰旧备份，保留最近 keep_groups 组。

    === PATCH v2.8.3 (4.5) === 原实现是 `sorted(os.listdir())` 后从头删到
    只剩 20 个文件。文件名字母序里 `entity_index_*` 恒排在 `memory_*` 之前，
    于是清理总是优先删光 entity_index 快照——实测跑成 memory 16 份、
    entity_index 仅 4 份，能配对的只有 4 组。一旦触发回滚，找不到同 ts 的
    entity_index，就只回滚了 memory，索引与记忆版本错位。
    现在改为按同一 ts 归组、整组淘汰，保证任何时刻两类快照都成对存在。
    """
    groups = {}
    try:
        names = os.listdir(bkp_dir)
    except OSError:
        return
    for name in names:
        if not name.endswith(".json"):
            continue
        base = name[:-len(".json")]
        for stem in _BACKUP_EXPECT:
            if base.startswith(stem + "_"):
                groups.setdefault(base[len(stem) + 1:], []).append(name)
                break
    if len(groups) <= keep_groups:
        return
    # 组的新旧以组内最新 mtime 为准（兼容 v2.8.3 之前的旧命名格式）
    def _group_mtime(files):
        best = 0.0
        for f in files:
            try:
                best = max(best, os.path.getmtime(os.path.join(bkp_dir, f)))
            except OSError:
                pass
        return best
    ordered = sorted(groups.items(), key=lambda kv: _group_mtime(kv[1]), reverse=True)
    for _, files in ordered[keep_groups:]:
        for f in files:
            try:
                os.unlink(os.path.join(bkp_dir, f))
            except OSError:
                pass

# === PATCH v2.3 === 实体归一化
def normalize_entity(entity, refs_dir, substring_match=False):
    """把实体名映射到标准名。

    === PATCH v2.8.3 === `substring_match` 默认关闭。
    此前只要已有实体名是新实体名的子串就强行归并（len>=2 且 std in entity），
    于是「读书笔记软件选型」被吞成「读书」、「运动损伤康复」被吞成「运动」——
    两件完全不同的事被压成一条，语义永久丢失且不可逆；search 的查询词同样被改写，
    导致精确查询召回一堆无关结果。现在只认 aliases.json / DEFAULT_ALIASES 里
    **显式配置**的精确映射，绝不再做启发式子串归并。
    """
    if not isinstance(entity, str):
        return entity
    alias_path = os.path.join(refs_dir, "aliases.json")
    aliases = read_json_typed(alias_path, dict)
    if entity in aliases:
        return aliases[entity]
    if entity in DEFAULT_ALIASES:
        return DEFAULT_ALIASES[entity]
    if substring_match:
        ei = read_json_typed(os.path.join(refs_dir, "entity_index.json"), dict)
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
    if not isinstance(entry, dict):
        return False
    if "core" in entry:
        return bool(entry.get("core"))
    return entry.get("kind") in CORE_KINDS

def _days_since(entry, now):
    """距离最近一次召回（或创建）的天数，用于遗忘衰减。"""
    if not isinstance(entry, dict):
        return 0.0
    ref = entry.get("last_recalled") or entry.get("created")
    if not ref or not isinstance(ref, str):
        return 0.0
    try:
        return max(0.0, (now - ts_parse(ref)).total_seconds() / 86400.0)
    except Exception:
        return 0.0

def effective_importance(entry, now):
    """当前有效权重：core 记忆恒为 1.0；其余按衰减公式计算。

    === PATCH v2.8.3 === importance 走 _safe_float 兜底。旧库/手工编辑可能留下
    importance=null 或 "高" 这类脏值，此前直接参与乘法会抛 TypeError 让整个
    decay/search/stats 崩掉——一条脏记录毒死整个记忆库。
    """
    if _is_core(entry):
        return 1.0
    imp = _safe_float(entry.get("importance", 1.0), 1.0)
    return imp * (DECAY_FACTOR ** _days_since(entry, now))

# ──────────────────────────────────────────
# 核心命令
# ──────────────────────────────────────────

def cmd_write(entity, etype, value, refs_dir, mode=None, sentiment=None,
              source="auto_detect", confidence=1.0, emotion_tags=None, reason=None,
              core=None, context=None, expires_at=None):
    """完整 upsert 写入管线（PATCH v2.6: 归一化 + kind/sentiment + 源/情感/遗忘增强；
    v2.9: context 情感锚点 + expires_at 到期时间）"""
    # === v2.8.2 增量防线：entity 必填，杜绝新的缺 entity 记录（旧数据由 cmd_selfcheck 自愈） ===
    if not entity or not str(entity).strip():
        raise ValueError("entity 必填且不能为空：记忆必须归属到具体实体（如 用户/读书/伴侣）")
    entity = str(entity)
    # 校验 kind
    if etype not in ALLOWED_KINDS:
        raise ValueError(f"非法 kind='{etype}'，允许：{sorted(ALLOWED_KINDS)}")
    # === PATCH v2.8.3 === confidence 必须是 [0,1] 有限数：
    # NaN/Inf 会让 json.dump 产出裸 NaN 字面量（Python 能读、JS/Go 读不了）→ 记忆库变砖；
    # 越界值会让 pending 阈值判断失真，故一并明确拒绝而非静默接受。
    confidence = _require_finite("confidence", confidence, CONFIDENCE_MIN, CONFIDENCE_MAX)
    value = _safe_str(value)

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

        mem_path = os.path.join(refs_dir, "memory.json")
        ei_path = os.path.join(refs_dir, "entity_index.json")
        # === PATCH v2.8.3 === 损坏时自动隔离 + 从 .backup 恢复，绝不用空列表覆盖历史
        memory = read_json_typed(mem_path, list, refs_dir, "memory")
        ei = read_json_typed(ei_path, dict, refs_dir, "entity_index")
        if "entities" not in ei or not isinstance(ei.get("entities"), dict):
            ei = {"version": 1, "entities": {}}

        existing_idx = None
        similar_idx = None

        for i, entry in enumerate(memory):
            if not isinstance(entry, dict):
                continue  # 脏数据（字符串/数字混入）跳过，不让它毒死整条写入链
            e_kind = entry.get("kind") or entry.get("type")
            if entry.get("entity") == entity and e_kind == kind:
                if _norm_status(entry) == "active":
                    old_val = _safe_str(entry.get("value", ""))
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
                # === PATCH v2.9 === 情感锚点（当时的气氛）+ 到期时间
                "context": _safe_str(context) or None,
                "expires_at": _parse_expiry(expires_at),
            }

        # === PATCH v2.8.3 (P0-1) === 幂等合并会复用旧记录的 id，因此**先定稿 memory_id
        # 再写 WAL**。此前 WAL 在函数开头就用新生成的 id 写了 upsert，而合并分支随后把
        # new_id 换成旧记录 id，commit 写的是旧 id → WAL 里永远躺着一条 (entity, 新id)
        # 的孤儿 upsert。cmd_recover 认为它未提交就重放，重放又追加一对 upsert+commit，
        # 于是每 recover 一次 WAL 就翻倍（实测 6→10→18→34），永远收敛不了。
        if similar_idx is not None:
            new_id = memory[similar_idx].get("id") or new_id

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
            "core": core,
            "reason": reason,
            "context": _safe_str(context) or None,   # === PATCH v2.9 ===
            "expires_at": _parse_expiry(expires_at), # === PATCH v2.9 ===
            "memory_id": new_id,
            "targets": targets
        })

        if similar_idx is not None:
            memory[similar_idx]["value"] = value
            memory[similar_idx]["updated"] = ts_now()
            memory[similar_idx]["id"] = new_id
            if sentiment is not None:
                memory[similar_idx]["sentiment"] = sentiment
            if emotion_tags is not None:
                memory[similar_idx]["emotion_tags"] = emotion_tags
            if context is not None:
                memory[similar_idx]["context"] = _safe_str(context) or None
            if expires_at is not None:
                memory[similar_idx]["expires_at"] = _parse_expiry(expires_at)
            memory[similar_idx]["source"] = source
            memory[similar_idx]["confidence"] = confidence
            if core is not None:
                memory[similar_idx]["core"] = bool(core)
        elif existing_idx is not None:
            # 内容不同（如偏好翻转）→ 旧 active 标 superseded，新建
            memory[existing_idx]["status"] = "superseded"
            memory.append(_make_entry())
        else:
            memory.append(_make_entry())

        safe_write_json(mem_path, memory)

        # === PATCH mirror_to_graph (v2.8 拆耦合: 默认零 sqlite3, 仅 graph 模式动态加载) ===
        final_entry = next((e for e in memory if e.get("id") == new_id), None)
        if final_entry:
            _maybe_mirror_to_graph(final_entry, refs_dir)

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
            # === v2.8.4 === 回滚取最新备份改用 mtime 排序（与 v2.8.3 _backup_candidates 一致），
            # 不再用文件名排序（微秒/时区两种时间戳混排会错判新旧，'+' < '-'）。
            cands = _backup_candidates(bkp_dir, "memory")
            if cands:
                latest = cands[0]
                ts_clean = os.path.basename(latest).split("_", 1)[-1].rsplit(".", 1)[0]
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

        # === PATCH v2.8.3 (3.3a) === write-through 备份：提交成功后立刻再拍一张快照。
        # 此前只在写入「之前」备份，快照永远滞后一次写入——一旦 memory.json 损坏，
        # 从备份恢复必然丢掉最后成功写入的那条记忆（实测 2 条→只回来 1 条）。
        # 放在 commit 之后，保证 .backup 最新快照 == 最后一次确认落盘的状态。
        # 注意：不能提前到 commit 之前，否则会破坏上面「回滚到最新备份」的语义。
        try:
            backup_files(refs_dir)
        except Exception as e:
            sys.stderr.write(f"[memory] write-through 备份失败（不影响本次写入）：{e}\n")

        # === PATCH v2.8.3 (3.4b) === 重新写入某实体 = 用户又要记它了，
        # 必须把它从"已遗忘清单"里摘掉。否则会出现自相矛盾的状态：
        # search 能召回这条记忆，suppressed_prompt.md 却仍在命令 AI「不要主动提及」。
        _unsuppress_entity(refs_dir, entity)

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
            "context": _safe_str(context) or None,     # === PATCH v2.9 ===
            "expires_at": _parse_expiry(expires_at),   # === PATCH v2.9 ===
            "mode": mode,
            "targets": targets
        }, ensure_ascii=False))

    finally:
        file_unlock(fd, refs_dir)


def _touch_recalled(refs_dir, mem_path, hit_ids, now=None):
    """=== PATCH v2.6 A1 + v2.7 C2 === 检索命中后戳 last_recalled，并对非 core 记忆
       应用遗忘衰减 importance *= DECAY_FACTOR ** days_since_last_recalled。

    === PATCH v2.8.3 (P0-3) === 整个 read-modify-write 必须在文件锁内完成。
    此前 search 在锁外读盘、改内存、再整体写回，与并发的 write 相互覆盖：
    write 刚落盘的新记录会被 search 手里的旧快照抹掉。实测 3 写 + 3 搜并发，
    期望 29 条只剩 16 条，**丢失 45%**。加锁后读到的一定是最新快照。

    === 修复 (finding #10) === 返回 {id: {"importance":..., "last_recalled":...}} 的
    逐条更新，供 cmd_search 把衰减/戳记结果回填到自己的输出里。此前 search 在调用
    _touch_recalled *之前* 就把 entry 快照拼好，_touch_recalled 改的是磁盘上的另一份
    副本，导致返回给调用方的 entry 永远是"触碰前"的快照（last_recalled 恒为 null、
    importance 未衰减）——即真正发生了召回，结果却说没发生。
    """
    if not hit_ids:
        return {}
    now = now or datetime.now(TZ)
    hit = set(hit_ids)
    updates = {}
    fd = file_lock(refs_dir)
    try:
        memory = read_json_typed(mem_path, list, refs_dir, "memory")
        changed = False
        for e in memory:
            if not isinstance(e, dict):
                continue
            if e.get("id") in hit and _norm_status(e) == "active":
                if not _is_core(e):
                    days = _days_since(e, now)
                    cur = _safe_float(e.get("importance", 1.0), 1.0)
                    e["importance"] = round(cur * (DECAY_FACTOR ** days), 6)
                e["last_recalled"] = now.isoformat(timespec="seconds")
                # 同一 id 在多源检索里可能出现多次，统一以磁盘最终态为准
                updates[e["id"]] = {
                    "importance": e["importance"],
                    "last_recalled": e["last_recalled"],
                }
                changed = True
        if changed:
            safe_write_json(mem_path, memory)
    finally:
        file_unlock(fd, refs_dir)
    return updates


def cmd_search(query, refs_dir):
    """本地检索；PATCH v2.6: 仅返回 active，标注 authority + supplement"""
    # === PATCH v2.8.3 (3.5b) === 查询词不再被子串归一化改写（见 normalize_entity 注释）
    query = _safe_str(query)
    query = normalize_entity(query, refs_dir)

    mem_path = os.path.join(refs_dir, "memory.json")
    ei_path = os.path.join(refs_dir, "entity_index.json")

    memory = read_json_typed(mem_path, list, refs_dir, "memory")
    ei = read_json_typed(ei_path, dict, refs_dir, "entity_index")
    if not isinstance(ei.get("entities"), dict):
        ei = {"version": 1, "entities": {}}

    results = []

    # === PATCH v2.8.3 (3.5c) === 空/纯空白查询直接返回空。
    # 此前 "" in val.lower() 恒为 True，一个空 query 就把整库（含 identity 等隐私记忆）
    # 全量倒出，等同于无差别信息泄露。
    if not query.strip():
        print(json.dumps({"query": query, "results_count": 0, "results": []}, ensure_ascii=False, indent=2))
        return

    q = query.lower()

    if query in ei.get("entities", {}):
        ent = ei["entities"].get(query) or {}
        mid = ent.get("last_memory_id") if isinstance(ent, dict) else None
        if mid:
            for entry in memory:
                if not isinstance(entry, dict):
                    continue
                # === PATCH v2.6 B1 === 只回 active；pending/superseded 不进检索（防幻觉固化/已遗忘漏网）
                if entry.get("id") == mid and entry.get("entity") == query and _norm_status(entry) == "active":
                    results.append({
                        "source": "entity_index",
                        "confidence": 0.95,
                        "entry": entry
                    })
                    break

    for entry in memory:
        # === PATCH v2.8.3 (P1) === 全部字段走 .get + _safe_str：
        # 旧库里缺 entity / 缺 id / value=null 的记录此前会让 search 直接
        # KeyError / AttributeError 崩掉，一条脏记录就让整个检索不可用。
        if not isinstance(entry, dict):
            continue
        if _norm_status(entry) != "active":
            continue
        val = _safe_str(entry.get("value", "")).lower()
        ent_name = _safe_str(entry.get("entity", "")).lower()
        if q in val or q in ent_name:
            eid = entry.get("id")
            if eid is None or not any(r["entry"].get("id") == eid for r in results):
                results.append({
                    "source": "keyword",
                    "confidence": 0.75,
                    "entry": entry
                })

    # === PATCH v2.6 B3 === 源排序 + 冲突 supplement 标记
    for r in results:
        r["authority"] = authority_of(r["entry"].get("source", "auto_detect"))
    # file 权威优先；同实体多源 → self 侧标 supplement
    file_entities = {_safe_str(r["entry"].get("entity", "")) for r in results if r["authority"] == "file"}
    for r in results:
        r["supplement"] = (r["authority"] == "self" and _safe_str(r["entry"].get("entity", "")) in file_entities)
    # === PATCH v2.7 C3 === 按"权威优先 + effective_importance 降序"排序（core 记忆恒靠前）
    now = datetime.now(TZ)
    for r in results:
        r["_eff_imp"] = effective_importance(r["entry"], now)
    results.sort(key=lambda r: (0 if r["authority"] == "file" else 1, -r["_eff_imp"]))

    # === PATCH v2.6 A1 + v2.7 C2 === 命中即戳 last_recalled + 衰减
    hit_ids = [r["entry"].get("id") for r in results if r["entry"].get("id")]
    touch_updates = _touch_recalled(refs_dir, mem_path, hit_ids, now)

    out = []
    for r in results[:5]:
        e = dict(r["entry"])
        # === 修复 (finding #10) === 把磁盘上已落定的衰减/戳记结果回填进输出，
        # 否则调用方拿到的永远是"触碰前"的快照（last_recalled=null、importance 未衰减）。
        uid = e.get("id")
        if uid in touch_updates:
            e["last_recalled"] = touch_updates[uid]["last_recalled"]
            e["importance"] = touch_updates[uid]["importance"]
        e["_authority"] = r["authority"]
        e["_supplement"] = r["supplement"]
        e["_match_source"] = r["source"]
        # _importance 以回填后的值重算，保证与 entry 内 importance 一致（衰减后更低）
        e["_importance"] = round(effective_importance(e, now), 4)
        e["_core"] = _is_core(e)
        out.append(e)

    print(json.dumps({
        "query": query,
        "results_count": len(results),
        "results": out
    }, ensure_ascii=False, indent=2))


# === v2.8.2 存量防线：扫描并自愈缺 entity 的旧记录 ===

def _norm_status(e):
    """读路径 status 归一：缺省/空 -> active（向后兼容 v2.9 前无 status 字段的存量库）。
    已带 status 的（active / denied / pending / superseded / confirmed / ...）原样保留，不改变原语义。"""
    st = e.get("status") if isinstance(e, dict) else None
    return st or "active"


def cmd_selfcheck(refs_dir):
    """扫描 memory.json，修复 entity 缺失/为空的旧记录（早期手写入库、entity 尚未必填时留下）。
    对缺 entity 的记录按 value 首行派生实体名自动补全（value 也为空则记为 '(未知实体)'），
    写回并打印 WARNING 日志；已带 entity 的不动。返回修复摘要。"""
    mem_path = os.path.join(refs_dir, "memory.json")
    # 注意：这里刻意不传 refs_dir/stem —— selfcheck 是只读体检，不应触发备份回滚副作用
    memory = read_json_typed(mem_path, list)
    if not memory:
        return {"checked": 0, "fixed": 0, "fixed_ids": []}

    fixed = []
    for entry in memory:
        if not isinstance(entry, dict):
            continue
        changed = False
        ent = entry.get("entity")
        if not ent or not str(ent).strip():
            val = _safe_str(entry.get("value") or "").strip()
            if val:
                first_line = val.splitlines()[0].strip()
                derived = (first_line[:40] + "...") if len(first_line) > 40 else first_line
            else:
                derived = "(未知实体)"
            entry["entity"] = derived
            kind = entry.get("kind") or entry.get("type")
            fixed.append({"id": entry.get("id"), "kind": kind, "source": entry.get("source"), "entity": derived})
            sys.stderr.write(
                f"[memory_selfcheck] 修复缺 entity 记录 id={entry.get('id')} "
                f"(kind={kind}, source={entry.get('source')}) -> entity='{derived}'\n"
            )
            changed = True
        # 存量库兼容：status 缺失/空 -> active（v2.9 前写入的记录无该字段）
        # 注意：显式 denied/pending/superseded/confirmed 等原值保留，不覆盖
        st = entry.get("status")
        if not st:
            entry["status"] = "active"
            changed = True
        if changed and not any(f.get("id") == entry.get("id") for f in fixed):
            kind = entry.get("kind") or entry.get("type")
            fixed.append({"id": entry.get("id"), "kind": kind, "source": entry.get("source"),
                          "entity": entry.get("entity"), "status_fixed": True})

    if fixed:
        backup_files(refs_dir)
        safe_write_json(mem_path, memory)

    return {"checked": len(memory), "fixed": len(fixed), "fixed_ids": fixed}


def cmd_stats(refs_dir):
    mem_path = os.path.join(refs_dir, "memory.json")
    ei_path = os.path.join(refs_dir, "entity_index.json")

    memory = [m for m in read_json_typed(mem_path, list, refs_dir, "memory") if isinstance(m, dict)]
    ei = read_json_typed(ei_path, dict, refs_dir, "entity_index")

    entities = ei.get("entities") if isinstance(ei.get("entities"), dict) else {}
    entities = {k: v for k, v in entities.items() if isinstance(v, dict)}
    confirmed = sum(1 for e in entities.values() if e.get("status") == "confirmed")
    pending = sum(1 for e in entities.values() if e.get("status") == "pending")
    superseded = sum(1 for m in memory if m.get("status") == "superseded")
    active = sum(1 for m in memory if _norm_status(m) == "active")
    pending_mem = sum(1 for m in memory if m.get("status") == "pending")

    type_counts = {}
    for e in entities.values():
        t = e.get("kind") or e.get("type", "general")
        type_counts[t] = type_counts.get(t, 0) + 1

    # === PATCH v2.7 C2/C3 === 核心记忆 / 平均权重 / 最弱记忆排行
    now = datetime.now(TZ)
    core_count = sum(1 for m in memory if _norm_status(m) == "active" and _is_core(m))
    noncore = [m for m in memory if _norm_status(m) == "active" and not _is_core(m)]
    avg_imp = round(sum(effective_importance(m, now) for m in noncore) / len(noncore), 4) if noncore else 0
    weakest = sorted(
        [(m.get("entity"), m.get("kind"), round(effective_importance(m, now), 4)) for m in noncore],
        key=lambda x: x[2]
    )[:5]

    timestamps = sorted(_safe_str(m.get("created", "")) for m in memory if m.get("created"))

    # === PATCH v2.6 A3 === 最久未召回排行（last_recalled 为空退回 created）
    stale = []
    for m in memory:
        if _norm_status(m) != "active":
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
    fd = file_lock(refs_dir)
    try:
        memory = read_json_typed(mem_path, list, refs_dir, "memory")
        now = datetime.now(TZ)
        decayed = 0
        for e in memory:
            if not isinstance(e, dict):
                continue
            if _norm_status(e) != "active" or _is_core(e):
                continue
            days = _days_since(e, now)
            # === PATCH v2.8.3 === importance 脏值（null / "高"）不再让整库 decay 崩溃
            cur = _safe_float(e.get("importance", 1.0), 1.0)
            new = round(cur * (DECAY_FACTOR ** days), 6)
            if new != cur:
                e["importance"] = new
                decayed += 1
        safe_write_json(mem_path, memory)
    finally:
        file_unlock(fd, refs_dir)
    print(json.dumps({
        "status": "decayed",
        "decayed_count": decayed,
        "active_noncore": sum(1 for m in memory
                              if isinstance(m, dict) and _norm_status(m) == "active" and not _is_core(m)),
    }, ensure_ascii=False))


def cmd_vacuum(refs_dir):
    mem_path = os.path.join(refs_dir, "memory.json")
    archive_dir = os.path.join(refs_dir, ".archive")
    os.makedirs(archive_dir, exist_ok=True)

    fd = file_lock(refs_dir)
    try:
        memory = read_json_typed(mem_path, list, refs_dir, "memory")
        now = datetime.now(TZ)
        cutoff_90d = now - timedelta(days=90)

        to_archive = []
        retained = []
        for entry in memory:
            if not isinstance(entry, dict):
                retained.append(entry)
                continue
            if entry.get("status") == "superseded":
                # === PATCH v2.8.3 (P1) === created 缺失或格式非法不再抛
                # KeyError/ValueError 让整个 vacuum 崩掉；无法判断年龄的记录一律
                # 保守保留（宁可不清理，也绝不误删或中断维护流程）。
                raw_created = entry.get("created")
                created = None
                if isinstance(raw_created, str):
                    try:
                        created = ts_parse(raw_created)
                    except (ValueError, TypeError):
                        created = None
                if created is not None and created < cutoff_90d:
                    to_archive.append(entry)
                    continue
            retained.append(entry)

        if to_archive:
            # === PATCH v2.8.3 (P0-6) === 同日多次 vacuum 必须**合并**而不是覆盖。
            # 此前归档名只到日期（memory_YYYYMMDD.json），当天第二次 vacuum 直接
            # 覆写，上一批归档数据永久丢失。现在先读回已有归档再追加。
            date_str = now.strftime("%Y%m%d")
            archive_path = os.path.join(archive_dir, f"memory_{date_str}.json")
            existing = read_json_typed(archive_path, list)
            existing_ids = {e.get("id") for e in existing if isinstance(e, dict)}
            merged = list(existing) + [e for e in to_archive if e.get("id") not in existing_ids]
            safe_write_json(archive_path, merged)
            safe_write_json(mem_path, retained)
    finally:
        file_unlock(fd, refs_dir)

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
        memory = read_json_typed(mem_path, list, refs_dir, "memory")

        forgotten = []
        for e in memory:
            if not isinstance(e, dict):
                continue
            if _norm_status(e) != "active":
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
        # === PATCH v2.8.3 (3.4d) === 按 (entity, kind) 去重。此前每次 forget 都无脑
        # append，反复遗忘同一实体会让 suppressed_prompt.md 里堆出成排重复的
        # 「不要主动提及 X」，白白挤占 AI 的系统提示词预算。
        supp_path = os.path.join(refs_dir, ".suppressed.json")
        supp = read_json_typed(supp_path, dict)
        if not isinstance(supp.get("suppressed"), list):
            supp = {"suppressed": []}
        seen = {(s.get("entity"), s.get("kind")) for s in supp["suppressed"] if isinstance(s, dict)}
        for e in forgotten:
            key = (e.get("entity"), e.get("kind") or e.get("type"))
            if key in seen:
                continue
            seen.add(key)
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


# ═══════════════════════════════════════════════════════════════
# === PATCH v2.9 === 人情味增强四件套：否认降权 / 到期记忆 / 主动回忆 / 承诺追踪
# ═══════════════════════════════════════════════════════════════

def cmd_deny(entity_or_id, refs_dir, reason=None):
    """=== PATCH v2.9 (否认降权闭环) === 用户否认一条记忆 → 立即大幅降权并记录。

    这是"被记住"的信任修复机制：用户纠正过的事（"我早不喝三分糖了"）绝不能再
    自信地排在最前。首次否认 importance ×DENY_FACTOR（0.1）；累积 DENY_TO_PENDING_AFTER
    （2）次 → 直接转 pending（彻底退出检索，需重新写入确认才复活）。"""
    fd = file_lock(refs_dir)
    try:
        mem_path = os.path.join(refs_dir, "memory.json")
        memory = read_json_typed(mem_path, list, refs_dir, "memory")

        denied = []
        for e in memory:
            if not isinstance(e, dict):
                continue
            if _norm_status(e) != "active":
                continue
            match = (e.get("id") == entity_or_id) or (e.get("entity") == entity_or_id)
            if not match:
                continue
            cur_deny = _safe_float(e.get("deny_count", 0.0), 0.0)
            new_deny = int(cur_deny) + 1
            e["deny_count"] = new_deny
            e["denied_at"] = ts_now()
            if reason:
                e["deny_reason"] = reason
            cur_imp = _safe_float(e.get("importance", 1.0), 1.0)
            e["importance"] = round(cur_imp * DENY_FACTOR, 6)
            e["updated"] = ts_now()
            if new_deny >= DENY_TO_PENDING_AFTER:
                e["status"] = "pending"
            denied.append({
                "id": e.get("id"),
                "entity": e.get("entity"),
                "deny_count": new_deny,
                "importance": e["importance"],
                "status": e.get("status"),
            })

        if not denied:
            print(json.dumps({"status": "nothing_to_deny", "entity_or_id": entity_or_id}, ensure_ascii=False))
            return

        safe_write_json(mem_path, memory)
        print(json.dumps({
            "status": "denied",
            "count": len(denied),
            "items": denied,
        }, ensure_ascii=False))
    finally:
        file_unlock(fd, refs_dir)


def cmd_expire_check(refs_dir):
    """=== PATCH v2.9 (到期记忆) === 扫描带 expires_at 的记忆：
    已到期 → 状态标 expired（移出 active，不再被检索召回，数据保留）；
    未来 EXPIRY_REMIND_DAYS 天内到期 → 列入 remind（供主动提醒兑现/提及）。"""
    fd = file_lock(refs_dir)
    try:
        mem_path = os.path.join(refs_dir, "memory.json")
        memory = read_json_typed(mem_path, list, refs_dir, "memory")
        now = datetime.now(TZ)
        expired = []
        remind = []
        changed = False
        for e in memory:
            if not isinstance(e, dict):
                continue
            if _norm_status(e) != "active":
                continue
            raw = e.get("expires_at")
            if not raw or not isinstance(raw, str):
                continue
            try:
                exp_dt = ts_parse(raw)
            except (ValueError, TypeError):
                continue  # 解析不了的到期时间跳过，不误伤
            if exp_dt <= now:
                e["status"] = "expired"
                e["expired_at"] = ts_now()
                e["updated"] = ts_now()
                changed = True
                expired.append({
                    "id": e.get("id"),
                    "entity": e.get("entity"),
                    "kind": e.get("kind"),
                    "value": _safe_str(e.get("value")),
                })
            elif (exp_dt - now).total_seconds() / 86400.0 <= EXPIRY_REMIND_DAYS:
                remind.append({
                    "id": e.get("id"),
                    "entity": e.get("entity"),
                    "kind": e.get("kind"),
                    "value": _safe_str(e.get("value")),
                    "expires_at": raw,
                    "days_left": round((exp_dt - now).total_seconds() / 86400.0, 1),
                })
        if changed:
            safe_write_json(mem_path, memory)
        print(json.dumps({
            "status": "expire_checked",
            "now": ts_now(),
            "expired": expired,
            "remind": remind,
        }, ensure_ascii=False, indent=2))
    finally:
        file_unlock(fd, refs_dir)


def cmd_recall(refs_dir, limit=3):
    """=== PATCH v2.9 (主动回忆触发器) === 从记忆库里挑出"值得此刻想起的"几条。

    双通道设计（修复"被遗忘的反而排不上号"的矛盾）：
      · 温热通道 —— 常提/核心记忆，按 effective_importance 排（保持"常聊的记得牢"）。
      · 旧事重提通道 —— 冷掉的记忆（从没提起过 / 很久没召回）单独捞，评分不看衰减后的
        importance，而看【从没提过 + 带当时的气氛 + 快到期 + 曾经很重要】——
        否则衰减越狠排越后，"最该被想起的旧事"永远被遗忘曲线压死。
    输出按通道混合，带 recall_reason 说明为什么此刻提起这条。"""
    mem_path = os.path.join(refs_dir, "memory.json")
    memory = read_json_typed(mem_path, list, refs_dir, "memory")
    now = datetime.now(TZ)
    COLD_DAYS = 90  # 距上次召回超过此天数 → 视为"冷掉的旧事"

    warm = []
    cold = []
    for e in memory:
        if not isinstance(e, dict):
            continue
        if _norm_status(e) != "active":
            continue
        imp = effective_importance(e, now)
        never = not e.get("last_recalled")
        has_emotion = bool(e.get("context") or e.get("emotion_tags"))
        expiring = False
        raw = e.get("expires_at")
        if raw and isinstance(raw, str):
            try:
                if 0 < (ts_parse(raw) - now).total_seconds() / 86400.0 <= EXPIRY_REMIND_DAYS:
                    expiring = True
            except (ValueError, TypeError):
                pass
        # 冷热度：core 恒温；其余按距上次召回天数
        cold_days = 0
        if not _is_core(e):
            ref = e.get("last_recalled") or e.get("created")
            if ref and isinstance(ref, str):
                try:
                    cold_days = max(0.0, (now - ts_parse(ref)).total_seconds() / 86400.0)
                except (ValueError, TypeError):
                    cold_days = 0
        item = {
            "id": e.get("id"),
            "entity": e.get("entity"),
            "kind": e.get("kind"),
            "value": _safe_str(e.get("value")),
            "context": e.get("context"),
            "emotion_tags": e.get("emotion_tags"),
            "never_recalled": never,
            "expires_at": e.get("expires_at"),
            "last_recalled": e.get("last_recalled"),
            "importance": round(imp, 4),
            "cold_days": round(cold_days, 1),
        }
        if _is_core(e) or cold_days < COLD_DAYS:
            item["score"] = round(imp, 4)  # 温热：按记得牢程度
            item["recall_reason"] = "常聊的，正在心头"
            warm.append(item)
        else:
            # 旧事重提：衰减后的 importance 不作主键，改看"值不值得捞回来"
            score = (RECALL_BONUS_NEVER if never else 0) \
                + (RECALL_BONUS_EMOTION if has_emotion else 0) \
                + (RECALL_BONUS_EXPIRING if expiring else 0) \
                + (0.25 * max(0.0, min(1.0, _safe_float(e.get("confidence", 1.0), 1.0))))
            item["score"] = round(score, 4)
            if never:
                item["recall_reason"] = "从没跟你提起过的旧事"
            elif expiring:
                item["recall_reason"] = "快到期了，最后想起一次"
            elif has_emotion:
                item["recall_reason"] = "想起来都带着当时的气氛"
            else:
                item["recall_reason"] = "很久没提起的旧事"
            cold.append(item)

    warm.sort(key=lambda x: -x["score"])
    cold.sort(key=lambda x: -x["score"])
    # 混合：温热至多 1 条（保持"常聊的稳"，但不让它霸占全部），其余名额留给旧事重提
    results = []
    if warm:
        results.append(warm[0])
    results += cold[: max(0, limit - len(results))]
    if len(results) < limit:
        results += warm[1: limit - len(results)]

    print(json.dumps({
        "status": "recalled",
        "count": len(results),
        "channel": {"warm": len(warm), "old_treasure": len(cold)},
        "results": results,
    }, ensure_ascii=False, indent=2))


# ---- 承诺追踪（promises.json）----
def _promises_path(refs_dir):
    return os.path.join(refs_dir, "promises.json")


def _read_promises(refs_dir):
    data = read_json_typed(_promises_path(refs_dir), dict, refs_dir, "promises")
    if not isinstance(data, dict) or not isinstance(data.get("promises"), list):
        data = {"promises": []}
    return data


def _write_promises(refs_dir, data):
    safe_write_json(_promises_path(refs_dir), data)


def cmd_promise(text, refs_dir, deadline=None):
    """=== PATCH v2.9 (承诺建档) === AI 亲口答应的事，建档必追。

    只要 AI 对用户做了承诺（明天写歌 / 纪念日准备惊喜 / 帮你查某件事），立即建档。
    deadline 可选（YYYY-MM-DD 或 ISO），到期未完成会被 cmd_promise_check 主动戳。"""
    text = _safe_str(text).strip()
    if not text:
        raise ValueError("承诺内容不能为空")
    data = _read_promises(refs_dir)
    entry = {
        "id": f"pro_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
        "text": text,
        "deadline": _parse_expiry(deadline),
        "created_at": ts_now(),
        "status": "open",
        "completed_at": None,
    }
    data["promises"].append(entry)
    _write_promises(refs_dir, data)
    print(json.dumps({"status": "promised", "promise": entry}, ensure_ascii=False, indent=2))


def cmd_promise_done(promise_id, refs_dir):
    """=== PATCH v2.9 (承诺完成) === 完成一项划掉一项：open → done + completed_at。"""
    data = _read_promises(refs_dir)
    found = None
    for p in data["promises"]:
        if not isinstance(p, dict):
            continue
        if p.get("id") == promise_id and p.get("status") == "open":
            p["status"] = "done"
            p["completed_at"] = ts_now()
            found = p
            break
    if not found:
        print(json.dumps({"status": "nothing_to_done", "promise_id": promise_id}, ensure_ascii=False))
        return
    _write_promises(refs_dir, data)
    print(json.dumps({"status": "promise_done", "promise": found}, ensure_ascii=False, indent=2))


def cmd_promise_list(refs_dir):
    """=== PATCH v2.9 (承诺清单) === 列出全部承诺：open 在前（含是否逾期）、done 在后。"""
    data = _read_promises(refs_dir)
    now = datetime.now(TZ)
    open_items = []
    done_items = []
    for p in data["promises"]:
        if not isinstance(p, dict):
            continue
        item = {
            "id": p.get("id"),
            "text": _safe_str(p.get("text")),
            "deadline": p.get("deadline"),
            "created_at": p.get("created_at"),
            "completed_at": p.get("completed_at"),
        }
        if p.get("status") == "open":
            overdue = False
            if item["deadline"]:
                try:
                    overdue = ts_parse(item["deadline"]) < now
                except (ValueError, TypeError):
                    overdue = False
            item["overdue"] = overdue
            open_items.append(item)
        elif p.get("status") == "done":
            done_items.append(item)
    open_items.sort(key=lambda x: (not x.get("overdue"), x.get("deadline") or "9999"))
    print(json.dumps({
        "status": "promises",
        "open_count": len(open_items),
        "done_count": len(done_items),
        "open": open_items,
        "done": done_items[-20:],  # 只回最近完成的，别撑爆输出
    }, ensure_ascii=False, indent=2))


def cmd_promise_check(refs_dir):
    """=== PATCH v2.9 (承诺主动戳) === 未完成的承诺要经常主动触发提醒 AI：当时答应了还没做。

    返回全部未完成承诺，逾期（deadline 已过）与"已过了好几天还没动静"的排最前——
    供 AI 在每次会话开始时自查、或由定时任务主动推送，别让承诺默默消失。"""
    data = _read_promises(refs_dir)
    now = datetime.now(TZ)
    pending = []
    for p in data["promises"]:
        if not isinstance(p, dict):
            continue
        if p.get("status") != "open":
            continue
        text = _safe_str(p.get("text"))
        created = p.get("created_at")
        deadline = p.get("deadline")
        overdue = False
        days_over = 0
        if deadline:
            try:
                d = ts_parse(deadline)
                diff = (now - d).total_seconds() / 86400.0
                overdue = diff > 0
                days_over = max(0, round(diff, 1))
            except (ValueError, TypeError):
                pass
        # 无 deadline 的承诺，按创建时长排序（越久越该戳）
        age_days = 0
        if created:
            try:
                age_days = max(0, round((now - ts_parse(created)).total_seconds() / 86400.0, 1))
            except (ValueError, TypeError):
                pass
        pending.append({
            "id": p.get("id"),
            "text": text,
            "deadline": deadline,
            "created_at": created,
            "overdue": overdue,
            "days_overdue": days_over,
            "age_days": age_days,
        })
    # 逾期排最前，其次按（deadline 越近越急）→ 无 deadline 按创建越久越急
    pending.sort(key=lambda x: (
        not x.get("overdue"),
        x.get("days_overdue"),
        x.get("age_days"),
    ))
    print(json.dumps({
        "status": "promise_check",
        "unfulfilled_count": len(pending),
        "unfulfilled": pending,
    }, ensure_ascii=False, indent=2))


# === v2.10 承诺闹钟：promise + deadline 自触发提醒（不依赖宿主 cron） ===
# 三种触发：① MCP server 内置后台线程（mcp_server.py 常驻时自动跑）
#           ② CLI `promise watch` 常驻子命令（零依赖，nohup 挂后台）
#           ③ 会话内注入：任何工具/命令返回时附上临期/逾期承诺（memory_promise_check 与
#              各 cmd_* 会自动附加 promise_reminders 字段），宿主每次唤醒 AI 都能撞见。
# 推送渠道：Bark（BARK_KEY 环境变量或 refs 目录 .bark_key 文件）/ 自定义 webhook
#           （MEMORY_TRIGGER_WEBHOOK_URL 环境变量）/ 落盘 .promise_reminders.md（默认，保证无网络也能留痕）。
PROMISE_REMIND_DAYS = 1              # 临期窗口：deadline 距今天 ≤ 该天数视为"临期"，与到期记忆 EXPIRY_REMIND_DAYS 对齐
PROMISE_WATCH_INTERVAL = 300         # 后台线程/常驻 watch 的默认检查间隔（秒）
PROMISE_NOTIFY_FILE = ".promise_notified.json"   # 已通知记录，防重复轰炸


def _promise_notify_path(refs_dir):
    return os.path.join(refs_dir, PROMISE_NOTIFY_FILE)


def _read_promise_notified(refs_dir):
    p = _promise_notify_path(refs_dir)
    data = safe_read_json(p)
    return data if isinstance(data, dict) else {}


def _write_promise_notified(refs_dir, data):
    safe_write_json(_promise_notify_path(refs_dir), data)


def _promise_due_items(refs_dir):
    """返回需要提醒的承诺：open 且（deadline 已逾期 或 deadline 在临期窗口内）。
    无 deadline 的承诺不在这里——提醒必须靠 deadline 这把尺子（见 README「承诺依赖 deadline」）。"""
    now = datetime.now(TZ)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    due = []
    data = _read_promises(refs_dir)
    for p in data.get("promises", []):
        if not isinstance(p, dict):
            continue
        if p.get("status") != "open":
            continue
        text = _safe_str(p.get("text"))
        deadline = p.get("deadline")
        if not deadline:
            continue
        try:
            d = ts_parse(deadline)
        except (ValueError, TypeError):
            continue
        # 逾期：deadline < now；临期：deadline 落在今天到 remind 窗口之间
        remind_from = today0 - timedelta(days=PROMISE_REMIND_DAYS)
        if d < remind_from:
            continue
        overdue = d < now
        due.append({
            "id": p.get("id"),
            "text": text,
            "deadline": deadline,
            "overdue": overdue,
            "days_overdue": round((now - d).total_seconds() / 86400.0, 1) if overdue else 0,
        })
    return due


def _send_bark(title, body):
    """Bark 原生推送：key 取环境变量 BARK_KEY，其次 refs_dir/.bark_key（在调用方补）。返回 bool。"""
    key = os.environ.get("BARK_KEY") or os.environ.get("MEMORY_TRIGGER_BARK_KEY")
    if not key:
        return False
    try:
        from urllib.request import urlopen, Request
        import urllib.parse
        url = f"https://api.day.app/{key}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
        req = Request(url, method="GET")
        try:
            with urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as ssl_err:
            # 部分环境（系统 python 缺 CA 证书）SSL 校验失败；个人通知场景降级为不校验，
            # 让 Bark 能送出去——失败仍有落盘文件兜底，不静默吞掉推送能力。
            ctx = _unverified_ssl_context()
            if ctx is None:
                raise ssl_err
            with urlopen(req, timeout=10, context=ctx) as resp:
                resp.read()
        return True
    except Exception:
        return False


def _unverified_ssl_context():
    try:
        import ssl
        return ssl._create_unverified_context()
    except Exception:
        return None


def _send_webhook(title, body):
    """自定义 webhook：环境变量 MEMORY_TRIGGER_WEBHOOK_URL。POST JSON，兼容 Bark 服务端格式。"""
    url = os.environ.get("MEMORY_TRIGGER_WEBHOOK_URL")
    if not url:
        return False
    try:
        from urllib.request import urlopen, Request
        payload = json.dumps({"title": title, "body": body, "device_key": None}).encode("utf-8")
        req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as ssl_err:
            ctx = _unverified_ssl_context()
            if ctx is None:
                raise ssl_err
            with urlopen(req, timeout=10, context=ctx) as resp:
                resp.read()
        return True
    except Exception:
        return False


def _append_promise_reminder_file(refs_dir, items):
    """落盘 .promise_reminders.md：无网络环境也能留痕，宿主或人可自行读取。"""
    path = os.path.join(refs_dir, ".promise_reminders.md")
    try:
        now = ts_now()
        lines = [f"# 承诺提醒（{now}）", ""]
        for it in items:
            tag = "逾期" if it.get("overdue") else "临期"
            lines.append(f"- [{tag}]（{it.get('deadline')}）{it.get('text')}")
        lines.append("")
        lines.append("> 由 memory-trigger 承诺闹钟生成。兑现后请 promise done <id>，下次巡检不再提醒。")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path
    except Exception:
        return None


def promise_notify_due(refs_dir):
    """检查临期/逾期承诺，去重后推送一次。返回本次是否推送了新提醒。

    被三处调用：CLI `promise watch` 循环、MCP server 后台线程、会话注入路径（附加提醒字段时不推送）。
    """
    items = _promise_due_items(refs_dir)
    if not items:
        return False
    notified = _read_promise_notified(refs_dir)
    ids = notified.get("promise_ids")
    if not isinstance(ids, list):
        ids = []
    fresh = [it for it in items if it["id"] not in ids]
    if not fresh:
        return False
    overdue = [it for it in fresh if it["overdue"]]
    soon = [it for it in fresh if not it["overdue"]]
    title = "memory-trigger：承诺需要兑现"
    lines = []
    if overdue:
        lines.append("【已逾期】")
        lines += [f"· {it['text']}（deadline {it['deadline']}，已过 {it['days_overdue']} 天）" for it in overdue]
    if soon:
        lines.append("【临期】")
        lines += [f"· {it['text']}（deadline {it['deadline']}）" for it in soon]
    body = "\n".join(lines)
    bark_ok = _send_bark(title, body)
    webhook_ok = _send_webhook(title, body)
    file_path = _append_promise_reminder_file(refs_dir, fresh)
    # 无论推送是否成功，都标记为已通知，避免每轮重复轰炸；文件落盘兜底保证留痕
    notified["promise_ids"] = list(dict.fromkeys(ids + [it["id"] for it in fresh]))
    _write_promise_notified(refs_dir, notified)
    return True


def cmd_promise_watch(refs_dir, interval=None):
    """=== v2.10 承诺闹钟（常驻）=== `python write_pipeline.py promise watch [refs_dir] [--interval N]`

    常驻循环：每隔 interval 秒检查一次临期/逾期承诺，去重推送（Bark/webhook/落盘）。
    不依赖宿主 cron——本进程活着，闹钟就活着；配合 MCP server 后台线程双保险。
    零依赖（纯标准库），nohup 挂后台即可：
        nohup python3 references/write_pipeline.py promise watch <REFS_DIR> &
    """
    if interval is None:
        interval = PROMISE_WATCH_INTERVAL
    print(json.dumps({"status": "promise_watch_started", "refs_dir": refs_dir,
                      "interval_sec": interval}, ensure_ascii=False))
    while True:
        try:
            promise_notify_due(refs_dir)
        except Exception as e:
            print(json.dumps({"status": "promise_watch_error", "error": str(e)}, ensure_ascii=False),
                  file=sys.stderr)
        time.sleep(max(1, int(interval)))


def _promise_reminders_field(refs_dir):
    """会话内注入用：返回 {promise_reminders: [...]}，有临期/逾期承诺时附加到工具输出。
    不推送、不标记已通知——只让 AI/宿主每次唤醒都看得见。"""
    items = _promise_due_items(refs_dir)
    if not items:
        return {}
    return {"promise_reminders": items}


def _unsuppress_entity(refs_dir, entity):
    """=== PATCH v2.8.3 (3.4b) === 把某实体从"已遗忘清单"里摘掉并重生成 prompt。

    用户重新写入一条记忆，就意味着这件事又该被记住了。若不同步清理 suppressed，
    记忆库会进入自相矛盾的状态：memory_search 能召回它，suppressed_prompt.md
    却仍在指示 AI「不要主动提及」——AI 于是"知道却装作不知道"。
    """
    supp_path = os.path.join(refs_dir, ".suppressed.json")
    if not os.path.exists(supp_path):
        return
    try:
        supp = read_json_typed(supp_path, dict)
        items = supp.get("suppressed")
        if not isinstance(items, list):
            return
        kept = [s for s in items if not (isinstance(s, dict) and s.get("entity") == entity)]
        if len(kept) == len(items):
            return
        supp["suppressed"] = kept
        safe_write_json(supp_path, supp)
        _regen_suppressed_prompt(refs_dir, supp)
    except Exception as e:  # 清理失败绝不影响写入主流程
        sys.stderr.write(f"[memory] unsuppress '{entity}' skipped: {e}\n")


def _regen_suppressed_prompt(refs_dir, supp):
    """根据 .suppressed.json 重新生成 suppressed_prompt.md"""
    lines = ["# 已遗忘清单（双向遗忘）",
             "",
             "以下记忆已从文件标 superseded，且你（agent）自身也应放下，**不要主动提及**：",
             ""]
    for s in supp.get("suppressed", []):
        if not isinstance(s, dict):
            continue
        ent = s.get("entity")
        kind = s.get("kind")
        reason = s.get("reason") or "用户要求遗忘"
        lines.append(f"- ⚠️ 不要主动提及：**{ent}**（{kind}）— 原因：{reason}")
    lines.append("")
    lines.append("> 规则：文件是唯一权威源。被遗忘项既不从文件召回，也不从自身印象复述。")
    with open(os.path.join(refs_dir, "suppressed_prompt.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def cmd_init(mode, refs_dir):
    # mode 校验放在 cmd_init 本身而非只放 CLI main()：MCP memory_init 也走本函数，
    # 否则 MCP 传 mode="hack" 会照样写进 backend_config.json（CLI 与 MCP 行为不一致）。
    if mode not in ("local", "graph"):
        raise ValueError(f"未知 mode: {mode!r}（仅支持 local / graph）")
    os.makedirs(refs_dir, exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".backup"), exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".archive"), exist_ok=True)

    backend_path = os.path.join(refs_dir, "backend_config.json")
    if not os.path.exists(backend_path):
        backend_info = "纯本地文件模式" if mode == "local" else "本地文件 + graph 镜像"
        safe_write_json(backend_path, {
            "mode": mode,
            "backend_info": backend_info
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

    # === PATCH v2.9 === 承诺数据文件（promises.md 保留为人类可读入口，真数据落 json）
    proj_path = os.path.join(refs_dir, "promises.json")
    if not os.path.exists(proj_path):
        safe_write_json(proj_path, {"promises": []})

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
        # === 修复 (finding #3) === 即便 WAL 文件存在但无内容，也清空它，
        # 避免残留空行不断累积。
        _wal_prune(refs_dir, keep=[])
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "empty_wal"}))
        return

    # === PATCH v2.8.3 (3.3d) === 逐行解析并跳过损坏行。此前用列表推导整体
    # json.loads，WAL 里只要混进一行断电写坏的内容，整个 recover 就抛
    # JSONDecodeError 退出——恰恰在最需要恢复的崩溃场景下彻底失效。
    entries = []
    bad_lines = 0
    for ln in lines:
        try:
            obj = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            bad_lines += 1
            continue
        if isinstance(obj, dict):
            entries.append(obj)
        else:
            bad_lines += 1
    if bad_lines:
        sys.stderr.write(f"[memory_recover] 跳过 {bad_lines} 行损坏 WAL 记录\n")

    committed = set()
    for e in entries:
        if e.get("op") == "commit":
            committed.add((e.get("entity"), e.get("memory_id")))

    # === PATCH v2.8.3 (P0-1) === 已落盘的 memory_id 也算已完成。
    # 双保险：即便历史 WAL 里残留了旧版本产生的孤儿 upsert，只要对应记录
    # 已在 memory.json 中存在，就不再重放，避免无限自我复制。
    existing_ids = {m.get("id") for m in read_json_typed(os.path.join(refs_dir, "memory.json"), list)
                    if isinstance(m, dict)}

    uncommitted_map = {}
    for e in entries:
        if e.get("op") != "upsert":
            continue
        ent = e.get("entity")
        mid = e.get("memory_id", "")
        if (ent, mid) in committed or mid in existing_ids:
            continue
        uncommitted_map[(ent, mid)] = e

    uncommitted = list(uncommitted_map.values())
    if not uncommitted:
        # === 修复 (finding #3) === 关键：正常路径下每次 cmd_write 都会向 WAL 追加
        # 一对 upsert+commit，但内存已落盘、WAL 永不自动清。recover 跑起来发现"全部已
        # 提交"就直接返回，等于放任 WAL 无限膨胀（实测每跑一次 1→3→5→7…）。这里必须
        # 把 WAL 整体清空——它们都已安全持久化，没有保留价值。
        _wal_prune(refs_dir, keep=[])
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "all_committed"}))
        return

    # === PATCH v2.8.3 (P0-5) === 绝不 unlink .lock。
    # flock 的互斥建立在 inode 上：删掉锁文件后，正持有锁的进程仍以为自己独占，
    # 而新进程 open 出的是全新 inode 也能立刻拿到锁 —— 两边同时写，互斥形同虚设。
    # 真正的死锁场景已由 file_lock 内的「持有者进程不存在 → truncate 复用同 inode」处理。

    recovered = []
    failed = []
    for e in uncommitted:
        ent_name = e.get("entity")
        try:
            kind = e.get("kind") or e.get("type") or "general"
            if kind not in ALLOWED_KINDS:
                kind = "general"
            cmd_write(
                ent_name,
                kind,
                e.get("value", ""),
                refs_dir,
                mode="local",
                sentiment=e.get("sentiment"),
                source=e.get("source", "auto_detect"),
                confidence=_safe_float(e.get("confidence", 1.0), 1.0),
                emotion_tags=e.get("emotion_tags"),
                core=e.get("core"),
                reason=e.get("reason"),
                context=e.get("context"),           # === PATCH v2.9 ===
                expires_at=e.get("expires_at"),     # === PATCH v2.9 ===
            )
            recovered.append(ent_name)
        except Exception as ex:
            sys.stderr.write(f"[memory_recover] recover failed for {ent_name!r}: {ex}\n")
            failed.append(e)

    # === 修复 (finding #3) === 重放完成后截断 WAL：只保留重放失败的行，
    # 已成功落盘的记录一律清除，避免 WAL 文件无限增长（1→3→5→7…）。
    _wal_prune(refs_dir, keep=failed)

    print(json.dumps({
        "status": "recovered" if recovered else "partial",
        "recovered_count": len(recovered),
        "recovered_entities": recovered,
        "failed_count": len(failed),
        "total_uncommitted": len(uncommitted)
    }, ensure_ascii=False))


def cmd_wellness(mood, sleep_hours, sleep_quality, note, refs_dir):
    today = datetime.now(TZ).date().isoformat()   # === PATCH v2.6 A2 === 统一用 TZ
    well_path = os.path.join(refs_dir, "wellness.json")
    entry = {
        "date": today,
        "mood": _safe_str(mood),
        "recorded_at": ts_now(),   # === PATCH v2.6 A2 === 改用 ts_now()，全库统一 UTC+8
    }
    # === PATCH v2.8.3 (P0-4) === sleep_hours 分三档处理：
    #   ① 空 / "-"          → 不记该字段
    #   ② 完全无法解析成数字（"abc"/"八小时"）→ 温和忽略并告警，不打断心情记录
    #   ③ 能解析但值非法（NaN/Inf/1e400/负数/>24）→ 明确拒绝。
    #      NaN、Inf 若落盘会让 wellness.json 出现裸 NaN 字面量，非 Python 客户端
    #      从此无法解析整个文件；负数/超 24 小时则是明显的脏数据。
    if sleep_hours not in (None, "", "-"):
        raw = sleep_hours.strip() if isinstance(sleep_hours, str) else sleep_hours
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            parsed = None
            sys.stderr.write(f"[memory_wellness] sleep_hours={sleep_hours!r} 无法解析为数字，已忽略该字段\n")
        if parsed is not None:
            entry["sleep_hours"] = _require_finite("sleep_hours", parsed,
                                                   SLEEP_HOURS_MIN, SLEEP_HOURS_MAX)
    if sleep_quality and sleep_quality != "-":
        entry["sleep_quality"] = _safe_str(sleep_quality)
    if note and note != "-":
        entry["note"] = _safe_str(note)

    # === 修复 (finding #8) === 整个 read-modify-write 必须在同一把文件锁内完成。
    # 此前只给 safe_write_json 加锁，但读盘（read_json_typed）在锁外——并发写心情时
    # 多个线程读到同一份旧快照、各自 append 一条再写回，后写的覆盖先写的，记录就丢
    # （实测 20 并发只剩 2 条）。现在读+改+写全在锁内，串行化保证不丢。
    fd = file_lock(refs_dir)
    try:
        # === PATCH v2.8.3 (3.3c) === 改走 read_json_typed。此前直接 json.load，
        # wellness.json 一旦损坏就抛 JSONDecodeError，连"记一下今天心情"都做不了。
        data = read_json_typed(well_path, dict, refs_dir, "wellness")
        if not isinstance(data.get("records"), list):
            data["records"] = []
        data["records"].append(entry)
        # === v2.8.4 === 记录上限：只保留最近 WELLNESS_MAX_RECORDS 条，防止天长日久无界增长。
        # 按 date 升序截断（写入顺序本就近似有序，直接取尾部最稳）。新写入必被保留。
        if len(data["records"]) > WELLNESS_MAX_RECORDS:
            data["records"] = data["records"][-WELLNESS_MAX_RECORDS:]
        safe_write_json(well_path, data)
    finally:
        file_unlock(fd, refs_dir)
    print(json.dumps({"status": "ok", "date": today, "mood": mood}, ensure_ascii=False))


def _maybe_mirror_to_graph(entry, refs_dir):
    """v2.8 拆耦合: graph 镜像逻辑已迁至 references/graph_backend.py（自包含, 仅该模块 import sqlite3）。

    本函数保证: 默认本地模式下**完全不接触 sqlite3 / graph_backend**——仅当
    backend_config.json 设 mirror_mode=='graph' 且 graph_db_path 存在时, 才动态
    加载 graph_backend 并调用其 mirror_to_graph()。这样核心文件顶部不再有 sqlite3
    硬依赖, '零依赖' 名副其实, graph 成真·可选后端。"""
    # 轻量预筛: 只读 config 判断, 不加载任何 sqlite 相关模块
    try:
        cfg_path = os.path.join(refs_dir, "backend_config.json")
        if not os.path.exists(cfg_path):
            return
        with open(cfg_path, "r", encoding="utf-8") as _f:
            cfg = json.load(_f) or {}
        if cfg.get("mirror_mode") != "graph":
            return
        gdb = cfg.get("graph_db_path")
        if not gdb or not os.path.exists(gdb):
            return
    except Exception:
        return
    # 到此才动态加载 graph 后端（此时才 import sqlite3）
    try:
        import importlib.util as _ilu
        _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graph_backend.py")
        _spec = _ilu.spec_from_file_location("graph_backend", _path)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _mod.mirror_to_graph(entry, refs_dir, cfg=cfg)
    except Exception as e:
        sys.stderr.write(f"[mirror_to_graph] skipped: {e}\n")


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
            context = None
            expires_at = None
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
                    # 校验交给 cmd_write 的 _require_finite（CLI 传 nan/inf 同样会被拒）
                    confidence = args[i + 1]; i += 2
                elif args[i] == "--emotion-tags" and i + 1 < len(args):
                    emotion_tags = [t.strip() for t in args[i + 1].split(",") if t.strip()]; i += 2
                elif args[i] == "--reason" and i + 1 < len(args):
                    reason = args[i + 1]; i += 2
                elif args[i] == "--core" and i + 1 < len(args):
                    core = args[i + 1].lower() in ("true", "1", "yes", "y"); i += 2
                elif args[i] == "--context" and i + 1 < len(args):
                    context = args[i + 1]; i += 2
                elif args[i] == "--expires" and i + 1 < len(args):
                    expires_at = args[i + 1]; i += 2
                else:
                    nm_args.append(args[i]); i += 1
            if len(nm_args) < 3:
                print(json.dumps({"error": "用法: write <entity> <kind> <value> [refs_dir] [--mode local] [--sentiment ...] [--source file_import|self_inferred|user_explicit] [--confidence 0..1] [--emotion-tags 占有,吃醋] [--reason 文本] [--core true|false] [--context 当时的气氛] [--expires YYYY-MM-DD]"}), file=sys.stderr)
                sys.exit(1)
            cmd_write(nm_args[0], nm_args[1], nm_args[2], refs_dir, mode, sentiment,
                      source, confidence, emotion_tags, reason, core, context, expires_at)
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
        elif cmd == "selfcheck":
            print(json.dumps(cmd_selfcheck(refs_dir), ensure_ascii=False))
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
        elif cmd == "deny":
            reason = None
            nm_args = []
            i = 0
            while i < len(args):
                if args[i] == "--reason" and i + 1 < len(args):
                    reason = args[i + 1]; i += 2
                else:
                    nm_args.append(args[i]); i += 1
            if not nm_args:
                print(json.dumps({"error": "用法: deny <entity|memory_id> [refs_dir] [--reason 文本]"}), file=sys.stderr)
                sys.exit(1)
            cmd_deny(nm_args[0], refs_dir, reason)
        elif cmd == "expire":
            cmd_expire_check(refs_dir)
        elif cmd == "recall":
            limit = 3
            nm_args = []
            i = 0
            while i < len(args):
                if args[i] == "--limit" and i + 1 < len(args):
                    try:
                        limit = max(1, min(10, int(args[i + 1])))
                    except ValueError:
                        pass
                    i += 2
                else:
                    nm_args.append(args[i]); i += 1
            cmd_recall(refs_dir, limit)
        elif cmd == "promise":
            if not args:
                print(json.dumps({"error": "用法: promise <add|done|list|check> ..."}), file=sys.stderr)
                sys.exit(1)
            sub = args[0]
            sub_args = args[1:]
            if sub == "add":
                deadline = None
                nm_args = []
                i = 0
                while i < len(sub_args):
                    if sub_args[i] == "--deadline" and i + 1 < len(sub_args):
                        deadline = sub_args[i + 1]; i += 2
                    else:
                        nm_args.append(sub_args[i]); i += 1
                if not nm_args:
                    print(json.dumps({"error": "用法: promise add <承诺内容> [refs_dir] [--deadline YYYY-MM-DD]"}), file=sys.stderr)
                    sys.exit(1)
                cmd_promise(nm_args[0], refs_dir, deadline)
            elif sub == "done":
                if not sub_args:
                    print(json.dumps({"error": "用法: promise done <promise_id> [refs_dir]"}), file=sys.stderr)
                    sys.exit(1)
                cmd_promise_done(sub_args[0], refs_dir)
            elif sub == "list":
                cmd_promise_list(refs_dir)
            elif sub == "check":
                cmd_promise_check(refs_dir)
            elif sub == "watch":
                interval = PROMISE_WATCH_INTERVAL
                i = 0
                while i < len(sub_args):
                    if sub_args[i] == "--interval" and i + 1 < len(sub_args):
                        try:
                            interval = max(1, int(sub_args[i + 1]))
                        except ValueError:
                            pass
                        i += 2
                    else:
                        i += 1
                cmd_promise_watch(refs_dir, interval)
            else:
                print(json.dumps({"error": f"未知 promise 子命令: {sub}（add/done/list/check/watch）"}), file=sys.stderr)
                sys.exit(1)
        elif cmd == "init":
            if not args:
                print(json.dumps({"error": "用法: init <mode> [refs_dir]  mode: local"}), file=sys.stderr)
                sys.exit(1)
            mode = args[0]
            # === 修复 (finding #4) === 路径显式取自第二个参数：上面的 isdir 回扫只识别
            # "已存在"的目录，而 init 的目标路径往往尚不存在（需要先建库）。若路径已存在，
            # 回扫已把它从 args 弹出并设好 refs_dir；若还不存在，这里用 args[1] 兜底。
            if len(args) > 1:
                refs_dir = os.path.abspath(args[1])
            if mode not in ("local", "graph"):
                print(json.dumps({"error": f"未知 mode: {mode!r}（仅支持 local / graph）"}), file=sys.stderr)
                sys.exit(1)
            cmd_init(mode, refs_dir)
        else:
            print(json.dumps({"error": f"未知命令: {cmd}"}), file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
