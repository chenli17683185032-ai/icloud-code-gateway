#!/bin/zsh
# 无 Docker 本地 control 启动器：创建/HME Session 在本机，验证码接码在云端 edge。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME_ROOT="${HOME}/.icloud-code-gateway"
DATA_DIR="${RUNTIME_ROOT}/data"
PROFILE_DIR="${RUNTIME_ROOT}/browser-profile"
LOG_DIR="${RUNTIME_ROOT}/logs"
PID_FILE="${RUNTIME_ROOT}/control.pid"
LOG_FILE="${LOG_DIR}/control.log"
ENV_FILE="${PROJECT_DIR}/.env"
CREDS_FILE="${HOME}/Desktop/云贝/服务器相关/icloud-control-plane.env"
VENV_PY="${PROJECT_DIR}/.venv/bin/python"
APP_HOST="127.0.0.1"
APP_PORT="18081"
ADMIN_URL="http://${APP_HOST}:${APP_PORT}/admin"
EDGE_URL="https://icloud.yunbay.xyz"
HME_PROXY_DEFAULT="socks5h://127.0.0.1:7897"

echo "========================================"
echo "  本地 iCloud 控制台（无 Docker / control）"
echo "========================================"
echo "创建 / Session：本地"
echo "验证码接码：云端 ${EDGE_URL}"
echo "数据目录：${DATA_DIR}"
echo "浏览器 profile：${PROFILE_DIR}"
echo

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "错误：找不到项目目录：${PROJECT_DIR}"
  exit 1
fi

mkdir -p "$DATA_DIR" "$PROFILE_DIR" "$LOG_DIR"
chmod 700 "$RUNTIME_ROOT" "$DATA_DIR" "$PROFILE_DIR" "$LOG_DIR" 2>/dev/null || true

read_env() {
  local key="$1"
  local file="$2"
  python3 - "$key" "$file" <<'PY'
import sys
from pathlib import Path
key, path = sys.argv[1], Path(sys.argv[2])
if not path.is_file():
    raise SystemExit(0)
for line in path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k == key:
        print(v)
        break
PY
}

ensure_venv() {
  if [[ -x "$VENV_PY" ]]; then
    return 0
  fi
  echo "首次准备 Python 环境..."
  if command -v uv >/dev/null 2>&1; then
    (cd "$PROJECT_DIR" && uv sync)
  else
    python3 -m venv "${PROJECT_DIR}/.venv"
    "${PROJECT_DIR}/.venv/bin/pip" install -U pip
    "${PROJECT_DIR}/.venv/bin/pip" install -e "${PROJECT_DIR}"
  fi
  if [[ ! -x "$VENV_PY" ]]; then
    echo "错误：无法创建虚拟环境 ${PROJECT_DIR}/.venv"
    exit 1
  fi
}

ensure_playwright() {
  # 只检查浏览器文件是否已安装；不要在启动阶段真的 launch Chromium
  # （本机沙箱/权限策略下 launch 可能被直接 SIGKILL）。
  if "$VENV_PY" - <<'PY' >/dev/null 2>&1
from pathlib import Path
from playwright._impl._driver import compute_driver_executable
from playwright.sync_api import sync_playwright

# Prefer explicit cache path check (works offline and without launching).
cache = Path.home() / "Library" / "Caches" / "ms-playwright"
candidates = list(cache.glob("chromium-*/chrome-mac*/Google Chrome for Testing.app"))
if candidates:
    raise SystemExit(0)
# Fallback: ask Playwright for executable path without launching.
with sync_playwright() as p:
    path = Path(p.chromium.executable_path)
    if path.exists():
        raise SystemExit(0)
raise SystemExit(1)
PY
  then
    return 0
  fi
  echo "安装 Playwright Chromium（仅首次）..."
  "$VENV_PY" -m playwright install chromium
}

export_required() {
  local name="$1"
  local value="$2"
  if [[ -z "${value}" ]]; then
    echo "错误：缺少 ${name}"
    exit 1
  fi
  export "${name}=${value}"
}

is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

health_ok() {
  curl -fsS --noproxy '*' "http://${APP_HOST}:${APP_PORT}/healthz" >/dev/null 2>&1
}

ensure_venv
ensure_playwright

MASTER_KEY="$(read_env LOCAL_ICLOUD_GATEWAY_MASTER_KEY "$CREDS_FILE" || true)"
CONTROL_TOKEN="$(read_env ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN "$CREDS_FILE" || true)"

if [[ -z "$MASTER_KEY" ]]; then
  MASTER_KEY="$(read_env ICLOUD_GATEWAY_MASTER_KEY "$ENV_FILE" || true)"
fi
if [[ -z "$CONTROL_TOKEN" ]]; then
  CONTROL_TOKEN="$(read_env ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN "$ENV_FILE" || true)"
fi

export_required ICLOUD_GATEWAY_MASTER_KEY "$MASTER_KEY"
export_required ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN "$CONTROL_TOKEN"
# 本地 control：免管理员密码（线上 edge 不启用此开关）
export ICLOUD_GATEWAY_ADMIN_OPEN=1
# 占位即可，open 模式下不会校验密码
export ICLOUD_GATEWAY_ADMIN_PASSWORD="${ICLOUD_GATEWAY_ADMIN_PASSWORD:-local-open-no-password}"

# 固定本机持久化路径：进程重启后 session/profile 仍在
export ICLOUD_GATEWAY_DATA_DIR="$DATA_DIR"
export ICLOUD_GATEWAY_BROWSER_PROFILE_DIR="$PROFILE_DIR"
export ICLOUD_GATEWAY_DEPLOYMENT_MODE=control
export ICLOUD_GATEWAY_EDGE_BASE_URL="$EDGE_URL"
export ICLOUD_GATEWAY_EDGE_SYNC_ENABLED=1
export ICLOUD_GATEWAY_EDGE_TIMEOUT_SECONDS=20
export ICLOUD_GATEWAY_PUBLIC_BASE_URL="$EDGE_URL"
export ICLOUD_GATEWAY_COOKIE_SECURE=0
export ICLOUD_GATEWAY_TRUSTED_HOSTS="localhost,127.0.0.1"
export ICLOUD_GATEWAY_HOST="$APP_HOST"
export ICLOUD_GATEWAY_PORT="$APP_PORT"
export ICLOUD_GATEWAY_LOG_LEVEL=INFO
export ICLOUD_GATEWAY_ALIAS_BATCH_LIMIT=50
# 无 Docker 不走远程 CDP；清空避免误连 docker 主机名
export ICLOUD_GATEWAY_CDP_URL=""
# 本机 Clash 回国代理（HME + 云端 edge 同步共用；本机直连 Cloudflare 常失败）
export ICLOUD_GATEWAY_HME_PROXY_SERVER="${ICLOUD_GATEWAY_HME_PROXY_SERVER:-$HME_PROXY_DEFAULT}"
export ICLOUD_GATEWAY_HME_PROXY_REQUIRED="${ICLOUD_GATEWAY_HME_PROXY_REQUIRED:-0}"

if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if is_running "${OLD_PID:-}"; then
    if health_ok; then
      echo "本地 control 已在运行 (pid=${OLD_PID})."
      echo "管理页：${ADMIN_URL}"
      if command -v open >/dev/null 2>&1; then
        open "$ADMIN_URL" || true
      fi
      exit 0
    fi
    echo "旧进程无响应，正在重启 (pid=${OLD_PID})..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

if command -v lsof >/dev/null 2>&1; then
  if lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "错误：端口 ${APP_PORT} 已被占用，且不在本脚本管理范围。"
    lsof -nP -iTCP:"$APP_PORT" -sTCP:LISTEN || true
    exit 1
  fi
fi

# 清理可能残留的 Chromium profile 锁（异常退出时）
rm -f \
  "${PROFILE_DIR}/SingletonLock" \
  "${PROFILE_DIR}/SingletonCookie" \
  "${PROFILE_DIR}/SingletonSocket" \
  "${PROFILE_DIR}/.gateway-browser.lock" 2>/dev/null || true

echo "启动 control 服务..."
# 源码目录直接运行，避免依赖 editable install；并强制无缓冲日志。
export PYTHONPATH="${PROJECT_DIR}${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUNBUFFERED=1
# 双 fork 守护进程，避免关闭终端/脚本会话后服务被一起带走。
"$VENV_PY" - "$PROJECT_DIR" "$PID_FILE" "$LOG_FILE" "$VENV_PY" <<'PY'
import os
import sys
from pathlib import Path

project = Path(sys.argv[1])
pid_file = Path(sys.argv[2])
log_file = Path(sys.argv[3])
python = Path(sys.argv[4])

if os.fork() > 0:
    raise SystemExit(0)
os.setsid()
if os.fork() > 0:
    raise SystemExit(0)

os.chdir(str(project))
os.environ["PYTHONPATH"] = str(project) + (
    ":" + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
)
os.environ["PYTHONUNBUFFERED"] = "1"

log_file.parent.mkdir(parents=True, exist_ok=True)
log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
os.dup2(log_fd, 1)
os.dup2(log_fd, 2)
os.close(log_fd)
devnull = os.open(os.devnull, os.O_RDONLY)
os.dup2(devnull, 0)
os.close(devnull)

pid_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
os.execv(str(python), [str(python), "-u", "-m", "icloud_gateway.main"])
PY

# 给守护进程写出 pid 的时间窗口
sleep 1.0
NEW_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -z "${NEW_PID}" ]] || ! kill -0 "$NEW_PID" 2>/dev/null; then
  echo "错误：control 进程启动后立刻退出 (pid=${NEW_PID:-none})。"
  echo "---- 日志尾部 ----"
  tail -n 80 "$LOG_FILE" || true
  exit 1
fi

echo "等待管理服务就绪..."
ok=0
for _ in {1..40}; do
  if health_ok; then
    ok=1
    break
  fi
  sleep 0.5
done

if (( ok == 0 )); then
  echo "错误：本地管理服务未在预期时间内就绪。"
  echo "日志：${LOG_FILE}"
  if [[ -f "$PID_FILE" ]]; then
    tail -n 80 "$LOG_FILE" || true
  fi
  exit 1
fi

PID="$(cat "$PID_FILE")"
echo
echo "本地服务已就绪（无 Docker）。"
echo "管理页：     ${ADMIN_URL}"
echo "进程 pid：   ${PID}"
echo "日志：       ${LOG_FILE}"
echo "Session 库： ${DATA_DIR}/gateway.sqlite3"
echo "浏览器目录： ${PROFILE_DIR}"
echo "管理员密码： 本地已关闭（ADMIN_OPEN=1）"
echo "VNC 密码：   无 Docker 不再使用 noVNC"
echo
echo "使用流程："
echo "1. 打开管理页（无需密码）"
echo "2. 点击「登录更新」，本机会弹出持久 Chromium"
echo "3. 在弹出窗口完成 Apple 登录，自动捕获 Session"
echo "4. 本地创建隐藏邮箱（自动同步云端）"
echo "5. 云端取码：${EDGE_URL}"
echo
echo "停止服务："
echo "  kill \$(cat ${PID_FILE})"
echo

if command -v open >/dev/null 2>&1; then
  open "$ADMIN_URL" || true
fi
