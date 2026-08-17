# iCloud Code Gateway

一个独立部署的 iCloud Hide My Email 五分钟验证码网关。

管理员可以导入并管理 Apple 账户中已有的全部隐藏邮箱，再把需要使用的活动 Alias 绑定到高强度访问密钥。使用者只需在公开页输入自己的密钥，系统会从 IMAP 实时读取该隐藏邮箱最近 300 秒内收到的最新 6 位验证码；公开接口不会返回 Alias、邮件正文、Apple Cookie 或 IMAP 凭据。

## 拆分模式（本地 control + 云端 edge）

默认仍是单机 `full`。也可以按你的要求拆开：

| 模式 | 职责 |
| --- | --- |
| `control` | 本地：iCloud 邮箱创建、Session 持久化、账号管理，并把 alias/token 注册到云端 |
| `edge` | 云端：只保存邮箱与访问密钥映射，负责验证码发码/等待（IMAP `/api/code`） |
| `full` | 兼容现网：管理 + 验证码一体 |

关键配置：

```dotenv
# 本地 control
ICLOUD_GATEWAY_DEPLOYMENT_MODE=control
ICLOUD_GATEWAY_EDGE_BASE_URL=https://icloud.yunbay.xyz
ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN=<shared-secret>
ICLOUD_GATEWAY_EDGE_SYNC_ENABLED=1
# 自动对账周期（秒）；0 关闭。control 会在启动 30 秒后先对账一次
ICLOUD_GATEWAY_EDGE_RECONCILE_SECONDS=1800

# 云端 edge
ICLOUD_GATEWAY_DEPLOYMENT_MODE=edge
ICLOUD_GATEWAY_CONTROL_PLANE_TOKEN=<same-shared-secret>
```

创建/签发密钥会即时推送云端（失败自动由对账兜底）；“同步云端”按钮和自动对账都会
并行推送全部活跃 Alias（含未签发密钥的），云端已存在的密钥不会被无密钥推送清掉。

控制面同步接口（仅 token 鉴权，不走管理员 Cookie）：

- `POST /control/v1/aliases`
- `POST /control/v1/aliases/by-email/{email}/key`
- `DELETE /control/v1/aliases/by-email/{email}/key`
- `POST /control/v1/aliases/by-email/{email}/state`
- `DELETE /control/v1/aliases/by-email/{email}`

本地签发或轮换 access key 后，会自动把密钥同步到云端 edge；公开用户仍然只访问云端 `https://icloud.yunbay.xyz/` 输入密钥取码。

### 本地 control（无 Docker）

本地控制台推荐直接跑 Python 进程，不依赖 Docker / OrbStack：

```bash
# 一键启动（桌面/鲨鱼工具库也可双击）
./scripts/run-local-control.sh
```

固定持久目录（重启不丢）：

- `~/.icloud-code-gateway/data`：SQLite + 加密 HME Session
- `~/.icloud-code-gateway/browser-profile`：本机持久 Chromium 登录态

无 Docker 时不配 `ICLOUD_GATEWAY_CDP_URL`，改为：

```dotenv
ICLOUD_GATEWAY_BROWSER_PROFILE_DIR=~/.icloud-code-gateway/browser-profile
```

管理页点击「登录更新」后会弹出本机 Chromium；完成 Apple 登录后自动捕获 Session。创建仍在本地完成，并同步到云端 edge。

本地脚本默认开启：

```dotenv
ICLOUD_GATEWAY_ADMIN_OPEN=1
```

即**本机管理页免管理员密码**。线上 edge 不设置该开关，生产管理员密码保持原样。无 Docker 本地也不再使用 noVNC，因此**没有 VNC 密码**。

本地 control 现已支持管理端 IMAP 取码：配好转发邮箱 IMAP 后，主页 Alias 列表每一行会显示最近 5 分钟验证码，并支持自动刷新。公开 `/api/code` 仍只在云端 edge 提供。

把下面这些写进 `~/Desktop/云贝/服务器相关/icloud-control-plane.env`（或项目 `.env`），本地启动脚本会自动注入：

```dotenv
ICLOUD_GATEWAY_IMAP_FORWARDING_EMAIL=you@example.com
ICLOUD_GATEWAY_IMAP_HOST=imap.mail.me.com
ICLOUD_GATEWAY_IMAP_PORT=993
ICLOUD_GATEWAY_IMAP_USERNAME=you@example.com
ICLOUD_GATEWAY_IMAP_PASSWORD=xxxx-xxxx-xxxx-xxxx
ICLOUD_GATEWAY_IMAP_FOLDER=INBOX
ICLOUD_GATEWAY_IMAP_JUNK_FOLDER=
```

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
- 只返回验证码语境附近的独立验证码；默认优先 6 位数字，Grok/xAI 邮件额外支持 `XXX-XXX` 字母数字码；同一邮件有多个候选时选择语境距离最近者。
- 以 IMAP `INTERNALDATE` 为主要时间依据，读取使用 `BODY.PEEK[]`，不把邮件标为已读。
- 可选只读扫描一个垃圾邮件文件夹；主文件夹与垃圾邮件文件夹共用一次登录和总超时，
  跨文件夹返回收件时间最新的验证码，管理员批量扫描仍保持 500 封总上限。
- OTP 和邮件正文不持久化；新签发的完整访问密钥使用环境主密钥进行 AES-GCM 加密保存，并继续以 SHA-256 哈希校验公开查询。
- Apple Session、IMAP 密码、Alias 远端 ID 和可恢复访问密钥密文均绑定用途加密；管理页初始 HTML 不包含完整密钥，只有显式查看动作才解密返回。
- HME Session 保存后自动导入完整远端 Alias 快照；重复刷新按邮箱幂等对账并保留本地标签、过滤条件和有效密钥。
- 停用、恢复和永久删除均在 Apple 写入后再次读取 HME 列表确认；确认前不改变本地状态，永久删除只允许失活 Alias 且需要输入完整邮箱。
- 管理页“查询记录”展示最近 7 天内最新 100 次验证码查询；Alias 邮箱快照加密保存，来源只保留不可逆指纹，不保存验证码、访问密钥或原始 IP。
- 管理员可手动刷新最近 5 分钟验证码，范围覆盖全部已导入 Alias，包括未签发访问密钥的 Alias；一次刷新最多扫描/返回 500 条，验证码只存在于当前响应和页面 DOM。
- Alias 邮箱文本可直接点击复制；每条 Alias 可在本地管理页标记为 GPT、Grok 或最多 80 字符的自定义用途。用途只是管理元数据，不参与 Apple、公开取码或 access key 校验。
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

把生成值写入 `ICLOUD_GATEWAY_MASTER_KEY`，再配置管理员密码、VNC 密码、域名和回国代理。生产环境应保持；其余会话维护与批量创建参数可按 `.env.example` 调整：

```dotenv
ICLOUD_GATEWAY_COOKIE_SECURE=1
ICLOUD_GATEWAY_HME_MAINTENANCE_SECONDS=21600
ICLOUD_GATEWAY_HME_FRESHNESS_SECONDS=3600
ICLOUD_GATEWAY_HME_RETRY_MAX_SECONDS=3600
ICLOUD_GATEWAY_ALIAS_BATCH_LIMIT=100
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
4. 配置转发邮箱 IMAP 和 App 专用密码；如需覆盖垃圾邮件，填写服务端显示的准确文件夹
   名称。保存动作会只读测试所有已配置文件夹。
5. Session 捕获成功后会自动导入已有 Alias；也可点击“导入 / 刷新”重新对账，随后为需要使用的活动 Alias 签发访问密钥。新签发/轮换的密钥可由管理员随时显式查看和复制；升级前只有哈希的旧密钥需先轮换。点击 Alias 邮箱可直接复制，并可用 GPT、Grok 或自定义用途按钮做本地标记。
6. 在“验证码”栏目手动读取全部 Alias 最近 5 分钟的验证码；该操作不要求 Alias 已配置访问密钥，也不会保存验证码。
7. 在“查询记录”查看哪些 Alias 被公开查询、查询结果、脱敏来源指纹和北京时间；已删除 Alias 的既有记录仍保留当时邮箱快照。

公开查询页位于 `https://<GATEWAY_DOMAIN>/`。

## 本地开发与测试

```bash
uv sync --extra dev
PYTHONPATH=. uv run pytest
uv run ruff check .
uv run python -m compileall -q icloud_gateway tests
```

完整部署、在线维护、代理验证、备份、恢复和回滚流程见 [OPERATIONS.md](OPERATIONS.md)。工程目标、控制闭环和逐节点验收记录见 [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)。

> 发布状态：`3030701` 已被并发与长任务审查标记为不可直接部署。修复分支必须完成 Alias 快照 CAS、SQLite 持久批任务、Compose 变量透传、有界停机及真实 Apple 会话的 validate/list 只读验收后，才能进入生产部署闭环。不要通过放宽 Cloudflare 超时部署同步批处理版本。

德国云贝服务器已有 Caddy 占用 80/443 时，使用 `docker-compose.server.yml`：它不会启动项目内置 Caddy，而是将 app/browser 以唯一别名接入现有 `app_yunbay-network`，并启动一个独立 Mihomo 进程复用联动小铺的代理订阅配置。对应 Caddy 站点片段在 `deploy/Caddyfile.icloud.yunbay.xyz`。

本次 IMAP 文件夹编码实现参考了 MIT 许可的
[IC-VeilMail](https://github.com/Redmig110/ic-veilmail)。完整归属与许可文本见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

## 明确不做

- 不保存或自动填写 Apple ID 密码、2FA 码、恢复密钥。
- 不向普通使用者开放收件箱、邮件正文、Alias 列表或 iCloud 管理能力。
- 不自动执行真实 HME Alias 生命周期操作；仅支持管理员显式选择最多 100 条后批量停用或永久删除。网关严格串行写入、逐条读取 Apple 状态确认并返回逐项结果；永久删除仅允许失活项且需要二次确认。
- 批量创建默认与硬上限均为 100，这不是 Apple 配额；普通 Alias 没有公开批量端点，仍由持久任务逐项串行 generate→reserve。Apple 返回 `-41015` 时按开源同款逻辑冷却 30 分钟（`ICLOUD_GATEWAY_HME_CREATE_COOLDOWN_SECONDS=1800`）后自动续跑；远端写结果未知时仍停止自动重放并对账。
- 不共享联动小铺正在运行的 Chromium 进程/profile。
- 不保证 Apple Session 永不过期；过期后管理员通过网站中的同一个浏览器重新登录维护。
