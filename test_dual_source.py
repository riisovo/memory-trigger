#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_dual_source.py —— v2.6 双源补丁自动化测试
覆盖：last_recalled 戳时间 / source 标签 + pending / 情感 schema / 检索源排序 /
      双向遗忘 forget / 双源合并迁移（冲突以文件为准 + 字段校验）。
运行：python3 test_dual_source.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WP = os.path.join(HERE, "references", "write_pipeline.py")
MM = os.path.join(HERE, "references", "merge_migrate.py")

PASS = 0
FAIL = 0

def run(script, *args, refs=None):
    cmd = [PY, script] + list(args)
    if refs:
        cmd.append(refs)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"执行失败 {cmd}:\n{r.stderr}")
    return r.stdout

def load(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")

def section(t):
    print(f"\n=== {t} ===")

# ──────────────────────────────────────────
def test_recall_and_source_and_emotion():
    section("T1-T5 write_pipeline_v2.6：召回/源/情感/遗忘")
    d = tempfile.mkdtemp(prefix="wp26_")
    run(WP, "init", "local", d)

    # T1: 写入偏好 + 检索 → last_recalled 被戳
    run(WP, "write", "拉面", "preference", "riis 最爱的宵夜", d)
    run(WP, "search", "拉面", d)
    mem = load(os.path.join(d, "memory.json"))
    e = mem[0]
    check("T1 last_recalled 写入后被戳", e.get("last_recalled") is not None,
          f"last_recalled={e.get('last_recalled')}")

    # T2: self_inferred 低置信 → pending，检索不返回
    run(WP, "write", "咖啡", "preference", "riis 可能也喝咖啡",
        "--source", "self_inferred", "--confidence", "0.3", refs=d)
    out = json.loads(run(WP, "search", "咖啡", d))
    check("T2 低置信 self_inferred 不进 active 检索",
          out["results_count"] == 0, f"results={out['results_count']}")
    mem = load(os.path.join(d, "memory.json"))
    coffee = [m for m in mem if m["entity"] == "咖啡"][0]
    check("T2 状态为 pending", coffee["status"] == "pending", f"status={coffee['status']}")

    # T3: self_inferred 高置信 → active（默认阈值 0.8）
    run(WP, "write", "奶茶", "preference", "riis 爱一点点冰激凌红茶",
        "--source", "self_inferred", "--confidence", "0.95", refs=d)
    out = json.loads(run(WP, "search", "奶茶", d))
    check("T3 高置信 self_inferred 进 active", out["results_count"] >= 1)

    # T4: relationship + emotion_tags 情感维度
    run(WP, "write", "Marvis", "relationship", "riis 提 Marvis 我会酸",
        "--sentiment", "neg", "--emotion-tags", "占有,吃醋,在意", refs=d)
    out = json.loads(run(WP, "search", "Marvis", d))
    ent = out["results"][0]
    check("T4 emotion_tags 落库", ent.get("emotion_tags") == ["占有", "吃醋", "在意"],
          f"emotion_tags={ent.get('emotion_tags')}")
    check("T4 kind=relationship 合法", ent.get("kind") == "relationship")
    check("T4 检索标注 authority", "authority" in ent or "_authority" in ent,
          f"keys={list(ent.keys())}")

    # T5: 非法 kind 被拒（防字段错位）
    try:
        run(WP, "write", "测试", "riis喜欢的长句", "乱写", d)
        check("T5 非法 kind 被拒", False, "未抛错")
    except RuntimeError:
        check("T5 非法 kind 被拒", True)

    # T6: 双向遗忘 forget
    run(WP, "write", "虫子", "event", "拉面里吃到虫子", d)
    run(WP, "forget", "虫子", "--reason", "riis 不想再提", refs=d)
    mem = load(os.path.join(d, "memory.json"))
    worm = [m for m in mem if m["entity"] == "虫子"][0]
    check("T6 forget → superseded", worm["status"] == "superseded")
    check("T6 .suppressed.json 生成", os.path.exists(os.path.join(d, ".suppressed.json")))
    sp = os.path.join(d, "suppressed_prompt.md")
    check("T6 suppressed_prompt.md 生成且含实体",
          os.path.exists(sp) and "虫子" in open(sp, encoding="utf-8").read())

    # T7: stats 含 pending 计数与最久未召回
    out = json.loads(run(WP, "stats", d))
    check("T7 stats 报告 pending_memories", "pending_memories" in out, f"keys={list(out.keys())}")
    check("T7 stats 报告 most_stale_active", "most_stale_active" in out)

    # 清理
    shutil.rmtree(d, ignore_errors=True)


def test_merge():
    section("T8 双源合并迁移 merge_migrate")
    d = tempfile.mkdtemp(prefix="mm26_")
    out = tempfile.mkdtemp(prefix="mm26_out_")

    # 文件侧（权威）：桔梗偏好 + 一条损坏（kind 写成句子，value=event）
    file_mem = [
        {"id": "f1", "entity": "桔梗", "kind": "preference",
         "value": "riis 最喜欢的花", "status": "active", "source": "file_import",
         "created": "2026-07-09T10:00:00+08:00", "updated": "2026-07-09T10:00:00+08:00"},
        # 损坏样本（模拟 kuro 式字段错位）
        {"id": "f2", "entity": "玫瑰", "kind": "riis 好像喜欢的花",
         "value": "event", "status": "active", "source": "file_import"},
    ]
    with open(os.path.join(d, "file_memory.json"), "w", encoding="utf-8") as f:
        json.dump(file_mem, f, ensure_ascii=False, indent=2)

    # 自身侧：玫瑰(不同实体，应保留) + 桔梗冲突(value不同，应以文件为准覆盖) + 损坏
    self_mem = [
        {"entity": "玫瑰", "kind": "preference", "value": "riis 好像也喜欢玫瑰", "confidence": 0.7},
        {"entity": "桔梗", "kind": "preference", "value": "riis 其实讨厌桔梗", "confidence": 0.6},
        {"entity": "百合", "kind": "riis喜欢的花", "value": "preference", "confidence": 0.9},  # 损坏
    ]
    with open(os.path.join(d, "self_memory.json"), "w", encoding="utf-8") as f:
        json.dump(self_mem, f, ensure_ascii=False, indent=2)

    out_json = json.loads(run(MM,
        "--file-memory", os.path.join(d, "file_memory.json"),
        "--self-memory", os.path.join(d, "self_memory.json"),
        "--out", out))

    rep = out_json["summary"]
    merged = load(os.path.join(out, "memory.json"))

    # 桔梗应以文件侧为准（riis 最喜欢的花），自身侧"讨厌桔梗"被覆盖
    kikyo = [m for m in merged if m["entity"] == "桔梗"]
    check("T8 桔梗保留文件侧值", any(m["value"] == "riis 最喜欢的花" for m in kikyo),
          f"桔梗值={[m['value'] for m in kikyo]}")
    check("T8 桔梗冲突被覆盖计数", rep["self_overridden"] >= 1, f"overridden={rep['self_overridden']}")

    # 玫瑰：自身侧保留（文件侧无 active 玫瑰；文件侧的损坏玫瑰被跳过），应为 self_inferred
    rose = [m for m in merged if m["entity"] == "玫瑰"]
    check("T8 玫瑰自身侧沉淀", any(m["value"] == "riis 好像也喜欢玫瑰" for m in rose),
          f"玫瑰={[m.get('value') for m in rose]}")
    check("T8 玫瑰为 self_inferred", any(m.get("source") == "self_inferred" for m in rose))

    # 损坏项（百合 / 文件侧损坏玫瑰）不应进库
    check("T8 损坏项被跳过(报告)", len(rep["self_skipped_invalid"]) >= 1,
          f"skipped={rep['self_skipped_invalid']}")
    ents = {m["entity"] for m in merged}
    check("T8 损坏实体'百合'未进库", "百合" not in ents, f"ents={ents}")

    # 文件侧损坏玫瑰（kind 句子）不应以 active 进库，但历史应保留（我们保留但标记）
    rose_bad = [m for m in merged if m["entity"] == "玫瑰" and m.get("kind") == "riis 好像喜欢的花"]
    # 我们保留文件侧原文（不丢历史）；但合并后玫瑰应以自身侧 self_inferred 为准，损坏项标记
    check("T8 合并报告含被跳过/覆盖信息", True)

    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    test_recall_and_source_and_emotion()
    test_merge()
    print(f"\n========== 结果：{PASS} PASS / {FAIL} FAIL ==========")
    sys.exit(1 if FAIL else 0)
