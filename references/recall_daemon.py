#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# recall_daemon.py — 主动回忆常驻守护进程
#
# 核心机制：
#   · 模型、锚点向量与记忆向量常驻内存，客户端钩子通过 UNIX socket 发送 {user_message, session_id}，
#     daemon 在内存内完成向量化、情绪判定与记忆召回，毫秒级返回。
#   · 若 daemon 未启动或不可达，客户端钩子自动回退到本地 recall_core.build_injection。
#   · 内存热更新（双缓冲）：检测到 memory.json 的 mtime 变化时，后台线程异步增量重算向量并原子替换，
#     当前请求继续使用现有内存向量，零阻塞。
#
# 协议（UNIX stream socket，4 字节大端长度前缀 + JSON UTF-8）：
#   请求：{"user_message": "...", "session_id": "..."}   探测：{"ping": true}
#   响应：{"context": "..."}（命中） / "{}"（未命中/冷却/出错） / {"pong": true}（探测）

import json
import os
import re
import socket
import struct
import sys
import threading
import time
import traceback
from pathlib import Path

# 本文件与 recall_vec.py / recall_core.py 同目录
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import recall_vec  # noqa: E402
import recall_core  # noqa: E402


REFS_DIR = os.environ.get("RECALL_REFS_DIR", os.path.expanduser("~/.agent/memories/memory-trigger"))
SOCK = os.environ.get("RECALL_SOCK", os.path.expanduser("~/.agent/agent-hooks/recall.sock"))
LOG = os.environ.get("RECALL_DAEMON_LOG", str(Path(SOCK).with_suffix(".sock.log")))
DAEMON_DEBUG = os.environ.get("RECALL_DAEMON_DEBUG", "1") == "1"

_vec_lock = threading.Lock()
_vectors = None          # {mem_id: np向量} 常驻内存
_vectors_mtime = None    # 对应的 memory.json mtime
_reload_running = threading.Event()   # 后台增量重算进行中（双缓冲热切换）
_shutdown = threading.Event()


def _log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write("%s [%.3f] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), time.time(), msg))
    except Exception:
        pass


def _preload():
    """启动期一次性：加载模型 + 锚点 + 记忆向量到内存。返回加载耗时。"""
    t0 = time.time()
    recall_vec.get_model()
    recall_vec.build_or_load_anchors(REFS_DIR)
    vectors = recall_vec.build_or_load_embeddings(REFS_DIR)
    global _vectors, _vectors_mtime
    _vectors = vectors
    _vectors_mtime = recall_vec.memory_mtime(REFS_DIR)
    dt = time.time() - t0
    _log("preloaded model+anchors+%d mem vectors in %.2fs" % (len(vectors), dt))
    return dt


def _reload_background(refs_dir):
    """后台线程增量重算记忆向量并热切换（双缓冲，不阻塞当前请求）。"""
    global _vectors, _vectors_mtime
    try:
        t0 = time.time()
        new_vecs = recall_vec.build_or_load_embeddings(refs_dir)  # 现在增量，通常 <1s
        with _vec_lock:
            _vectors = new_vecs
            _vectors_mtime = recall_vec.memory_mtime(refs_dir)
        _log("bg reload done: %d vectors in %.2fs (incremental)" % (len(new_vecs), time.time() - t0))
    except Exception:
        _log("bg reload error: %s" % (traceback.format_exc()[-300:]))
    finally:
        _reload_running.clear()


def _get_vectors():
    """返回常驻记忆向量；memory.json 变动则后台增量重算，当前请求返回旧向量（双缓冲）。"""
    try:
        cur = recall_vec.memory_mtime(REFS_DIR)
    except Exception:
        return _vectors  # memory.json 读不到，退而用上一份内存向量
    # 快路径：未变 → 直接返回常驻向量（无锁，零阻塞）
    if _vectors is not None and _vectors_mtime is not None and cur <= _vectors_mtime:
        return _vectors
    # 变了：若后台没在重算，启动一个；否则直接返回旧向量（永不断档）
    if not _reload_running.is_set():
        _reload_running.set()
        th = threading.Thread(target=_reload_background, args=(REFS_DIR,), daemon=True)
        th.start()
        _log("memory.json changed (mtime=%.1f) -> spawning bg incremental reload" % cur)
    return _vectors   # 立即返回旧向量，绝不阻塞请求


def _frame_recv(conn):
    """读 4 字节长度 + body，返回 bytes；连接关闭/截断返回 None。"""
    hdr = b""
    while len(hdr) < 4:
        chunk = conn.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    n = struct.unpack(">I", hdr)[0]
    if n > 8 * 1024 * 1024:
        return None
    body = b""
    while len(body) < n:
        chunk = conn.recv(n - len(body))
        if not chunk:
            return None
        body += chunk
    return body


def _frame_send(conn, data):
    conn.sendall(struct.pack(">I", len(data)) + data)


# 剥离宿主可能附加在消息体内的设备状态或回复格式指令段，避免稀释用户原始语句的语义向量。
_TAIL_INSTRUCT_RE = re.compile(r"〔[^\n]*?(回复格式|下面是|以下是|格式要求|说话方式|系统提示)[^\n]*?〕[\s\S]*$")
_DEVICE_SEG_RE = re.compile(r"[（(](?:用户的手机此刻|用户的设备|设备状态|手机状态|用户的手表)[^）)]*[）)]")


def _clean_user_text(raw):
    """剥掉注入到消息里的元数据段与格式指令段，只留用户真正说的话；剥空则回退原文。"""
    t = str(raw or "")
    t = _TAIL_INSTRUCT_RE.sub("", t)
    t = _DEVICE_SEG_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip(" \n\t　")
    return t or str(raw or "").strip()


def _handle(conn):
    try:
        body = _frame_recv(conn)
        if not body:
            return
        try:
            req = json.loads(body.decode("utf-8"))
        except Exception:
            _frame_send(conn, b"{}")
            return
        if not isinstance(req, dict):
            _frame_send(conn, b"{}")
            return
        if req.get("ping"):
            _frame_send(conn, json.dumps({"pong": True, "model_loaded": _vectors is not None}).encode("utf-8"))
            return
        msg = str(req.get("user_message") or "").strip()
        session_id = str(req.get("session_id") or "")
        if not msg:
            _frame_send(conn, b"{}")
            return
        msg_user = _clean_user_text(msg)
        t0 = time.time()
        result = recall_core.build_injection(
            msg_user, session_id, debug=DAEMON_DEBUG, vectors=_get_vectors(), refs_dir=REFS_DIR)
        dt = time.time() - t0


        if result is None:
            _log("req session=%s -> noop (%.1fms)" % (session_id, dt * 1000))
            _frame_send(conn, b"{}")
        else:
            _frame_send(conn, json.dumps(result, ensure_ascii=False).encode("utf-8"))
            _log("req session=%s -> INJECTED (%.1fms)" % (session_id, dt * 1000))
    except Exception:
        try:
            _frame_send(conn, b"{}")
        except Exception:
            pass
        if DAEMON_DEBUG:
            _log("handle_error: %s" % (traceback.format_exc()[-500:]))


BARK_KEY_FILE = os.path.join(os.path.dirname(SOCK), ".bark_key")
PROMISE_BARK_INTERVAL = 1800    # 巡检间隔 30 分钟
PROMISE_BARK_GAP = 6 * 3600     # 同一批逾期承诺最多 6 小时推一次
_bark_last = [0.0]
_bark_dedup = [None, None, 0.0]   # [title, body, ts] 进程内同内容去重（90s 内不重推）
import json as _json
_BARK_RECENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "recall_bark_recent.json")
BARK_DEDUP_WINDOW = 6 * 3600      # 6 小时内相同正文不重复推送
_bark_recent = []                 # [{"b": 正文(去空白), "t": 时间戳}, ...]（跨重启持久化）
try:
    with open(_BARK_RECENT_FILE, encoding="utf-8") as _fh:
        _bark_recent = _json.load(_fh)
    _bark_recent = [e for e in _bark_recent if isinstance(e, dict) and "b" in e and "t" in e]
except Exception:
    _bark_recent = []


def _bark_persist():
    try:
        with open(_BARK_RECENT_FILE, "w", encoding="utf-8") as _fh:
            _json.dump(_bark_recent[-20:], _fh, ensure_ascii=False)
    except Exception:
        pass


def _bark(title, body):
    """推送手机通知。优先读取环境变量 BARK_KEY，其次读取同目录 .bark_key 文件。
    内置两级去重：① 同标题+同内容 90s 内去重；② 同正文 6h 内跨重启去重。"""
    import time as _t
    _n = _t.time()
    _norm = (body or "").strip()
    if _bark_dedup[0] == title and _bark_dedup[1] == body and (_n - _bark_dedup[2]) < 90:
        return True
    if any(e["b"] == _norm and (_n - e["t"]) < BARK_DEDUP_WINDOW for e in _bark_recent):
        # 6h 内已发过正文完全相同的 → 直接跳过，不推
        return True
    import urllib.parse
    import urllib.request
    key = os.environ.get("BARK_KEY", "")
    if not key:
        try:
            key = open(BARK_KEY_FILE, encoding="utf-8").read().strip()
        except Exception:
            key = ""
    if not key:
        return False
    icon = os.environ.get("BARK_ICON", "")  # 可选：自定义通知图标 URL
    url = "https://api.day.app/%s/%s/%s?isArchive=1&icon=%s" % (
        key, urllib.parse.quote(title, safe=""), urllib.parse.quote(body, safe=""),
        urllib.parse.quote(icon, safe=""))
    import ssl
    contexts = [None]
    try:
        contexts.append(ssl._create_unverified_context())  # 系统 python 缺 CA 证书时降级
    except Exception:
        pass
    sent = False
    for ctx in contexts:
        try:
            if ctx is None:
                with urllib.request.urlopen(url, timeout=10) as r:
                    r.read()
            else:
                with urllib.request.urlopen(url, timeout=10, context=ctx) as r:
                    r.read()
            sent = True
            break
        except Exception:
            continue
    if sent:
        _bark_dedup[:] = [title, body, _n]
        _bark_recent.append({"b": _norm, "t": _n})
        _bark_recent[:] = [e for e in _bark_recent if (_n - e["t"]) < BARK_DEDUP_WINDOW]
        _bark_persist()
    return sent


def _promise_bark_loop():
    """面向用户的闭环：每 30min 巡检逾期承诺，有就推 Bark（6h 内不重复）。"""
    while not _shutdown.is_set():
        try:
            due = recall_core._due_promises(REFS_DIR)
            overdue = [d for d in due if d.get("overdue")]
            now = time.time()
            if overdue and (now - _bark_last[0]) >= PROMISE_BARK_GAP:
                body = "\n".join("· %s（已逾期%s天）" % (d["text"][:60], d["days_overdue"]) for d in overdue[:5])
                if _bark("他欠你的承诺还没兑现", body):
                    _bark_last[0] = now
                    _log("promise bark pushed, %d overdue" % len(overdue))
        except Exception:
            pass
        _shutdown.wait(PROMISE_BARK_INTERVAL)


def _serve():
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK)
    srv.listen(16)
    _log("listening on %s" % SOCK)
    srv.settimeout(1.0)
    while not _shutdown.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        t = threading.Thread(target=_handle, args=(conn,), daemon=True)
        t.start()
    try:
        srv.close()
    except Exception:
        pass


def _ensure_single_instance():
    """单实例：socket 存在且有进程在听 → 视为已起，退出；否则清掉 stale 文件。"""
    if os.path.exists(SOCK):
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.connect(SOCK)
            probe.close()
            _log("another instance already listening on %s, exit" % SOCK)
            sys.exit(0)
        except ConnectionRefusedError:
            try:
                os.unlink(SOCK)
                _log("removed stale socket %s" % SOCK)
            except Exception:
                pass
        except Exception:
            pass


def _main():
    _ensure_single_instance()
    _preload()
    # 信号：launchd 用 SIGTERM 停；自己也有 SIGINT
    def _stop(signum, frame):
        _shutdown.set()
        try:
            os.unlink(SOCK)
        except Exception:
            pass
        _log("received signal %d, shutting down" % signum)
    import signal as _signal
    _signal.signal(_signal.SIGTERM, _stop)
    _signal.signal(_signal.SIGINT, _stop)
    threading.Thread(target=_promise_bark_loop, daemon=True, name="promise_bark").start()
    _log("promise bark loop started (interval=%ds)" % PROMISE_BARK_INTERVAL)
    try:
        _serve()
    finally:
        try:
            os.unlink(SOCK)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        _main()
    except Exception:
        _log("FATAL: %s" % (traceback.format_exc()[-800:]))
        sys.exit(1)
