# iCloud Code Gateway

一个独立部署的 iCloud Hide My Email 五分钟验证码网关。

管理员把每个隐藏邮箱绑定到一把高强度访问密钥。使用者只需在公开页输入自己的密钥，系统会从 IMAP 实时读取该隐藏邮箱最近 300 秒内收到的最新 6 位验证码；公开接口不会返回 Alias、邮件正文、Apple Cookie 或 IMAP 凭据。

## 架构取舍

本项目复用联动小铺所用的 `mcr.microsoft.com/playwright:v1.61.1-jammy` Chromium 基础镜像层，但不复用它正在运行的浏览器进程、BrowserContext 或 profile。

这样能共享本机/服务器上的大体积镜像层，同时保持以下故障边界独立：

- iCloud 使用唯一、固定、持久化的 `/browser-data/profile`。
- iCloud Chromium、noVNC、健康检查和重启不影响收款 Worker。
- 浏览器容器重建后继续使用原 Cookie；捕获任务只连接该 Chromium，不创建临时 profile。
- browser 与 HME API 可复用同一个回国代理端点，但不会在代理故障时静默改走德国 IP。

## 核心保证

- 密钥与 Alias 一对一绑定；轮换后旧密钥立即失效。
- 邮件收件人精确匹配 Alias，默认只接受当前时间前 300 秒至未来 60 秒的邮件。
- 以 IMAP `INTERNALDATE` 为主要时间依据，读取使用 `BODY.PEEK[]`，不把邮件标为已读。
- OTP、邮件正文和完整访问密钥不持久化。
- Apple Session、IMAP 密码和 Alias 远端 ID 使用环境主密钥进行 AES-GCM 加密。
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
Chromium    -> proxychains strict_chain -> configured CN proxy
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
5. 同步已有 Alias，或创建少量 Alias；为每个使用者签发一次性展示的访问密钥。

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
- 不提供 HME 删除或停用操作。
- 不共享联动小铺正在运行的 Chromium 进程/profile。
- 不保证 Apple Session 永不过期；过期后管理员通过网站中的同一个浏览器重新登录维护。
