#!/usr/bin/env bash
set -euo pipefail

# memory-trigger 一键安装脚本
# 用法:
#   curl -fsSL https://raw.githubusercontent.com/riisovo/memory-trigger/main/install.sh | bash
#   npx github:riisovo/memory-trigger

# --- Python3 预检测（macOS 12+ 不自带 python3）---
PY=""
for candidate in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
  if command -v "$candidate" >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "错误：未找到 python3。"
  echo "  macOS 用户执行: brew install python"
  echo "  Linux 用户执行: sudo apt install python3  或  sudo yum install python3"
  exit 1
fi

REPO_URL="https://github.com/riisovo/memory-trigger.git"
SKILL_NAME="memory-trigger"
SKILLS_DIR=""

# 自动检测 skills 目录
detect_skills_dir() {
  # Marvis
  if [ -d "$HOME/.marvis/skills" ]; then
    SKILLS_DIR="$HOME/.marvis/skills"
    return
  fi
  # Claude Desktop (macOS)
  if [ -d "$HOME/Library/Application Support/Claude/skills" ]; then
    SKILLS_DIR="$HOME/Library/Application Support/Claude/skills"
    return
  fi
  # 通用兜底：用户可选
  echo "未检测到已知的 skills 目录，请手动指定："
  echo "  curl -fsSL ... | SKILLS_DIR=/你的/skills/路径 bash"
  exit 1
}

detect_skills_dir
TARGET="$SKILLS_DIR/$SKILL_NAME"

if [ -d "$TARGET" ]; then
  echo "已存在 $TARGET，正在更新..."
  cd "$TARGET" && git pull --ff-only
else
  echo "正在安装到 $TARGET..."
  git clone "$REPO_URL" "$TARGET"
fi

cd "$TARGET"

# 初始化本地文件结构
if [ ! -f "references/backend_config.json" ]; then
  echo "初始化记忆后端..."
  $PY references/write_pipeline.py init local
fi

# 跑自检
echo ""
echo "运行自检（v2.2 基础用例）..."
$PY test_ramen.py
echo ""
echo "运行自检（v2.6 双源用例）..."
$PY test_dual_source.py

echo ""
echo "=============================="
echo "  memory-trigger 安装完成！"
echo "  位置: $TARGET"
echo "  重启你的 AI / Agent 即可使用"
echo "=============================="
