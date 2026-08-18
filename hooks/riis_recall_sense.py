#!/Users/xiaozuzong/.hermes/agent-hooks/venv/bin/python
# -*- coding: utf-8 -*-
# riis_recall_sense.py — hermes pre_llm_call shell hook（薄客户端）
#
# 行为：
#   1) 优先连 recall_daemon 的 UNIX socket，把 {user_message, session_id} 发过去，
#      直接打印 daemon 回的 {"context":...} 或 "{}"（常驻内存，~100ms，不重载模型）。
#   2) 连不上 daemon（没起 / 挂了 / 超时）→ 退化回本地 recall_core.decide_and_recall
#      （向后兼容：模型在子进程里冷启动一次，慢但照常工作）。
#
# 2026-08-18 修复（承接严格审查报告）：
#   · payload 非 dict 直接 noop（P2-3，原 L63 未校验）。
#   · 去掉 len(msg)>80 硬过滤（P1：长段冷信号也曾漏触发）。
#   · 多余逗号已无（P2 项）。
#   · debug 日志统一带 session_id（P2-6）。
#   · 不再裸 except: pass 吞错：错误走 fallback 或记日志。

import json
import os
import socket
import struct
import sys
import time
import traceback
from pathlib import Path

# 注意：recall_core / recall_vec 不在顶层 import——它们会拉起 fastembed（~300ms 模块加载）。
# 只有 daemon 连不上、走本地回退时才需要，故在 _fallback 内惰性 import，避免热路径每次付这个税。

REFS_DIR = os.environ.get("RECALL_REFS_DIR", "/Users/xiaozuzong/.hermes/memories/memory-trigger")
SOCK = os.environ.get("RECALL_SOCK", "/Users/xiaozuzong/.hermes/agent-hooks/recall.sock")
DEBUG_LOG = Path.home() / ".hermes" / "agent-hooks" / ".recall_sense_debug.log"
SOCK_TIMEOUT = float(os.environ.get("RECALL_SOCK_TIMEOUT", "2.0"))


def _dbg(tag, session="", **kv):
    try:
        parts = " ".join("%s=%r" % (k, v) for k, v in kv.items())
        with DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write("%s [%.3f] %s session=%s %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), time.time(), tag, session, parts))
    except Exception:
        pass


def _frame_recv(conn):
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


def _ask_daemon(msg, session_id):
    """连 daemon，返回原始响应 bytes（b'{}' / b'{"context":...}' / b'{"pong":true}'）。失败返回 None。"""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(SOCK_TIMEOUT)
    try:
        s.connect(SOCK)
        _frame_send(s, json.dumps({"user_message": msg, "session_id": session_id}).encode("utf-8"))
        return _frame_recv(s)
    finally:
        try:
            s.close()
        except Exception:
            pass


def _fallback(msg, session_id):
    """daemon 不可用时的本地回退：惰性 import recall_core 后跑核心判定（子进程内加载模型，慢但工作）。"""
    try:
        sys.path.insert(0, "/Users/xiaozuzong/.hermes/memory-trigger-src/references")
        import recall_core  # 仅在回退时加载 fastembed（热路径不付此税）
        return recall_core.build_injection(msg, session_id, debug=True, refs_dir=REFS_DIR)
    except Exception:
        _dbg("FALLBACK_ERROR", session_id, tb=traceback.format_exc()[-400])
        return None


def main():
    # ---- 读 payload（严格校验 dict） ----
    try:
        payload = json.load(sys.stdin)
    except Exception:
        _dbg("BAD_STDIN", reason="json_parse")
        print("{}")
        return
    if not isinstance(payload, dict):
        _dbg("BAD_PAYLOAD", reason="not_dict")
        print("{}")
        return
    if payload.get("hook_event_name") != "pre_llm_call":
        _dbg("SKIP_EVENT", event=payload.get("hook_event_name"))
        print("{}")
        return

    # ---- 取 user_message（兼容 extra.extra 嵌套） ----
    extra = payload.get("extra") or {}
    if isinstance(extra, dict):
        inner = extra.get("extra")
        if isinstance(inner, dict) and inner.get("user_message"):
            extra = inner
    msg = (extra.get("user_message") if isinstance(extra, dict) else None) or ""
    msg = msg.strip()
    session_id = str(payload.get("session_id") or "")
    _dbg("INVOKED", session=session_id, msg=msg[:60])

    if not msg or msg.startswith("/"):
        _dbg("NOOP", session=session_id, reason="empty_or_slash")
        print("{}")
        return
    if session_id.lower().startswith("cron"):
        _dbg("NOOP", session=session_id, reason="cron_session")
        print("{}")
        return

    # ---- 优先 daemon（常驻内存，~100ms） ----
    t0 = time.time()
    try:
        resp = _ask_daemon(msg, session_id)
    except Exception as exc:
        resp = None
        _dbg("DAEMON_UNREACHABLE", session=session_id, err=str(exc)[:120])
    if resp is not None:
        dt = (time.time() - t0) * 1000
        _dbg("DAEMON_OK", session=session_id, ms=round(dt, 1), bytes=len(resp))
        sys.stdout.write(resp.decode("utf-8") if isinstance(resp, (bytes, bytearray)) else str(resp))
        if not resp.endswith(b"\n") and not str(resp).endswith("\n"):
            sys.stdout.write("\n")
        return

    # ---- 回退：本地 recall_core（daemon 不在） ----
    _dbg("FALLBACK", session=session_id, reason="daemon_down")
    result = _fallback(msg, session_id)
    print(json.dumps(result, ensure_ascii=False) if result else "{}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        _dbg("UNCAUGHT", tb=traceback.format_exc()[-600])
        print("{}")
