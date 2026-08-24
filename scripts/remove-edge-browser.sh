#!/bin/zsh
# One-time split-plane cleanup: remove a legacy cloud Chromium container.
# The persistent browser volume and image are deliberately preserved.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.server.yml)

deployment_mode="$(python3 - "$ENV_FILE" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = "edge"
if path.is_file():
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        if key.strip() == "ICLOUD_GATEWAY_DEPLOYMENT_MODE":
            value = candidate.strip().casefold()
            break
print(value)
PY
)"

if [[ "$deployment_mode" != "edge" ]]; then
  echo "拒绝清理：ICLOUD_GATEWAY_DEPLOYMENT_MODE 不是 edge。"
  exit 1
fi

cd "$PROJECT_DIR"
container_id="$("${COMPOSE[@]}" --profile legacy-browser ps -aq browser | head -n 1)"
if [[ -z "$container_id" ]]; then
  echo "VPS 上没有遗留 browser 容器，无需处理。"
  exit 0
fi

project_label="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$container_id")"
service_label="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.service" }}' "$container_id")"
if [[ "$project_label" != "icloud-code-gateway" || "$service_label" != "browser" ]]; then
  echo "拒绝清理：目标容器不属于 icloud-code-gateway/browser。"
  exit 1
fi

echo "正在停止遗留 browser 容器（最多等待 10 秒）..."
"${COMPOSE[@]}" --profile legacy-browser stop -t 10 browser
"${COMPOSE[@]}" --profile legacy-browser rm -f browser

if "${COMPOSE[@]}" --profile legacy-browser ps -aq browser | grep -q .; then
  echo "错误：browser 容器仍存在。"
  exit 1
fi

echo "遗留 browser 容器已移除；app/cn-proxy 未重启，browser-data 卷和镜像仍保留。"
