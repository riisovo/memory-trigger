#!/usr/bin/env bash
# setup_recall_env.sh — 主动回忆 daemon 一键部署（幂等，全程可见进度）
# 作用：建自包含 venv、装 fastembed+numpy、复制模型缓存到稳定目录、加载 launchd 守护、健康检查。
# 运行：bash /Users/xiaozuzong/.hermes/agent-hooks/setup_recall_env.sh
# 说明：装 fastembed 会下载 onnxruntime/tokenizers 等依赖（几十 MB，1–3 分钟），请耐心等待，勿中断。
set -uo pipefail

HERMES=/Users/xiaozuzong/.hermes
HOOKS=$HERMES/agent-hooks
VENV=$HOOKS/venv
PLIST=$HOOKS/com.riis.hermes-recall-daemon.plist
STABLE_CACHE=/Users/xiaozuzong/.cache/fastembed_cache

fail() { echo ""; echo "✗ 出错：$1"; echo "  把上面整段输出发给我即可。"; exit 1; }

echo "==> [1/5] 选择 python"
PY=$(command -v python3 || true)
[ -n "$PY" ] || fail "找不到 python3"
echo "    使用 $PY ($($PY --version 2>&1))"

echo "==> [2/5] 建 venv + 装依赖（首次要下载，可能 1–3 分钟）"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV" || fail "创建 venv 失败"
fi
"$VENV/bin/pip" install --upgrade pip || fail "升级 pip 失败"
"$VENV/bin/pip" install fastembed numpy || fail "装 fastembed/numpy 失败（网络问题？重跑一次即可）"
"$VENV/bin/python" -c "import fastembed, numpy; print('    ✓ fastembed', fastembed.__version__, 'numpy ok')" || fail "fastembed import 失败"

echo "==> [3/5] 模型缓存（离线，需已存在一份 bge 模型）"
SRCC=""
for cand in "$STABLE_CACHE" "$HOME/.cache/fastembed_cache" "$(python3 -c 'import tempfile,os;print(os.path.join(tempfile.gettempdir(),"fastembed_cache"))' 2>/dev/null || true)"; do
  if [ -n "$cand" ] && [ -d "$cand/models--Qdrant--bge-small-zh-v1.5" ]; then SRCC="$cand"; break; fi
done
if [ -z "$SRCC" ]; then
  echo "    ⚠ 没找到已缓存的 bge 模型。若 daemon 启动报模型加载失败，把模型放到 $STABLE_CACHE 再重跑。"
else
  mkdir -p "$STABLE_CACHE"
  if [ "$SRCC" != "$STABLE_CACHE" ]; then cp -R "$SRCC"/. "$STABLE_CACHE"/; fi
  echo "    ✓ 模型缓存就绪: $STABLE_CACHE"
fi

echo "==> [4/5] 加载 launchd 守护（KeepAlive 自愈）"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST" || fail "launchctl load 失败"
sleep 2

echo "==> [5/5] 健康检查"
if [ -S "$HERMES/agent-hooks/recall.sock" ]; then
  PONG=$("$VENV/bin/python" - "$HERMES/agent-hooks/recall.sock" <<'PY' 2>/dev/null || true
import socket, json, struct, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(2.0)
s.connect(sys.argv[1])
s.sendall(struct.pack(">I", len(b'{"ping":true}')) + b'{"ping":true}')
hdr = s.recv(4); n = struct.unpack(">I", hdr)[0]; body = b""
while len(body) < n: body += s.recv(n-len(body))
print(body.decode())
PY
)
  echo "    ✓ 守护响应: ${PONG:-（无响应，见日志）}"
else
  echo "    ⚠ socket 未建立，查看 $HOOKS/recall_daemon.err.log"
fi
echo ""
echo "✓ 完成。守护日志见 $HOOKS/recall_daemon.out.log / recall.sock.log"
