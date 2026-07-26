# iCloud Code Gateway 运维手册

本文件是项目唯一的部署与运维记录。后续每次服务器上线、迁移、代理变更、备份恢复演练和事故处理都追加到“运维记录”，不要另建同类手册。

## 1. 生产边界

- `app`：FastAPI 与 SQLite，非 root，根文件系统只读，数据卷为 `icloud-code-gateway_gateway-data`。
- `browser`：独立 Chromium/Xvfb/noVNC，固定非 root UID 102，profile 卷为 `icloud-code-gateway_browser-data`。
- `caddy`：公网 80/443、自动 HTTPS、管理员认证后的 noVNC 反向代理。
- 原始 noVNC：只监听宿主 `127.0.0.1:${BROWSER_NOVNC_PORT}`。
- CDP：只 `expose` 给 Docker 网络，不发布宿主端口。
- iCloud browser 与 HME API：共用 `CN_PROXY_*`；`CN_PROXY_REQUIRED=1` 时配置缺失或代理故障均失败关闭。
- IMAP：在管理页单独配置；是否走代理按邮箱可达性决定，不自动继承 HME 代理。

数据库中的密文依赖 `ICLOUD_GATEWAY_MASTER_KEY`。丢失该主密钥时，数据库备份无法恢复 Apple Session、IMAP 密码和 Alias 远端 ID。

## 2. 首次部署

### 2.1 服务器准备

1. 域名 A/AAAA 记录指向服务器。
2. 防火墙只对公网开放 TCP 80/443；如启用 HTTP/3，再开放 UDP 443。
3. 保持系统 NTP 正常。验证码窗口依赖准确时间。
4. 安装 Docker Engine 与 Compose v2。
5. 从私有 GitHub 仓库拉取项目，不把 `.env` 提交到 Git。

### 2.2 Secret 文件

```bash
cp .env.example .env
chmod 600 .env
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
openssl rand -base64 32
```

第一条随机输出用于 `ICLOUD_GATEWAY_MASTER_KEY`。分别生成并保存至少 16 字符的管理员密码和至少 12 字符的 VNC 密码。

生产必填项：

```dotenv
GATEWAY_DOMAIN=codes.example.com
ICLOUD_GATEWAY_PUBLIC_BASE_URL=https://codes.example.com
ICLOUD_GATEWAY_TRUSTED_HOSTS=codes.example.com
ICLOUD_GATEWAY_COOKIE_SECURE=1
ICLOUD_GATEWAY_MASTER_KEY=<32-byte-urlsafe-base64>
ICLOUD_GATEWAY_ADMIN_PASSWORD=<long-random-password>
BROWSER_VNC_PASSWORD=<different-long-random-password>
BROWSER_NOVNC_PORT=6080
CN_PROXY_SERVER=socks5h://proxy.example.com:1080
CN_PROXY_USERNAME=<optional>
CN_PROXY_PASSWORD=<optional>
CN_PROXY_REQUIRED=1
```

代理支持 `http://`、`socks5://` 和 `socks5h://`，必须显式填写端口。带密码时必须同时提供用户名。推荐 `socks5h://`，让域名解析也在代理端完成。

### 2.3 构建与上线

按顺序构建，避免同时生成多份大型 Chromium 临时层：

```bash
docker compose build app
docker compose build browser
docker compose up -d
docker compose ps
```

验收：

```bash
curl -fsS https://codes.example.com/healthz
docker compose exec browser id
docker compose exec browser stat -c '%u:%g %a %n' /browser-data/profile
docker compose port browser 6080
docker compose port browser 9222
```

预期：`/healthz` 返回 `{"status":"ok"}`；browser 用户 UID 为 102；6080 只映射到 `127.0.0.1`；9222 没有宿主映射。

## 3. 回国代理验收

浏览器出口（通过现有 CDP 打开一次临时页面，随后立即关闭）：

```bash
docker compose exec app python - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp("http://browser:9222")
    page = browser.contexts[0].new_page()
    page.goto("http://ip-api.com/line/?fields=query", wait_until="domcontentloaded", timeout=20_000)
    print(page.locator("body").inner_text().strip())
    page.close()
PY
```

HME API 使用的出口：

```bash
docker compose exec app python -c "from icloud_gateway.config import Settings; import requests; p=Settings.from_environment().hme_proxy; s=requests.Session(); s.trust_env=False; print(s.get('https://api.ipify.org', proxies={'http':p,'https':p}, timeout=15).text)"
```

两者应显示预期的回国代理出口，而不是德国服务器公网 IP。不要把代理 URL 或认证信息打印到日志。

失败关闭测试应在维护窗口用隔离容器执行，不能改动正式 `.env`：

```bash
docker run --rm --name icg-proxy-failclosed-check \
  -e BROWSER_PROXY_REQUIRED=1 \
  -e BROWSER_PROXY_SERVER=socks5h://127.0.0.1:9 \
  --entrypoint bash icloud-code-gateway-browser \
  -lc 'proxy=$(python3 /usr/local/lib/icloud-browser/proxy.py /tmp/proxychains.conf); timeout 15s /ms-playwright/chromium-*/chrome-linux64/chrome --headless=new --no-sandbox --proxy-server="$proxy" --dump-dom https://www.icloud.com.cn/ 2>/dev/null | grep -q ERR_PROXY_CONNECTION_FAILED'
```

预期为连接失败；成功访问反而表示验收失败。

### 3.1 云贝共享服务器

云贝服务器上的联动小铺代理只绑定其网络命名空间回环地址，不能让 iCloud app/browser 直接加入该命名空间，否则双方重启边界会耦合。生产采用以下方式：

- 只读复用 `/opt/new-api/secrets/ldxp-browser-proxy.yaml` 中的代理订阅与规则。
- 复制为 `/opt/new-api/icloud-code-gateway/secrets/cn-proxy.yaml`，权限 `0600`。
- 在副本中设置 `allow-lan: true`、`bind-address: 0.0.0.0`；该容器不发布宿主端口，只加入项目私有 `gateway` 网络。
- `CN_PROXY_SERVER=socks5h://cn-proxy:7891`，app 与 browser 使用同一无认证端点；browser 启动时将其解析为 Chromium 原生 `socks5://<container-ip>:7891`，HME API 保留 `socks5h` 语义。
- 联动小铺现有 proxy/worker 容器、配置文件和数据目录不修改、不重启。

共享服务器使用覆盖文件：

```bash
docker compose -f docker-compose.yml -f docker-compose.server.yml build app
docker compose -f docker-compose.yml -f docker-compose.server.yml build browser
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d cn-proxy browser app
```

覆盖文件把项目内置 `caddy` 放入 `standalone-caddy` profile，因此不会与现有 80/443 冲突。把 `deploy/Caddyfile.icloud.yunbay.xyz` 追加到现有 Caddyfile，先执行 `caddy validate`，再优雅 reload。现有 Caddy 与 app/browser 通过 `app_yunbay-network` 上的唯一别名通信。

## 4. 在线 iCloud 维护

1. 登录 `https://<domain>/admin/login`。
2. 点击“打开 iCloud 浏览器”。该链接走 `/admin/browser/*`，页面、静态资源和 WebSocket 均需有效管理员会话。
3. noVNC 会再次要求 `BROWSER_VNC_PASSWORD`。管理员密码和 VNC 密码必须不同。
4. 在画面内人工登录 iCloud。Apple ID 密码、2FA 和恢复信息只输入 Apple 页面，不录入本系统。
5. 回到管理页点击“开始捕获”，在同一浏览器访问 iCloud+ 隐藏邮件页面，直到状态变为“已捕获”。
6. 执行 HME 同步，只读确认 Session 可用。

浏览器容器、app 容器或服务器重启后仍使用同一个 profile。iCloud 显示离线或 HME Session 失效时，重复上述流程；不要删除 browser-data 卷，也不要创建第二个同时占用该卷的 browser 容器。

公网入口不可用时，可通过 SSH 隧道访问原始 noVNC 作为恢复通道：

```bash
ssh -L 6080:127.0.0.1:6080 <user>@<server>
```

随后打开 `http://127.0.0.1:6080/vnc.html`。该备用入口仍要求 VNC 密码，但不经过管理员网站认证，因此只允许经 SSH 隧道使用。

## 5. 日常健康检查

```bash
docker compose ps
curl -fsS https://<domain>/healthz
docker compose logs --tail 100 app
docker compose logs --tail 100 browser
docker compose logs --tail 100 caddy
```

判断顺序：

1. 先确认 app 与 browser 容器健康。
2. 再确认 browser 内 `http://127.0.0.1:9223/json/version` 可达。
3. 再确认代理出口和 iCloud 页面。
4. 最后确认 HME Session 与 IMAP 凭据。

上游 Apple、代理或 IMAP 故障不会使本地 `/healthz` 失败；管理页会显示相应可恢复状态。

## 6. 备份

先创建仅管理员可读的备份目录：

```bash
mkdir -p backups
chmod 700 backups
```

### 6.1 SQLite 在线备份

```bash
docker compose exec -T app python -c "import sqlite3; src=sqlite3.connect('/data/gateway.sqlite3'); dst=sqlite3.connect('/data/gateway.backup.sqlite3'); src.backup(dst); dst.close(); src.close()"
docker cp "$(docker compose ps -q app):/data/gateway.backup.sqlite3" backups/gateway.sqlite3
docker compose exec -T app rm -f /data/gateway.backup.sqlite3
python3 -c "import sqlite3; c=sqlite3.connect('backups/gateway.sqlite3'); print(c.execute('pragma quick_check').fetchone()[0])"
```

预期输出 `ok`。

### 6.2 Chromium profile 一致性备份

profile 包含高权限 Apple Cookie，备份文件必须按 Secret 管理。为了得到一致快照，短暂停止 browser：

```bash
docker compose stop browser
docker run --rm --user 0 \
  -v icloud-code-gateway_browser-data:/source:ro \
  -v "$PWD/backups:/backup" \
  caddy:2.10-alpine tar -C /source -czf /backup/browser-profile.tgz .
docker compose up -d browser
docker compose ps browser
```

正常 profile 在常规规模下应在一分钟内完成。browser 恢复健康后再结束维护窗口。

### 6.3 校验与异地保存

```bash
shasum -a 256 backups/gateway.sqlite3 backups/browser-profile.tgz > backups/SHA256SUMS
chmod 600 backups/gateway.sqlite3 backups/browser-profile.tgz backups/SHA256SUMS
```

必须另外安全保存 `.env` 中的主密钥和密码；不要把 `.env` 放入源码仓库或普通对象存储。

## 7. 恢复

恢复前先保存当前数据，避免把“恢复错误”变成不可逆覆盖。

### 7.1 SQLite

```bash
docker compose stop app
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backup:ro" \
  --entrypoint python app \
  -c "import os,sqlite3; src=sqlite3.connect('file:/backup/gateway.sqlite3?mode=ro',uri=True); dst=sqlite3.connect('/data/gateway.restore.sqlite3'); src.backup(dst); dst.close(); src.close(); os.replace('/data/gateway.restore.sqlite3','/data/gateway.sqlite3'); [os.remove(p) for p in ('/data/gateway.sqlite3-wal','/data/gateway.sqlite3-shm') if os.path.exists(p)]"
docker compose up -d app
docker compose exec app python -c "import sqlite3; print(sqlite3.connect('/data/gateway.sqlite3').execute('pragma quick_check').fetchone()[0])"
```

### 7.2 Chromium profile

确保 browser 已停止且没有其他容器占用该卷：

```bash
docker compose stop browser
docker compose run --rm --no-deps \
  -v "$PWD/backups:/backup:ro" \
  --entrypoint bash browser \
  -lc 'find /browser-data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar -C /browser-data -xzf /backup/browser-profile.tgz'
docker compose up -d browser
docker compose ps browser
```

恢复后检查浏览器健康、iCloud 登录状态、HME list 和一个只读 IMAP 测试。不要用恢复动作撤销已经在 Apple 远端成功创建的 Alias；以 HME list 同步对账。

## 8. 更新与回滚

更新前完成 SQLite 在线备份。按顺序构建，验证新镜像后只替换需要更新的服务：

```bash
git pull --ff-only origin main
docker compose build app
docker compose build browser
docker compose up -d --no-deps app
docker compose up -d --no-deps browser
docker compose up -d --no-deps caddy
docker compose ps
```

数据库和 profile 卷不得随容器替换删除。60 秒内健康检查不恢复时，切回上一个 Git 标签/镜像并重新 `up -d`；只有迁移或数据损坏时才恢复备份。

## 9. 密钥轮换

- Alias 访问密钥：在管理页轮换，旧密钥立即失效。
- 管理员密码/VNC 密码：修改 `.env` 后重建对应容器；不要复用两者。
- 回国代理凭据：修改 `.env` 后重建 app 和 browser，并重新验证两个出口。
- 主密钥：不能只替换环境值。需要先实现/执行数据库密文重加密迁移；直接替换会使已有密文不可读。

## 10. 运维记录

- 2026-07-26：完成本地容器闭环。确认复用 Playwright 基础镜像层、独立 Chromium/profile；旧 profile 从 UID 102 无损接管。故障注入后 browser 自动重启，app 未重启，Cookie 与 SQLite Alias 保持。
- 2026-07-26：发现并修复 Caddy `forward_auth` 携带 WebSocket Upgrade 头导致 403 的问题。验收结果为：未登录 WebSocket 303 拒绝，登录后 101 Upgrade 并收到 RFB 3.8 握手。
- 2026-07-26：使用无效代理端点执行隔离容器测试，连接失败且没有直连降级。
