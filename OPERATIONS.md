# iCloud Code Gateway 运维手册

本文件是项目唯一的部署与运维记录。后续每次服务器上线、迁移、代理变更、备份恢复演练和事故处理都追加到“运维记录”，不要另建同类手册。

## 1. 生产边界

生产数据面位于 `cloudflare-mailbox/`：Cloudflare Email Worker 接收域名邮件，D1 加密保存，查询网页同时供操作者和普通用户使用。服务器 2 是 Apple HME 控制面：运行 FastAPI、SQLite、持久 Chromium/noVNC，负责创建 Alias、保存 Session、签发 Token，并把映射同步给 Worker。QQ IMAP 不再是生产收件路径。

- `app`：服务器 2 上的 `control` 模式 FastAPI 与 SQLite，非 root，根文件系统只读，数据卷为 `icloud-code-gateway_gateway-data`。
- `browser`：独立 Chromium/Xvfb/noVNC，固定非 root UID 102，profile 卷为 `icloud-code-gateway_browser-data`。
  服务器部署时显式启用 `legacy-browser` profile；普通不带 profile 的 `up -d` 仍不会误启 Chromium。
- `cn-proxy`：旧兼容容器继续保留，但 iCloud 控制面不再复用它。2026-08-26 实测其 HTTPS 为 SSL 错误，而服务器直连 Apple/Cloudflare 均正常。
- `caddy`：共享 Caddy 承担公网 `/admin*`、`/static/*` 与管理员认证后的 noVNC；根用户页、API、控制同步和 `/admin/mail*` 继续由 Worker 精确路由接管。
- 原始 noVNC：只监听宿主 `127.0.0.1:${BROWSER_NOVNC_PORT}`。
- CDP：只 `expose` 给 Docker 网络，不发布宿主端口；`edge` 模式下 `Settings` 会强制清空 CDP 与 profile。
- iCloud browser 与 HME API：分别使用 `BROWSER_PROXY_*` 与 `ICLOUD_GATEWAY_HME_PROXY_*`；当前服务器生产均留空直连。Edge 同步另支持 `ICLOUD_GATEWAY_EDGE_PROXY_*`。
- IMAP：在管理页单独配置。**`edge` 模式下若未单独填写代理，会自动继承 HME 代理**，否则德国直连 QQ 很慢甚至不通。
  可选填写一个垃圾邮件文件夹，保存时会与主文件夹一并执行只读验证；QQ 主机即使未填写也会自动扫描 `Junk`。

## 1.1 验证码读取路径

进程内有一个常驻邮箱监听器（`icloud_gateway/mailbox_watcher.py`）：单条 IMAP 长连接，增量拉取新
UID，每封邮件只解析一次，按收件别名建内存索引。公开 `/api/code` 与管理页验证码都读这个索引，
不再各自登录扫描。`/api/code` 在未命中时会挂起请求最多 `ICLOUD_GATEWAY_OTP_REQUEST_TIMEOUT`
秒，邮件一到立刻返回。监听器未就绪（未配置 IMAP、正在重连）时自动回退到原来的按需扫描。

数据库中的密文依赖 `ICLOUD_GATEWAY_MASTER_KEY`。丢失该主密钥时，数据库备份无法恢复 Apple Session、IMAP 密码、Alias 远端 ID 和新版本签发的访问密钥明文。

### 1.2 Cloudflare Email Worker + D1 路径

- iCloud Hide My Email 转发到 Cloudflare 域名地址，由 Email Worker 从原始收件头匹配 Alias。
- Worker 保存加密的标题、发件人、纯文本正文和验证码；Token 与邮箱索引只保存 HMAC。
- 操作者与用户使用同一“邮箱 + Token”网页；本地 control 继续签发 Token 和同步 Alias。
- 默认邮件保留 24 小时、会话 15 分钟、清理 Cron 每 15 分钟运行一次。
- 切换前必须证明 Apple 转发保留原始 iCloud Alias 头；否则不得下线 QQ 回滚链路。
- 完整上线与回滚命令见 `cloudflare-mailbox/README.md`。

### 1.1 管理端静态资源与批任务结果

- 管理页 JS/CSS 使用内容哈希查询参数，`/static/*` 响应要求浏览器每次重新验证；部署后不得继续使用无版本号的旧 `admin.js`。
- 创建和批量操作均返回持久任务，前端必须持续轮询普通状态接口，并只通过管理员 Cookie + CSRF 的 POST results 显式读取成功项密钥。
- 单次创建默认与硬上限均为 100；任务仍逐项串行调用 Apple，不得把 100 项改成并发 HME 写入，也不得用真实 Apple 批量创建作为发布测试。
- `needs_reconcile` 不是整批失败：页面须分别显示成功、明确失败、远端结果不确定和尚未开始数量；已成功项仍可输出标准字段。
- Apple generate/reserve 等写操作的远端结果不确定时禁止自动重放。先人工或只读对账，再决定后续操作。
- HME API 响应 Cookie 轮换会在原 Session 仍是当前版本时安全保存；若人工登录或后台刷新已写入更新 Session，则丢弃旧客户端的轮换结果。

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
docker compose -f docker-compose.yml -f docker-compose.server.yml up -d
```

该命令只会启动 `cn-proxy` 与 `app`；`browser` 在 `legacy-browser` profile 中，云端不再运行 Chromium。
如需临时恢复（仅排障），追加 `--profile legacy-browser`。

注意：profile 只控制“以后是否启动”，不会停止迁移前已经存在的 browser 容器。首次从 `full` 切换到 `edge` 后执行一次：

```bash
./scripts/remove-edge-browser.sh
```

脚本会核验 deployment mode 与 Compose 标签，只移除 `icloud-code-gateway/browser` 容器；app、cn-proxy、SQLite、browser-data 卷和 browser 镜像均保持不变。确认不再需要云端回滚浏览器后，镜像清理由运维人员另行执行，不放进自动脚本。

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
- 点击 Alias 邮箱文本会复制完整邮箱；Clipboard API 不可用时页面使用临时选区回退，不把邮箱写入浏览器存储。
- 每条 Alias 可标记 GPT、Grok 或自定义用途。用途值最多 80 字符，空值表示未标记；Apple 对账、密钥轮换和状态变化不得清空该字段。用途目前只保存在当前管理平面的 SQLite 中，不进入 control→edge 协议。
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

更新前完成 SQLite 在线备份。升级到本版本时，先把新增的 `ICLOUD_GATEWAY_HME_MAINTENANCE_SECONDS`、`ICLOUD_GATEWAY_HME_FRESHNESS_SECONDS`、`ICLOUD_GATEWAY_HME_RETRY_MAX_SECONDS` 和 `ICLOUD_GATEWAY_ALIAS_BATCH_LIMIT` 从 `.env.example` 合并到服务器 `.env`。本地 `control` 的批量上限设为 `100`；不负责生成 Alias 的云端 `edge` 当前显式保持 `50`，避免在生命周期门控尚未独立收敛前扩大破坏半径。本版本对 `aliases` 增加 `usage_label TEXT NOT NULL DEFAULT ''`，属于可向后忽略的加法迁移。再按顺序构建，验证新镜像后只替换需要更新的服务：

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

app 镜像的 Docker healthcheck 必须为 `127.0.0.1:8080/healthz` 显式携带从 `ICLOUD_GATEWAY_PUBLIC_BASE_URL` 解析出的 Host。生产 Trusted Host 或依赖版本变化时，无 Host 的探针可能得到 HTTP 400，即使带正确 Host 的业务探针为 200；发布时必须同时保留 Docker health、app 内网、共享 Caddy 容器内 origin、公网和 SQLite 探针，不能用单一元状态替代实际反馈。

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

- 2026-08-26：修复服务器 noVNC 中“浏览器捕获一直等待登录”。现场 Apple 登录 iframe 的初始化与静态资源均正常，`signin/init=200`；用户重复提交后 `signin/complete` 从 401 变为 403，属于账号格式/临时风控，不是网络故障，也没有代填或识别验证码。真正的捕获器缺陷是 Apple 已把登录入口改成可点击 `DIV` 文本 `Sign in with Apple Account`，旧 `_click_icloud_sign_in()` 只按 button role 查找 `登录/Sign In`，因此没有自动打开 iframe；多次重试还留下 7 个重复 iCloud+ 页面。现场先收敛到单页并打开登录 iframe，随后复用数据库内仍有效且可只读列出 622 个 Alias 的加密 HME Session，将 Cookie 仅在服务器内注入持久 browser，并从 iCloud+ 页面发起一次 `GET /v2/hme/list`（HTTP 200）；捕获状态于 `2026-08-26T10:40:03Z` 变为 `captured`，数据库记录 `hme_session=saved`，未输出 Cookie、Apple ID、手机号、密码、验证码或响应正文。
- 2026-08-26：持久修复提交 `58c1edf` 增加 Apple 当前中英文文本控件回退，并补单元测试证明 role button 不存在时仍可点击；Ruff 与 54 项定向测试通过。生产只替换 app，browser/profile、cn-proxy、Caddy 和 Worker 均未重启；browser 容器 ID 保持不变。新 app 镜像 `sha256:2965a7f59ad9c7546f4d9c959c60eb8b6957f86ba11cab73750d2bcf289ed1c7`，回滚标签 `icloud-code-gateway-app:rollback-pre-signin-20260826T104520Z`。捕获后 SQLite 备份位于 `/opt/new-api/icloud-code-gateway/backups/hme-capture-20260826T104625Z-58c1edf`，清单摘要 `b054c49c835a5218b070e2998092da86c16245dae64e3c4df94c8100e7c1c614`；最终 HME list=622、SQLite `quick_check=ok`、604 active/372 keyed、浏览器三枚核心 Apple Cookie 均存在，公网 `/admin/login`、`/admin/mail/`、`/healthz` 为 200，app 严重日志为 0。
- 2026-08-26：按用户要求把本机 iCloud Alias 控制系统迁到服务器 2，并统一到 `https://icloud.yunbay.xyz/admin`。事故起点是新建 5 个 Alias 在 Apple/本地均成功且 Token 已签发，但本机 control 启动时只探测旧 Clash `7897`，实际 FlClash 为 `7890`；直连 Cloudflare 又被重置，导致 5 次 `edge_sync=failed`、D1 中完全缺失。先经 `7890` 幂等回填 598 个活动 Alias（失败 0），原 5 个邮箱随后均为 D1 active、Token 两个索引齐全，逐一公网登录与消息接口均为 200。代码新增独立 `ICLOUD_GATEWAY_EDGE_PROXY_*`，本机启动器优先 `7890`、兼容 `7897`，提交为 `d238fe1`。
- 2026-08-26：服务器迁移最终提交链为 `d238fe1`、`ee13d92`、`e3ece26`，服务器源码标记 `e3ece26`；app 镜像 `sha256:d930d54c655dd94a9bde9c5abe08c2851e5945ac957bef11e8ee57ccdb079b8b`，既有 browser 镜像 `sha256:a271017d9e56f3b363e647203f8726e946e8ff82d2f93c8e1eef8e2c972d1a56` 继续复用。迁移前本机一致性快照位于 `/Users/ethan/.icloud-code-gateway/backups/server2-migration-20260826T094330Z-d238fe1`，`SHA256SUMS` 摘要为 `74fb6a7b5db7a41e09cc7093e755bb03cf0d70887c4566ca5030e108a87ee199`；服务器旧源码、环境、SQLite 卷、browser 卷和镜像备份位于 `/opt/new-api/icloud-code-gateway/backups/server-control-migration-20260826T094004Z-d238fe1`，最终清单摘要为 `40018862ca877f4ba5120e2661a0281137dd004e68f33b1e3a2a4d28b68c0209`，旧 app/browser 回滚标签均为 `rollback-pre-server-control-20260826T094004Z`。本机 control 已停止，两个“打开本地 iCloud 控制台”快捷方式已改为打开线上 `/admin`；本机数据库/profile 只作离线回滚，不得与服务器同时启动写 Apple。
- 2026-08-26：服务器旧 `cn-proxy` 对所有 HTTPS 均报 SSL 错误，而服务器直连 Apple、Cloudflare 和 iCloud 页面均为 200；因此 `ee13d92` 将服务器 HME API 与 browser 的代理独立覆盖为空，当前 iCloud 控制面直连，旧代理容器保留但不参与 iCloud。迁移后 Apple HME 只读列表与 SQLite 收敛为 622 条（604 active、18 inactive），372 个 Token 全部可恢复；187 个批任务 active=0。迁移时两个排队 5 项任务中，旧任务按安全边界 5 项均以 `remote write was not attempted` 失败，没有重放不确定写；用户较新的任务 5/5 完成，5 个新 Alias 均创建后自动 upsert D1，逐一公网 Token 登录、邮箱匹配和消息接口均为 200。D1 最终为 605 个 active Alias、373 个 Token（均包含 1 个操作员映射），与本地 604/372 精确相差操作员 1 条。
- 2026-08-26：Worker 最终版本 `565f351f-37dd-47d8-89e9-0c1cc7194ce6`。原 `icloud.yunbay.xyz/*` 全站路由拆为根页、静态文件、`/api/*`、`/control/*`、`/healthz`、`/readyz`、`/admin/mail*` 和 `/admin/app.js` 的精确路由；未匹配的 `/admin*` 与 `/static/*` 回到服务器 2。统一管理员入口 `/admin` 使用原服务器管理员密码；顶部“邮件后台”进入 `/admin/mail/`，操作员 Token 和 API 仍由 Worker 管理；`mailbox.yunbay.xyz/admin/` 继续可用。生产复核根页/健康/API/管理员登录/静态资源/邮件后台均为 200，未登录 `/admin` 与 noVNC 为 303，管理员登录后 dashboard/noVNC 为 200，HME 显示“连接正常”；app/browser/cn-proxy/Caddy 均 healthy、restart=0、OOM=false，SQLite `quick_check=ok`，iCloud 站点 Caddy upstream/5xx 为 0。仅回滚入口时应从 `f40af3c` 重新部署旧 Worker 路由；不要仅执行 Worker version rollback 后假设 route 配置会自动恢复。普通服务器代码回滚不恢复 SQLite，以免丢失迁移后新建 Alias。
- 2026-08-25：操作员后台解除“最近 50 封”总量限制并新增全档案搜索，生产 Worker 最终版本 `5dcbc4db-8483-4df6-aa53-d90dc867fcd3`，上一版 `ed8fd5ec-26cc-49ff-977c-cbbfc1b9abed` 保留回滚。`GET /api/operator/messages` 保持单页最多 50 封，新增基于 `(received_at, created_at, id)`、不含敏感数据且严格校验格式的游标响应 `next_cursor/has_more`；分页查询使用严格倒序和参数绑定，非法游标返回 422。前端首屏仍只解密最新 50 封，显示“50+ 封”和“加载全部历史邮件”；点击后循环读取全部页。搜索框匹配邮箱、发件人、标题、正文、验证码及附件名，若仍有历史页会自动加载全部后再搜索，解密结果只驻留当前操作员页面内存，不进入 localStorage/sessionStorage，退出或会话失效时立即清空列表和阅读器 DOM。生产候选 `60edb2e9-6f79-4eeb-b4d7-24a595903e42` 在当时 89 封保留邮件上验证真实分页为 `50 + 39`，合计与 D1 精确一致且 0 重复；1440px/390px 后台均从“50+”加载为 89 封，第二页关键词搜索结果正确、清空后恢复 89 行，无横向溢出和控制台错误。26 项 Worker 测试、格式/类型/dry-run、非法游标与三封测试分页合同均通过。
- 2026-08-25：操作员后台切换为大视野工作台，生产 Worker `ed8fd5ec-26cc-49ff-977c-cbbfc1b9abed`，上一版 `b2e0a597-b331-478b-ab36-2242e4844b61` 保留回滚。登录页仍保持原 1180px 居中排版；成功进入后台后由脚本给 `body` 添加 `operator-active`，将页头压至 72px、页面内边距压至 16px、隐藏页脚，并把工作区宽度扩至最大 1800px/浏览器两侧仅 1–2rem。1440×1000 生产实测工作区从上一版约 `1180×570` 扩至 `1411×822`（占视口 81%），邮件栏保持 400px，新增宽度全部给正文区，正文从 778px 扩至 1009px且同屏邮件从 7 封升至 10 封；1024×768 工作区 `1004×590`、正文 681px、占视口 75%。390px 继续使用上下排列，邮件栏与正文均为 372px 宽，无横向溢出；三个视口控制台错误均为 0。25 项 Worker 测试、格式/类型/dry-run 通过。
- 2026-08-25：操作员后台邮件栏密度与完整显示修复，生产 Worker `b2e0a597-b331-478b-ab36-2242e4844b61`，上一稳定版 `d9c17ee1-dbb9-45eb-a87d-c391725a6412` 保留回滚。按用户澄清，邮件栏宽度保持原样（1440px 视口仍为 400px，1024px 仍为 319px），只压缩单封邮件行：高度从约 118px 降为 72px，隐藏列表内重复的正文预览，将保留标记放到主题同一行，并让操作员列表使用完整可用高度和栏内滚动。真实生产 50 封邮件验证：1440px 同屏完整显示 7 封、1024px 显示 6 封、390px 显示 4 封，滚动到底最后一封可完整到达；原文阅读区、用户取码页和邮件栏宽度均未改变，三个尺寸横向溢出与控制台错误均为 0。25 项 Worker 测试、格式/类型/dry-run 通过。
- 2026-08-25：操作员“管理档案”升级为原格式邮件与附件归档，生产 Worker 最终版本 `d9c17ee1-dbb9-45eb-a87d-c391725a6412`，旧版本 `e3c9f905-f0fd-4773-9698-aaf88b5d6e1c` 保留回滚。Cloudflare 账户尚未启用 R2，因此创建私有 Workers KV `icloud-mailbox-attachments`（namespace `ebdb0c1f8dfa48249054c1ff5978df0e`）保存附件密文；附件字节继续使用 `DATA_ENCRYPTION_KEY` 做带上下文 AES-GCM，D1 新迁移 `0004_message_archives.sql` 只保存随机对象键、大小和加密文件名/MIME 元数据。新邮件额外加密保存最多 250,000 字符 HTML，单封邮件上限从 5MB 提升至 20MB；操作员后台默认以同源 sandbox iframe 展示原排版，可切换纯文本。HTML 响应使用独立 CSP，移除脚本、meta refresh、远程图片/字体/CSS 引用与跟踪资源，仅保留文字、表格、内联样式；单张不超过 1.5MB、合计不超过 2MB 的 CID 内嵌图片可离线显示，避免大邮件渲染耗尽 Worker 内存。普通用户接口仍只返回 GPT/Grok 验证码与时间。附件下载接口只接受操作员 HttpOnly 会话，强制 `Content-Disposition: attachment`、`nosniff` 和 `no-store`；临时附件随 24 小时邮件清理，永久附件跟随长期邮件，Alias 删除造成的跨存储孤儿由 15 分钟 Cron 对账删除。上线前 D1 加密备份为 `/Users/ethan/.icloud-code-gateway/backups/cloudflare-mailbox-20260825T140000/icloud-mailbox-before-archives.sql`，SHA-256 `6e43acde0c07e7f0b8dde1d7cb611244f67e32b4772acc45ce5c4d6003764792`；迁移前 63 封邮件保持不变，历史邮件因未保存 HTML/附件无法逆向恢复。25 项 Worker 测试、类型/格式/dry-run、1440px 与 390px 浏览器原排版/纯文本切换/无溢出、真实本地下载均通过。生产候选 `28c5d972-ab65-4f48-9d85-420fdb66df28` 再用 QQ→真实 HME Alias 发送带表格样式和文本附件的邮件，HTML、沙箱、加密 KV、操作员 API 与浏览器下载内容全部验证一致；最终版只追加内嵌图片总预算。测试邮件、D1 元数据和 KV 对象随后定向删除，最终 KV 与附件表均为空，双域名健康/就绪正常。
- 2026-08-25：生产验证码解码逻辑完成防漏码加固，Worker 升级到 `e3c9f905-f0fd-4773-9698-aaf88b5d6e1c`，旧版本 `9c4a25f1-ca51-4fbc-a863-f3a1ff82bf50` 保留回滚。本轮先解密审计生产最近 50 封邮件：24 条 GPT 均为 6 位数字、1 条 Grok 为三位-三位混合码，既有 25 条验证码重跑后 0 个不一致；15 封 GPT/Grok 无验证码账号通知重跑后 0 个误抓，疑似数字均为年份、时间或地址。解码器新增 Unicode NFKC、零宽字符和 Unicode 横线归一化，支持 `123 456`、`123-456`、HTML 逐位拆分数字、全角数字、Grok 空格/无连字符码，以及中文“登录码、注册码、邮箱验证码”等语境；仍要求验证码上下文或品牌邮件中的独立代码行，避免把 IP、订单号和日期放行。格式、类型、23 项 Worker 测试和 dry-run 通过；随后用 QQ 发送 HTML 六个 `<span>` 拆分的验证码，经真实 HME→`otp@yunbay.xyz` 后正确得到 `category=gpt`、完整验证码，且用户 API 可见，测试邮件已定向删除。
- 2026-08-25：紧急修复 Cloudflare 收件箱“管理员可见、用户端 0 封”。根因是 iCloud Hide My Email 将 ChatGPT/Grok 发件地址改写为随机 `@icloud.com` 中继地址，旧分类器只检查邮箱域名，导致已成功入库且已提取验证码的邮件全部标成 `other`，被普通用户查询条件过滤。Worker 改为“官方域名优先；Apple 中继地址 + 发件显示名/标题/正文多处品牌证据”分类，并保留单一伪造显示名不能放行的门槛；生产版本升级为 `9c4a25f1-ca51-4fbc-a863-f3a1ff82bf50`。上线前 D1 加密导出保存在 `/Users/ethan/.icloud-code-gateway/backups/cloudflare-mailbox-20260825T130000/icloud-mailbox-before-relay-classifier.sql`，SHA-256 为 `b7054bf9a7fef85c927688232253921b849202053acc1efe35617e696e1dc8bb`，旧 Worker `f86f3e1b-e752-411f-a929-f775745af386` 保留回滚。历史数据回填 20 条 GPT 验证码、1 条 Grok 验证码和 14 条 GPT 非验证码账号邮件；两条用户报告的 Alias 已从用户接口真实验证恢复。随后用 QQ→真实 HME Alias→`otp@yunbay.xyz` 做端到端测试，Apple 确实改写发件地址，新 Worker 仍正确得到 `category=gpt/has_code=true`；测试邮件已定向删除。格式、TypeScript、13 项 Worker 测试、dry-run、双域名 `/healthz`/`/readyz` 均通过。
- 2026-08-25：本地 control 的单邮箱导出改成一步完成。Alias 首页原“眼睛/查看完整密钥”按钮改为复制图标和“导出信息”语义，点击后读取当前密钥并直接复制“邮箱 + 取码链接”，不再打开查看弹窗；签发新密钥和批量导出仍使用结果弹窗。手机端工具栏允许安全换行，390px 与 1440px 真实 Chromium 验证均无横向滚动、无控制台错误。
- 2026-08-25：签发/批量导出弹窗继续去重。单个邮箱不显示顶部批量按钮或中间取码链接文本，只保留邮箱右侧“导出信息”；多个邮箱时顶部仅保留“复制取码链接、一键复制”，移除重复的顶部“导出信息”，每行仍可单独导出。复制内容只保留“邮箱、取码链接”，不再单独输出“网站、密钥”；Token 仍只存在于 URL fragment，不进入服务端请求日志。
- 2026-08-25：生产 Worker 升级到 `f86f3e1b-e752-411f-a929-f775745af386`，D1 应用 `0003_message_visibility.sql`。普通 Alias Token 的 `/api/messages` 只查询 `category IN (gpt,grok) AND has_code=1`，响应只含验证码与时间，不返回邮箱正文、标题或发件人；独立 `OPERATOR_ACCESS_TOKEN` 只允许 `/api/operator/*`，后台无需邮箱即可查看全部 Alias 的完整邮件。操作员 Token 已写入 Cloudflare secret，并继续只备份在 macOS Keychain。
- 2026-08-25：邮件保留改为服务端分级策略：验证码及普通非重要邮件为 `temporary`，24 小时后由 Cron 清理；无验证码的 GPT/Grok 账号邮件，以及命中会员/订阅/付款/封号/停用/申诉/工单/售后支持语境的邮件为 `permanent`，不进入过期删除。既有两封真实邮件在 migration 中默认标记长期，避免升级时误删。生产真实 HME 测试证明普通发件人的验证码邮件不会出现在用户响应，但操作员后台可见且标记 24 小时；售后工单邮件后台可见并标记长期。测试邮件随后定向删除。
- 2026-08-25：用户页与操作员后台的 3 秒轮询改为内容签名去重。自动刷新在响应未变化时不操作 DOM，出现新邮件时无动画更新；手动刷新强制重绘并播放 220ms 状态反馈。生产 390px 用户页与 1440px 操作员后台连续自动刷新均无闪烁/动画类，手动刷新反馈正常，控制台错误为 0。本机快捷入口已改为 `https://icloud.yunbay.xyz/admin/#key=...`。
- 2026-08-25：Cloudflare 隐邮收件箱已正式上线并接管历史入口。生产 Worker `icloud-mailbox-worker` 最终版本为 `53e102ec-3699-476d-ba5b-1dcbc646a629`；用户主入口继续使用 `https://icloud.yunbay.xyz`，通过 zone Worker route 接管现有 DNS，维护入口为 `https://mailbox.yunbay.xyz` custom domain。原 VPS/DNS 未删除，移除 `icloud.yunbay.xyz/*` route 即可回滚。D1 `icloud-mailbox` ID 为 `8454dc17-0494-4e89-b8fa-3f1e321f75dc`，位于 APAC；四个 Worker secret 已写入 Cloudflare，其中三个加密/HMAC key 另备份到 macOS Keychain，未进入 Git、聊天或日志。
- 2026-08-25：Email Routing 规则 `05aa9be2627f4f35a85c8d8f4febf6a5` 已创建：`otp@yunbay.xyz → worker:icloud-mailbox-worker`；既有 `support@yunbay.xyz → QQ` 保持不变。iCloud Hide My Email 已切换转发到 `otp@yunbay.xyz`。先用 QQ 直投验证域名收件，再向一条现有真实 HME Alias 发信；Apple 转发保留原 Alias，约 12 秒内由 Worker 写入 D1，并能从旧网址用原 Token 查询正文和验证码。两封部署测试邮件已定向删除，真实邮件按 24 小时策略保留。
- 2026-08-25：本地 592 条 Alias 中 574 条 active 同步成功、18 条 inactive 跳过、失败 0；加操作员地址后 D1 为 575 条 active Alias。344 个历史 Token 全部可恢复、hash-only 为 0；D1 最终 345 个 Token（历史 344 + 操作员 1）均具备绑定摘要与全局 HMAC 查找索引。新 API/真实浏览器均证明旧 `/#key=...` 链接可自动识别邮箱，历史链接无需重发；新链接使用 `/#email=...&key=...`。本地凭据保留 QQ 用户名/密码作回滚，但 `ICLOUD_GATEWAY_IMAP_ENABLED=0`，重启后日志确认“Cloudflare Email Worker 接管”，QQ 凭据未注入进程。
- 2026-08-24：新增 `cloudflare-mailbox/`，实现 Cloudflare Email Worker + D1 收件箱，目标是完整替代 QQ/IMAP 与旧 VPS edge。Worker 兼容现有五个 `/control/v1/*` 同步合同；Email Worker 从 iCloud 转发头匹配 Alias，使用 AES-GCM 加密发件人、标题、纯文本正文和验证码，邮箱/Token/来源索引只保存 HMAC。操作者与普通用户共用“隐藏邮箱 + Token”网页，会话为 HttpOnly/Secure/SameSite=Strict，Token 轮换或 Alias 停用立即使旧会话失效；默认邮件保留 24 小时，15 分钟会话，15 分钟 Cron 清理，超过 5MB 的邮件不解析，附件不保存。
- 2026-08-24：Cloudflare 子项目门禁为 Prettier、TypeScript 7、Workers runtime Vitest、真实 D1 migration 和 Wrangler dry-run；10 项测试覆盖加密上下文、验证码解析、HTML 转纯文本、控制面同步、Email Worker 入库/去重、查询会话、Token 轮换失效和静态资源隐私。1440×1000 与 390×844 的登录页、空收件箱和真实加密测试邮件状态均通过 Playwright，横向溢出和控制台错误为 0。Python 侧取码链接升级为 `#email=...&key=...`，本地启动器可从受限凭据文件读取 Worker 的 edge/public URL。
- 2026-08-24：本轮只完成代码、迁移、文档和本地 Worker/D1 验收；没有登录或修改远端 Cloudflare，没有创建生产 D1、Email Routing、Worker route 或自定义域名，也没有修改 Apple 转发目标、QQ 配置、服务器 2 或旧 VPS。正式切换必须先用单个 Alias 证明 Apple→Cloudflare 邮件保留原始 iCloud 收件头，再同步全部 Alias/Token，并保留 QQ 回滚至少 24 小时。
- 2026-08-24：本地 control 会话捕获故障已修复。根因是启动器在本机 `127.0.0.1:7897` 无监听时仍无条件配置 HME SOCKS 代理；同一份已保存 Session 直连 Apple 可只读列出 582 条 Alias，经离线代理则返回 `HmeNetworkError`。启动器现仅在默认本地代理端口实际可连接时自动启用，否则直连；捕获状态另外区分 Apple API 网络、Session 拒绝、验证响应和本地保存失败。定向 140 项测试通过，重启本地 control 后真实“登录更新”约 11 秒进入 `captured`，原数据库和 browser profile 保留。
- 2026-08-24：已有邮箱点击复制改为在原始点击手势中优先执行同步本地复制，成功后不等待 Clipboard 权限检查；静态合同继续证明该动作不调用后端、不固定单一 Alias、不触发 IMAP。新增 `scripts/remove-edge-browser.sh`，用于 `full -> edge` 迁移后一次性移除 Compose profile 不会自动停止的旧 browser 容器，严格核验 edge mode 和 Compose 标签，并保留 app/cn-proxy、SQLite、browser-data 卷与镜像。
- 2026-08-24：公网 `https://icloud.yunbay.xyz/healthz` 保持 200，但本轮没有进入 VPS。手册原放行节点 `US 33 AI加速 x1.0` 当前无法建立外网连接；FlClash 当前 Mixed 端口为 7890，当前新加坡及三个不同美国出口均在 SSH banner 阶段被服务器白名单丢弃。服务器未执行命令，未修改容器、防火墙、数据或部署标记；遗留 browser 容器的实时状态与清理仍待可用白名单出口恢复后完成。
- 2026-08-09：Alias 用途状态、点击邮箱复制和本地单次建模 100 项已完成生产收口。云端 `edge` 继续显式保持批量上限 50；生产提交为 `83df4668e4ccbd606c47a0d97c1bee5c6118fa04`，其中 `b38773b` 承载功能适配，`83df466` 只修正 Docker healthcheck Host 合同并增加回归测试。最终新 app 镜像为 `sha256:d25512d051937fe161f697ed770040fdc207537d3297e9c57f7762e6c534e5f8`，固定为 `latest/prod/release-83df466`；旧镜像 `sha256:5defdf1f4b1b93b66187d97fe05e38ac4ffcb87a77948cba67f799a483c36f4a` 保留 `rollback-pre-alias-usage-83df466-20260809T162411Z`。watchdog 26 秒接受容器、内网、公网、Caddy-origin、SQLite/schema 和 edge=50 连续探针，最终 app/browser/cn-proxy/Caddy 均 `healthy/restart=0/OOM=false`，后三者 ID 未变。
- 2026-08-09：生产 SQLite 前后均为 `quick_check=ok`，375 条 Alias、357 条 active/keyed、2 条 setting、1 条 metadata、2158 条 audit、22 个 job、156 个 item 及表摘要守恒；`usage_label TEXT NOT NULL DEFAULT ''` 存在，非空用途为 0，active job 为 0。候选迁移、用途 API/XSS、邮箱复制静态合同、OTP fixture、旧镜像读新 schema、无效 key 404、实时有效 key HTTP 200 `waiting`、公网/管理页/noVNC 边界全部通过，未执行真实 Apple 100 项创建。成功审计目录为 `/opt/new-api/icloud-code-gateway/backups/alias-usage-20260809T162411Z-83df466`，最终清单 SHA-256 为 `01cf7b4c85b331d94cc411d3249d1888e82b83b0c8bc11f731d283f89f81d115`，候选资源为 0。
- 2026-08-09：此前数次未接受切换均由 watchdog 自动恢复 `07a3534`，没有等待人工恢复。最终窄字段 `Health.Log` 证明新镜像原 healthcheck 连续返回 HTTP 400，而带可信 Host 的业务探针为 200；修复后新容器在正式切换中正常转为 `healthy`。共享 Caddy 未重建、未在最终发布中 reload，browser/profile 和 cn-proxy 未动。
- 2026-08-02：复制/导出格式修复提交 `49b2d9b0ecd6cd41a9aeb9b248d90858541f4ec3` 已部署。“一键复制”仅输出邮箱且一行一个，“导出信息”采用“邮箱、网站、密钥”标准字段。生产从 `e9fdc4f` 升级，目标仅变更管理页脚本、模板和测试；候选镜像在隔离临时数据目录中通过健康及 UI 契约断言后，由独立 60 秒 watchdog 只 force-recreate app，`22.668s` 完成 healthy、revision、marker、内外 200 和数据指纹闭环，状态 `accepted`，未触发回滚。新 app 容器 `7dd21948d0820e7be762cd930bfa47439f2c0b34d11c97a9b2f583d22d29c4fe`，镜像 `sha256:2fbc3239f6a803f8dd1e0774400899e40a09eab9d4f3d1718702f398975fb874`，固定为 `prod/release-49b2d9b`；browser `bcef6bba...`、cn-proxy `ed6f331e...` 未重建，共享 Caddy 未改，正式容器均为 `healthy/restart=0/OOM=false`。SQLite `quick_check=ok`，194/176 个 Alias、446 条审计、2 条设置以及 job/item 数据指纹前后完全一致；既有 5 个 `needs_reconcile` 终态任务保持不变，未自动重放，本轮未访问 Apple/HME 或 IMAP。公网健康/首页/登录为 200，未登录 noVNC 为 303，严重日志为 0。审计目录为 `/opt/new-api/icloud-code-gateway/backups/ui-copy-export-20260801T174254Z-49b2d9b`，清单 SHA-256 为 `2d228e321756d9a1885e54ca7780e205dbb662f72c4dd0bbdb6737b3f4c89e6b`；旧镜像 `sha256:b1eeaa894651a696a4485f2d00d448d7085e0a9460b0906f981d1905ad49978f` 保留专用 rollback 标签。普通回滚只恢复源码/marker、重标旧镜像并仅重建 app，不恢复 SQLite，不动 browser/profile、cn-proxy 或 Caddy。
- 2026-08-01：用户更新 HME Session 后明确要求直接部署，因此本轮未执行额外 setup validate/
  HME list，也没有任何 HME 写调用。功能提交 `a954e06caf3368eff9a0a0c10269c1724b4eaaea`
  已部署；第一次切换因 Caddy 首个主动健康窗口探针为 503，60 秒 watchdog 自动
  恢复旧版并重新达到公网 200；第二次切换持续等待入口恢复，10 秒后 app healthy、
  公网 200、SQLite `ok`、179/161 个 Alias 和空 batch job/item 表通过，watchdog=`accepted`。
  新 app 容器为 `7357afff94924930b5762e95cca728f2e635994bde50bc0ce2eac984a18b7641`，镜像为
  `sha256:d4770065d6d8451cbaada12e3c372255466050203631e3abe8de490a22cae7f6`，固定为
  `latest/prod/release-a954e06`；旧镜像保留 `rollback-pre-a954e06-20260801T044824Z`。browser、
  cn-proxy 和 Caddy 的 ID 不变，均为 healthy/restart=0/OOM=false；上传包、候选标签、临时
  发布目录和部署锁已清理。审计目录为
  `/opt/new-api/icloud-code-gateway/backups/state-machine-retry-20260801T044824Z-a954e06`，最终清单
  SHA-256 为 `2e2d1eb94eeb6ac8dd98fad431f7d4eb79baf0ff0d14e6ee48765d5d8609a603`。
- 2026-08-01：状态机修复提交 `a954e06caf3368eff9a0a0c10269c1724b4eaaea` 已推送 GitHub
  `main`，但未部署。生产预检、完整备份和隔离迁移均通过；候选数据库保持 179 个 Alias（161
  活动）、54 个 key hash、50 个 key 密文、378 条审计和 2 条设置，三类摘要与生产完全一致，job
  表为空且未启动 worker。唯一一次真实 setup validate 被 Apple 拒绝，HME list 调用数为 0，未执行
  HME 写或 app 切换。按门禁恢复服务器源码/marker 到 `67968b7` 并清理候选镜像、卷、容器、上传
  包和部署锁；四容器保持原 ID、healthy/restart=0/OOM=false，公网 200，SQLite/profile 未覆盖。
  失败审计目录为 `/opt/new-api/icloud-code-gateway/backups/state-machine-20260801T041859Z-a954e06`，
  18 个文件的最终清单 SHA-256 为
  `808bb1fb685d199746fbcee5f8b6404b86ac38d40edbd09afe1a1734f5569de2`。再次发布前必须先在持久
  Chromium 中重新认证并捕获 Apple Session，再建立新的单次 validate/list 门禁；禁止直接重试本次
  已失败的 Session。
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

## 运维记录 · 2026-08-03 HME 邮箱同步与 Token 签发

- 时间：2026-08-03（UTC 06:40 左右）
- 目标：云端先同步 iCloud 隐藏邮箱，再给活动 Alias 签发/补齐 access token，供紧急使用。
- 现象：Apple HME Session 已被拒绝；browser CDP 经 Host 名访问返回 500（Chrome 要求 Host 为 IP/localhost）。重建 browser 后用 IP 解析连接 CDP 成功。
- 处理：
  1. 热备份 SQLite。
  2. 仅重建 `icloud-code-gateway-browser-1`（保留 profile 卷）。
  3. 重新捕获 HME Session 成功。
  4. 执行 `sync_aliases`。
  5. 给所有缺 key 的活动 Alias 签发 token；失败 0。
- 结果：
  - 同步前：209 条 Alias（191 活动 / 18 失活），84 个已有 key。
  - 同步后：217 条 Alias（199 活动 / 18 失活）。
  - 新签发 token：115 个；活动邮箱无 key 数：0。
  - 可恢复导出：195 个 token（4 个历史 hash-only key 不可查看）。
- 产物目录（服务器，0600）：
  - `/opt/new-api/icloud-code-gateway/backups/hme-sync-token-20260803T064044Z/`
    - `issued.tsv`：本轮新签发
    - `tokens.tsv`：全部可恢复 token
    - `gateway.pre.sqlite3` / `gateway.post.sqlite3`
  - 早期导出：`/opt/new-api/icloud-code-gateway/backups/token-export-20260803T063843Z/`
- 服务状态：app/browser/cn-proxy healthy；未替换 app 镜像，未改 `.env`，未做架构改造。
- 备注：本轮未轮换既有 key；未执行 Apple 停用/删除。后续“本地账号管理 + 云端发码”改造另开。

## 本地 control + 云端 edge 部署

### 边界

- 本地 `control`：负责浏览器登录、HME Session、创建/停用/删除隐藏邮箱、签发密钥。
- 云端 `edge`：只接收本地注册的邮箱与密钥映射，负责 IMAP 验证码查询与公开页。
- 两端共享 `ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN`；token 只放服务器/本地 `.env`（0600），不进 Git。

### 云端 edge

```dotenv
ICLOUD_GATEWAY_DEPLOYMENT_MODE=edge
ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN=<shared-secret>
# 保留 IMAP 配置与公开页；可不配 CDP/HME session
```

### 本地 control

```dotenv
ICLOUD_GATEWAY_DEPLOYMENT_MODE=control
ICLOUD_GATEWAY_EDGE_BASE_URL=https://icloud.yunbay.xyz
ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN=<shared-secret>
ICLOUD_GATEWAY_EDGE_SYNC_ENABLED=1
```

本地签发 key、轮换 key、停用/删除 Alias 时会调用云端 `/control/v1/*`。云端不执行 Apple HME 写操作。

## 验证码格式支持

- 公开页 / `/api/code`：只返回 GPT 与 Grok 验证码。发件人须匹配 `openai.com` / `chatgpt.com` / `oaistatic.com`，或 `x.ai` / `xai.com` / `grok.com`。Cursor 及其他来源对用户显示为等待。
- 管理页取码不受该白名单限制，仍可读全部验证码。
- Grok / xAI：额外支持 `XXX-XXX` 字母数字码（如 `A1B-2C3`）；HTML 与纯文本均可。
- 仍支持对 Alias 设置发件人过滤，例如 `@x.ai`；公开白名单与 Alias 过滤同时生效。

## 运维记录 · 2026-08-03 Grok 验证码支持上线

- 提交：`ebd0638 Support Grok xAI alphanumeric OTP extraction`
- 内容：IMAP 收码支持 Grok/xAI 的 `XXX-XXX` 字母数字码，同时保留 6 位数字通用提取。
- 生产：仅重建 `icloud-code-gateway-app-1`，browser/cn-proxy 未动；app 约 8 秒恢复 healthy。
- 服务器验收：容器内提取 `A1B-2C3` 成功，通用 6 位 `777777` 仍成功。
- 回滚目录：`/opt/new-api/icloud-code-gateway/backups/grok-otp-20260803T075144Z-ebd0638`
- 镜像回滚标签：`icloud-code-gateway-app:rollback-pre-grok-otp-20260803T075144Z`

## 运维记录 · 2026-08-17 创建同步闭环修复 + 云端 Junk 扫描

- 事故：云端 7 天内 173 次 `invalid_key`。根因是批量创建任务用 `database.issue_access_key` 直接落库，从不推送云端；手动“同步云端”又跳过无密钥 Alias。实测云端缺 30 个本地 Alias（含 5 个已签发密钥）。
- 修复（本地 control，无需改云端代码）：
  1. `jobs.py` 创建成功后在 HME 锁外立即 `_push_alias_to_edge`（upsert 带 key），失败只审计不影响本地结果。
  2. `push_all_access_keys_to_edge` 重写：包含无密钥活跃 Alias（云端保留既有 key）、8 线程并行、首个请求网络失败时快速中止。444 个 Alias 实测 21~26 秒推完（原串行且遗漏）。
  3. 新增 30 分钟自动对账线程（`ICLOUD_GATEWAY_EDGE_RECONCILE_SECONDS`，0 关闭），启动后 30 秒先跑一次。
- 云端配置变更：edge IMAP 配置补 `junk_folder=Junk`（`configure_imap` 只读校验通过后保存）。原公开取码只扫 INBOX，QQ 常把 HME 转发投进 Junk，导致“本地能看到码、公开页暂无验证码”。
- 管理页降频：聚焦轮询 800ms→5s（`admin.js`），找到验证码后服务端 4 秒正缓存。原每 2 秒一次完整 QQ IMAP 登录扫描（实测 7.3 分钟 200 次），历史 2607 次 `admin_code_scan=imap_error` 与 QQ 限流相符。
- 验收：修复后云端 469 个 Alias，本地活跃 Alias 0 缺失、密钥哈希 0 不一致；夜间自动对账连续 8+ 次 444/0 失败；新建 5 项批任务逐项 Apple generate 0.9~1.4s、reserve 0.7~1.5s、创建后推送均 HTTP 200，新密钥即时可在公开页使用。
- 附带：`scripts/sync-local-keys-to-edge.py` 与 `连接云贝服务器.command` 的旧“桌面/云贝”路径已改为“鲨鱼工具库”现路径（带回退）。

## 运维记录 · 2026-08-03 云端 edge + 本地 control 落地

- 云端 `ICLOUD_GATEWAY_DEPLOYMENT_MODE=edge`，只负责 IMAP 接码与 `/control/v1` 注册；现有 201 个 keyed alias 保留。
- 本地 control：`http://127.0.0.1:18081/admin/login`，noVNC `http://127.0.0.1:16080/vnc.html`。
- 控制面共享 token 与本地密码：`/Users/ethan/Desktop/云贝/服务器相关/icloud-control-plane.env`（0600）。
- 快捷入口：`/Users/ethan/Desktop/云贝/服务器相关/打开本地iCloud控制台.command`。
- 已验证：本地 control → 云端 control API upsert/delete 成功；公开接码页仍在 `https://icloud.yunbay.xyz`。
