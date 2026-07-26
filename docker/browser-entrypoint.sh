#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -z "${BROWSER_VNC_PASSWORD:-}" || ${#BROWSER_VNC_PASSWORD} -lt 12 ]]; then
  printf '%s\n' "BROWSER_VNC_PASSWORD must contain at least 12 characters" >&2
  exit 1
fi

umask 077
mkdir -p /browser-data/profile /run/icloud-browser

proxy_config=/run/icloud-browser/proxychains.conf
python3 /usr/local/lib/icloud-browser/proxy.py "$proxy_config"

browser_executable="${BROWSER_EXECUTABLE:-}"
if [[ -z "$browser_executable" ]]; then
  browser_candidates=(
    /ms-playwright/chromium-*/chrome-linux/chrome
    /ms-playwright/chromium-*/chrome-linux64/chrome
  )
  for candidate in "${browser_candidates[@]}"; do
    if [[ -x "$candidate" ]]; then
      browser_executable="$candidate"
      break
    fi
  done
fi
if [[ -z "$browser_executable" || ! -x "$browser_executable" ]]; then
  printf '%s\n' "Playwright Chromium executable was not found" >&2
  exit 1
fi

exec 9>/browser-data/.gateway-browser.lock
if ! flock -n 9; then
  printf '%s\n' "browser profile is already in use by another container" >&2
  exit 1
fi
rm -f \
  /browser-data/profile/SingletonCookie \
  /browser-data/profile/SingletonLock \
  /browser-data/profile/SingletonSocket

x11vnc -storepasswd "$BROWSER_VNC_PASSWORD" /run/icloud-browser/vnc.pass >/dev/null

Xvfb "$DISPLAY" -screen 0 "$BROWSER_SCREEN" -nolisten tcp -ac \
  >/run/icloud-browser/xvfb.log 2>&1 &
xvfb_pid=$!

display_number="${DISPLAY#:}"
display_number="${display_number%%.*}"
for _ in {1..100}; do
  if [[ -S "/tmp/.X11-unix/X${display_number}" ]]; then
    break
  fi
  if ! kill -0 "$xvfb_pid" 2>/dev/null; then
    printf '%s\n' "Xvfb exited before the display became ready" >&2
    exit 1
  fi
  sleep 0.1
done
if [[ ! -S "/tmp/.X11-unix/X${display_number}" ]]; then
  printf '%s\n' "Xvfb display did not become ready within 10 seconds" >&2
  exit 1
fi

fluxbox >/run/icloud-browser/fluxbox.log 2>&1 &
fluxbox_pid=$!

x11vnc \
  -display "$DISPLAY" \
  -rfbport 5900 \
  -rfbauth /run/icloud-browser/vnc.pass \
  -forever \
  -shared \
  -localhost \
  -noxdamage \
  >/run/icloud-browser/x11vnc.log 2>&1 &
x11vnc_pid=$!

websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 \
  >/run/icloud-browser/websockify.log 2>&1 &
websockify_pid=$!

socat TCP-LISTEN:9222,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:9223 \
  >/run/icloud-browser/cdp-proxy.log 2>&1 &
cdp_proxy_pid=$!

browser_command=(
  "$browser_executable"
  "--display=$DISPLAY"
  "--remote-debugging-address=127.0.0.1"
  "--remote-debugging-port=9223"
  "--remote-allow-origins=*"
  "--user-data-dir=/browser-data/profile"
  "--disable-dev-shm-usage"
  "--no-sandbox"
  "--no-first-run"
  "--no-default-browser-check"
  "--password-store=basic"
  "--lang=zh-CN"
  "https://www.icloud.com.cn/icloudplus/"
)
if [[ -f "$proxy_config" ]]; then
  browser_command=(proxychains4 -q -f "$proxy_config" "${browser_command[@]}")
fi
"${browser_command[@]}" >/run/icloud-browser/chromium.log 2>&1 &
chromium_pid=$!

cleanup() {
  kill "$chromium_pid" "$cdp_proxy_pid" "$websockify_pid" "$x11vnc_pid" \
    "$fluxbox_pid" "$xvfb_pid" \
    2>/dev/null || true
  wait "$chromium_pid" "$cdp_proxy_pid" "$websockify_pid" "$x11vnc_pid" \
    "$fluxbox_pid" "$xvfb_pid" \
    2>/dev/null || true
}
trap cleanup EXIT INT TERM

set +e
wait -n "$chromium_pid" "$cdp_proxy_pid" "$websockify_pid" "$x11vnc_pid" \
  "$fluxbox_pid" "$xvfb_pid"
component_status=$?
set -e
printf '%s\n' "browser component exited unexpectedly with status ${component_status}" >&2
exit 1
