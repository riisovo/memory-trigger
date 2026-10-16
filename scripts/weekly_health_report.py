#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory-trigger · 每周记忆健康周报

对给定 refs_dir 跑一遍维护流水线（backup -> decay -> vacuum -> selfcheck -> stats），
把体检结果格式化成一份简洁周报，通过 Bark 推给用户。

用法:
  python weekly_health_report.py --refs-dir <记忆库目录> --bark
  python weekly_health_report.py --refs-dir <DIR> --dry-run        # 只出报告, 不改数据
  python weekly_health_report.py --refs-dir <DIR> --no-bark         # 只打印, 不推送

设计要点:
  - decay/vacuum/selfcheck/backup 属于"写"操作；--dry-run 时全部跳过, 只跑只读的 stats。
  - Bark 用 icon=(头像) 而非 image=, 与用户既定约定一致。
"""
import argparse
import os
import sys
import io
import json
import datetime
import contextlib
import subprocess
import time
import urllib.parse

# 让脚本能 import 同仓库的 write_pipeline.py（本脚本在 <repo>/scripts/，逻辑库在 <repo>/references/）
_HERE = os.path.dirname(os.path.abspath(__file__))
_REFERENCES = os.path.normpath(os.path.join(_HERE, "..", "references"))
if _REFERENCES not in sys.path:
    sys.path.insert(0, _REFERENCES)

import write_pipeline as wp  # noqa: E402

# —— Bark 推送配置（全部来自环境变量；代码里绝不写死任何 key / 私人路径）——
#   BARK_KEY        : Bark 设备 key（设了才推送；也可改用 BARK_NOTIFY_SH 指向你本机通知脚本）
#   BARK_NOTIFY_SH  : 可选，指向本机已有的 bark 通知脚本（它自己管 key），设了优先用它
#   BARK_ICON       : 可选，通知头像 URL（留空用 Bark 默认）
#   MEMORY_TRIGGER_REFS_DIR: 记忆库目录（不设默认用当前目录）
BARK_KEY = os.environ.get("BARK_KEY", "")
BARK_NOTIFY_SH = os.environ.get("BARK_NOTIFY_SH", "")
BARK_ICON = os.environ.get("BARK_ICON", "")
DEFAULT_REFS = os.environ.get("MEMORY_TRIGGER_REFS_DIR", ".")

TZ = wp.TZ if hasattr(wp, "TZ") else datetime.timezone(datetime.timedelta(hours=8))


def _cap(fn, *args):
    """write_pipeline 的 cmd_* 都是 print(json) 而非 return；这里重定向 stdout 捕获并解析。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args)
    out = buf.getvalue().strip()
    try:
        return json.loads(out)
    except Exception:
        return {"_raw": out}


def _run_maintenance(refs_dir, dry_run):
    """返回维护动作的结果摘要。dry_run 时跳过所有写操作。"""
    summary = {}
    if dry_run:
        summary["mode"] = "dry-run (只读)"
        return summary
    summary["mode"] = "full"
    try:
        _cap(wp.cmd_backup, refs_dir)
        summary["backup"] = "ok"
    except Exception as e:
        summary["backup"] = f"err: {e}"
    try:
        d = _cap(wp.cmd_decay, refs_dir)
        summary["decayed"] = d.get("decayed_count") if isinstance(d, dict) else "?"
    except Exception as e:
        summary["decayed"] = f"err: {e}"
    try:
        v = _cap(wp.cmd_vacuum, refs_dir)
        summary["archived"] = v.get("archived") if isinstance(v, dict) else "?"
    except Exception as e:
        summary["archived"] = f"err: {e}"
    try:
        _cap(wp.cmd_selfcheck, refs_dir)
        summary["selfcheck"] = "ok"
    except Exception as e:
        summary["selfcheck"] = f"err: {e}"
    try:
        ex = _cap(wp.cmd_expire_check, refs_dir)
        summary["expired"] = len(ex.get("expired", [])) if isinstance(ex, dict) else "?"
        summary["expire_remind"] = len(ex.get("remind", [])) if isinstance(ex, dict) else "?"
    except Exception as e:
        summary["expire_check"] = f"err: {e}"
    try:
        pc = _cap(wp.cmd_promise_check, refs_dir)
        summary["unfulfilled_promises"] = pc.get("unfulfilled_count") if isinstance(pc, dict) else "?"
    except Exception as e:
        summary["promise_check"] = f"err: {e}"
    return summary


def _build_report(refs_dir, maint):
    stats = _cap(wp.cmd_stats, refs_dir)
    if not isinstance(stats, dict):
        stats = {}
    now = datetime.datetime.now(TZ)
    lines = []
    lines.append("🧠 记忆健康周报 %s" % now.strftime("%Y-%m-%d"))
    lines.append("───")
    lines.append("核心记忆: %s 条（永不衰减）" % stats.get("core_memories", "?"))
    lines.append("非核平均权重: %s（越近 1 越鲜活）" % stats.get("avg_importance_noncore", "?"))
    lines.append("活跃/待确认/过期: %s / %s / %s" % (
        stats.get("active", "?"), stats.get("pending_memories", "?"), stats.get("superseded", "?")))
    lines.append("")
    weakest = stats.get("weakest_active") or []
    if weakest:
        lines.append("最弱 5 条（快淡没了）:")
        for ent, kind, imp in weakest:
            lines.append("  · %s (%s) 权重 %s" % (ent, kind, imp))
    lines.append("")
    stale = stats.get("most_stale_active") or []
    if stale:
        lines.append("最久没提 5 个话题:")
        for ent, kind, age, lr in stale:
            lr_s = lr if lr else "(从未召回)"
            lines.append("  · %s (%s) 已 %s 天没召回" % (ent, kind, age))
    lines.append("")
    lines.append("本周维护: %s" % json.dumps(maint, ensure_ascii=False))
    # 核心记忆健康检查：扫描是否存在 core=true 但状态为 superseded 的异常条目
    try:
        _mem = json.loads(open(os.path.join(refs_dir, "memory.json"), encoding="utf-8").read())
        _core_dead = [e for e in _mem if isinstance(e, dict)
                      and e.get("core") and e.get("status") == "superseded"]
        if _core_dead:
            lines.append("")
            lines.append("🚨 core 残骸: %d 条（core 却被标 superseded = 检索看不到，必须人工救回）" % len(_core_dead))
            for e in _core_dead[:5]:
                lines.append("  · %s %s" % (e.get("id"), (e.get("value") or "")[:50]))
        else:
            lines.append("✅ core 健康: 0 条 core 被静默作废")
    except Exception as e:
        lines.append("core 自检跳过: %s" % e)
    if isinstance(maint, dict) and maint.get("unfulfilled_promises"):
        lines.append("")
        lines.append("⚠️ 还没兑现的承诺: %s 条（去 promise check 看明细，兑现后记得 done）" % maint["unfulfilled_promises"])
    return "\n".join(lines)


def _push_bark(title, content):
    if BARK_NOTIFY_SH and os.path.exists(BARK_NOTIFY_SH):
        try:
            r = subprocess.run(["bash", BARK_NOTIFY_SH, title, content],
                               capture_output=True, text=True, timeout=20)
            return (r.stdout or r.stderr or "").strip()
        except Exception as e:
            return "Bark 推送失败(notify_sh): %s" % e
    if not BARK_KEY:
        return "Bark 未配置(BARK_KEY / BARK_NOTIFY_SH 均未设)，跳过推送"
    title_e = urllib.parse.quote(title)
    content_e = urllib.parse.quote(content)
    icon = urllib.parse.quote(BARK_ICON, safe="") if BARK_ICON else ""
    try:
        import json as _json
        payload = {"body": content, "title": title, "device_key": BARK_KEY}
        if icon:
            payload["icon"] = icon
        r = subprocess.run(
            ["curl", "-s", "-m", "15", "-X", "POST", "https://api.day.app/push",
             "-H", "Content-Type: application/json",
             "-d", _json.dumps(payload, ensure_ascii=False)],
            capture_output=True, text=True, timeout=20)
        return r.stdout.strip() or "(无返回)"
    except Exception as e:
        return "Bark 推送失败: %s" % e


def main():
    ap = argparse.ArgumentParser(description="memory-trigger 每周记忆健康周报")
    ap.add_argument("--refs-dir", default=DEFAULT_REFS, help="记忆库目录（默认 %s）" % DEFAULT_REFS)
    ap.add_argument("--dry-run", action="store_true", help="只出报告，不跑 decay/vacuum/selfcheck/backup")
    ap.add_argument("--bark", dest="bark", action="store_true", help="通过 Bark 推送给用户")
    ap.add_argument("--no-bark", dest="bark", action="store_false", help="只打印不推送")
    ap.set_defaults(bark=True)
    args = ap.parse_args()

    refs_dir = os.path.abspath(os.path.expanduser(args.refs_dir))
    if not os.path.isdir(refs_dir):
        print("❌ refs_dir 不存在: %s" % refs_dir)
        sys.exit(1)

    print("[每周维护] refs_dir=%s  dry_run=%s" % (refs_dir, args.dry_run))
    maint = _run_maintenance(refs_dir, args.dry_run)
    # 维护完成后更新 .last_maintenance 时间戳，供会话内维护到期检查读取
    if not args.dry_run:
        try:
            marker = os.path.join(refs_dir, ".last_maintenance")
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%S+08:00"))
        except Exception as e:
            print("[touch] 登记失败(不影响周报): %s" % e)
    report = _build_report(refs_dir, maint)
    print("=" * 40)
    print(report)
    print("=" * 40)

    if args.bark:
        resp = _push_bark("记忆健康周报", report)
        print("[Bark] %s" % resp)
    else:
        print("[Bark] 跳过（--no-bark）")


if __name__ == "__main__":
    main()
