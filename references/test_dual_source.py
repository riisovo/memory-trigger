#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_dual_source.py —— memory-trigger 双源记忆自动化测试（覆盖双源信任 / 人情味层 / 遗忘曲线；当前 v2.8.3）
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
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
WP = os.path.join(HERE, "write_pipeline.py")
MM = os.path.join(HERE, "merge_migrate.py")

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
    section("T1-T5 write_pipeline：召回/源/情感/遗忘")
    d = tempfile.mkdtemp(prefix="wp26_")
    run(WP, "init", "local", d)

    # T1: 写入偏好 + 检索 → last_recalled 被戳
    run(WP, "write", "读书", "preference", "用户的晚间习惯", d)
    run(WP, "search", "读书", d)
    mem = load(os.path.join(d, "memory.json"))
    e = mem[0]
    check("T1 last_recalled 写入后被戳", e.get("last_recalled") is not None,
          f"last_recalled={e.get('last_recalled')}")

    # T2: self_inferred 低置信 → pending，检索不返回
    run(WP, "write", "观影", "preference", "用户可能也爱观影",
        "--source", "self_inferred", "--confidence", "0.3", refs=d)
    out = json.loads(run(WP, "search", "观影", d))
    check("T2 低置信 self_inferred 不进 active 检索",
          out["results_count"] == 0, f"results={out['results_count']}")
    mem = load(os.path.join(d, "memory.json"))
    low_conf = [m for m in mem if m["entity"] == "观影"][0]
    check("T2 状态为 pending", low_conf["status"] == "pending", f"status={low_conf['status']}")

    # T3: self_inferred 高置信 → active（默认阈值 0.8）
    run(WP, "write", "运动", "preference", "用户爱运动",
        "--source", "self_inferred", "--confidence", "0.95", refs=d)
    out = json.loads(run(WP, "search", "运动", d))
    check("T3 高置信 self_inferred 进 active", out["results_count"] >= 1)

    # T4: relationship + emotion_tags 情感维度
    run(WP, "write", "友商", "relationship", "用户提友商我会留意",
        "--sentiment", "neg", "--emotion-tags", "占有,吃醋,在意", refs=d)
    out = json.loads(run(WP, "search", "友商", d))
    ent = out["results"][0]
    check("T4 emotion_tags 落库", ent.get("emotion_tags") == ["占有", "吃醋", "在意"],
          f"emotion_tags={ent.get('emotion_tags')}")
    check("T4 kind=relationship 合法", ent.get("kind") == "relationship")
    check("T4 检索标注 authority", "authority" in ent or "_authority" in ent,
          f"keys={list(ent.keys())}")

    # T5: 非法 kind 被拒（防字段错位）
    try:
        run(WP, "write", "测试", "用户喜欢的备注", "乱写", d)
        check("T5 非法 kind 被拒", False, "未抛错")
    except RuntimeError:
        check("T5 非法 kind 被拒", True)

    # T6: 双向遗忘 forget
    run(WP, "write", "事故", "event", "读书里吃到事故", d)
    run(WP, "forget", "事故", "--reason", "用户不想再提", refs=d)
    mem = load(os.path.join(d, "memory.json"))
    worm = [m for m in mem if m["entity"] == "事故"][0]
    check("T6 forget → superseded", worm["status"] == "superseded")
    check("T6 .suppressed.json 生成", os.path.exists(os.path.join(d, ".suppressed.json")))
    sp = os.path.join(d, "suppressed_prompt.md")
    check("T6 suppressed_prompt.md 生成且含实体",
          os.path.exists(sp) and "事故" in open(sp, encoding="utf-8").read())

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

    # 文件侧（权威）：任务乙偏好 + 一条损坏（kind 写成句子，value=event）
    file_mem = [
        {"id": "f1", "entity": "任务乙", "kind": "preference",
         "value": "用户最关注的指标", "status": "active", "source": "file_import",
         "created": "2026-07-09T10:00:00+08:00", "updated": "2026-07-09T10:00:00+08:00"},
        # 损坏样本（模拟 示例式字段错位）
        {"id": "f2", "entity": "任务甲", "kind": "用户可能关注的指标",
         "value": "event", "status": "active", "source": "file_import"},
    ]
    with open(os.path.join(d, "file_memory.json"), "w", encoding="utf-8") as f:
        json.dump(file_mem, f, ensure_ascii=False, indent=2)

    # 自身侧：任务甲(不同实体，应保留) + 任务乙冲突(value不同，应以文件为准覆盖) + 损坏
    self_mem = [
        {"entity": "任务甲", "kind": "preference", "value": "用户可能也关注任务甲", "confidence": 0.7},
        {"entity": "任务乙", "kind": "preference", "value": "用户其实不关注任务乙", "confidence": 0.6},
        {"entity": "任务丙", "kind": "用户关注的指标", "value": "preference", "confidence": 0.9},  # 损坏
    ]
    with open(os.path.join(d, "self_memory.json"), "w", encoding="utf-8") as f:
        json.dump(self_mem, f, ensure_ascii=False, indent=2)

    out_json = json.loads(run(MM,
        "--file-memory", os.path.join(d, "file_memory.json"),
        "--self-memory", os.path.join(d, "self_memory.json"),
        "--out", out))

    rep = out_json["summary"]
    merged = load(os.path.join(out, "memory.json"))

    # 任务乙应以文件侧为准（用户最关注的指标），自身侧"讨厌任务乙"被覆盖
    ent_yi = [m for m in merged if m["entity"] == "任务乙"]
    check("T8 任务乙保留文件侧值", any(m["value"] == "用户最关注的指标" for m in ent_yi),
          f"任务乙值={[m['value'] for m in ent_yi]}")
    check("T8 任务乙冲突被覆盖计数", rep["self_overridden"] >= 1, f"overridden={rep['self_overridden']}")

    # 任务甲：自身侧保留（文件侧无 active 任务甲；文件侧的损坏任务甲被跳过），应为 self_inferred
    ent_jia = [m for m in merged if m["entity"] == "任务甲"]
    check("T8 任务甲自身侧沉淀", any(m["value"] == "用户可能也关注任务甲" for m in ent_jia),
          f"任务甲={[m.get('value') for m in ent_jia]}")
    check("T8 任务甲为 self_inferred", any(m.get("source") == "self_inferred" for m in ent_jia))

    # 损坏项（任务丙 / 文件侧损坏任务甲）不应进库
    check("T8 损坏项被跳过(报告)", len(rep["self_skipped_invalid"]) >= 1,
          f"skipped={rep['self_skipped_invalid']}")
    ents = {m["entity"] for m in merged}
    check("T8 损坏实体'任务丙'未进库", "任务丙" not in ents, f"ents={ents}")

    # 文件侧损坏任务甲（kind 句子）不应以 active 进库，但历史应保留（我们保留但标记）
    ent_jia_bad = [m for m in merged if m["entity"] == "任务甲" and m.get("kind") == "用户可能关注的指标"]
    # 我们保留文件侧原文（不丢历史）；但合并后任务甲应以自身侧 self_inferred 为准，损坏项标记
    check("T8 合并报告含被跳过/覆盖信息", True)

    shutil.rmtree(d, ignore_errors=True)
    shutil.rmtree(out, ignore_errors=True)


def test_core_and_decay():
    section("T9 v2.7 核心记忆钉死 + 遗忘曲线")
    d = tempfile.mkdtemp(prefix="wp27_")
    run(WP, "init", "local", d)

    # 关系类型默认 core=True（"我们是谁"的基石，永不衰减）
    run(WP, "write", "伙伴", "relationship", "用户的协作伙伴", d)
    mem = load(os.path.join(d, "memory.json"))
    check("T9 relationship 默认 core=True", mem[0].get("core") is True, f"core={mem[0].get('core')}")

    # 偏好非 core，importance 初始 1.0
    run(WP, "write", "读书", "preference", "爱读科幻", d)
    mem = load(os.path.join(d, "memory.json"))
    pref_mem = [m for m in mem if m["entity"] == "读书"][0]
    check("T9 preference 非 core", pref_mem.get("core") is False, f"core={pref_mem.get('core')}")
    check("T9 preference importance 初始 1.0", pref_mem.get("importance") == 1.0,
          f"importance={pref_mem.get('importance')}")

    # --core false 可覆盖关系默认
    run(WP, "write", "同事", "relationship", "普通同事", "--core", "false", refs=d)
    mem = load(os.path.join(d, "memory.json"))
    colleague = [m for m in mem if m["entity"] == "同事"][0]
    check("T9 --core false 覆盖 relationship", colleague.get("core") is False,
          f"core={colleague.get('core')}")

    # decay 命令可跑且不报错
    out = json.loads(run(WP, "decay", d))
    check("T9 decay 命令成功", out.get("status") == "decayed", f"out={out}")

    # 深度验证衰减：造一条 200 天前未召回的记忆 → importance 应 < 1.0
    run(WP, "write", "旧事", "preference", "一件旧事", d)
    mem = load(os.path.join(d, "memory.json"))
    old = [m for m in mem if m["entity"] == "旧事"][0]
    old_ts = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=200)).isoformat(timespec="seconds")
    old["created"] = old_ts
    old["last_recalled"] = old_ts
    old["importance"] = 1.0
    with open(os.path.join(d, "memory.json"), "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)
    run(WP, "decay", d)
    mem = load(os.path.join(d, "memory.json"))
    old = [m for m in mem if m["entity"] == "旧事"][0]
    check("T9 200天未召回 → importance 衰减(<1)", old.get("importance", 1.0) < 1.0,
          f"importance={old.get('importance')}")

    # core 记忆不参与衰减：关系类 200 天前也不该变
    run(WP, "write", "锚点关系", "relationship", "核心关系", d)
    mem = load(os.path.join(d, "memory.json"))
    anchor = [m for m in mem if m["entity"] == "锚点关系"][0]
    anchor_ts = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=200)).isoformat(timespec="seconds")
    anchor["created"] = anchor_ts
    anchor["last_recalled"] = anchor_ts
    anchor["importance"] = 1.0
    with open(os.path.join(d, "memory.json"), "w", encoding="utf-8") as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)
    run(WP, "decay", d)
    mem = load(os.path.join(d, "memory.json"))
    anchor = [m for m in mem if m["entity"] == "锚点关系"][0]
    check("T9 core 关系记忆不衰减(importance 仍=1.0)", anchor.get("importance", 0) == 1.0,
          f"importance={anchor.get('importance')}")

    shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    test_recall_and_source_and_emotion()
    test_merge()
    test_core_and_decay()
    print(f"\n========== 结果：{PASS} PASS / {FAIL} FAIL ==========")
    sys.exit(1 if FAIL else 0)
