#!/usr/bin/env bash
# install.sh —— memory-trigger 记忆技能「一键加载即用」安装器
# 别人从 GitHub clone 后只需: bash install.sh
# 会自动: 跑引擎冒烟 + 双源自检（纯标准库，零额外依赖）。
# 本模板默认本地文件模式，零依赖；想要语义检索/关系时间线见 SKILL.md §9 接 mcp-memory-graph。
# 路径全部基于仓库位置动态计算，绝不写死任何机器路径。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REFS="$HERE/references"

echo "== memory-trigger 安装 =="

# 1) 选 python3（纯标准库，无需 venv / 无需 pip install）
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || { echo "[ERR] 找不到 python3，请先安装 Python 3.10+"; exit 1; }
echo "-- python: $("$PY" --version 2>&1)"

# 2) 引擎冒烟 + 双源自检（验证随包代码没坏；全部用系统 python3，零依赖）
echo "-- 引擎冒烟: stats"
"$PY" "$REFS/write_pipeline_v2.6.py" stats "$REFS" >/dev/null
echo "-- 双源自检: test_dual_source.py"
"$PY" "$REFS/test_dual_source.py"

echo
echo "== 安装完成 =="
echo "默认本地文件模式，零依赖，clone 即用。"
echo "想要语义检索 / 关系时间线 / 自动梦境修剪，见 SKILL.md §9 接 mcp-memory-graph（可选，不在此脚本内自动注入）。"
