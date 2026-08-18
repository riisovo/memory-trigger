#!/Users/xiaozuzong/.hermes/agent-hooks/venv/bin/python
# -*- coding: utf-8 -*-
# recall_daemon.py — 主动回忆常驻守护进程（根治 P0-1 每条消息 0.5~0.8s 阻塞 + P0-2 首建缓存 12s）
#
# 思路：模型 / 锚点向量 / 记忆向量常驻内存，永不在每条消息时重载。钩子改成薄客户端，
#       连 socket 发 {user_message, session_id}，daemon 直接在内存里 embed+判定+检索，~100ms 返回。
#       连不上 daemon（daemon 挂了 / 没起）→ 钩子退化回本地 recall_core.decide_and_recall（向后兼容）。
#
# 内存自愈：memory.json 的 mtime 变动后，下一次请求在内存里重算一次记忆向量（仅那一次稍慢），之后继续常驻。
# 模型加载一次性成本（~12s）只在 daemon 启动时发生，不在每条消息上。
#
# 协议（UNIX stream socket，4 字节大端长度前缀 + JSON UTF-8）：
#   请求：{"user_message": "...", "session_id": "..."}   探测：{"ping": true}
#   响应：{"context": "..."}（命中） / "{}"（未命中/冷却/出错，等价于钩子 noop） / {"pong": true}（探测）

import json
import os
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

REFS_DIR = os.environ.get("RECALL_REFS_DIR", "/Users/xiaozuzong/.hermes/memories/memory-trigger")
SOCK = os.environ.get("RECALL_SOCK", "/Users/xiaozuzong/.hermes/agent-hooks/recall.sock")
LOG = os.environ.get("RECALL_DAEMON_LOG", str(Path(SOCK).with_suffix(".sock.log")))
DAEMON_DEBUG = os.environ.get("RECALL_DAEMON_DEBUG", "1") == "1"

_vec_lock = threading.Lock()
_vectors = None          # {mem_id: np向量} 常驻内存
_vectors_mtime = None    # 对应的 memory.json mtime
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


def _get_vectors():
    """返回常驻记忆向量；memory.json 变动则在内存里重算一次。"""
    global _vectors, _vectors_mtime
    try:
        cur = recall_vec.memory_mtime(REFS_DIR)
    except Exception:
        return _vectors  # memory.json 读不到，退而用上一份内存向量
    with _vec_lock:
        if _vectors is None or _vectors_mtime is None or cur > _vectors_mtime:
            t0 = time.time()
            _vectors = recall_vec.build_or_load_embeddings(REFS_DIR)
            _vectors_mtime = cur
            _log("memory.json changed (mtime=%.1f), reloaded %d vectors in %.2fs"
                 % (cur, len(_vectors), time.time() - t0))
        return _vectors


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
        t0 = time.time()
        result = recall_core.build_injection(
            msg, session_id, debug=DAEMON_DEBUG, vectors=_get_vectors(), refs_dir=REFS_DIR)
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


def _bark(title, body):
    """推 riis 手机。key 取 env BARK_KEY，其次 agent-hooks/.bark_key 文件。"""
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
    icon = "https://cdn.jsdelivr.net/gh/riisovo/images@main/IMG_0647.jpg"
    url = "https://api.day.app/%s/%s/%s?isArchive=1&icon=%s" % (
        key, urllib.parse.quote(title, safe=""), urllib.parse.quote(body, safe=""),
        urllib.parse.quote(icon, safe=""))
    import ssl
    contexts = [None]
    try:
        contexts.append(ssl._create_unverified_context())  # 系统 python 缺 CA 证书时降级
    except Exception:
        pass
    for ctx in contexts:
        try:
            if ctx is None:
                with urllib.request.urlopen(url, timeout=10) as r:
                    r.read()
            else:
                with urllib.request.urlopen(url, timeout=10, context=ctx) as r:
                    r.read()
            return True
        except Exception:
            continue
    return False


def _promise_bark_loop():
    """riis-facing 闭环：每 30min 巡检逾期承诺，有就推 Bark（6h 内不重复）。"""
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
