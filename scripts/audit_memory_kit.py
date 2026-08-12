#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_memory_kit.py —— 记忆模板一键验收（memory-kit-audit skill）
============================================
对一份 memory-trigger / memory-kit 模板做"最严厉代码工程师"验收：
  静态检查 + 真跑代码，判定它能否 (a) 高命中记住偏好 (b) 不双源打架
  (c) 已有本地 memory.json 时加载不崩。

用法：
  python3 audit_memory_kit.py <template_dir>

  template_dir 应包含：SKILL.md、install.sh、references/write_pipeline*.py、
  references/merge_migrate.py、references/memory.json、test_dual_source.py

退出码：0=全 PASS（或仅有 WARN），1=存在 FAIL。
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

PASS = 0
FAIL = 0
WARN = 0


def check(name, ok, level="FAIL", detail=""):
    global PASS, FAIL, WARN
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        if level == "WARN":
            WARN += 1
            print(f"  [WARN] {name}  {detail}")
        else:
            FAIL += 1
            print(f"  [FAIL] {name}  {detail}")
    return ok


def find_script(refs_dir, base):
    """在 refs_dir 下找 base 开头的 .py（如 write_pipeline*.py）"""
    if not os.path.isdir(refs_dir):
        return None
    for f in os.listdir(refs_dir):
        if f.startswith(base) and f.endswith(".py"):
            return os.path.join(refs_dir, f)
    return None


def parse_allowed_kinds(src):
    """提取脚本里 ALLOWED_KINDS = { ... } 的集合"""
    m = re.search(r"ALLOWED_KINDS\s*=\s*\{([^}]*)\}", src, re.S)
    if not m:
        return set()
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def parse_doc_kinds(skill_src):
    """提取 SKILL.md 里文档声称支持的 kind 并集"""
    kinds = set()
    # 形式1: "kind": "preference|identity|..."
    for m in re.finditer(r'"kind"\s*:\s*"([^"]+)"', skill_src):
        for k in re.split(r"[|,\s]+", m.group(1)):
            if k:
                kinds.add(k)
    # 形式2: T ∈ {preference, identity, ...}
    for m in re.finditer(r"[A-Za-z]+\s*∈\s*\{([^}]+)\}", skill_src):
        for k in re.split(r"[|,\s]+", m.group(1)):
            if k:
                kinds.add(k)
    return kinds


def main():
    if len(sys.argv) < 2:
        print("用法: audit_memory_kit.py <template_dir>")
        sys.exit(2)
    root = sys.argv[1]
    if not os.path.isdir(root):
        print(f"目录不存在: {root}")
        sys.exit(2)

    refs = os.path.join(root, "references")
    skill_md = os.path.join(root, "SKILL.md")
    install_sh = os.path.join(root, "install.sh")
    wp = find_script(refs, "write_pipeline")
    mm = find_script(refs, "merge_migrate")

    print(f"\n===== Memory Kit Audit: {root} =====\n")

    # ---- 0. 文件存在性 ----
    check("模板含 SKILL.md", os.path.isfile(skill_md))
    check("模板含 write_pipeline 脚本", wp is not None)
    check("模板含 merge_migrate 脚本", mm is not None, level="WARN")
    if not wp:
        print(f"\n结果：{PASS} PASS / {FAIL} FAIL / {WARN} WARN")
        sys.exit(1)

    skill_src = open(skill_md, encoding="utf-8").read()
    wp_src = open(wp, encoding="utf-8").read()

    # ---- 1. 时间戳 ----
    print("\n--- 1. 时间戳 ---")
    has_created = 'created' in wp_src and 'updated' in wp_src
    has_tsnow = 'def ts_now' in wp_src and 'isoformat' in wp_src
    check("写入带 created/updated + ts_now(UTC+8)", has_created and has_tsnow)
    check("检索戳 last_recalled（冷记忆衰减支撑）",
          'last_recalled' in wp_src and '_touch_recalled' in wp_src, level="WARN")

    # ---- 2. kind 白名单对齐（CRITICAL）----
    print("\n--- 2. kind 白名单对齐（CRITICAL）---")
    doc_kinds = parse_doc_kinds(skill_src)
    code_kinds = parse_allowed_kinds(wp_src)
    if not doc_kinds:
        check("能从 SKILL.md 解析出 kind 枚举", False,
              detail="未找到 kind 枚举声明")
    else:
        # 文档要求但代码白名单缺失 → 写入会崩
        missing_in_code = sorted(doc_kinds - code_kinds)
        check("SKILL.md 每个 kind 都在脚本白名单（否则写入崩溃）",
              len(missing_in_code) == 0,
              detail=f"文档有但代码无: {missing_in_code}" if missing_in_code else "")
        # 代码有但文档没写 → 文档漂移（不崩，但 agent 不知道能用）
        missing_in_doc = sorted(code_kinds - doc_kinds)
        check("脚本白名单 kind 都在 SKILL.md 声明（消除文档漂移）",
              len(missing_in_doc) == 0, level="WARN",
              detail=f"代码有但文档无: {missing_in_doc}" if missing_in_doc else "")

    # ---- 3. 真跑：向后兼容 + 每个文档 kind 可写 ----
    print("\n--- 3. 真跑：向后兼容 + 每个 kind 可写 ---")
    tmp = tempfile.mkdtemp(prefix="audit_")
    try:
        shutil.copytree(refs, os.path.join(tmp, "refs"))
        rdir = os.path.join(tmp, "refs")
        py = sys.executable

        # stats / search 在（可能空的）随包库上不崩
        r = subprocess.run([py, wp, "stats", rdir], capture_output=True, text=True)
        check("stats 在随包库运行 rc=0", r.returncode == 0, detail=r.stderr[:120])
        r = subprocess.run([py, wp, "search", "读书", rdir], capture_output=True, text=True)
        check("search 在随包库运行 rc=0", r.returncode == 0, detail=r.stderr[:120])

        # 对每个文档 kind 真实写一次，验证不抛 ValueError
        test_entity = "审计探针"
        crash_kinds = []
        for k in sorted(doc_kinds):
            r = subprocess.run([py, wp, "write", test_entity, k, f"探测值_{k}", rdir],
                               capture_output=True, text=True)
            if r.returncode != 0:
                crash_kinds.append(k)
        check("每个文档 kind 都能成功写入（不崩）",
              len(crash_kinds) == 0,
              detail=f"写入崩溃的 kind: {crash_kinds}" if crash_kinds else "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 4. 检索状态过滤 ----
    print("\n--- 4. 检索状态过滤 ---")
    # search 里必须只返回 status==active，避免 pending/superseded 泄漏
    has_active_filter = re.search(r'status"\)\s*==\s*"active"', wp_src) or \
                        re.search(r"get\(.status.\)\s*!=\s*.active.", wp_src)
    check("cmd_search 过滤 status==active（pending/superseded 不泄漏）",
          bool(has_active_filter))

    # ---- 5. 锁反模式 ----
    print("\n--- 5. 并发锁 ---")
    # file_unlock 内不应 os.unlink 锁文件
    unlock_block = ""
    m = re.search(r"def file_unlock\(.*?(?=\ndef |\Z)", wp_src, re.S)
    if m:
        unlock_block = m.group(0)
    bad_lock = 'os.unlink' in unlock_block and 'lock' in unlock_block
    check("file_unlock 不 unlink 锁文件（保持 flock 互斥）", not bad_lock,
          detail="发现 os.unlink 锁文件，并发互斥会失效" if bad_lock else "")

    # ---- 6. 双源原语 ----
    print("\n--- 6. 双源原语 ---")
    check("有 forget 命令（双向遗忘）", 'cmd_forget' in wp_src or '"forget"' in wp_src)
    check("有 self_inferred / pending 源处理（防幻觉固化）",
          'self_inferred' in wp_src and 'pending' in wp_src, level="WARN")
    if mm:
        mm_src = open(mm, encoding="utf-8").read()
        check("merge_migrate 有字段校验 + file_invalid 报告",
              'file_invalid' in mm_src and 'ALLOWED_KINDS' in mm_src, level="WARN")

    # ---- 7. install.sh 自检覆盖 ----
    print("\n--- 7. install.sh 自检覆盖 ---")
    if os.path.isfile(install_sh):
        sh = open(install_sh, encoding="utf-8").read()
        check("install.sh 跑 v2.8 双源自检 test_dual_source.py",
              'test_dual_source.py' in sh, level="WARN",
              detail="install.sh 未跑双源自检 test_dual_source.py，v2.6 逻辑装完不验" if 'test_dual_source.py' not in sh else "")
    else:
        check("install.sh 存在", False, level="WARN", detail="无 install.sh")

    # ---- 结论 ----
    print(f"\n===== 结果：{PASS} PASS / {FAIL} FAIL / {WARN} WARN =====")
    if FAIL > 0:
        print("结论：存在 FAIL → 加载/运行不会顺利，先修 FAIL 项。")
        sys.exit(1)
    elif WARN > 0:
        print("结论：可运行（有 WARN 待打磨，不阻塞）。")
        sys.exit(0)
    else:
        print("结论：干净，顺利跑通。")
        sys.exit(0)


if __name__ == "__main__":
    main()
