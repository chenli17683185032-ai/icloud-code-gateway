# iCloud Code Gateway 运维手册

本文件是项目唯一的部署与运维记录。后续每次服务器上线、迁移、代理变更、备份恢复演练和事故处理都追加到“运维记录”，不要另建同类手册。

## 1. 生产边界

- `app`：FastAPI 与 SQLite，非 root，根文件系统只读，数据卷为 `icloud-code-gateway_gateway-data`。
- `browser`：独立 Chromium/Xvfb/noVNC，固定非 root UID 102，profile 卷为 `icloud-code-gateway_browser-data`。
- `caddy`：公网 80/443、自动 HTTPS、管理员认证后的 noVNC 反向代理。
- 原始 noVNC：只监听宿主 `127.0.0.1:${BROWSER_NOVNC_PORT}`。
- CDP：只 `expose` 给 Docker 网络，不发布宿主端口。
- iCloud browser 与 HME API：共用 `CN_PROXY_*`；`CN_PROXY_REQUIRED=1` 时配置缺失或代理故障均失败关闭。
- IMAP：在管理页单独配置；是否走代理按邮箱可达性决定，不自动继承 HME 代理。可选
  填写一个垃圾邮件文件夹，保存时会与主文件夹一并执行只读验证。

数据库中的密文依赖 `ICLOUD_GATEWAY_MASTER_KEY`。丢失该主密钥时，数据库备份无法恢复 Apple Session、IMAP 密码、Alias 远端 ID 和新版本签发的访问密钥明文。

## 2. 首次部署

### 2.1 服务器准备

1. 域名 A/AAAA 记录指向服务器。
2. 防火墙只对公网开放 TCP 80/443；如启用 HTTP/3，再开放 UDP 443。
3. 保持系统 NTP 正常。验证码窗口依赖准确时间。
4. 安装 Docker Engine 与 Compose v2。
5. 从私有 GitHub 仓库拉取项目，不把 `.env` 提交到 Git。

IMAP 文件夹名称必须使用服务端实际名称。应用会处理空格、引号、`&` 和 modified
UTF-7 编码，但不会猜测 `Junk`、`Spam` 或本地化名称。查询时两个文件夹共享一次登录
和同一总截止时间；任一文件夹暂时不可用时可由另一文件夹维持查询，全部不可用时失败
关闭。

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
from icloud_gateway.browser_capture import _resolve_cdp_endpoint
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    endpoint = _resolve_cdp_endpoint("http://browser:9222")
    browser = playwright.chromium.connect_over_cdp(endpoint)
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

覆盖文件把项目内置 `caddy` 放入 `standalone-caddy` profile，因此不会与现有 80/443 冲突。把 `deploy/Caddyfile.icloud.yunbay.xyz` 追加到现有 Caddyfile，先执行 `caddy validate`，再优雅 reload。现有 Caddy 与 app/browser 通过 `app_yunbay-network` 上的唯一别名通信；主动健康检查必须携带 `Host: icloud.yunbay.xyz`，否则应用的 Trusted Host 中间件会把 Docker 别名 Host 拒绝为 400。站点片段只在直连对端属于 Cloudflare 官方网段时采用 `CF-Connecting-IP`；部署前应与 `https://www.cloudflare.com/ips-v4` 和 `https://www.cloudflare.com/ips-v6` 核对网段，不能无条件信任该请求头。

## 4. 在线 iCloud 维护

1. 登录 `https://<domain>/admin/login`。
2. 点击“打开 iCloud 浏览器”。该链接走 `/admin/browser/*`，页面、静态资源和 WebSocket 均需有效管理员会话。
3. noVNC 会再次要求 `BROWSER_VNC_PASSWORD`。管理员密码和 VNC 密码必须不同。
4. 在画面内人工登录 iCloud。Apple ID 密码、2FA 和恢复信息只输入 Apple 页面，不录入本系统。
5. 回到管理页点击“开始捕获”，在同一浏览器访问 iCloud+ 隐藏邮件页面，直到状态变为“已捕获”。
6. 捕获成功会自动读取并导入当前 Apple 账户的完整 Alias 列表；点击“导入 / 刷新”可再次执行只读对账。

### 4.1 Alias 生命周期管理

- 活动 Alias 可以签发/轮换访问密钥，也可以远端停用。新签发或轮换的密钥同时保存 SHA-256 哈希与 AES-GCM 密文，管理员可点击眼睛图标显式查看；管理页初始 HTML 只包含末 4 位提示。
- 升级前已经存在的访问密钥只有不可逆哈希，页面显示“轮换后可查看”。系统不会自动轮换；管理员明确轮换后旧密钥立即失效。
- Apple 列表确认 `isActive=false` 后，本地才标记失活并清除该 Alias 的密钥哈希、提示和密文。
- 失活 Alias 可以恢复；Apple 列表确认 `isActive=true` 后，本地才恢复活动状态，恢复后仍需按需重新签发访问密钥。
- 永久删除只对失活 Alias 开放，并要求输入完整 Alias 邮箱。Apple 列表确认远端 ID 已消失后，本地记录才删除。
- 远端写请求不会自动重试。页面提示状态未确认时，先使用“导入 / 刷新”读取实际 Apple 状态，不要连续重复点击破坏性动作。
- 数据库备份只能恢复本地配置和密钥映射，不能撤销已经在 Apple 远端完成的停用、恢复或永久删除。

### 4.2 验证码查询记录

- 管理页“查询记录”只对已认证管理员开放，按最新事件倒序展示最近 7 天内最多 100 条 `code_lookup`。
- 页面显示查询时对应的 Alias 邮箱、结果、不可逆来源指纹和北京时间。无效 Key 无法归属 Alias，显示“未匹配邮箱”。
- 查询事件复制 Alias 的加密邮箱快照，因此 Alias 后续永久删除后，既有记录仍可辨识；旧事件会在兼容迁移时从仍存在的 Alias 幂等回填。
- 审计数据不保存验证码、访问 Key、邮件正文或原始客户端 IP，也不新增公开查询历史接口。
- 公开页等待验证码时可能每 5 秒轮询一次，因此同一 Alias 连续出现多条“暂无验证码”是正常反馈，不代表重复邮件或重复签发。

### 4.3 管理员验证码面板

- 管理页“验证码”栏目由管理员手动刷新，读取本地全部 Alias 最近 300 秒至未来 60 秒内的 6 位验证码，包括没有访问密钥和当前失活的 Alias。
- 一次刷新只使用一个 IMAP 会话；每个已配置文件夹各执行一次时间窗搜索，再在文件夹间
  公平分配同一个 500 封扫描预算并批量读取，最多返回 500 条；达到上限时页面明确提示
  截断。
- 验证码只存在于管理员专用 `no-store` JSON 响应和当前页面 DOM，不写入 SQLite、审计日志、服务器日志、Cookie 或浏览器存储。刷新前会清空上一批结果，离开页面后清除 DOM。
- `admin_code_scan` 审计只记录 `found`、`empty`、`truncated`、`busy` 或 IMAP 错误结果，不包含验证码、UID、主题、正文或发件人。
- 读取超时、IMAP 正忙或凭据失效时失败关闭。不要通过提高 500 条上限、为每个 Alias 单独连接或持久化验证码来规避上游故障。

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

更新前完成 SQLite 在线备份。升级到本版本时，先把新增的 `ICLOUD_GATEWAY_HME_MAINTENANCE_SECONDS`、`ICLOUD_GATEWAY_HME_FRESHNESS_SECONDS`、`ICLOUD_GATEWAY_HME_RETRY_MAX_SECONDS` 和 `ICLOUD_GATEWAY_ALIAS_BATCH_LIMIT` 从 `.env.example` 合并到服务器 `.env`，再按顺序构建，验证新镜像后只替换需要更新的服务：

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

- Alias 访问密钥：在管理页轮换，旧密钥立即失效；轮换后新密钥可由管理员显式查看。升级前 hash-only 密钥必须轮换后才能查看，无法反解恢复原值。
- 历史 Alias：HME Session 保存时自动导入；活动项可以直接签发 key，失活项必须先在 Apple 远端恢复。
- 管理员密码/VNC 密码：修改 `.env` 后重建对应容器；不要复用两者。
- 回国代理凭据：修改 `.env` 后重建 app 和 browser，并重新验证两个出口。
- 主密钥：不能只替换环境值。需要先实现/执行数据库密文重加密迁移；直接替换会使已有密文不可读。

## 10. 发布阻断：`400f484` 与后续修复

`3030701` 的第一层发布阻断已由 `400f484` 修复，但复审确认 `400f484` 本身仍不得部署。生产只能接收晚于该提交且同时满足以下条件的修复版本：

1. Alias lifecycle 写成功后只提交已经证明集合完整的同一份确认快照，不进行第二次 list；停用、恢复和删除均不能让无关 Alias 失活或丢失 key。
2. `queued -> running` 在单个 SQLite `BEGIN IMMEDIATE` 事务中原子完成；数据库旁的 worker owner 锁跨进程独占。只有 owner 执行恢复和远端副作用，阻塞线程未退出时不得释放锁。
3. `needs_reconcile` 持续出现在管理页恢复接口，逐项错误只返回规范化代码；远端结果不确定的项不自动重放，尚未开始的项保持 queued。
4. lifespan 先同时广播 job 和 Gateway stop，再使用同一个 10 秒 deadline 等待；stop 后不得开始 reserve/deactivate/reactivate/delete，任一后台线程未停时不得关闭 SQLite。
5. 两套 Compose 自定义环境展开、完整测试、Ruff check/format、Python/JS 语法、diff 和秘密扫描必须通过。切换前还必须在隔离数据库副本上验证迁移与任务恢复，候选不得连接生产数据库启动 worker。
6. 使用真实现有 Apple 会话只执行一次 setup validate 和一次 HME list 只读验收；不记录 Cookie、token、Apple ID、Alias 或响应正文，不执行 generate/reserve/deactivate/reactivate/delete。

真实只读协议验收、隔离候选或 60 秒 watchdog 任一失败时，保持或自动恢复生产 `67968b7` 镜像、旧源码和旧部署标记；普通代码回滚不恢复 SQLite。详细实施节点、回滚和测试矩阵见 `IMPLEMENTATION_PLAN.md` 第 23 节。

## 11. 运维记录

- 2026-07-26：完成本地容器闭环。确认复用 Playwright 基础镜像层、独立 Chromium/profile；旧 profile 从 UID 102 无损接管。故障注入后 browser 自动重启，app 未重启，Cookie 与 SQLite Alias 保持。
- 2026-07-26：发现并修复 Caddy `forward_auth` 携带 WebSocket Upgrade 头导致 403 的问题。验收结果为：未登录 WebSocket 303 拒绝，登录后 101 Upgrade 并收到 RFB 3.8 握手。
- 2026-07-26：使用无效代理端点执行隔离容器测试，连接失败且没有直连降级。
- 2026-07-26：生产 `icloud.yunbay.xyz` 已通过 Cloudflare 代理上线。共享 Caddyfile 备份为 `/opt/new-api/app/Caddyfile.bak-20260726T055403Z`，候选与挂载配置均通过 Caddy 2.11.4 验证后 graceful reload；Caddy、app、browser、cn-proxy 全程 `restart=0`。
- 2026-07-26：生产主动健康检查必须携带 `Host: icloud.yunbay.xyz`；实测该 Host 返回 200，而 `icloud-code-gateway-app` 返回 400。公网 `/healthz`、首页、管理员登录页为 200；未登录 noVNC 为 303；登录后认证探针为 204、noVNC 页面为 200、WebSocket 为 101 并收到 RFB 3.8。
- 2026-07-26：browser 使用 Chromium 原生代理，生产出口为 `116.31.164.94`（中国广东）；杀死 Chromium 后约 11 秒恢复且 app 未重启，持久 profile 停启 Cookie 测试通过。当前 profile 尚无真实 iCloud 登录 Cookie，首次 Apple 登录和 HME 捕获需由管理员在 noVNC 中完成。
- 2026-07-26：定向删除专用 `icg-builder-20260726`、`qa-f1d8c4a`、`qa-37adf3f`、本任务悬空 browser 镜像 `7af025cae297` 和候选临时件；保留正式/回滚镜像、三个正式卷、默认 builder 与其他项目镜像。清理前 `docker system df` 为 Images `61.54GB`、Volumes `4.965GB`、Build Cache `57.65GB`，根卷可用 `56GB`；清理后为 `61.54GB`、`1.02GB`、`57.65GB`，根卷可用 `59GB`。清理后 SQLite、CDP、公网健康与认证边界复测通过，所有容器仍 `restart=0`。
- 2026-07-26：本地审查 `82463cf` 安全与性能修订。确认 Caddy 默认已拒绝不可信 XFF，生产片段改为仅在直连对端命中 Cloudflare 官方网段时采用 `CF-Connecting-IP`；运行时注入证明直连伪造失败、可信链恢复真实客户端地址。另补齐 SQLite 提交失败回滚与 IMAP bytes 能力识别；全量 `104 passed`，Compose、Caddy 2.11.4 和服务器当前完整候选配置只读验证通过。审查修正 `f2f4d3c` 已快进并推送 GitHub `main`，本地临时分支、探针容器/网络和 Caddy 校验镜像已清理。本次未 reload、未部署生产服务。
- 2026-07-27：功能提交 `91369cf1c54fb5161b4cfc5f8953c95e94878ac2` 已部署。服务器 69 个跟踪文件逐项校验，`.env` 保持 `0600`；独立候选卷验收通过后，60 秒 watchdog 只替换 app，新容器 10 秒恢复健康且未触发回滚。新镜像 `sha256:906ffbefc34aff91ad762523cae37859b42c7d5a937acc3169e88edf865fe99e` 保留 `latest/prod/release-91369cf`，旧镜像 `sha256:36e201c7c852e3270cb3fbc3da16633e5d573d68c31fb837050b7ac1a0eeb204` 保留 `rollback-pre-91369cf-20260726T170444Z`。
- 2026-07-27：共享 Caddy 完整候选与挂载配置均通过运行中 2.11.4 验证，原位写入和 graceful reload 保持 inode、权限、容器 ID 与 `restart=0`；最终 SHA-256 为 `90dec04b2024eeabaa67f0dcde1d254e3ed02ce9f4bd6f9dc550f7c51f13c3f8`。直连伪造 XFF/`CF-Connecting-IP` 被丢弃，Cloudflare 链恢复真实出口 `202.8.9.242`；公网健康/首页/登录为 200，未登录 noVNC 303，中文错误口令 401，3 MiB 分块请求 413，小分块请求正常进入路由，SQLite/CDP 和安全头均通过。
- 2026-07-27：回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/hardening-20260726T170444Z-91369cf`，包含部署前 SQLite、Caddy、源码及前后元数据/watchdog 日志，最终清单哈希 `cbcc6b9b0d7a06b536454ea167253428ac58a31365536fca0b224dcff27bc0ed`。生产标记 `.icloud-code-gateway-deploy-sha` 已写入完整功能提交；browser、cn-proxy、Caddy 全程未重启，四容器最终均 `restart=0`。候选容器/卷/标签、部署锁和一次性文件已清理；CDP 9222 网络边界未改，也未执行 Apple/HME 远端写操作。
- 2026-07-27：历史 Alias 管理功能提交 `20f17f5907e03a8a8cdef25178c24a0b904a16f7` 已部署。保存/重新捕获 HME Session 后只读导入 Apple 当前完整列表，生产最终为 107 条 Alias、97 条活动、10 条失活、0 条已配 key；重复同步计数不变，SQLite `quick_check=ok`。管理页已显示全部记录，活动项可签发或轮换 key，失活项必须先恢复。
- 2026-07-27：部署前镜像 `sha256:906ffbefc34aff91ad762523cae37859b42c7d5a937acc3169e88edf865fe99e` 保留 `rollback-pre-20f17f5-20260726T184755Z`；新镜像 `sha256:52a53fadd5bca55a1db32209fa84ccb4513e37ffc7bcfe119e4f1c81d0138f28` 保留 `latest/prod/release-20f17f5`。60 秒 watchdog 只替换 app，10 秒恢复健康且未回滚；browser、cn-proxy、共享 Caddy 未重启，四容器最终均为 `healthy / restart=0 / OOM=false`。
- 2026-07-27：生产只执行 HME list 和本地幂等导入，未对真实 Alias 调用停用、恢复或永久删除，也未任意签发 key。部署标记已更新为完整功能提交；项目候选容器、卷和镜像均为 0，CDP 9222 网络边界、Caddy 和正式卷未改。回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/history-management-20260726T184755Z-20f17f5`，最终清单文件 SHA-256 为 `391758c46cfb520a26434463ce3395528eb004667892810d009694570b0ff5b4`，清单内全部文件复验通过。
- 2026-07-27：验证码查询记录功能提交 `bd1b4fe97897b47492263c9d1509e0aa3d35ea9a` 已部署，新 app 镜像为 `sha256:4944e85e1a65172c93454567516063867c44c3f2f0743a761bcf4e9ed82fa003`。60 秒 watchdog 只替换 app，约 10 秒恢复健康；browser、cn-proxy、共享 Caddy 未重启。生产保留原 17 条 `code_lookup`，迁移后 17 条均有加密 Alias 快照，管理页显示 17 行记录。
- 2026-07-27：回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/query-history-20260727T045045Z-bd1b4fe`。补充验收使用 `docker exec -i` 重建三份空证据，生产与隔离候选均为 `quick_check=ok` 且页面断言通过；最终清单 SHA-256 为 `38a2f2bc832681d8a4f6c54fc112e61c22aa0e797aa280666a258cf24ae22119`。候选容器、卷和远程脚本已清理，部署标记继续保持功能提交 `bd1b4fe`。
- 2026-07-27：管理页两次创建请求均返回 502，审计为 `alias_create=failed`；只读 HME list 同时返回 `HmeSessionError`，确认根因是已保存 Apple Web Session 过期。持久 Chromium 登录态仍有效，重新捕获后 Apple list 验证成功并自动对账：Alias 从 107 条收敛到 108 条（98 活动、10 失活），远端/本地 ID 集双向差异为 0，说明两次失败请求中一次已在 Apple 侧创建成功。恢复项未自动签发 key；刷新管理页后对该活动项手工签发，不要再次点击创建来补 key。本轮没有第三次 create 或任何停用、恢复、永久删除操作，四个正式容器最终均为 `healthy / restart=0 / OOM=false`。
- 2026-07-27：管理员密钥查看与全 Alias 验证码面板功能提交 `312e8ba809d5cf9799fb54780ebe6dc2902fa20f` 已部署，新 app 镜像为 `sha256:d2963e4f4330deab82d004927d88d13eb68581b645d4341bb8f535dc21ec1461`，固定为 `latest/prod/release-312e8ba`。60 秒 watchdog 只替换 app，约 22.4 秒恢复 healthy；250ms 公网采样最长连续非 200 为 16.243 秒，未触发回滚。browser、cn-proxy、共享 Caddy 容器 ID 不变，四容器最终均为 `healthy / restart=0 / OOM=false`。
- 2026-07-27：生产迁移后 SQLite `quick_check=ok`，115 条 Alias（105 活动、10 失活）、4 条既有 hash-only key 的哈希指纹和 17 条 `code_lookup` 均保持；新增密文列存在，既有 key 密文数为 0，旧 key 管理员 reveal 实测为 409，未自动轮换。管理员验证码接口实测扫描 44 封候选、返回 0 条、未截断，`admin_code_scan=empty` 且未保存验证码。回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/admin-key-codes-20260727T094750Z-312e8ba`，最终清单 SHA-256 为 `4fc2916f35fbbfdccd928577a5a9a0f06859797fb7fc0d5f4ad60386137e58ba`；旧镜像保留 `rollback-pre-312e8ba-20260727T094750Z`，候选容器/卷、release、候选标签和部署锁已清理，未执行 Apple/HME 写操作或制造验证码。
- 2026-07-31：验证码语境与可选 Junk 只读检索功能提交 `46c8bbb1515936119314cf899eaca6ad0b016255` 已部署。新 app 容器 `783385aad7d3dc0c62fa1694b57c222d71c98fedb068677369cc251cb1d05767` 使用镜像 `sha256:eb34a40827f3a046f430a4e150b7ef43be165135da41fc68cccd3ab50b14daa3`，固定为 `latest/prod/release-46c8bbb`。独立 watchdog 只替换 app，`19.121` 秒完成新镜像、内部/公网健康、源码清单和部署标记闭环且未回滚；该切换窗口作为公网不可用保守上界，后续本机 10 次和服务器复验均为 200。browser、cn-proxy、共享 Caddy 的容器 ID 不变，四容器最终均为 `healthy / restart=0 / OOM=false`。
- 2026-07-31：生产 SQLite `quick_check=ok`，159 条 Alias（148 活动、11 失活）、41 个 key hash、37 个 key 密文、262 条审计及 Alias/审计前缀/设置聚合指纹均守恒。旧加密 IMAP 配置成功加载且 Junk 默认为空，安装模板包含 Junk 字段，HTML/日志秘密命中和严重日志均为 0；本轮未连接真实邮箱读取邮件、未制造验证码、未执行 Apple/HME 写操作、未轮换访问密钥。
- 2026-07-31：回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/imap-junk-20260730T161519Z-46c8bbb`，SQLite 在线备份为 `ok`，34 个受限文件的最终清单 SHA-256 为 `6f160aaf1d89d69257153b1c485ebcb6f27e18bee4c4ae8a387bba8d42bcda57`。旧镜像 `sha256:d2963e4f4330deab82d004927d88d13eb68581b645d4341bb8f535dc21ec1461` 只保留 `rollback-pre-46c8bbb-20260730T161519Z`；候选容器/卷/标签、旧 release 标签、临时源码、一次性脚本和部署锁均已清理，`.env` 保持 `0600`，根卷可用 45GB。
- 2026-07-31：只读诊断 10:30 验证码未返回事故。10:30-10:35 同一 Alias 的 33 次查询均为 `no_code`，但 INBOX 中 10:30、10:31、10:33 三封精确投递邮件均位于有效查询窗，Alias 活动、有 key、sender filter 为空；NTP、健康、SQLite、IMAP 和日志均正常。三封 HTML-only 邮件各有一个独立六位数字，旧提取器均可返回，`46c8bbb` 均拒绝。根因是 HTML 去标签后未压缩排版空白，已识别语境与数字的字符距离被放大为 167，超过门限 80；压缩连续空白后距离为 4/5。取证使用 `BODY.PEEK[]`，Seen 数前后均为 4；未输出或持久化邮件内容、身份、验证码或凭据，未修改生产代码、配置、镜像、数据库或 Apple/HME 状态。
- 2026-07-31：HTML 验证码语境距离修复提交 `67968b7d0f79c5b9b981f7ea118fab3ac4d9d57a` 已部署。修复只在语境评分前压缩连续空白，不扩充词表、不调整 80 字符门限，也不改变 IMAP/Alias/sender filter/300 秒窗口；全量 `146 passed`。同三封事故邮件只读复验为旧版 `0/3`、候选 `3/3`，Seen 总数前后不变，未输出身份、正文或验证码。
- 2026-07-31：新 app 容器 `e28b19bd6c8120235735d6cb31dfc3ad1989e594f9f79b814627cbb3f1f812f1` 使用镜像 `sha256:bdbc7846d2e89b07db0c48acaf7e5bd5cfd4f83475cfba29ccd882e23c4f341a`，固定为 `latest/prod/release-67968b7`。60 秒 watchdog 只替换 app，`14.387` 秒完成 healthy、内外 200、源码与部署标记闭环且未回滚；该时间作为公网不可用保守上界。服务器后续 health `20/20` 为 200，app 严重日志、Caddy 502 和秘密命中为 0；browser、cn-proxy、Caddy 的 ID/restart/OOM 未变。
- 2026-07-31：生产 SQLite 前后均 `quick_check=ok`，159 条 Alias（148 活动）、41 个 key hash、37 个 key 密文、299 条审计、2 条设置和三类脱敏摘要逐字一致。回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/html-whitespace-20260731T042125Z-67968b7`，22 个文件的最终清单 SHA-256 为 `f41a509fd184dbbdad196e2a8d0e97d13066477066b088d9432de5cd1673eb24`；旧镜像只保留 `rollback-pre-67968b7-20260731T042125Z`。候选容器/卷/网络/标签、旧 release、发布目录和部署锁已清理，`.env=0600`，根卷可用 44GB。
