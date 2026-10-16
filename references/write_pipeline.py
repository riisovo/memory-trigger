#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
write_pipeline.py —— 长期记忆核心引擎与 CLI 入口
============================================
核心机制：
  1) 双源信任（Dual-Source Trust）：
     · 区分文件权威源（user_explicit / file_import）与自身推断源（self_inferred）。
     · 写入必填 confidence（0..1）；self_inferred 且 confidence ≤ 0.85 自动挂起为 pending（不进检索），
       待同一事实被高置信或用户明说再次确认时自动转正为 active。
     · 跨实体/跨类型全库相似度扫描（bigram + 文档频率过滤主题词），返回疑似冲突与用户确认问句（ask_user_this），
       不静默作废已有记忆。
  2) 人情味层与遗忘曲线：
     · 核心记忆钉死（core）：relationship / identity 默认 core=True，永不衰减；支持 --core 显式指定。
     · 艾宾浩斯衰减（importance）：非核心记忆按距上次提及（last_mentioned）天数指数衰减（半衰期≈138 天）。
     · 情感锚点（context）、情绪标签（emotion_tags）、到期时间（expires_at）、否认降权（deny，2 次转 pending）、
       双向遗忘（forget，标记 superseded 并生成 suppressed_prompt.md）。
  3) 检索与主动回忆（想起 ≠ 提及）：
     · search（提及）：支持多词模糊检索，按权威层、匹配度、有效权重排序，结合 72h 展示冷却；
       命中即重置衰减并刷新 last_mentioned 与 last_recalled。
     · recall（想起）：温热通道 + 旧事重提通道（入库 >30 天且距上次想起 >7 天，按沉默天数排序），
       仅刷新 last_recalled（供 24h 去重），不续命、不加分；支持按 tags 定向召回。
  4) 承诺闭环与自我进化：
     · promise（add / done / list / check / watch）：承诺建档、逾期追踪与三路提醒推送（Bark / webhook / 落盘）。
     · evolve / evolve-check / retraction-scan：全谱记忆四档配额轮批复盘、六道判据举证闸门、宿主记忆文件悬空引用扫描。
  5) 并发与数据安全：
     · flock 文件锁互斥、写前 WAL（.wal.jsonl）与恢复后截断、微秒级成对快照备份（.backup）与损坏自动隔离恢复。
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
import re
import sys
import time
import shutil
import uuid
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=8))  # UTC+8

# 数值合法区间
CONFIDENCE_MIN, CONFIDENCE_MAX = 0.0, 1.0
SLEEP_HOURS_MIN, SLEEP_HOURS_MAX = 0.0, 24.0

# wellness 心情记录条数上限（保留最近 N 条，防止文件无界增长）
WELLNESS_MAX_RECORDS = 365

# 到期记忆 / 否认降权 / 主动回忆 / 承诺追踪参数
# 到期记忆：expires_at 到期后自动移出 active（不再被检索召回），数据保留可查
EXPIRY_REMIND_DAYS = 3            # 到期前多少天列为"即将到期"提醒
# 否认降权：用户否认一条记忆 → importance ×DENY_FACTOR 且记录 deny_count；
# 累积 2 次否认 → 直接转 pending（彻底退出检索，需重新确认才复活）
DENY_FACTOR = 0.1
DENY_TO_PENDING_AFTER = 2
# 主动回忆：recall 建议的评分加分（让"从未被提起 / 带情感 / 快到期"的记忆更容易被想起）
RECALL_BONUS_NEVER = 0.15
RECALL_BONUS_EXPIRING = 0.20
# 情绪偏好（recall 排序）：正向温暖标签优先，关心类次之，平淡/严肃不加分
SWEET_TAGS = {"甜蜜", "温暖", "想念", "", "撒娇", "激动", "关心", "害羞"}
CARE_TAGS = {"委屈", "担心", "生气", "疲惫"}
RECALL_BONUS_SWEET = 0.5
RECALL_BONUS_CARE = 0.25
RECALL_RECENCY_HOURS = 24  # 非定向 recall 后 24h 内不再进入常规召回池
# 旧事重提通道双阈值（同时满足才进入冷通道）：
RECALL_COLD_AGE_DAYS = 30   # 阈值一：入库满 30 天
RECALL_GAP_DAYS = 7         # 阈值二：距上次被 recall 满 7 天

def recall_emotion_boost(tags):
    """按情绪标签给 recall 排序加分：甜弹药优先，关心类次之，平淡/严肃不加分。"""
    ts = set(tags) if isinstance(tags, (list, tuple, set)) else set()
    if ts & SWEET_TAGS:
        return RECALL_BONUS_SWEET
    if ts & CARE_TAGS:
        return RECALL_BONUS_CARE
    return 0.0

# 允许的记忆类型枚举（与 SKILL.md §3.1 对齐）
ALLOWED_KINDS = {
    "preference", "event", "habit", "rule", "scene",
    "relationship", "emotion",
    "identity", "milestone", "general",
}
# self_inferred 置信度小于等于此阈值时落 pending（不进 active 检索，防低置信推断固化）
SELF_INFERRED_PENDING_THRESHOLD = 0.85

# 遗忘曲线与核心记忆钉死
# Ebbinghaus 式衰减：importance *= DECAY_FACTOR ** days_since_last_mentioned（半衰期 ≈ 138 天）
DECAY_FACTOR = 0.995
# 默认钉死为核心记忆（永不衰减）的类别
CORE_KINDS = {"relationship", "identity"}

# 默认实体别名归一化表
DEFAULT_ALIASES = {
    "旧书店": "读书",
    "科幻小说": "读书",
    "杂志": "读书",
    "图书馆": "读书",
    "报刊亭": "读书",
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

# 类型安全转换：对 None / 异常类型提供默认值兜底
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
    """将损坏文件隔离另存为 .corrupt.<微秒时间戳>（同名递增序号），保留现场供排查恢复。"""
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
    """读取 JSON 并校验顶层类型（expect=list|dict）。

    读取策略：
      1. 文件不存在        → 返回空容器（正常冷启动）
      2. 文件损坏/类型不符 → 先隔离 .corrupt 副本，再尝试从 .backup 恢复最近可用快照；
                            无可用快照时返回空容器（原始损坏文件已保存在 .corrupt）
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
            # allow_nan=False：禁止写出裸 NaN/Infinity，确保跨语言标准 JSON 兼容性
            json.dump(data, f, ensure_ascii=False, indent=2, allow_nan=False)
        # 使用 os.replace 执行跨平台原子覆盖替换
        os.replace(tmp_path, path)
    except PermissionError as e:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise PermissionError(
            f"无写权限：无法写入 {path}。常见于记忆库目录由 root/其他用户创建、"
            f"当前 MCP 进程无写权限。"
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
    """WAL 重放完成后截断 .wal.jsonl，仅保留重放失败的条目（keep），防止 WAL 文件无限增长。
    整个重写过程在文件锁内原子执行。"""
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
    """返回指定 stem 的备份快照路径列表，按文件 mtime 从新到旧排序。"""
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
    """为 entity_index.json 与 memory.json 创建一致性快照到 .backup。

    备份策略：
      1. 校验通过才备份：备份前校验 JSON 可解析且顶层类型正确，避免将损坏文件写入备份。
      2. 微秒级时间戳命名：防止同一秒内多次写入产生同名覆盖。
      3. 内容去重：与最新快照逐字节相同时跳过写入，避免无变化快照冲刷保留窗口。
      4. 成对写入：任一文件发生变化时使用同一时间戳成对备份，确保回滚时索引与数据版本一致。
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
    """按同一时间戳的快照组为单位淘汰旧备份，保留最近 keep_groups 组，确保 memory 与 entity_index 成对留存。"""
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

# 查询词拆分：按空格/逗号/顿号/分号/斜杠拆分为词项（保留单字词）
def _split_terms(query: str):
    if not isinstance(query, str):
        return []
    import re as _re
    parts = _re.split(r"[\s,，、;；/]+", query.strip())
    return [p.strip() for p in parts if p.strip()]


# 冲突扫描关键词通道：抽取两段文本共有的 2~6 字语义片段（按标点切段，过滤停用词与全库高频词），
# 作为 bigram 相似度的补充通道，捕捉「措辞不同但核心主题词重合」的同主题记忆。

# 停用数字与符号残片
_STOP_FRAGS = {
    "20", "26", "10", "19", " c", " m", " s", " t", " a", " b", " o", " p", " d",
    "6-", "-0", "-1", "-2", "02", "01", "03", "04", "05", "06", "07", "08", "09", "12",
    "2026", "2025", "2024", "026", "025", "024",
}
# 中文高频虚词、通用动词与无区分度抽象词
_STOP_WORDS = {
    "的", "了", "是", "在", "我", "你", "有", "和", "就", "不", "也",
    "很", "都", "这", "那", "个", "们", "与", "或", "被", "把", "让", "说", "看",
    "到", "去", "来", "会", "要", "能", "为", "以", "于", "之", "其", "该", "如",
    "直接", "完整", "一起", "一样", "已经", "但是", "因为", "所以", "如果", "这个",
    "那个", "什么", "怎么", "为什么", "可以", "应该", "必须", "时候", "现在", "知",
    "道", "想起", "记得", "感觉", "觉得", "在家", "出来", "进去", "没有", "不是",
    "铁律", "规则", "自己", "知道", "动作", "开口", "memory", "soul", "md",
    "真实", "执行", "三条", "称呼", "瞬间", "跳过", "一直", "每次", "重复",
    "明确", "必须", "不要", "不能", "需要", "记住", "真的", "什么", "时候",
    "今天", "明天", "昨天", "最近", "现在", "以后", "之前", "一个", "一件",
    "事情", "问题", "地方", "东西", "方法", "方式", "情况", "结果", "原因",
    "目标", "状态", "记忆", "提醒", "检查", "确认", "验证", "发现",
    "emory", "memor", "emor", "mory", "ory", "soul", "user", "Agent",
    "bark", "tts", "api", "json", "http", "sdk", "cli",
}
def _shared_topic_words(a: str, b: str, df_stop=None):
    """提取两段文本共有的 2~6 字语义片段（按标点切分意群，过滤停用词、数字与高频片段，长词吸收短词）。"""
    if not isinstance(a, str) or not isinstance(b, str):
        return []

    _SEP = "。，、！？；：\n\t 　()[)（】】[]【】「」『』\"'～~!?;:,.…—-_/\\|"
    def _segments(t):
        out, cur = [], []
        for ch in t:
            if ch in _SEP:
                if cur:
                    out.append("".join(cur)); cur = []
            else:
                cur.append(ch)
        if cur:
            out.append("".join(cur))
        return out

    a_segs = _segments(a.lower())
    b_segs = _segments(b.lower())

    def _subwords(seg):
        """从一个连续片段里切出所有 ≥2 字的子串（不跨标点），并过滤停用词。"""
        res = set()
        n = len(seg)
        for ln in (2, 3, 4, 5, 6):
            if ln > n:
                break
            for i in range(n - ln + 1):
                w = seg[i:i+ln]
                if w in _STOP_FRAGS or w in _STOP_WORDS:
                    continue
                if w.isdigit():
                    continue
                if w.isascii() and len(w) <= 3:
                    continue
                res.add(w)
        return res

    b_all = set()
    for seg in b_segs:
        b_all |= _subwords(seg)
    a_all = set()
    for seg in a_segs:
        a_all |= _subwords(seg)
    shared = a_all & b_all
    # 按全库文档频率过滤高频无区分度片段（出现率 > 3% 的 2-gram）
    if df_stop:
        shared = {w for w in shared if w not in df_stop}
    # 长词吸收短词：若长片段已命中，其子串不再重复计数
    deduped = {w for w in shared if not any(w != o and w in o for o in shared)}
    return sorted(deduped)


def normalize_entity(entity, refs_dir, substring_match=False):
    """将实体名映射为标准实体名（基于 aliases.json 与 DEFAULT_ALIASES 精确映射；可选启用子串归并）。"""
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

# 来源 → 权威层级映射
def authority_of(source):
    """file 层：file_import / user_explicit / auto_detect（默认视为文件侧沉淀）
       self 层：self_inferred（Agent 自身印象，仅补充）"""
    if source == "self_inferred":
        return "self"
    return "file"

# 遗忘曲线与核心记忆判定
def _is_core(entry):
    """是否核心记忆：显式 core 字段优先；缺省时按 kind 推导（relationship/identity）。"""
    if not isinstance(entry, dict):
        return False
    if "core" in entry:
        return bool(entry.get("core"))
    return entry.get("kind") in CORE_KINDS

def _days_since(entry, now):
    """计算距离最近一次被提及（last_mentioned，缺省回退 created）的天数，作为遗忘衰减的时间跨度。
    仅 search 命中更新 last_mentioned；recall 的「想起」不更新该锚点。"""
    if not isinstance(entry, dict):
        return 0.0
    ref = entry.get("last_mentioned") or entry.get("created")
    if not ref or not isinstance(ref, str):
        return 0.0
    try:
        return max(0.0, (now - ts_parse(ref)).total_seconds() / 86400.0)
    except Exception:
        return 0.0

def effective_importance(entry, now):
    """计算当前有效权重：core 记忆恒为 1.0；非核心记忆按 DECAY_FACTOR ** days 指数衰减。"""
    if _is_core(entry):
        return 1.0
    imp = _safe_float(entry.get("importance", 1.0), 1.0)
    return imp * (DECAY_FACTOR ** _days_since(entry, now))

# ──────────────────────────────────────────
# 核心命令
# ──────────────────────────────────────────

def cmd_write(entity, etype, value, refs_dir, mode=None, sentiment=None,
              source="auto_detect", confidence=None, emotion_tags=None, reason=None,
              core=None, context=None, expires_at=None):
    """完整 upsert 写入管线（实体归一化、双源信任判定、全库冲突扫描、WAL 与成对备份）。

    confidence 为必填项（[0, 1] 有限数）：
      · 用户明说或有原文佐证 → 0.9~1.0（直接 active）
      · Agent 自行推断无原话佐证 → 0.6~0.85（当 source=self_inferred 且 confidence ≤ 0.85 时挂 pending，
        待同一事实被高置信或用户明说再次写入时自动转正为 active）
    """
    if not entity or not str(entity).strip():
        raise ValueError("entity 必填且不能为空：记忆必须归属到具体实体（如 用户/读书/伴侣）")
    entity = str(entity)
    if etype not in ALLOWED_KINDS:
        raise ValueError(f"非法 kind='{etype}'，允许：{sorted(ALLOWED_KINDS)}")
    if confidence is None:
        raise ValueError(
            "confidence 必填：写记忆前先自己判断这条有没有依据。"
            "用户亲口说的/文件查得到 → 0.9~1.0；自己推断但没原话佐证 → 0.6~0.85"
            "（会自动挂 pending，不进检索；同一事实被用户本人再次明确写入时自动转正）；拿不准 → 先别写，去问用户。"
        )
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

    # 写入前执行实体别名归一化
    entity = normalize_entity(entity, refs_dir)
    kind = etype

    fd = file_lock(refs_dir)
    try:
        backup_files(refs_dir)

        new_id = f"mem_{datetime.now(TZ).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        mem_path = os.path.join(refs_dir, "memory.json")
        ei_path = os.path.join(refs_dir, "entity_index.json")
        memory = read_json_typed(mem_path, list, refs_dir, "memory")
        ei = read_json_typed(ei_path, dict, refs_dir, "entity_index")
        if "entities" not in ei or not isinstance(ei.get("entities"), dict):
            ei = {"version": 1, "entities": {}}

        similar_idx = None
        # 跨实体/跨类型全库相似记忆扫描（用于生成冲突报告，不自动作废旧记录）
        _conflict_cands = []

        # 预计算全库 active 记忆的 2-gram 文档频率，过滤出现率 > 3% 的高频片段
        _df = {}
        _active_vals = [_safe_str(e.get("value", "")) for e in memory
                        if isinstance(e, dict) and _norm_status(e) == "active"]
        _n_docs = max(1, len(_active_vals))
        for _av in _active_vals:
            for _frag in {_av[j:j+2] for j in range(len(_av) - 1)}:
                _df[_frag] = _df.get(_frag, 0) + 1
        _DF_MAX = 0.03
        _df_stop = {f for f, c in _df.items() if c / _n_docs > _DF_MAX}

        for i, entry in enumerate(memory):
            if not isinstance(entry, dict):
                continue
            # 跨实体/跨类型相似检测（双通道：bigram 相似度 ≥ 0.30 或共有主题词 ≥ 4 个）
            if _norm_status(entry) == "active":
                old_val = _safe_str(entry.get("value", ""))
                if len(old_val) >= 4 and len(value) >= 4:
                    old_bg = {old_val[j:j+2] for j in range(len(old_val)-1)}
                    new_bg = {value[j:j+2] for j in range(len(value)-1)}
                    inter = old_bg & new_bg
                    uni = old_bg | new_bg
                    sim = (len(inter) / len(uni)) if uni else 0.0
                    shared_kw = [w for w in _shared_topic_words(old_val, value, df_stop=_df_stop) if len(w) >= 2]
                    if sim >= 0.30 or len(shared_kw) >= 4:
                        _conflict_cands.append((i, max(sim, 0.32), entry))
            e_kind = entry.get("kind") or entry.get("type")
            if entry.get("entity") == entity and e_kind == kind:
                # 同 entity+kind 下的幂等合并与转正匹配（同时覆盖 active 与 pending 状态）：
                # 若内容完全相同或 bigram 重合度 > 0.7，则复用原记录合并更新；其余内容不同的记录新建共存。
                if _norm_status(entry) in ("active", "pending"):
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

        # 双源信任状态判定：self_inferred 且 confidence ≤ 阈值时挂 pending，其余进 active
        if source == "self_inferred" and confidence <= SELF_INFERRED_PENDING_THRESHOLD:
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
                "source": source,
                "confidence": confidence,
                "emotion_tags": emotion_tags or [],
                "value": value,
                "created": ts_now(),
                "updated": ts_now(),
                "last_recalled": None,
                "status": init_status,
                "reason": reason,
                "importance": 1.0,
                "core": (kind in CORE_KINDS) if core is None else bool(core),
                "context": _safe_str(context) or None,
                "expires_at": _parse_expiry(expires_at),
            }

        # 幂等合并时先定稿复用原记录 id，再写入 WAL，保证 upsert 与 commit 的 memory_id 一致
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
            "context": _safe_str(context) or None,
            "expires_at": _parse_expiry(expires_at),
            "memory_id": new_id,
            "targets": targets
        })

        _pending_upgraded = False
        _prev_old_value = None
        if similar_idx is not None:
            # 覆盖前先记录原正文，供返回值 previous_value 回显
            _prev_old_value = _safe_str(memory[similar_idx].get("value", ""))
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
        else:
            memory.append(_make_entry())
        # 待确认记录转正：若命中原 pending 记录且本次写入达到 active 标准，则自动升级为 active
        if (similar_idx is not None and _norm_status(memory[similar_idx]) == "pending"
                and init_status == "active"):
            memory[similar_idx]["status"] = "active"
            memory[similar_idx]["confirmed_at"] = ts_now()
            memory[similar_idx]["confirm_reason"] = "同一事实被再次确认（user_explicit 或高置信重写）"
            _pending_upgraded = True

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
            # 按文件 mtime 取最新备份组执行成对回滚
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

        # Write-through 备份：commit 成功后立即生成最新一致性快照
        try:
            backup_files(refs_dir)
        except Exception as e:
            sys.stderr.write(f"[memory] write-through 备份失败（不影响本次写入）：{e}\n")

        # 重新写入某实体时同步解除其遗忘抑制状态（.suppressed.json 与 suppressed_prompt.md）
        _unsuppress_entity(refs_dir, entity)

        # 生成跨库相似记忆冲突报告（排除本次写入自身）
        conflict_report = []
        try:
            for _i, _sim, _e in sorted(_conflict_cands, key=lambda x: -x[1])[:5]:
                if _e.get("id") == new_id:
                    continue
                conflict_report.append({
                    "id": _e.get("id"),
                    "entity": _e.get("entity"),
                    "kind": _e.get("kind") or _e.get("type"),
                    "similarity": round(_sim, 3),
                    "value_preview": _safe_str(_e.get("value", ""))[:120],
                    "_full_value": _safe_str(_e.get("value", "")),
                    "hint": "疑似新旧规则并存——考虑 memory_forget 这条或确认共存",
                })
        except Exception:
            conflict_report = []

        # 回显最终落盘正文与覆盖前旧值
        _final = next((e for e in memory if e.get("id") == new_id), None) or {}
        _final_value = _safe_str(_final.get("value", "")) or value
        _prev_value = _prev_old_value

        # 当最相似候选与新正文重合度 > 0.35 时生成用户确认问句（ask_user_this）
        _ask_user_this = None
        if conflict_report:
            _ov = _jaccard(_bigrams(_final_value),
                           _bigrams(conflict_report[0].get("_full_value") or ""))
            if _ov > 0.35:
                _ask_user_this = (
                    "【只在确实疑似重复时转达，别的话自己判断，不要为无关条目打扰用户】\n"
                    "用户，我刚记了一条：「" + _final_value[:70] + "」\n"
                    "库里有一条相关的：「"
                    + (conflict_report[0].get("value_preview") or "")[:70] + "」\n"
                    "这条需不需要作废？还是都留着？（大多数情况都该留着——只有确实是"
                    "同一件事的过时版本才作废）"
                )

        # 剥离内部临时字段
        for _cr in conflict_report:
            _cr.pop("_full_value", None)

        print(json.dumps({
            "status": "committed",
            "memory_id": new_id,
            "entity": entity,
            "kind": kind,
            "value": _final_value,
            "action": ("created" if similar_idx is None else "merged"),
            "previous_value": _prev_value,
            "sentiment": sentiment,
            "source": source,
            "status_field": init_status,
            "pending_upgraded": _pending_upgraded,
            "core": (kind in CORE_KINDS) if core is None else bool(core),
            "importance": 1.0,
            "context": _safe_str(context) or None,
            "expires_at": _parse_expiry(expires_at),
            "mode": mode,
            "targets": targets,
            "conflict_scan": {
                "similar_count": len(_conflict_cands),
                "conflicts": conflict_report[:3],
                "ask_user_this": _ask_user_this,
                "note": "相似度≥0.30 的跨库旧记忆，主题撞上即须裁定留或废",
            },
        }, ensure_ascii=False))

    finally:
        file_unlock(fd, refs_dir)


def _touch_recalled(refs_dir, mem_path, hit_ids, now=None):
    """更新 search 命中条目的召回与提及状态（想起 ≠ 提及）：
      · last_recalled 刷新为当前时间（供 24h 去重抑制）
      · last_mentioned 刷新为当前时间（作为遗忘衰减锚点）
      · 非核心记忆的 importance 重置为 1.0（被重新提及即恢复鲜活度）
    recall 工具仅标记 last_recalled，不调用本函数。
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
                    # 重新被提起 = 尚未过时：衰减清账，回到满分重新计息。
                    # 若某条已被反复沉淀成核心事实，可由 cmd_decay 显式压回，不走这里。
                    e["importance"] = 1.0
                e["last_recalled"] = now.isoformat(timespec="seconds")
                e["last_mentioned"] = e["last_recalled"]  # 同一时刻：search 命中即提及
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
    """本地关键词与多词模糊检索：仅返回 active 记忆，按权威层、匹配度与有效权重排序，并执行 72h 展示冷却。"""
    query = _safe_str(query)
    query = normalize_entity(query, refs_dir)

    mem_path = os.path.join(refs_dir, "memory.json")
    ei_path = os.path.join(refs_dir, "entity_index.json")

    memory = read_json_typed(mem_path, list, refs_dir, "memory")
    ei = read_json_typed(ei_path, dict, refs_dir, "entity_index")
    if not isinstance(ei.get("entities"), dict):
        ei = {"version": 1, "entities": {}}

    results = []

    # 空查询或纯空白查询直接返回空结果
    if not query.strip():
        print(json.dumps({"query": query, "results_count": 0, "results": []}, ensure_ascii=False, indent=2))
        return

    q = query.lower()

    # 多词查询拆分：支持按空格/逗号/顿号分词并按命中词数比例计分
    _q_terms = [t for t in _split_terms(query) if t]
    if not _q_terms:
        _q_terms = [q]

    if query in ei.get("entities", {}):
        ent = ei["entities"].get(query) or {}
        mid = ent.get("last_memory_id") if isinstance(ent, dict) else None
        if mid:
            for entry in memory:
                if not isinstance(entry, dict):
                    continue
                if entry.get("id") == mid and entry.get("entity") == query and _norm_status(entry) == "active":
                    results.append({
                        "source": "entity_index",
                        "confidence": 0.95,
                        "entry": entry
                    })
                    break

    for entry in memory:
        if not isinstance(entry, dict):
            continue
        if _norm_status(entry) != "active":
            continue
        val = _safe_str(entry.get("value", "")).lower()
        ent_name = _safe_str(entry.get("entity", "")).lower()
        # 单词精确子串匹配或多词比例匹配
        if len(_q_terms) == 1 and q in val or q in ent_name:
            eid = entry.get("id")
            if eid is None or not any(r["entry"].get("id") == eid for r in results):
                results.append({
                    "source": "keyword",
                    "confidence": 0.75,
                    "entry": entry
                })
        elif len(_q_terms) > 1:
            hits = sum(1 for t in _q_terms if t in val or t in ent_name)
            if hits > 0:
                eid = entry.get("id")
                if eid is None or not any(r["entry"].get("id") == eid for r in results):
                    results.append({
                        "source": "keyword_fuzzy",
                        "confidence": 0.4 + 0.5 * (hits / len(_q_terms)),
                        "entry": entry,
                        "_fuzzy_hits": hits,
                        "_fuzzy_total": len(_q_terms),
                    })

    # 标注权威层（file / self）与多源补充标记（supplement）
    for r in results:
        r["authority"] = authority_of(r["entry"].get("source", "auto_detect"))
    file_entities = {_safe_str(r["entry"].get("entity", "")) for r in results if r["authority"] == "file"}
    for r in results:
        r["supplement"] = (r["authority"] == "self" and _safe_str(r["entry"].get("entity", "")) in file_entities)
    # 排序优先级：文件权威层优先 → 匹配度降序 → 有效权重（effective_importance）降序
    now = datetime.now(TZ)
    for r in results:
        r["_eff_imp"] = effective_importance(r["entry"], now)
        if r["source"] == "keyword_fuzzy":
            r["_match"] = r.get("_fuzzy_hits", 1) / max(1, r.get("_fuzzy_total", 1))
        elif r["source"] == "keyword":
            r["_match"] = 0.95
        else:  # entity_index
            r["_match"] = 1.0
    results.sort(key=lambda r: (0 if r["authority"] == "file" else 1,
                                -r["_match"], -r["_eff_imp"]))

    # 72 小时展示冷却：读取 .shown_log.json，优先返回未在冷却窗口内的条目；全部冷却时按序回填兜底
    _shown_path = os.path.join(refs_dir, ".shown_log.json")
    _shown = {}
    try:
        _shown = json.loads(open(_shown_path, encoding="utf-8").read())
        if not isinstance(_shown, dict):
            _shown = {}
    except Exception:
        _shown = {}
    _now_epoch = time.time()
    _COOLDOWN_S = 72 * 3600
    def _is_cooled(eid):
        ts = _shown.get(eid)
        return isinstance(ts, (int, float)) and (_now_epoch - ts) < _COOLDOWN_S
    _DISPLAY = 5
    _fresh_sorted = [r for r in results if not _is_cooled(r["entry"].get("id"))]
    _cooled_sorted = [r for r in results if _is_cooled(r["entry"].get("id"))]
    if _fresh_sorted:
        results = _fresh_sorted[:_DISPLAY]
    else:
        results = _cooled_sorted[:_DISPLAY]

    # 仅对实际展示的条目（前 5 条）更新提及状态与冷却记录
    hit_ids = [r["entry"].get("id") for r in results if r["entry"].get("id")]
    touch_updates = _touch_recalled(refs_dir, mem_path, hit_ids, now)
    for uid in hit_ids:
        _shown[uid] = _now_epoch
    try:
        # 保留最近 7 天的展示记录
        _shown = {k: v for k, v in _shown.items()
                  if isinstance(v, (int, float)) and (_now_epoch - v) < 7 * 86400}
        tmp = _shown_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_shown, fh, ensure_ascii=False)
        os.replace(tmp, _shown_path)
    except Exception:
        pass

    out = []
    for r in results[:5]:
        e = dict(r["entry"])
        uid = e.get("id")
        if uid in touch_updates:
            e["last_recalled"] = touch_updates[uid]["last_recalled"]
            e["importance"] = touch_updates[uid]["importance"]
        # 精简输出字段，仅保留核心契约字段以节省上下文开销
        slim = {
            "id": e.get("id"),
            "entity": e.get("entity"),
            "kind": e.get("kind") or e.get("type"),
            "value": e.get("value"),
            "authority": r.get("authority"),
            "emotion_tags": e.get("emotion_tags"),
            "core": _is_core(e),
            "importance": round(effective_importance(e, now), 2),
            "match": r["source"],
        }
        if e.get("status") != "active":
            slim["status"] = e.get("status")
        out.append(slim)

    print(json.dumps({
        "query": query,
        "results_count": len(results),
        "results": out
    }, ensure_ascii=False))


# 存量数据自检与自愈

def _norm_status(e):
    """读取路径 status 归一化：缺省或空值视为 active，显式状态原样保留。"""
    st = e.get("status") if isinstance(e, dict) else None
    return st or "active"


def cmd_selfcheck(refs_dir):
    """扫描 memory.json，自动补全缺失/空 entity 字段与缺省 status 字段，返回修复摘要。"""
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

    # 核心记忆数 / 非核心平均权重 / 最弱记忆排行
    now = datetime.now(TZ)
    core_count = sum(1 for m in memory if _norm_status(m) == "active" and _is_core(m))
    noncore = [m for m in memory if _norm_status(m) == "active" and not _is_core(m)]
    avg_imp = round(sum(effective_importance(m, now) for m in noncore) / len(noncore), 4) if noncore else 0
    weakest = sorted(
        [(m.get("entity"), m.get("kind"), round(effective_importance(m, now), 4)) for m in noncore],
        key=lambda x: x[2]
    )[:5]

    timestamps = sorted(_safe_str(m.get("created", "")) for m in memory if m.get("created"))

    # 最久未召回排行（last_recalled 为空时回退 created 计算）
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
    """梦境周期：对全部 active 且非 core 的记忆统一应用艾宾浩斯遗忘衰减。"""
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
                # created 缺失或无法解析时保守保留，避免误归档
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
            # 按日期归档并支持同日多次归档增量合并去重
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
    """双向遗忘：将匹配记忆标记为 superseded，并写入 .suppressed.json 与 suppressed_prompt.md。"""
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

        # 按 (entity, kind) 去重写入 .suppressed.json
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

        # 生成 suppressed_prompt.md（供贴进 Agent 系统提示 / SOUL）
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
# 否认降权 / 到期记忆 / 主动回忆 / 承诺追踪
# ═══════════════════════════════════════════════════════════════

def cmd_deny(entity_or_id, refs_dir, reason=None):
    """否认降权：用户否认/纠正某条记忆时将其 importance 乘以 DENY_FACTOR（0.1）并累加 deny_count；
    累计达到 DENY_TO_PENDING_AFTER（2）次时自动转为 pending 退出检索。"""
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
    """到期记忆扫描：已到期的记忆标记为 expired（移出检索，保留数据）；
    未来 EXPIRY_REMIND_DAYS 天内到期的记忆列入 remind 提醒列表。"""
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


def cmd_recall(refs_dir, limit=3, recent_hours=None, tags=None, tag_mode="any"):
    """主动回忆触发器：从记忆库中挑出此刻值得想起的记忆。

    双通道机制：
      · 温热通道：核心记忆、近期入库（< 30 天）或近期被想起过（< 7 天）的记忆，按有效权重 + 情绪标签偏好排序。
      · 旧事重提通道：入库满 30 天且距上次被想起满 7 天的非核心记忆，按沉默天数（距上次提及天数）主导排序，
        辅以从未提及、临期与初始置信度加成。
    去重抑制与定向召回：
      · 常规召回下，recent_hours（默认 24h）内被 recall 过的记忆暂不入池，召回后刷新 last_recalled（不改 importance 与 last_mentioned）。
      · 传入 tags 定向召回时，自动关闭 24h 抑制且不刷新 last_recalled，双通道合并按分排序输出。
    """
    mem_path = os.path.join(refs_dir, "memory.json")
    memory = read_json_typed(mem_path, list, refs_dir, "memory")
    now = datetime.now(TZ)
    COLD_DAYS = RECALL_COLD_AGE_DAYS
    if recent_hours is None:
        recent_hours = RECALL_RECENCY_HOURS
    # 定向标签过滤（any=命中任一，all=须全部命中）
    _tag_filter = None
    if tags:
        _tag_filter = {str(t).strip() for t in tags if str(t).strip()} or None
    _tag_all = str(tag_mode or "any").lower() == "all"

    warm = []
    cold = []
    for e in memory:
        if not isinstance(e, dict):
            continue
        if _norm_status(e) != "active":
            continue
        e_tags = {str(t).strip() for t in (e.get("emotion_tags") or []) if str(t).strip()}
        if _tag_filter is not None:
            if _tag_all:
                if not _tag_filter.issubset(e_tags):
                    continue
            elif not (_tag_filter & e_tags):
                continue
        imp = effective_importance(e, now)
        never = not e.get("last_recalled")
        tags = e.get("emotion_tags") or []
        emo_boost = recall_emotion_boost(tags)
        has_sweet = bool(set(tags) & SWEET_TAGS)
        has_care = bool(set(tags) & CARE_TAGS)
        expiring = False
        raw = e.get("expires_at")
        if raw and isinstance(raw, str):
            try:
                if 0 < (ts_parse(raw) - now).total_seconds() / 86400.0 <= EXPIRY_REMIND_DAYS:
                    expiring = True
            except (ValueError, TypeError):
                pass
        # 计算入库天数（age_days）与距上次被想起天数（recall_gap_days）
        age_days = 0
        created = e.get("created")
        if created and isinstance(created, str):
            try:
                age_days = max(0.0, (now - ts_parse(created)).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                age_days = 0
        recall_gap_days = age_days
        _lr = e.get("last_recalled")
        if _lr and isinstance(_lr, str):
            try:
                recall_gap_days = max(0.0, (now - ts_parse(_lr)).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                recall_gap_days = age_days
        # 计算沉默天数（距上次被 search 真提及的天数；仅 recall 想起不更新 last_mentioned）
        silent_days = recall_gap_days
        _lm = e.get("last_mentioned")
        if _lm and isinstance(_lm, str):
            try:
                silent_days = max(0.0, (now - ts_parse(_lm)).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                silent_days = recall_gap_days
        # 24h 近期召回抑制（定向标签召回时不启用抑制）
        recent_h = None
        lr = e.get("last_recalled")
        if _tag_filter is None and lr and isinstance(lr, str):
            try:
                recent_h = (now - ts_parse(lr)).total_seconds() / 3600.0
            except (ValueError, TypeError):
                recent_h = None
        if (recent_h is not None) and (recent_h < recent_hours):
            continue
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
            "age_days": round(age_days, 1),
            "recall_gap_days": round(recall_gap_days, 1),
            "_last_recalled_short": _short_ts(e.get("last_recalled")),
        }
        # 使用简短时间格式并移除空值字段以紧凑输出
        item.pop("last_recalled", None)
        for _k in ("expires_at", "context"):
            if item.get(_k) is None:
                item.pop(_k, None)
        # 通道分流：核心记忆、入库未满 30 天或距上次 recall 未满 7 天走温热通道，其余走旧事重提通道
        if _is_core(e) or age_days < COLD_DAYS or recall_gap_days < RECALL_GAP_DAYS:
            item["score"] = round(imp + emo_boost, 4)
            item["recall_reason"] = "常聊的，正在心头"
            warm.append(item)
        else:
            # 旧事重提通道评分：以沉默天数为主键，附加从未提及、临期与置信度权重
            score = min(silent_days / 30.0, 10.0) \
                + (RECALL_BONUS_NEVER if never else 0) \
                + (RECALL_BONUS_EXPIRING if expiring else 0) \
                + (0.25 * max(0.0, min(1.0, _safe_float(e.get("confidence", 1.0), 1.0))))
            item["score"] = round(score, 4)
            item["silent_days"] = round(silent_days, 1)
            if never:
                item["recall_reason"] = "从没跟你提起过的旧事"
            elif expiring:
                item["recall_reason"] = "快到期了，最后想起一次"
            elif has_sweet:
                item["recall_reason"] = "想起来都带着甜"
            elif has_care:
                item["recall_reason"] = "想起来该多关心一下"
            else:
                item["recall_reason"] = "很久没提起的旧事"
            cold.append(item)

    warm.sort(key=lambda x: -x["score"])
    cold.sort(key=lambda x: -x["score"])
    if _tag_filter is not None:
        # 定向标签召回：合并温热与旧事通道按得分排序
        results = sorted(warm + cold, key=lambda x: -x["score"])[:limit]
    else:
        # 常规混合输出：温热通道至多占 1 条，其余名额优先分配给旧事重提通道，不足时由温热通道补齐
        results = []
        if warm:
            results.append(warm[0])
        results += cold[: max(0, limit - len(results))]
        if len(results) < limit:
            results += warm[1: 1 + (limit - len(results))]

    # 常规召回完成后刷新被召回条目的 last_recalled（不修改 importance 与 last_mentioned；定向标签召回不刷新）
    recalled_ids = {r.get("id") for r in results if r.get("id")}
    if recalled_ids and _tag_filter is None:
        changed = False
        for e in memory:
            if isinstance(e, dict) and e.get("id") in recalled_ids:
                e["last_recalled"] = ts_now()
                changed = True
        if changed:
            safe_write_json(mem_path, memory)

    print(json.dumps({
        "status": "recalled",
        "count": len(results),
        "recency_hours": recent_hours,
        "tags": sorted(_tag_filter) if _tag_filter else [],
        "tag_mode": "all" if _tag_all else "any",
        "channel": {"warm": len(warm), "old_treasure": len(cold)},
        "results": results,
    }, ensure_ascii=False, indent=2))


# ---- 自我进化（自省复盘）----
# 机制：按四档优先级（教训/负面 > 里程碑/身份 > 偏好/情绪 > 日常事实）交织轮转并提供批内配额，
# 自动维护分页游标（.evolve_cursor.json），单批默认 50 条（上限 300 条，带 6 万字符上下文守卫）。
# 产物由 LLM 经 memory_evolve_check 校验后写入宿主常驻记忆文件，本函数只读不修改 memory.json。
SELF_EVOLVE_DEFAULT_LIMIT = 50
SELF_EVOLVE_DEFAULT_DAYS = 365
SELF_EVOLVE_CHAR_BUDGET = 60000
SELF_EVOLVE_MAX_LIMIT = 300


def cmd_self_evolve(refs_dir, limit=SELF_EVOLVE_DEFAULT_LIMIT, focus=None,
                    days=SELF_EVOLVE_DEFAULT_DAYS, cursor=None):
    """自省复盘：按四档配额与自动游标轮批提取 active 记忆供 LLM 复盘（只读，不修改 memory.json）。

    focus: 可选定向标签集合（仅提取含指定标签的记忆）。
    days: 提取最近 N 天内的记忆（默认 365）。
    limit: 单批提取上限（默认 50，上限 300，受 SELF_EVOLVE_CHAR_BUDGET 字符预算守卫约束）。
    cursor: 分页游标；为 None 时自动读取并推进 .evolve_cursor.json，传整数时按指定位置读取。
    """
    mem_path = os.path.join(refs_dir, "memory.json")
    memory = read_json_typed(mem_path, list, refs_dir, "memory")
    now = datetime.now(TZ)
    # 显式 None 判断，不用 `or`：limit=0/days=0 是合法输入（0 天 = 只看今天的），
    # `or` 会把 0 当成"没传"而悄悄兜底成默认值——调用方以为要 0，实际拿到 300。
    limit = SELF_EVOLVE_DEFAULT_LIMIT if limit is None else int(limit)
    limit = max(1, min(SELF_EVOLVE_MAX_LIMIT, limit))
    days = SELF_EVOLVE_DEFAULT_DAYS if days is None else int(days)
    days = max(0, days)
    _focus = {str(t).strip() for t in (focus or []) if str(t).strip()} or None

    pool = []
    for e in memory:
        if not isinstance(e, dict) or _norm_status(e) != "active":
            continue
        e_tags = {str(t).strip() for t in (e.get("emotion_tags") or []) if str(t).strip()}
        if _focus and not (_focus & e_tags):
            continue
        created = e.get("created")
        if not created or not isinstance(created, str):
            continue
        try:
            age = (now - ts_parse(created)).total_seconds() / 86400.0
        except (ValueError, TypeError):
            continue
        if age > days:
            continue
        silent = age
        lm = e.get("last_mentioned")
        if lm and isinstance(lm, str):
            try:
                silent = max(0.0, (now - ts_parse(lm)).total_seconds() / 86400.0)
            except (ValueError, TypeError):
                silent = age
        pool.append({
            "id": e.get("id"),
            "entity": e.get("entity"),
            "kind": e.get("kind"),
            "value": _safe_str(e.get("value")),
            "context": e.get("context"),
            "emotion_tags": e.get("emotion_tags"),
            "importance": round(effective_importance(e, now), 4),
            "core": bool(_is_core(e)),
            "age_days": round(age, 1),
            "silent_days": round(silent, 1),
            "recall_reason": None,
        })

    # 排序：**全量进池，不筛标签**（人复盘不可能只想坏事——
    # 让用户开心的事、惊喜的事、偏好，全是「我是谁/该怎么对 TA」的原料）。
    # 只分「优先级」不分「资格」：
    #   P0 教训/负面情绪（做错了要改）
    #   P1 里程碑/身份/关系（用户是谁、我们在哪）
    #   P2 偏好/习惯（用户喜欢什么，可直接复用）
    #   P3 其余日常事实
    # 同档内沉默越久越优先（久没人提 = 最可能被忽略的）。
    _LESSON_TAGS = {"教训", "委屈", "失望", "心疼", "生气", "内疚", "愧疚", "被纠正", "担心", "疲惫"}
    _IDENTITY_TAGS = {"里程碑", "新突破", "身份", "重要", "核心偏好", "爱意", "被信任", "认真"}
    _PREF_TAGS = {"甜蜜", "温暖", "想念", "撒娇", "激动", "关心", "害羞", "期待", "心情", "健康"}
    def _prio(it):
        tags = set(it.get("emotion_tags") or [])
        if tags & _LESSON_TAGS:
            return 0
        if tags & _IDENTITY_TAGS:
            return 1
        if tags & _PREF_TAGS:
            return 2
        return 3
    def _rv_key(it):
        return (_prio(it), -it.get("silent_days", 0.0))
    _prio_label = {
        0: "教训/负面情绪——做错了要改",
        1: "里程碑/身份/关系——用户是谁、我们在哪",
        2: "偏好/情绪——用户喜欢什么，可直接复用",
        3: "日常事实",
    }
    for it in pool:
        it["recall_reason"] = _prio_label[_prio(it)]

    # 四档分桶交织与批内按占比配额，确保每批四档均衡覆盖且不遗漏大基数日常档
    _buckets = {0: [], 1: [], 2: [], 3: []}
    for it in pool:
        _buckets[_prio(it)].append(it)
    for _k in _buckets:
        _buckets[_k].sort(key=lambda x: -x.get("silent_days", 0.0))
    _share = {_k: len(v) / max(1, len(pool)) for _k, v in _buckets.items()}
    _rotor = []
    _idx = {_k: 0 for _k in _buckets}
    _total = sum(len(v) for v in _buckets.values())
    while len(_rotor) < _total:
        for _k in (0, 1, 2, 3):
            if _idx[_k] < len(_buckets[_k]):
                _rotor.append(_buckets[_k][_idx[_k]])
                _idx[_k] += 1
    pool = _rotor

    _pool_size = len(pool)
    # 自动游标轮批：未显式传 cursor 时从 .evolve_cursor.json 读取并自动推进，遍历完一轮后归零
    _cursor_path = os.path.join(refs_dir, ".evolve_cursor.json")
    _auto_cursor = cursor is None
    if _auto_cursor:
        try:
            cursor = int(json.loads(open(_cursor_path, encoding="utf-8").read()).get("cursor", 0))
        except Exception:
            cursor = 0
    cursor = max(0, int(cursor or 0))
    if cursor > max(0, len(pool) - 1):
        cursor = 0
    _batch = pool[cursor:cursor + limit]
    # 批内按四档占比分配名额，余数按占比从高到低依次补齐至 limit
    _bb = {0: [], 1: [], 2: [], 3: []}
    for it in _batch:
        _bb[_prio(it)].append(it)
    _quota = {}
    for _k, _items in _bb.items():
        _quota[_k] = min(len(_items), int(round(len(_batch) * _share[_k])))
    _gap = len(_batch) - sum(_quota.values())
    if _gap > 0:
        for _k in sorted(_bb, key=lambda k: -_share[k]):
            if _gap <= 0:
                break
            take = min(_gap, len(_bb[_k]) - _quota[_k])
            if take > 0:
                _quota[_k] += take
                _gap -= take
    _batch = []
    for _k in (0, 1, 2, 3):
        _batch.extend(_bb[_k][:_quota[_k]])
    _next_cursor = cursor + len(_batch)
    # 走完一圈：记住下一轮从 0 开始
    if _next_cursor >= len(pool):
        _next_cursor = 0
    if _auto_cursor:
        try:
            with open(_cursor_path + ".tmp", "w", encoding="utf-8") as fh:
                json.dump({"cursor": _next_cursor, "updated": ts_now()}, fh, ensure_ascii=False)
            os.replace(_cursor_path + ".tmp", _cursor_path)
        except Exception:
            pass
    pool = _batch

    # === 守卫 === 超预算时从最旧的往最新涂内容，最后强制「凭已知道的下结论」。
    budget = SELF_EVOLVE_CHAR_BUDGET
    used = 0
    compacted = False
    for it in pool:
        vlen = len(it.get("value") or "") + len(it.get("context") or "")
        if used + vlen > budget:
            it["value"] = "（已查过，内容已压缩——请基于此前已知信息下结论）"
            it["context"] = None
            compacted = True
        else:
            used += vlen

    print(json.dumps({
        "status": "evolve_ready",
        "count": len(pool),
        "pool_size": _pool_size,
        "batch": {"cursor": cursor, "limit": limit, "next_cursor": _next_cursor,
                  "round_done": _next_cursor == 0},
        "focus": sorted(_focus) if _focus else [],
        "days": days,
        "char_budget": budget,
        "chars_used": used,
        "compacted": compacted,
        "how_to_use": [
            "这些是最近值得复盘的全量记忆（含当时的气氛+情绪标签+沉默天数），不是主题摘要。",
            "★原料是全谱的★ 默认覆盖全库（days=365），让用户开心的事、惊喜、用户的偏好、",
            "身份/里程碑、日常事实，和教训一样都是「我是谁、我该怎么对 TA」的素材——",
            "人复盘不可能只想坏事。",
            "★自动轮批★ 工具自己记游标：每次调用自动换下一批，走完一圈回到开头。",
            "所以连续复盘时直接再调一次就行，不用你记批次；想回看某一批才手动传 cursor。",
            "请回答三个问题：①我曾经在哪里做得不好（引记忆事实，不许编）；",
            "②现在有没有更好，没有的话卡在哪；③未来具体怎么做，写成可执行的行为规则。",
            "",
            "★产物写哪里（定，不许写 trigger）★",
            "结论必须写进 ~/.agent/memories/MEMORY.md 的「十、自我进化」分区，",
            "由你自己推理后自己写——memory.md 是每轮都注入给 LLM 的文件，",
            "trigger(memory.json) 必须主动搜才读得到，铁律不能靠「想起来去搜」。",
            "写之前先读 MEMORY.md 现有内容，同主题的更新原条目而不是新起一条，",
            "保持「■ 小标题（日期）：正文」的格式，日期用今天。",
            "",
            "★0 条是合法产出★ 但「没产出」和「偷懒」要能分开：写不出时",
            "必须回一句「这批没新东西」+ 为什么（原料重复/无规律/证据不足），",
            "然后停。说不出为什么的沉默 = 白跑一轮；说得出理由的 0 条 = 诚实复盘。",
            "",
            "★★★ 要精准，不要数量 ★★★",
            "不是数量要上去，不是写越多越好，而是要写精准——",
            "一条合格的进化结论必须同时满足：",
            "  ① 是**规律/洞察/可执行规则**，不是对某条记忆的复述——",
            "     从原料里照搬一条写进 MEMORY.md = 充数，是最坏的结果。",
            "  ② 回答了这四个问题里的至少一个：这批记忆里有没有**规律**？",
            "     有没有**值得写下来的**？对我们的**未来有没有帮助**？",
            "     有没有**我现在没做、以后可以做得更好的**？",
            "  ③ 能说出它是从哪几条原料里看出来的（证据），不是凭空感慨。",
            "四个问题一个都答不上来 → 这批原料没新东西，老实说，然后停。",
            "★宁愿只写 1 条精准的，也不写 5 条充数的——写不出就是 0 条，0 条同样合格。★",
            "★写之前先读 MEMORY.md 现有内容——已经有的规律不要重复写，",
            "  发现旧结论被新证据推翻，就更新原条目并注明依据。",
            "",
            "写 MEMORY.md 的铁律：禁带命令行原文和明文密钥，",
            "密钥只写存放路径；运维细节优先宿主运维文档，MEMORY.md 只放高频铁律。",
            "",
            "★★★ 提交前必须过举证闸门 memory_evolve_check（程序会算，别想蒙）★★★",
            "每条候选结论必须带齐：claim + source_ids(≥2条真实id) + key_diff(关键差异)",
            " + trigger(触发条件) + behavior_change(行为改变) + falsifier(失效条件)"
            " + existing_ref(存量对照：与哪条现有条目相关，或写「无同类」)。",
            "闸门会判：整句/截断/短片段的原文搬运（连续≥12字一样就死）、与现有条目重复",
            "（>0.75 须 update 不许 add）、三字段缺失（=装饰品）、假 id（=编坐标）。",
            "verdict: pass=写正文 / weak=2条支撑标待加固 / watch=1条支撑只进待观察子区 / reject=不许写。",
            "一次最多 2 条；写不出 0 条合法，硬凑不是。",
            "把候选结论逐条交给 memory_evolve_check 校验，它逐条验：",
            "① 举证：source_ids 必须 ≥2 条（单条只能进待观察区），且必须写出",
            "   这几条之间的「关键差异」——照搬的结论写不出差异，会被拒；",
            "② 反冗余：逐字重合度 ≥0.85 判为复述原文，整条作废；",
            "③ 反重复：与 MEMORY.md「十、自我进化」现有条目 >0.75 相似 → 判重复，",
            "   要求你改成 update 而不是 add；",
            "④ 三字段：trigger / behavior_change / falsifier 三个填不满 → 判装饰品，拒收；",
            "⑤ 每轮上限 2 条，写不出 0 条是合法的，硬凑不是。",
        ],
        "results": pool,
    }, ensure_ascii=False, indent=2))


# ---- 自我进化举证闸门 ----
# 通过可计算判据校验候选进化结论（来源存在性、存量对照、反照搬双保险、反冗余、可证伪三字段、支撑度分级）。
EVOLVE_MAX_PER_ROUND = 2        # 一次进化最多 2 条，写不出 0 条合法
EVOLVE_MIN_SOURCES = 2          # 少于 2 条支撑 → 只能进待观察区
EVOLVE_VERBATIM_THRESHOLD = 0.85   # 与单条原料逐字重合度超过它 → 判照搬，作废
EVOLVE_DUP_THRESHOLD = 0.75     # 与自我进化区现有条目相似度超过它 → 判重复，须 update
EVOLVE_WEAK_SOURCES = 2         # 恰好 2 条支撑 → 中，标记待加固


def _short_ts(v):
    """完整 ISO 时间戳 → 「MM-DD HH:mm」，砍掉秒/微秒/时区后缀。

    Presentation 瘦身用：25 字符的时间戳里，
    秒和时区对「这条多久没被提起」这个判断没有任何贡献。
    """
    v = str(v or "")
    if len(v) >= 16:
        return v[5:16].replace("T", " ")
    return v or None


def _bigrams(s):
    """字符二元组集合（中文按字切，中英混排也适用）。"""
    s = re.sub(r"\s+", "", str(s or ""))
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def cmd_evolve_check(refs_dir, entries, memory_md_path=None):
    """举证闸门：校验候选进化结论，返回逐条 verdict。

    entries: list[dict]，每条必须带：
      claim        : 结论正文（要写进 MEMORY.md 的那句话）
      source_ids   : list[str] 支撑这条结论的记忆 id（来自 memory_evolve 的原料）
      key_diff     : str       这几条原料之间的「关键差异」——照搬写不出来
      existing_ref : str       写之前先对照宿主记忆文件：说明与现有哪条相关（或明确写「无同类」）——
                                相关时只许 update 原条目、不许 add，这一条直接掐死「换个说法再写一遍」。
      trigger      : str       触发条件：当 ______ 出现时
      behavior_change: str     行为改变：我不再 ______ / 我会先 ______
      falsifier    : str       失效条件：如果 ______ 出现，说明这条是错的，应删除
      action       : "add" | "update" | "watch"（默认 add；watch=只进待观察区）
      target_line  : str       action=update 时，被更新的 MEMORY.md 条目原文/行号

    判定（按顺序，先撞死哪条报哪条）：
      reject  → 这条不许写，附 reason
      watch  → 降级进待观察区（只有 1 条支撑时的唯一去向）
      weak   → 可写但标「待加固」（恰好 2 条支撑）
      pass   → 可写
    """
    if not isinstance(entries, list) or not entries:
        print(json.dumps({"ok": False, "error": "entries 必须是非空 list"}, ensure_ascii=False))
        return
    if len(entries) > EVOLVE_MAX_PER_ROUND:
        print(json.dumps({
            "ok": False,
            "error": f"一次进化最多 {EVOLVE_MAX_PER_ROUND} 条（用户定的硬上限）。收到 {len(entries)} 条。"
                     f"写不出可以只交 0 条——0 条合法，硬凑不是。",
        }, ensure_ascii=False))
        return

    mem_path = os.path.join(refs_dir, "memory.json")
    memory = read_json_typed(mem_path, list, refs_dir, "memory")
    by_id = {e.get("id"): e for e in memory if isinstance(e, dict) and e.get("id")}

    # MEMORY.md 自我进化区现有条目（反重复用）
    _md = memory_md_path or os.path.expanduser("~/.agent/memories/MEMORY.md")
    _existing = []
    try:
        _lines = open(_md, encoding="utf-8").read().split("\n")
        _in_sec = False
        for ln in _lines:
            if ln.startswith("十、自我进化"):
                _in_sec = True
                continue
            if _in_sec and ln.startswith("━━"):
                if _existing:
                    break
                continue
            if _in_sec and ln.startswith("■"):
                _existing.append(ln)
    except Exception:
        _md = None

    verdicts = []
    for i, ent in enumerate(entries):
        if not isinstance(ent, dict):
            verdicts.append({"index": i, "verdict": "reject", "reason": "条目不是对象"})
            continue
        claim = _safe_str(ent.get("claim") or "")
        sids = ent.get("source_ids") or []
        sids = [str(s) for s in sids if str(s)]
        verdict, reason, notes = "pass", "", []

        # ① 举证：≥2 条真实存在的来源 id
        if not sids:
            verdict, reason = "reject", "没给 source_ids——不举证不许写。规律陈述必须能指回哪几条记忆。"
        elif len(sids) == 1:
            verdict, reason = "watch", "只有 1 条支撑：不够格进正文，只能进「待观察」区，补足支撑再升级。"
        elif len([s for s in sids if s in by_id]) < len(sids):
            missing = [s for s in sids if s not in by_id]
            verdict, reason = "reject", f"source_ids 里有 {len(missing)} 个 id 在记忆库不存在：{missing[:3]}——不许编造坐标。"

        # ①bis 存量对照（v3.7）：「换个说法再写一遍」是自我进化最大的注水方式——
        # 写之前必须先读宿主记忆文件的自我进化区：要么指出与哪条现有条目相关
        # （相关 → 只许 update），要么明确声明「无同类」。这一条直接掐死重复新增。
        if verdict != "reject":
            _eref = _safe_str(ent.get("existing_ref") or "").strip()
            if not _eref:
                verdict, reason = "reject", ("没给 existing_ref——写之前必须先读宿主记忆文件的自我进化区做存量对照："
                                             "要么写出「与第 K 条相关」，要么明确声明「无同类」。")
            elif ("无同类" not in _eref) and ("none" not in _eref.lower()):
                if str(ent.get("action") or "add").lower() != "update":
                    verdict, reason = "reject", (f"existing_ref 已指明与现有条目相关（{_eref[:60]}）却用 add——"
                                                 f"相关条目只许 update，不许新增。")

        # ② 反照搬：两道判据，截断式搬运（抄前半句）也能被第二道抓住
        if verdict != "reject":
            worst, worst_id = 0.0, None
            longest_run, run_id = 0, None
            _claim_sq = re.sub(r"\s+", "", claim)
            for sid in sids:
                src = by_id.get(sid)
                if not src:
                    continue
                sim = _jaccard(_bigrams(claim), _bigrams(src.get("value")))
                if sim > worst:
                    worst, worst_id = sim, sid
                # 第二道：原文里最长的连续片段（≥12 字）是否原样出现在 claim 里。
                # jaccard 会被「claim 比原文短」稀释——抄前半句 60% 只有 0.67，
                # 但「原样搬运一段连续文字」这件事本身就该判死。
                _src_sq = re.sub(r"\s+", "", str(src.get("value") or ""))
                for _ln in (12, 16, 20, 30):
                    if len(_src_sq) < _ln:
                        continue
                    for _st in range(0, min(len(_src_sq) - _ln + 1, 200)):
                        if _src_sq[_st:_st + _ln] in _claim_sq:
                            if _ln > longest_run:
                                longest_run, run_id = _ln, sid
                            break
            notes.append(f"最高原文重合度 {worst:.2f}（vs {worst_id}）")
            if longest_run >= 12:
                notes.append(f"检出 {longest_run} 字连续原文搬运（vs {run_id}）")
                verdict, reason = "reject", (f"与来源 {run_id} 有 {longest_run} 字连续原文一模一样——"
                                             f"这是搬运不是提炼，即使整体重合度只有 {worst:.2f}。改成自己的话。")
            elif worst >= EVOLVE_VERBATIM_THRESHOLD:
                verdict, reason = "reject", (f"与来源 {worst_id} 重合度 {worst:.2f} ≥ {EVOLVE_VERBATIM_THRESHOLD}——"
                                             f"这是照搬原文，不是提炼。改写或放弃。")
            elif not _safe_str(ent.get("key_diff") or "").strip():
                verdict, reason = "reject", ("没写 key_diff——必须说出这几条原料之间的关键差异。"
                                             "照搬的结论写不出差异；找不出差别就说明你只是在搬运。")

        # ③ 反冗余：与自我进化区现有条目比
        if verdict not in ("reject",) and _existing:
            best, best_ln = 0.0, None
            for ln in _existing:
                sim = _jaccard(_bigrams(claim), _bigrams(ln))
                if sim > best:
                    best, best_ln = sim, ln
            if best >= EVOLVE_DUP_THRESHOLD:
                action = str(ent.get("action") or "add").lower()
                if action != "update":
                    verdict, reason = "reject", (f"与自我进化区现有条目相似度 {best:.2f} ≥ {EVOLVE_DUP_THRESHOLD}——"
                                                 f"memory.md 里已经在说这件事（「{best_ln[:60]}…」）。"
                                                 f"禁止 add，只能 update 原条目（把 action 改成 update 并给 target_line）。")

        # ④ 三字段：可证伪性
        if verdict in ("pass", "weak"):
            missing_fields = [k for k in ("trigger", "behavior_change", "falsifier")
                              if not _safe_str(ent.get(k) or "").strip()]
            if missing_fields:
                verdict, reason = "reject", (f"缺字段 {missing_fields}——触发条件/行为改变/失效条件三个填不满，"
                                             f"这条就是装饰品。连怎么被证伪都说不出的记忆是不可检验的废话。")

        # ⑤ 支撑度分级
        if verdict == "pass":
            if len(sids) == EVOLVE_WEAK_SOURCES:
                verdict, notes = "weak", notes + [f"恰好 {EVOLVE_WEAK_SOURCES} 条支撑：标「待加固」，下次复盘优先补证据。"]

        verdicts.append({
            "index": i, "verdict": verdict, "reason": reason,
            "claim": claim[:120], "source_ids": sids, "notes": notes,
        })

    counts = {}
    for v in verdicts:
        counts[v["verdict"]] = counts.get(v["verdict"], 0) + 1
    print(json.dumps({
        "ok": True,
        "verdicts": verdicts,
        "summary": counts,
        "memory_md": _md,
        "next_steps": [
            "pass → 写进 MEMORY.md「十、自我进化」正文区",
            "weak → 写进正文，但标题带「待加固」",
            "watch → 写进 MEMORY.md「十、自我进化」的待观察子区（WATCHLIST-START/END 之间）",
            "reject → 不许写；按 reason 改写后重新提交，或承认这条推不出来",
        ],
    }, ensure_ascii=False, indent=2))


# ---- 引用重写检测（悬空与过期引用扫描）----
# 扫描宿主记忆文件（如 MEMORY.md）及可选的记忆库正文中引用的记忆坐标（mem_xxx / uuid），
# 检测是否存在已删除/已归档（dangling）或已作废（stale，status=superseded/expired）的失效引用。
def _memory_ids_in_text(text):
    """从文本里抠出所有记忆坐标：mem_ 开头的 id + uuid 坐标。"""
    out = set()
    for m in re.finditer(r"mem_[A-Za-z0-9_]{6,}", str(text or "")):
        out.add(m.group(0))
    for m in re.finditer(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                         str(text or "")):
        out.add(m.group(0))
    return out


def cmd_retraction_scan(refs_dir, memory_md_path=None, include_memory_text=False):
    """扫描引用悬空：MEMORY.md（默认）里引用了已不存在/已作废记忆坐标的句子。

    返回两类：
      dangling  —— 引用的 id 在库里彻底不存在（被删/被 vacuum 归档）
      stale     —— 引用的 id 还在，但已 superseded（内容被更新过，旧引用可能说旧版）

    每条附 citing_line（引用它的原文），方便直接定位改写。
    include_memory_text=True 时连 memory.json 内部互相引用也扫（更全但更慢）。
    """
    mem_path = os.path.join(refs_dir, "memory.json")
    memory = read_json_typed(mem_path, list, refs_dir, "memory")
    live = {}
    for e in memory:
        if isinstance(e, dict) and e.get("id"):
            live[e["id"]] = e
    _md = memory_md_path or os.path.expanduser("~/.agent/memories/MEMORY.md")

    def _classify(cid):
        ent = live.get(cid)
        if ent is None:
            return "dangling", None
        if ent.get("status") == "superseded":
            return "stale", ent
        if ent.get("status") == "expired":
            return "stale", ent
        return "live", ent

    findings = []
    md_readable = True
    try:
        md_lines = open(_md, encoding="utf-8").read().split("\n")
    except Exception:
        md_lines = []
        md_readable = False
    for ln_no, ln in enumerate(md_lines, 1):
        if not ln.strip():
            continue
        for cid in _memory_ids_in_text(ln):
            kind, ent = _classify(cid)
            if kind == "live":
                continue
            findings.append({
                "where": "MEMORY.md",
                "line": ln_no,
                "citing": ln.strip()[:160],
                "ref_id": cid,
                "kind": kind,
                "current_value": (str(ent.get("value"))[:100] if ent else None),
                "current_status": (ent.get("status") if ent else "gone"),
            })

    # 可选：memory.json 内部互相引用
    if include_memory_text:
        for e in memory:
            if not isinstance(e, dict):
                continue
            for cid in _memory_ids_in_text(e.get("value")):
                if cid == e.get("id"):
                    continue
                kind, ent = _classify(cid)
                if kind == "live":
                    continue
                findings.append({
                    "where": "memory.json",
                    "line": None,
                    "citing": f"{e.get('id')} → {str(e.get('value'))[:100]}",
                    "ref_id": cid,
                    "kind": kind,
                    "current_value": (str(ent.get("value"))[:100] if ent else None),
                    "current_status": (ent.get("status") if ent else "gone"),
                })

    _d = [f for f in findings if f["kind"] == "dangling"]
    _s = [f for f in findings if f["kind"] == "stale"]
    print(json.dumps({
        "ok": True,
        "memory_md": _md,
        "memory_md_readable": md_readable,
        "scanned": {"memory_md_lines": len(md_lines),
                    "memory_entries": len(memory) if include_memory_text else None},
        "dangling_count": len(_d),
        "stale_count": len(_s),
        "dangling": _d[:50],
        "stale": _s[:50],
        "what_to_do": [
            "dangling：MEMORY.md 这句话引用了一个已经不存在的记忆——删掉这句引用，",
            "或改成现在仍然成立的说法（先 memory_search 确认现在的事实是什么）。",
            "stale：引用的记忆还在但已被 superseded（内容更新过）——这句话可能在说旧版，",
            "读一眼 current_value，是旧版就改写，仍然成立就留着。",
            "★为什么重要★ MEMORY.md 每轮都注入。一个没有支撑的引用 = 每轮都在重复一个",
            "已经没有依据的事实。删除不产生信号，所以只有扫才发现得了。",
        ],
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
    """承诺建档：将 Agent 对用户的承诺写入 promises.json（可选附带 deadline 供闹钟与逾期检查使用）。"""
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
    """承诺完成：将指定 promise_id 的状态从 open 更新为 done 并记录 completed_at。"""
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
    """承诺清单：列出全部承诺，未完成（open，标注是否逾期）排前，最近已完成（done）排后。"""
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
        "done": done_items[-20:],
    }, ensure_ascii=False, indent=2))


def cmd_promise_check(refs_dir):
    """未完成承诺检查：返回全部 open 状态的承诺，按已逾期、临期、创建时长排序。"""
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
#              各 cmd_* 会自动附加 promise_reminders 字段），宿主每次唤醒 Agent 都能撞见。
# 推送渠道：Bark（BARK_KEY / MEMORY_TRIGGER_BARK_KEY 环境变量）/ 自定义 webhook
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
        # 筛选已逾期（deadline < now）或处于临期窗口内（距今天 ≤ PROMISE_REMIND_DAYS 天）的未完成承诺
        overdue = d < now
        due_soon = (not overdue) and ((d - today0).days <= PROMISE_REMIND_DAYS)
        if not (overdue or due_soon):
            continue
        due.append({
            "id": p.get("id"),
            "text": text,
            "deadline": deadline,
            "overdue": overdue,
            "days_overdue": round((now - d).total_seconds() / 86400.0, 1) if overdue else 0,
        })
    # 逾期排最前，逾期越久越靠前；其次临期
    due.sort(key=lambda x: (not x["overdue"], -x["days_overdue"]))
    return due


def _send_bark(title, body):
    """Bark 原生推送：key 只取环境变量 BARK_KEY / MEMORY_TRIGGER_BARK_KEY。返回 bool。"""
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
            # 系统 Python 缺少 CA 证书导致 SSL 校验失败时，降级使用未校验上下文发送通知（仍有本地文件落盘兜底）。
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
    不推送、不标记已通知——只让 Agent/宿主每次唤醒都看得见。"""
    items = _promise_due_items(refs_dir)
    if not items:
        return {}
    return {"promise_reminders": items}


# 会话内维护到期提示注入：当距上次全套维护（.last_maintenance 的 mtime）超过 7 天时，
# 在工具返回中附加 maintenance_due 字段，引导 Agent 主动执行维护五件套并调用 memory_touch_maintenance 登记。
_MAINT_DUE_FILE = ".last_maintenance"
_MAINT_INTERVAL_DAYS = 7


def _maintenance_due_field(refs_dir):
    """会话内注入用：距上次全套维护超过 7 天时返回 {maintenance_due: {...}}。"""
    try:
        marker = os.path.join(refs_dir, _MAINT_DUE_FILE)
        if os.path.exists(marker):
            age_days = (time.time() - os.path.getmtime(marker)) / 86400.0
        else:
            age_days = float("inf")
        if age_days < _MAINT_INTERVAL_DAYS:
            return {}
        return {
            "maintenance_due": {
                "days_since_last": None if age_days == float("inf") else round(age_days, 1),
                "steps": [
                    "memory_decay", "memory_vacuum", "memory_stats",
                    "memory_selfcheck", "memory_expire_check",
                ],
                "note": "距上次记忆维护已超 7 天——撞见此字段立即依次调用上面 5 个工具，"
                        "跑完调 memory_touch_maintenance 登记时间，不要等用户提醒。",
            }
        }
    except Exception:
        return {}


def cmd_touch_maintenance(refs_dir):
    """刷新 .last_maintenance 时间戳，登记已完成全套周期维护。"""
    marker = os.path.join(refs_dir, _MAINT_DUE_FILE)
    try:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(ts_now())
        print(json.dumps({"status": "touched", "at": ts_now()}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))


def _unsuppress_entity(refs_dir, entity):
    """当某实体被重新写入时，将其从 .suppressed.json 中移除并重新生成 suppressed_prompt.md。"""
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
             "以下记忆已从文件标 superseded，且你（Agent）自身也应放下，**不要主动提及**：",
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
    if mode != "local":
        raise ValueError(f"未知 mode: {mode!r}（仅支持 local）")
    os.makedirs(refs_dir, exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".backup"), exist_ok=True)
    os.makedirs(os.path.join(refs_dir, ".archive"), exist_ok=True)

    backend_path = os.path.join(refs_dir, "backend_config.json")
    if not os.path.exists(backend_path):
        backend_info = "纯本地文件模式"
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

    # 承诺账本数据文件（promises.md 保留为人类可读入口，结构化数据落盘 promises.json）
    proj_path = os.path.join(refs_dir, "promises.json")
    if not os.path.exists(proj_path):
        safe_write_json(proj_path, {"promises": []})

    print(json.dumps({
        "status": "initialized",
        "mode": mode,
        "refs_dir": refs_dir
    }))


def cmd_recover(refs_dir):
    """从 WAL（.wal.jsonl）重放崩溃时未提交的写入，按 (entity, memory_id) 幂等去重并在完成后截断 WAL。"""
    wal_path = os.path.join(refs_dir, ".wal.jsonl")
    if not os.path.exists(wal_path):
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "no_wal_file"}))
        return

    with open(wal_path, "r", encoding="utf-8") as f:
        lines = [l.strip() for l in f if l.strip()]

    if not lines:
        _wal_prune(refs_dir, keep=[])
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "empty_wal"}))
        return

    # 逐行解析并跳过格式损坏的记录行
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

    # 已存在于 memory.json 的 memory_id 视为已完成持久化，不重复重放
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
        _wal_prune(refs_dir, keep=[])
        print(json.dumps({"status": "ok", "recovered": 0, "reason": "all_committed"}))
        return

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
                context=e.get("context"),
                expires_at=e.get("expires_at"),
            )
            recovered.append(ent_name)
        except Exception as ex:
            sys.stderr.write(f"[memory_recover] recover failed for {ent_name!r}: {ex}\n")
            failed.append(e)

    # 重放完成后截断 WAL，仅保留重放失败的记录
    _wal_prune(refs_dir, keep=failed)

    print(json.dumps({
        "status": "recovered" if recovered else "partial",
        "recovered_count": len(recovered),
        "recovered_entities": recovered,
        "failed_count": len(failed),
        "total_uncommitted": len(uncommitted)
    }, ensure_ascii=False))


def cmd_wellness(mood, sleep_hours, sleep_quality, note, refs_dir):
    today = datetime.now(TZ).date().isoformat()
    well_path = os.path.join(refs_dir, "wellness.json")
    entry = {
        "date": today,
        "mood": _safe_str(mood),
        "recorded_at": ts_now(),
    }
    # sleep_hours 校验：空值或 "-" 略过；非数字字符串告警略过；数值则校验 [0, 24] 有限区间
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

    # 在同一文件锁内完成读取、追加、截断与写回，保证并发安全
    fd = file_lock(refs_dir)
    try:
        data = read_json_typed(well_path, dict, refs_dir, "wellness")
        if not isinstance(data.get("records"), list):
            data["records"] = []
        data["records"].append(entry)
        # 保留最近 WELLNESS_MAX_RECORDS 条记录
        if len(data["records"]) > WELLNESS_MAX_RECORDS:
            data["records"] = data["records"][-WELLNESS_MAX_RECORDS:]
        safe_write_json(well_path, data)
    finally:
        file_unlock(fd, refs_dir)
    print(json.dumps({"status": "ok", "date": today, "mood": mood}, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print("用法: write_pipeline.py <command> [args...]", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    refs_dir = os.path.dirname(os.path.abspath(__file__))

    args = list(sys.argv[2:])
    # 从后往前查找最后一个已存在的目录参数作为 refs_dir，兼容在路径后追加 --flag 的调用顺序
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
            confidence = None  # 必填参数，由 cmd_write 校验
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
            recent_hours = None
            tags = None
            tag_mode = "any"
            nm_args = []
            i = 0
            while i < len(args):
                if args[i] == "--limit" and i + 1 < len(args):
                    try:
                        limit = max(1, min(20, int(args[i + 1])))
                    except ValueError:
                        pass
                    i += 2
                elif args[i] == "--recent-hours" and i + 1 < len(args):
                    try:
                        recent_hours = max(1, int(args[i + 1]))
                    except ValueError:
                        pass
                    i += 2
                elif args[i] == "--tags" and i + 1 < len(args):
                    tags = [t.strip() for t in args[i + 1].split(",") if t.strip()] or None
                    i += 2
                elif args[i] == "--tag-mode" and i + 1 < len(args):
                    tag_mode = args[i + 1] if args[i + 1] in ("any", "all") else "any"
                    i += 2
                else:
                    nm_args.append(args[i]); i += 1
            cmd_recall(refs_dir, limit, recent_hours, tags=tags, tag_mode=tag_mode)
        elif cmd == "evolve":
            # 自我进化：按游标自动轮批提取复盘原料
            limit = SELF_EVOLVE_DEFAULT_LIMIT
            days = SELF_EVOLVE_DEFAULT_DAYS
            focus = None
            cursor = None
            i = 0
            while i < len(args):
                if args[i] == "--limit" and i + 1 < len(args):
                    try:
                        limit = max(1, min(SELF_EVOLVE_MAX_LIMIT, int(args[i + 1])))
                    except ValueError:
                        pass
                    i += 2
                elif args[i] == "--days" and i + 1 < len(args):
                    try:
                        days = max(0, int(args[i + 1]))
                    except ValueError:
                        pass
                    i += 2
                elif args[i] == "--focus" and i + 1 < len(args):
                    focus = [t.strip() for t in args[i + 1].split(",") if t.strip()] or None
                    i += 2
                elif args[i] == "--cursor" and i + 1 < len(args):
                    try:
                        cursor = max(0, int(args[i + 1]))
                    except ValueError:
                        pass
                    i += 2
                else:
                    i += 1
            cmd_self_evolve(refs_dir, limit=limit, days=days, focus=focus, cursor=cursor)
        elif cmd == "retraction-scan":
            # 引用悬空扫描：检测宿主记忆文件或记忆库内的失效引用
            _mdp = None
            _inc = False
            i = 0
            while i < len(args):
                if args[i] == "--memory-md" and i + 1 < len(args):
                    _mdp = args[i + 1]; i += 2
                elif args[i] == "--include-memory-text":
                    _inc = True; i += 1
                else:
                    i += 1
            cmd_retraction_scan(refs_dir, memory_md_path=_mdp, include_memory_text=_inc)
        elif cmd == "evolve-check":
            # 举证闸门：从 stdin 读取候选结论 JSON 数组并逐条校验
            try:
                _payload = json.loads(sys.stdin.read() or "[]")
            except Exception as e:
                print(json.dumps({"ok": False, "error": f"stdin 不是合法 JSON: {e}"}), file=sys.stderr)
                sys.exit(1)
            _entries = _payload.get("entries") if isinstance(_payload, dict) else _payload
            _mdp = _payload.get("memory_md_path") if isinstance(_payload, dict) else None
            cmd_evolve_check(refs_dir, _entries or [], memory_md_path=_mdp)
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
            # 若目标目录尚不存在（未被上方 isdir 预扫描弹出），则从 args[1] 读取目标路径
            if len(args) > 1:
                refs_dir = os.path.abspath(args[1])
            if mode != "local":
                print(json.dumps({"error": f"未知 mode: {mode!r}（仅支持 local）"}), file=sys.stderr)
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
