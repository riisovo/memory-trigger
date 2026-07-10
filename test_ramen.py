#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_ramen.py —— 验证 v2.4 的「记忆不打架」三件套：
  1) 实体归一化（「拉面」vs「那家拉面店」归一到同一实体）
  2) 偏好翻转（喜欢→讨厌，旧值 superseded，检索只返回 active）
  3) 事件分层（「吃到虫子」作为 event 独立留档，不覆盖 preference）
  4) recover 重复写不丢数据（v2.3 修复）

用法: python3 test_ramen.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

PY = sys.executable  # 自动取当前 Python 解释器
HERE = os.path.dirname(os.path.abspath(__file__))
PATCH = os.path.join(HERE, "references", "write_pipeline.py")
ALIASES = os.path.join(HERE, "references", "aliases.json")

fails = []
def check(name, cond, detail=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        fails.append(name)

def run(*a, cwd):
    return subprocess.run([PY, PATCH, *a, cwd], capture_output=True, text=True)

def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)

def main():
    tmp = tempfile.mkdtemp(prefix="ramen_test_")
    try:
        # 用补丁脚本初始化（会自带 aliases.json）
        r = run("init", "local", cwd=tmp)
        check("init 成功", r.returncode == 0, r.stderr.strip())
        # 把示例别名表拷进去（init 已生成默认表，这里用 ours 以覆盖更全）
        shutil.copy(ALIASES, os.path.join(tmp, "aliases.json"))

        print("\n--- 场景：昨天喜欢拉面，昨晚吃到虫子，再也不想吃，并吐槽 ---")

        # 昨天：喜欢拉面（preference, pos）
        run("write", "拉面", "preference", "喜欢吃拉面，尤其豚骨", "--sentiment", "pos", cwd=tmp)
        # 昨晚：那家拉面店吃到虫子（preference 翻转 -> neg）
        run("write", "那家拉面店", "preference", "昨晚吃到虫子，再也不想吃拉面了", "--sentiment", "neg", cwd=tmp)
        # 昨晚：吐槽这件事作为 event（独立留档，含因果）
        run("write", "那家拉面店", "event", "2026-07-09 在那家拉面店吃出虫子，体验极差", "--sentiment", "neg", cwd=tmp)

        mem = load(os.path.join(tmp, "memory.json"))
        ei = load(os.path.join(tmp, "entity_index.json"))

        # T1 归一化：entity_index 不应出现「那家拉面店」这个分裂实体
        check("归一化生效：实体只有「拉面」",
              "拉面" in ei["entities"] and "那家拉面店" not in ei["entities"],
              list(ei["entities"].keys()))

        # T2 偏好翻转：active 的 preference 只有 1 条，且为「再也不想吃」
        pref_active = [e for e in mem
                       if e.get("kind") == "preference" and e.get("status") == "active"]
        check("偏好翻转：active preference 唯一", len(pref_active) == 1,
              f"active preference 数={len(pref_active)}")
        check("偏好翻转：最新 active 为阴性(neg)「再也不想吃」",
              pref_active and pref_active[0]["sentiment"] == "neg"
              and "再也不想吃" in pref_active[0]["value"],
              pref_active[0]["value"] if pref_active else "空")

        # T3 旧偏好被保留为 superseded（历史不丢，不污染上下文）
        pref_sup = [e for e in mem
                    if e.get("kind") == "preference" and e.get("status") == "superseded"]
        check("旧偏好标 superseded（保留历史）",
              any("喜欢吃拉面" in e["value"] for e in pref_sup),
              f"superseded 数={len(pref_sup)}")

        # T4 不打架核心断言：不存在「两条都 active 且 sentiment 相反」的 preference
        pos_active = [e for e in pref_active if e.get("sentiment") == "pos"]
        check("不打架：无相互矛盾(active pos + active neg)的 preference",
              len(pos_active) == 0,
              f"active pos preference 数={len(pos_active)}")

        # T5 事件分层：event 独立存在，且不与 preference 互斥
        ev = [e for e in mem if e.get("kind") == "event" and e.get("status") == "active"]
        check("事件分层：吃到虫子的 event 独立留档",
              any("虫子" in e["value"] for e in ev),
              f"active event 数={len(ev)}")

        # T6 检索「拉面」返回的 active 里，preference 是阴性、event 可解释因果
        sr = run("search", "拉面", cwd=tmp)
        res = json.loads(sr.stdout)
        ents = [r["entry"] for r in res["results"]]
        check("检索「拉面」至少有 1 条 active", len(ents) >= 1)
        pref_in_res = [e for e in ents if e.get("kind") == "preference"]
        check("检索结果里 preference 为最新阴性（不返回旧的喜欢）",
              pref_in_res and pref_in_res[0]["sentiment"] == "neg"
              and "再也不想吃" in pref_in_res[0]["value"])

        print("\n--- 场景：recover 重复写不应丢数据（PATCH v2.3 修复）---")
        tmp2 = tempfile.mkdtemp(prefix="recover_test_")
        run("init", "local", cwd=tmp2)
        # 模拟崩溃：同一实体「咖啡」写两次（不同 memory_id），均未 commit
        for v in ("第一次没提交:A", "第二次没提交:B"):
            mid = f"mem_crash_{v[-1]}"
            wal = {
                "ts": "2026-07-10T00:00:00",
                "op": "upsert",
                "entity": "咖啡",
                "value": v,
                "type": "preference",
                "kind": "preference",
                "memory_id": mid,
                "targets": ["memory.json"]
            }
            with open(os.path.join(tmp2, ".wal.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(wal, ensure_ascii=False) + "\n")
        rr = run("recover", cwd=tmp2)
        # recover 内部每调一次 cmd_write 都会 print 一条 JSON，取最后一行（recover 自己的汇总）
        rj = json.loads(rr.stdout.strip().splitlines()[-1])
        check("recover 恢复 2 条（不按 entity 名去重吞掉）",
              rj.get("recovered_count") == 2,
              f"recovered_count={rj.get('recovered_count')}")
        mem2 = load(os.path.join(tmp2, "memory.json"))
        vals = [e["value"] for e in mem2]
        check("recover 后两条值都在",
              "第一次没提交:A" in vals and "第二次没提交:B" in vals,
              str(vals))

        # 对照组：旧 v2.2 行为（按 entity 去重）会只恢复 1 条——这里验证 patch 修复
        print("\n--- 对照：原 v2.2 的 recover 按 entity 去重会丢 1 条，本补丁已修复 ---")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp2, ignore_errors=True)

    print("\n" + ("=" * 50))
    if fails:
        print(f"结果：{len(fails)} 项 FAIL -> {fails}")
        sys.exit(1)
    else:
        print("结果：全部 PASS ✅（记忆不打架 + 归一化 + 事件分层 + recover 修复 均验证通过）")
        sys.exit(0)

if __name__ == "__main__":
    main()
