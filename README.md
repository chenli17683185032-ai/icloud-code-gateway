# iCloud Code Gateway

一个独立部署的 iCloud Hide My Email 五分钟验证码网关。

管理员可以导入并管理 Apple 账户中已有的全部隐藏邮箱，再把需要使用的活动 Alias 绑定到高强度访问密钥。使用者只需在公开页输入自己的密钥，系统会从 IMAP 实时读取该隐藏邮箱最近 300 秒内收到的最新 6 位验证码；公开接口不会返回 Alias、邮件正文、Apple Cookie 或 IMAP 凭据。

## 架构取舍

本项目复用联动小铺所用的 `mcr.microsoft.com/playwright:v1.61.1-jammy` Chromium 基础镜像层，但不复用它正在运行的浏览器进程、BrowserContext 或 profile。

这样能共享本机/服务器上的大体积镜像层，同时保持以下故障边界独立：

- iCloud 使用唯一、固定、持久化的 `/browser-data/profile`。
- iCloud Chromium、noVNC、健康检查和重启不影响收款 Worker。
- 浏览器容器重建后继续使用原 Cookie；捕获任务只连接该 Chromium，不创建临时 profile。
- browser 与 HME API 可复用同一个回国代理端点，但不会在代理故障时静默改走德国 IP。
- Chromium 使用原生代理参数，避免 `LD_PRELOAD` 代理库破坏其多进程稳定性；browser 端点应为无认证的内网代理，HME API 仍可使用带认证的上游代理。

## 核心保证

- 密钥与 Alias 一对一绑定；轮换后旧密钥立即失效。
- 邮件收件人精确匹配 Alias，默认只接受当前时间前 300 秒至未来 60 秒的邮件。
- 以 IMAP `INTERNALDATE` 为主要时间依据，读取使用 `BODY.PEEK[]`，不把邮件标为已读。
- OTP、邮件正文和完整访问密钥不持久化。
- Apple Session、IMAP 密码和 Alias 远端 ID 使用环境主密钥进行 AES-GCM 加密。
- HME Session 保存后自动导入完整远端 Alias 快照；重复刷新按邮箱幂等对账并保留本地标签、过滤条件和有效密钥。
- 停用、恢复和永久删除均在 Apple 写入后再次读取 HME 列表确认；确认前不改变本地状态，永久删除只允许失活 Alias 且需要输入完整邮箱。
- 管理页“查询记录”展示最近 7 天内最新 100 次验证码查询；Alias 邮箱快照加密保存，来源只保留不可逆指纹，不保存验证码、访问密钥或原始 IP。
- 管理端采用 HttpOnly/Secure/Strict Cookie 和 CSRF；noVNC 页面、静态资源与 WebSocket 均经过管理员会话认证。
- CDP 仅在 Docker 内网可达；原始 noVNC 只绑定服务器 `127.0.0.1`。
- 浏览器或 Apple/IMAP 上游异常均有界失败，不让公网请求无限等待。

## 服务组成

```text
public user -> Caddy -> FastAPI -> access-key mapping -> IMAP
admin       -> Caddy -> FastAPI admin
admin       -> Caddy -> forward_auth -> noVNC -> persistent Chromium
FastAPI     -> Docker-only CDP -> persistent Chromium
FastAPI     -> configured CN proxy -> iCloud HME API
Chromium    -> native SOCKS5/HTTP proxy -> configured CN proxy
```

## 快速启动

要求：Docker Engine、Docker Compose v2、一个解析到服务器的域名，以及可用的 iCloud/转发邮箱配置。

```bash
cp .env.example .env
chmod 600 .env
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

把生成值写入 `ICLOUD_GATEWAY_MASTER_KEY`，再配置管理员密码、VNC 密码、域名和回国代理。生产环境应保持：

```dotenv
ICLOUD_GATEWAY_COOKIE_SECURE=1
CN_PROXY_REQUIRED=1
```

首次构建和启动：

```bash
docker compose build app
docker compose build browser
docker compose up -d
docker compose ps
```

打开 `https://<GATEWAY_DOMAIN>/admin/login`：

1. 使用管理员密码登录。
2. 点击“打开 iCloud 浏览器”，输入独立的 VNC 密码，在同一个长期 Chromium 中人工登录 iCloud。
3. 点击“开始捕获”，在 iCloud+ 隐藏邮件页面触发一次真实 HME list 请求。
4. 配置转发邮箱 IMAP 和 App 专用密码并执行只读测试。
5. Session 捕获成功后会自动导入已有 Alias；也可点击“导入 / 刷新”重新对账，随后为需要使用的活动 Alias 签发一次性展示的访问密钥。
6. 在“查询记录”查看哪些 Alias 被查询、查询结果、脱敏来源指纹和北京时间；已删除 Alias 的既有记录仍保留当时邮箱快照。

公开查询页位于 `https://<GATEWAY_DOMAIN>/`。

## 本地开发与测试

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run python -m compileall -q icloud_gateway tests
```

完整部署、在线维护、代理验证、备份、恢复和回滚流程见 [OPERATIONS.md](OPERATIONS.md)。工程目标、控制闭环和逐节点验收记录见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

德国云贝服务器已有 Caddy 占用 80/443 时，使用 `docker-compose.server.yml`：它不会启动项目内置 Caddy，而是将 app/browser 以唯一别名接入现有 `app_yunbay-network`，并启动一个独立 Mihomo 进程复用联动小铺的代理订阅配置。对应 Caddy 站点片段在 `deploy/Caddyfile.icloud.yunbay.xyz`。

## 明确不做

- 不保存或自动填写 Apple ID 密码、2FA 码、恢复密钥。
- 不向普通使用者开放收件箱、邮件正文、Alias 列表或 iCloud 管理能力。
- 不自动批量停用、恢复或删除真实 HME Alias；远端生命周期操作只由管理员逐条明确确认。
- 不共享联动小铺正在运行的 Chromium 进程/profile。
- 不保证 Apple Session 永不过期；过期后管理员通过网站中的同一个浏览器重新登录维护。
