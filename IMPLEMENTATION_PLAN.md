# iCloud 五分钟验证码网关实施计划

## 1. 目标与验收指标

从 `Team-Workflow` 中提取 iCloud Hide My Email (HME) 会话、Alias 生命周期和 IMAP 精确收件能力，建立一个可独立部署的网站。管理员为每个 iCloud 隐藏邮箱签发一个高强度密钥；访客输入密钥后，只能查询与该密钥绑定的 Alias，且只返回当前时刻往前 300 秒内收到的最新 6 位验证码。

验收指标：

- 一个有效密钥在任意时刻最多绑定一个 Alias；密钥轮换后旧密钥立即失效。
- 邮件收件人必须精确匹配绑定 Alias，其他 Alias 或主邮箱的邮件不得返回。
- 以 IMAP `INTERNALDATE` 作为收件时间主依据，仅接受 `now - 300s <= received_at <= now + 60s` 的邮件；缺失时才回退到带时区的 `Date` 头。
- 公网 API 不返回邮件主题、正文、完整发件人、iCloud 会话、IMAP 凭据或 Alias 全文。
- 公网查询有界完成：默认 IMAP 操作超时 20 秒，全请求不因网络或 Apple 会话异常无限阻塞。
- HME Session 失效时禁止 Alias 写操作，保留上一份已验证数据，并向管理员显示可恢复状态。
- 密钥、Apple Cookie、IMAP 密码、VNC 密码不进入 Git、计划、日志、截图或普通 API 响应。
- Docker 服务重启后 60 秒内恢复 `/healthz` HTTP 200，SQLite 数据、浏览器 profile 和已签发密钥保持不变。
- Chromium 始终复用唯一的 `/browser-data/profile`，容器替换或异常退出后仍保留长期 Cookie，不允许捕获任务创建临时 profile。
- 管理员登录网站后可通过同域 HTTPS 的 `/admin/browser/*` 在线操作同一 Chromium；未登录请求不得取得 noVNC 页面、静态资源或 WebSocket。
- 德国服务器上的 iCloud CN 浏览器与 HME API 通过显式配置的回国 HTTP/SOCKS5 代理出站；代理已配置时故障必须失败关闭，不得静默改为德国 IP 直连。
- iCloud 与联动小铺复用同一 Playwright/Chromium 基础镜像层以减少磁盘占用，但保持独立进程、独立 profile、独立健康检查与重启边界。

## 2. 工程控制抽象

| 控制元素 | 本项目对象 |
| --- | --- |
| 目标 | 按 Alias 隔离地交付 5 分钟内的最新验证码 |
| 被控对象 | Apple HME 资源池、转发邮箱、IMAP 收件箱、持久 Chromium profile |
| 控制器 | FastAPI 管理服务、密钥映射、限流器、会话状态机 |
| 测量 | HME list 只读验证、IMAP TLS/登录检测、Alias 精确收件人、`INTERNALDATE`、健康端点 |
| 执行器 | HME generate/reserve、Session 加密更新、密钥签发/轮换/撤销、验证码响应 |
| 环境与扰动 | Apple 会话过期、HME 限频、IMAP 延迟、邮件时钟偏差、网络抖动、并发请求、公网扫描 |
| 稳定性策略 | 默认失败关闭、有界超时、只读验证先于写入、旧会话原子保留、无限重试禁止 |

最小充分闭环：

```text
配置一个转发 IMAP
  -> 导入或捕获一份通过 HME list 验证的 Session
  -> 创建或接管一个 Alias
  -> 签发一个密钥
  -> 向该 Alias 发送测试验证码
  -> 公开页输入密钥
  -> 只返回 300 秒窗口内的对应验证码
  -> 轮换密钥并证明旧密钥失效
```

这条闭环稳定、可重复后，再批量创建 Alias 和扩大并发。

## 3. 已有系统与 GitHub 经验

### 3.1 `Team-Workflow` 可复用部分

- `team_protocol/icloud_hme.py`：HME cURL/HAR 白名单解析、Session 验证、list/generate/reserve 客户端。
- `team_protocol/icloud_hme_capture.py`：隔离 profile、持久 Cookie/设备信任、可见登录、只捕获真实 `/v2/hme/list` 请求。
- `team_protocol/registrar_runtime/icloud_imap_provider.py`：IMAP TLS、SOCKS/HTTP 代理、Alias 精确匹配、`BODY.PEEK[]` 不标已读、6 位码提取。

提取时不引入 Team、ChatGPT、OpenBrowser、代理链、换班或账号池逻辑。源项目当前存在未提交差异，本项目只读参考，不修改源工作树。

### 3.2 GitHub 项目结论

- `heartmore/icloud-hme`：证明了多账号独立 Cookie/HME 会话与 Web 管理的可行性；其 JSON 明文凭据与全邮件缓存不适合本项目的公网密钥网关。
- `pwnapplehat/icloud-hide-my-email`：证明 HME 生成与 iCloud IMAP OTP 可以形成同一条闭环，并确认 iCloud IMAP 需使用 App 专用密码。
- `lukenmorris/icloud-hide-my-email-manager`：预览与写操作分离，破坏性操作需要额外确认。本项目首版不暴露停用或删除 Alias 能力。
- `not-knope/Hide-My-Email-iCloud-Manager`：再次证明 Apple Cookie 等价于高权限账号凭据，不能与普通用户查询服务共享边界。

采用结论：

- HME 管理面与 OTP 公开面完全分离。
- HME Session 和 IMAP 凭据只以环境主密钥加密存储。
- 不持久化邮件正文或 OTP；每次查询从 IMAP 只读测量。
- 远程 HME list 是 Alias 状态事实来源，本地数据不猜测 Apple 状态。
- 浏览器 profile 持久化且只允许一个容器持有；Chromium CDP 监听浏览器自身回环并经内部代理仅暴露在 Docker 网络，不映射宿主端口，app 每次捕获前动态解析 browser 容器地址；noVNC 原始端口只绑定服务器回环地址，并额外经管理员认证后的同域 HTTPS 路由提供在线维护。
- 联动小铺 Worker 使用 `mcr.microsoft.com/playwright:v1.61.1-jammy`；iCloud browser 使用同一基础镜像与其内置 Chromium，Docker 共享基础层，不把支付 Worker 的临时 BrowserContext 与 iCloud 长期 profile 放进同一浏览器进程。
- 回国代理参数只由服务器 Secret/`0600` 环境文件注入；浏览器整体出站与 HME HTTPS 请求使用同一代理端点，IMAP 是否走代理仍按其邮箱可达性独立配置。

## 4. 系统边界与架构

```text
公开浏览器
  -> POST /api/code (access key)
  -> 密钥哈希查询 + IP/密钥限流
  -> 获得唯一 Alias
  -> IMAP 只读扫描
  -> Alias + 300s + 6 位码筛选
  -> 无缓存 JSON 响应

管理员浏览器
  -> 管理员会话 + CSRF
  -> /admin/browser/* -> 管理员会话前置校验 -> noVNC -> 唯一持久 Chromium
  -> HME Session 导入/远程 Chromium 捕获
  -> HME list 只读校验
  -> Alias 同步/创建
  -> 密钥签发/轮换/撤销

FastAPI
  -> SQLite WAL (映射与状态)
  -> AES-GCM (凭据密文)
  -> iCloud HME HTTPS (德国机房经显式回国代理)
  -> iCloud/转发邮箱 IMAP TLS
  -> Chromium CDP (Docker 内网)

Browser runtime
  -> 复用联动小铺的 Playwright/Chromium 基础镜像层
  -> 独立 Chromium 进程 + 独立持久 profile
  -> 回国代理出站 + noVNC 管理界面
```

首版明确不做：

- 不保存或自动输入 Apple ID 密码、2FA 码、恢复密钥。
- 不向普通用户展示收件箱、邮件正文、HME 列表或 Alias 全文。
- 不开放 HME 停用、删除或 Apple 账户其他写操作。
- 不把 noVNC 原始端口或 CDP 端口直接公开到互联网；noVNC 只能经管理员认证后的 HTTPS 反向代理访问。
- 不在请求日志中记录原始密钥、OTP、Cookie 或 IMAP 密码。
- 不复用联动小铺正在运行的 Chromium 进程、临时 BrowserContext 或 profile，也不让其收款任务重启影响 iCloud。

## 5. 数据模型

### `settings`

- `key`：配置名。
- `encrypted_value`：AES-GCM 密文，用途名作为 AAD。
- `updated_at`：UTC 更新时间。

存放 HME Session、IMAP 配置、可选 HME/IMAP 代理。环境主密钥不入库。

### `aliases`

- `id`：UUID。
- `email`：Alias，管理面可见，公开面不返回。
- `anonymous_id`：Apple HME 远程 ID，加密存放。
- `label` / `note`：管理员标识。
- `sender_filter`：可选的发件地址或域名过滤。
- `state`：`active | inactive`。
- `access_key_hash`：高熵密钥 SHA-256，不存原文。
- `access_key_hint`：仅用于管理员识别的末 4 位。
- `key_issued_at` / `key_revoked_at`。
- `created_at` / `updated_at` / `last_synced_at`。

### `audit_events`

仅记录管理事件和脱敏查询结果，不记录密钥、OTP 或邮件正文。字段包含事件类型、Alias ID、结果、IP HMAC 摘要、UTC 时间。默认保留 7 天。

## 6. 安全边界

- 主密钥：`ICLOUD_GATEWAY_MASTER_KEY`，32 字节 URL-safe Base64，用于 AES-GCM 与会话签名派生。
- 管理员密码：通过 Docker Secret/环境注入，仅用 `hmac.compare_digest` 校验；不写 SQLite。
- 访问密钥：`icg_` 前缀 + 32 字节随机值，只在签发或轮换时显示一次。
- 管理会话：`HttpOnly` + `Secure` + `SameSite=Strict`，有限有效期，所有管理写请求校验 CSRF。
- 在线浏览器：Caddy 对 noVNC 页面、资源和 WebSocket 逐请求执行管理员会话前置校验；noVNC 继续要求独立 VNC 密码。
- 回国代理：端点和认证凭据不入库、不入 Git、不进程参数明文展示；已启用代理时无直连降级。Chromium 使用无认证内网 relay 的原生代理参数，避免凭据出现在进程参数；HME API 仍支持认证代理。
- 公开响应：`Cache-Control: no-store`、`Pragma: no-cache`、`Referrer-Policy: no-referrer`、严格 CSP。
- 限流：按 IP 和密钥摘要双维滑动窗口；连续失败时延长冷却，不影响其他密钥。
- 日志：应用层禁止打印 request body，所有异常转换为不包含上游响应正文的结构化错误。
- 服务器时钟：部署验收必须检查 UTC/NTP；时钟异常时 OTP 窗口失败关闭。

## 7. 实施节点

### 节点 A：项目骨架与密钥存储

- [x] 建立 FastAPI 应用、配置加载、SQLite WAL 与迁移。
- [x] 实现 AES-GCM 用途隔离存储、访问密钥签发/哈希/轮换/撤销。
- [x] 建立结构化日志脱敏和安全响应头中间件。

验证：数据库 `quick_check=ok`；重启后凭据可解密；错误主密钥不能静默返回错误明文。

### 节点 B：HME 客户端提取

- [x] 提取 cURL/HAR/request 解析和 Apple host/path/origin 白名单。
- [x] 提取 list/generate/reserve，保留强制直连或显式代理语义。
- [x] 导入 Session 前调用 list 只读验证；验证失败不覆盖旧 Session。
- [x] 同步远程 Alias 时以 HME list 为权威快照，但不自动签发密钥。

验证：白名单、字段缺失、会话过期、错误响应脱敏和创建 Alias 协议测试通过。

### 节点 C：持久浏览器会话

- [x] 支持连接 Docker 内网 Chromium CDP，复用唯一持久 profile。
- [x] 管理员启动捕获后，监听真实 `GET /v2/hme/list` 200 请求并提取最小 Session。
- [x] 捕获任务状态机：`idle -> waiting_login -> verifying -> captured | failed | cancelled`。
- [x] 捕获连接断开时不关闭远程 Chromium，不清理持久 profile。
- [x] 提供手动 cURL/HAR 导入作为可恢复备用路径。
- [x] 捕获任务只连接唯一 CDP 浏览器，不启动或切换临时 profile。

验证：重启 app 不丢 profile；取消/超时不存无效 Session；已登录 profile 可再次捕获。

### 节点 D：IMAP 五分钟 OTP 测量器

- [x] 提取 IMAP TLS 和可选 HTTP/SOCKS5 代理。
- [x] 增加 `INTERNALDATE BODY.PEEK[]` 只读获取，精确解析多种转发收件头。
- [x] 仅扫描有界 UID 集合，对 Alias、时间窗、可选 sender 过滤后再提取 6 位码。
- [x] 多封合格邮件时返回 `received_at` 最新的一封，不依赖 UID 大小猜测时间。
- [x] IMAP 登录失败、网络超时和无码邮件返回不泄露上游细节的状态。

验证：299/300/301 秒边界、未来时钟、缺失时间、其他 Alias、HTML/纯文本、重复码和多封排序测试通过。

### 节点 E：公开查询闭环

- [x] `POST /api/code`：密钥校验、双维限流、IMAP 线程隔离、统一脱敏错误。
- [x] 公开页不保存密钥到 URL、cookie、localStorage 或 sessionStorage。
- [x] 页面支持查询、有界自动轮询、复制验证码、结果过期清除、加载/无码/错误/限流状态。
- [x] 服务端与浏览器端均禁止缓存敏感响应。

验证：密钥串号、无效/撤销密钥、限流、超时、无代码和成功响应 API 测试；浏览器历史中不出现密钥。

### 节点 F：管理工作台

- [x] 管理员登录/退出、会话签名和 CSRF。
- [x] HME Session 状态、手动导入、远程浏览器捕获、HME list 同步。
- [x] IMAP 配置与只读连接检测，秘密字段留空表示保留旧值。
- [x] 创建 1-5 个 Alias，每个 Alias 创建后可单独签发密钥。
- [x] 同步已有 Alias，为选中项签发/轮换/撤销密钥。
- [x] 管理页只显示密钥末 4 位；完整密钥只在创建结果模态框中显示一次。

验证：未登录、CSRF 失败、Session 失效、IMAP 失败、Alias 重复、密钥轮换/撤销测试通过。

### 节点 G：前端与可访问性

设计判断：面向受邀用户的安全工具页和管理员工作台，信任优先、克制、可扫描。

- `DESIGN_VARIANCE: 3`
- `MOTION_INTENSITY: 2`
- `VISUAL_DENSITY: 6`
- 服务端渲染 + 原生 CSS/JavaScript，自动跟随浅色/深色系统主题。
- 石墨中性色 + 单一青绿强调色；输入、按钮、错误和焦点对比度达到 WCAG AA。
- 统一 6px 边角；仅在重复 Alias 项与模态框使用卡片。
- 动效只用于加载和状态变化，完整支持 `prefers-reduced-motion`。

验证：`1440x900`、`1024x768`、`390x844`、`360x800`下无横向溢出、按钮换行、文字重叠或不可达操作；键盘可完成全流程。

- [x] 公开页与管理页完成上述四个视口的浅色/深色浏览器验收。
- [x] Lucide 图标全部从官方 `lucide-static` 包提取并保留许可证。
- [x] 浏览器控制台和网络错误为 0，移动端按钮尺寸与一次性密钥模态框通过验收。

### 节点 H：容器与服务器部署

- [x] App Dockerfile：非 root 运行、健康检查、持久 `/data`。
- [x] Browser Dockerfile：复用联动小铺的 Playwright/Chromium 基础镜像层，独立 Chromium + Xvfb + noVNC，唯一持久 `/browser-data/profile`，跨容器独占锁与异常恢复，Chromium CDP 回环监听并通过不映射宿主的内部代理供 app 使用。
- [x] 德国机房出站：browser 和 HME API 复用同一回国代理配置，支持 HTTP/SOCKS5，代理开启后故障失败关闭；browser 使用无认证内网 Mihomo 端点，HME API 可选认证；生产使用独立 Mihomo 进程，避免与收款 Worker 共用重启边界。
- [x] Docker Compose：app/browser/Caddy，持久卷，重启策略，noVNC 原始端口只绑定 `127.0.0.1`；共享服务器覆盖文件禁用内置 Caddy并接入既有 edge 网络。
- [x] Caddy HTTPS：域名环境变量、安全头、请求体限制、管理员认证后的 `/admin/browser/*` noVNC 页面与 WebSocket 转发。
- [x] 运维手册：网站内在线登录、SSH 隧道备用访问、Session 续期、备份、恢复、密钥轮换。

验证：Compose 冷启动、异常重启、SQLite 持久化、Chromium Cookie/profile 持久化、基础 Chromium 镜像层复用、代理出口 IP/故障关闭、CDP/noVNC 暴露边界、未登录 noVNC 拒绝、登录后页面与 WebSocket 可用、HTTPS 与备份恢复演练通过。

### 节点 I：验收、GitHub 与清理

- [x] 运行全量测试、Python 编译、静态检查；提交前继续执行 `git diff --check`。
- [x] 扫描密钥、Cookie、邮箱授权码、真实邮箱和私有主机名；当前命中仅为测试 canary。
- [x] 用 Playwright 完成公开页/管理页桌面与移动截图、控制台和网络错误检查。
- [x] 建立私有 GitHub 仓库，提交并推送 `main`。
- [ ] 清理测试缓存、截图临时件、构建产物和未使用的容器，保留源码与必要 QA 记录。
- [ ] 在实际服务器执行最小停机部署和线上闭环验收。

当前线上闭环（2026-07-26）：

- [x] 独立 browser、app、cn-proxy 在生产持续健康，异常 Chromium 可在 60 秒内自恢复且不重启 app。
- [x] Cloudflare 控制面新增 `icloud.yunbay.xyz A 13.140.180.223`，开启代理并使用自动 TTL。
- [x] Cloudflare 权威服务与 Cloudflare/Google 公共 DoH 返回该代理记录。
- [x] 为共享 Caddy 主动健康检查显式设置 `Host: icloud.yunbay.xyz`，回归测试和候选配置验证通过。
- [x] 备份共享 Caddyfile，使用 graceful reload 上线站点；Caddy 与业务容器重启计数保持不变。
- [x] 公网 `/healthz`、首页、管理员登录、未登录 noVNC 拒绝以及登录后 noVNC WebSocket/RFB 全部通过。
- [x] 修正 CDP 运维探针并把服务器部署结果写回本文件、`OPERATIONS.md` 与云贝唯一连接手册。
- [x] 全量门禁通过，提交并推送 GitHub `main`，随后只清理本任务 builder、QA 标签和悬空镜像。
- [x] 清理后复测服务并记录 `docker system df` 与根卷可用空间前后值，最终工作树保持干净。

验证：GitHub `main` 与本地 HEAD 一致；工作树只保留用户明确要求保留的本地运行数据。

## 8. 测试矩阵

| 类别 | 必须覆盖 |
| --- | --- |
| 密钥 | 签发、哈希、串号、轮换、撤销、并发唯一性 |
| HME | cURL/HAR/request、host/path/origin 白名单、list、generate/reserve、Session 过期 |
| 捕获 | 唯一持久 profile、长期 Cookie、CDP 断连不关浏览器、取消、超时、无 list、无效 list |
| IMAP | Alias 精确匹配、转发头、`INTERNALDATE`、299/300/301 秒、HTML、附件排除、发件人过滤 |
| API | 成功、无码、无效密钥、撤销密钥、限流、IMAP 超时、无缓存头 |
| 管理 | 登录、会话过期、CSRF、noVNC 前置认证、凭据保留语义、Alias 同步/创建、密钥只显示一次 |
| 部署 | 非 root、持久卷、基础 Chromium 镜像层复用、回国代理/无直连降级、健康检查、异常重启恢复、CDP/noVNC 暴露边界、备份/恢复 |
| UI | 浅色/深色、键盘、减少动效、桌面/移动、加载/空/错误/成功状态 |

## 9. 部署与回滚

部署前：

1. 生成主密钥、管理员密码和 VNC 密码，以服务器 Secret/权限 `0600` 环境文件保存。
2. 从联动小铺已有 Secret 取得回国代理的协议、端点和可选认证信息，注入 iCloud browser 与 HME API，不复制到仓库。
3. 验证域名 DNS、80/443 端口、服务器 NTP 以及 browser/HME 代理出口 IP。
4. 如有旧数据，先使用 SQLite 在线备份并检查 `quick_check`。
5. 先启动 browser 与 app，健康后再让 Caddy 切换公网流量。

回滚：

- 代码回滚到上一个 Git 标签/镜像，不删除 SQLite 和 browser profile 卷。
- 只在迁移或启动导致数据库异常时恢复部署前备份。
- 回滚不撤销已经在 Apple 远程成功创建的 Alias；上线后通过 HME list 只读同步对账。
- 60 秒内健康检查不恢复时立即回滚，不把服务留在等待人工输入的停机状态。

## 10. 当前假设与外部输入

- 首版支持一个 iCloud HME 资源池和一个转发 IMAP，数据模型不阻塞以后扩展多资源池。
- 验证码形式以现有 `Team-Workflow` 业务的 6 位数字码为准。
- 隐藏邮箱邮件已转发到可用 IMAP 登录的邮箱。
- 联动小铺现有 Worker 已证明代理参数形式为 server + 可选 username/password；最终上线前仍需在服务器上只读确认实际协议、出口 IP 和 Secret 文件位置。
- 最终上线需要实际服务器 IP/主机名、SSH 用户/密钥路径和域名。这些信息不影响本地实现与容器验收。
- 本计划不记录任何真实 Apple/IMAP/API 凭据。

## 11. 实施记录

- 2026-07-25：完成源项目只读检查。确认 `Team-Workflow` 工作树存在用户未提交差异，本项目不修改该工作树。
- 2026-07-25：完成 GitHub 相近项目检索与架构取舍，选定“HME 管理面与 OTP 公开面分离、不持久化邮件正文、服务器持久 Chromium profile”方案。
- 2026-07-26：根据新增要求，将“唯一长期 Cookie 浏览器”和“管理员登录后通过同域网站在线可视化维护 iCloud”升级为强制验收项；SSH 隧道保留为故障恢复备用路径。
- 2026-07-26：故障注入证明 app/browser 共享网络命名空间会在 browser 单独重启后留下旧网络引用，方案改为独立容器网络 + browser 动态地址解析，消除必须人工重启 app 的失稳点。
- 2026-07-26：根据德国服务器与联动小铺现有回国代理，方案调整为复用同一 Playwright/Chromium 基础镜像层、独立 iCloud 进程/profile，browser 与 HME API 同代理出站且无直连降级。
- 2026-07-26：本地容器验收通过。Browser 7 秒健康，固定 UID 102 无损接管旧 profile；Chromium 故障注入后 browser 独立恢复，app 未重启，Cookie 与 SQLite Alias 保持。Caddy WebSocket 认证缺陷已修复，登录后 101/RFB、未登录 303。SQLite/profile 备份恢复演练通过，实际 browser 停机约 28 秒。
- 2026-07-26：只读审计德国服务器。现有 Caddy 独占 80/443；联动小铺 Mihomo 仅绑定共享网络命名空间回环。生产方案确定为：复用其订阅配置但运行独立 `cn-proxy`，app/browser 通过唯一别名接入既有 Caddy 网络，不修改或重启收款 Worker。
- 2026-07-26：私有 GitHub 仓库 `chenli17683185032-ai/icloud-code-gateway` 已建立，首个完整实现提交已推送 `main`。服务器部署目录选定为 deploy 用户可控的 `/opt/new-api/icloud-code-gateway`，不依赖 sudo。
- 2026-07-26：生产首次启动发现 `proxychains4` 不接受 Docker DNS 名 `cn-proxy` 作为首个代理节点；故障 browser 已停止，app 与独立 cn-proxy 保持健康。修复限定为 browser 配置渲染时将代理主机解析为容器网络 IPv4，解析失败继续失败关闭；HME 请求仍保留 `socks5h` 主机名语义。当前节点为 H 的线上闭环验收，完成后进入 I 的定向清理。
- 2026-07-26：browser 代理 DNS 修复完成本地闭环：新增解析成功与失败关闭回归测试，全量 `79 passed`；Ruff、compileall、Compose 合并配置与 `git diff --check` 均通过。
- 2026-07-26：服务器验证发现 proxychains 的 `LD_PRELOAD` 与 Chromium 多进程冲突，Chromium 在 CDP 就绪后以 133 退出，随后 fluxbox 令清理阶段失去上界。隔离 QA 证明 Chromium 原生 SOCKS5 可稳定加载真实 iCloud CN 页面；实现调整为启动时解析/校验代理后传入原生 `--proxy-server`，并为所有子进程增加 5 秒有界清理与最终 KILL。
- 2026-07-26：`icloud.yunbay.xyz` 生产入口上线。共享 Caddy 主动健康检查曾因 Docker 别名 Host 被 Trusted Host 中间件返回 400，现已固定携带域名 Host；候选与挂载配置均通过 Caddy 2.11.4 验证并 graceful reload，Caddy/app/browser/cn-proxy 全程 `restart=0`。公网健康、首页、管理员登录、未登录 noVNC 303、登录后 noVNC 200 以及 WebSocket 101/RFB 3.8 均通过。浏览器 profile 尚未完成真实 Apple 登录，管理员首次登录仍是唯一外部输入。
- 2026-07-26：完成定向清理。删除专用 `icg-builder-20260726`、两个 `qa-*` 标签、本任务 2026-07-26 悬空 browser 镜像和 Caddy 候选临时件；保留 `latest/prod/rollback-37adf3f`、三个正式数据卷、默认 builder 及其他项目 2026-07-16 的两个悬空镜像。清理前 Docker 为 Images `61.54GB`、Volumes `4.965GB`、Build Cache `57.65GB`，根卷可用 `56GB`；清理后分别为 `61.54GB`、`1.02GB`、`57.65GB`，根卷可用 `59GB`。SQLite quick check、Chromium CDP、公网健康和 noVNC 未登录拒绝复测通过，容器仍全为 `restart=0`。
- 2026-07-25：建立本计划。当前节点为 A，尚未修改业务代码、访问 Apple 远程写接口或部署服务。
- 2026-07-25：完成节点 A-D 的本地实现；`ruff` 通过，47 项单元测试通过。新增覆盖 AES-GCM 用途隔离、Alias 密文、密钥轮换/撤销、HME 白名单、持久 Chromium 所有权边界、IMAP `INTERNALDATE` 与 299/300/301 秒边界。当前节点转为 E，仍未访问真实 Apple/IMAP 或调用远程写接口。
- 2026-07-25：完成公开查询页、管理员登录页、管理工作台和对应 FastAPI 路由初版。Web 验收前基线为 56 项测试通过；发现 3 个 Python 文件仅有格式化差异，模板引用的 Lucide 图标尚待从官方包落盘。当前继续节点 E-G 的接口测试、静态检查与浏览器验收。
- 2026-07-25：完成节点 E-G。新增 Web/API、管理员会话、CSRF、请求体限制、Trusted Host、批量部分成功、凭据失败不覆盖等回归测试；IMAP 超时改为贯穿整次测量的总截止时间。当前 66 项测试、`ruff`、Python 编译和 `git diff --check` 通过。浏览器在 `1440x900`、`1024x768`、`390x844`、`360x800` 的浅色/深色视口完成验收，网络和控制台错误为 0。当前节点转为 H。

## 12. `82463cf` 安全与性能修订审查计划

### 12.1 目标与验收指标

目标是审查 `harden-gateway` 上的 10 项修订，先证明安全边界与性能改造形成可重复闭环，再合并并推送 GitHub `main`。CDP 9222 网络归属不在本轮范围。

- 空或不可解析的 HME 快照不得改变已有 Alias 状态和访问密钥。
- Caddy 必须丢弃客户端伪造的来源头，同时保留 Cloudflare 后真实客户端 IP 的可信恢复路径。
- Unicode 管理密码和 CSRF 输入只产生确定的认证结果，不返回 500。
- 120 个候选邮件的正常 IMAP 路径不超过 6 次 UID 往返；组合 SEARCH、批量 FETCH、单 UID 回退和总截止时间都保持兼容。
- SQLite 每线程只建立一个连接，事务失败可回滚，线程退出/服务关闭不留下错误状态，审计数据长期有界。
- 无 `Content-Length` 的分块请求超过 2 MiB 时返回 413，小请求仍完整到达路由。
- Alias 活动项优先、管理会话失效跳转、代理默认失败关闭和 Docker 构建上下文排除规则均有验证。
- 全量测试、Ruff、Python 编译、Compose/Caddy 配置、`git diff --check` 和秘密扫描通过。
- 最终 `main` 与 `origin/main` 一致，工作树干净；不自动部署生产服务器。

### 12.2 控制结构与扰动

| 控制元素 | 本轮对象 |
| --- | --- |
| 目标 | 消除破坏性同步、身份伪造和无界资源使用，同时降低查询时延 |
| 被控对象 | HME 映射、访问密钥、IMAP 会话、SQLite、Caddy 信任边界、管理会话 |
| 测量 | 回归测试、UID 命令计数、事务/线程试验、Caddy 适配、Compose 展开、静态检查 |
| 控制器 | 失败关闭、字节常量时比较、可信头重建、批处理、线程本地连接、周期清理 |
| 扰动 | Apple 空响应、Cloudflare 转发、Unicode、IMAP 能力差异、分块传输、并发线程、代理缺失 |
| 稳定性策略 | 先拒绝不确定状态，再优化速度；所有回退有界且不改变选择语义 |

### 12.3 GitHub 经验复核

- Caddy 同类部署在可信代理链中区分直连对端与解析后的客户端 IP；生产站点不能直接信任任意 `CF-Connecting-IP`。
- Python 项目在 `hmac.compare_digest` 前统一为 UTF-8 字节，保持精确比较并兼容非 ASCII 输入。
- IMAP 客户端以组合 SEARCH 和 UID sequence-set FETCH 降低往返，但需兼容服务端能力差异及不支持集合的服务器。
- SQLite 长寿命连接通常按线程归属并由显式事务控制；连接复用必须验证异常回滚和关闭后重建。

### 12.4 实施节点

- [x] 节点 1：确认正确仓库、提交、分支和远端；`82463cf` 当前仅在本地 `harden-gateway`。
- [x] 节点 2：逐文件审查实现与测试；确认核心修复通过，发现 Caddy 默认信任模型说明错误、Cloudflare 真实客户端链未建立，以及多项共享行为缺少直接测试。
- [x] 节点 3：修正 Cloudflare 可信代理边界、覆盖无长度流请求，并补齐数据库、配置、Unicode、前端会话和精确 IMAP 往返测试；运行时注入继续发现并移除无条件 `CF-Connecting-IP` 覆写，同时补上 SQLite 提交失败回滚与 IMAP bytes 能力识别。
- [x] 节点 4：全量 `104 passed`；Ruff、格式化、Python 编译、Compose 基础/服务器覆盖、Caddy 2.11.4、来源头运行时注入、diff 与秘密扫描通过。
- [x] 节点 5：审查修正提交为 `f2f4d3c`，已快进合并并推送 GitHub `main`；本地 `harden-gateway` 分支、Caddy 探针容器/网络和临时 `caddy:2.11.4` 镜像均已清理。

### 12.5 回滚边界

- 本轮只提交和推送代码，不部署服务器；生产上线仍需先备份 SQLite 与共享 Caddy 配置，再以健康检查和 60 秒内回滚为约束。
- 任何代理、来源 IP 或上游快照无法建立可信事实时均失败关闭，不采用直连或破坏本地状态作为恢复手段。

### 12.6 验证记录

- `82463cf` 原始提交通过 `95 passed`；补齐边界修正与直接测试后为 `104 passed`，唯一警告是 TestClient 的上游迁移提示。
- 真实 Uvicorn HTTP/1.1 分块试验：3 MiB 返回 413，界内 JSON 正常到达路由；直接 ASGI HTTP/2 无长度流同样返回 413。
- Caddy 2.11.4 源码确认默认不信任任何代理并丢弃不可信 `X-Forwarded-*`。独立配置移除多余覆写；生产片段只对 Cloudflare 官方网段信任 `CF-Connecting-IP`，直连请求仍失败关闭。
- Caddy 运行时注入证明仅配置 `trusted_proxies` 不能约束无条件 `header_up CF-Connecting-IP`；现改为先以直连 `remote_ip` 匹配 Cloudflare，再重写请求 XFF，并由同一组网段建立反代信任边界。
- 来源头运行时闭环：直连请求同时伪造 XFF 与 `CF-Connecting-IP` 时，上游只收到直连 socket IP；Cloudflare 网段对端得到 `CF-Connecting-IP, Cloudflare-peer`，Uvicorn 继续解析首个真实客户端地址。
- 本地 Caddy 配置、站点片段以及服务器当前完整共享 Caddyfile 加新片段均只读验证通过；未 reload、未部署生产服务。
- Compose 两种展开配置通过；跟踪文件秘密扫描无 API Key、私钥或长 Apple Session 命中，`.env.example` 保持允许跟踪。
- 原始功能提交 `82463cf` 与审查修正 `f2f4d3c` 已进入远端 `main`；本节最终状态作为后续纯文档提交推送，完成后再次核对本地 `main == origin/main` 与干净工作树。

## 13. `91369cf` 生产部署计划

### 13.1 目标与验收指标

目标是在不改变 browser、cn-proxy、SQLite 卷和 CDP 网络归属的前提下，把 GitHub `main` 的 `91369cf` 部署到云贝服务器，并上线新的 Cloudflare 来源 IP 信任边界。

- 构建期间旧 app 持续提供服务；正式 app 替换必须有 60 秒上界，失败立即恢复旧镜像。
- 部署前完成 SQLite 在线备份和 `quick_check=ok`，部署后再次确认数据库完整。
- 只重建 app；browser、cn-proxy、共享 Caddy 和其他云贝服务不得重启。
- 共享 Caddyfile 先生成候选、用运行中 Caddy 2.11.4 验证，再原位更新并 graceful reload；验证失败不改运行时配置。
- 公网 `/healthz`、首页、管理员登录、未登录 noVNC 拒绝、分块超限 413 和安全响应头全部通过。
- app 恢复健康应小于 60 秒；browser/cn-proxy/Caddy 的 restart count 前后不变。
- 服务器部署标记记录实际功能提交 `91369cf`；本地计划、运维手册和云贝唯一连接手册写回结果后推送 GitHub `main`。
- 最终本地 `main == origin/main`，工作树干净，候选容器、临时文件和无用镜像全部清理。

### 13.2 控制结构与扰动

| 控制元素 | 本次部署对象 |
| --- | --- |
| 目标 | 以最小中断上线安全、正确性和性能修订 |
| 被控对象 | app 容器、SQLite 数据卷、共享 Caddy 配置、Cloudflare 请求链 |
| 测量 | 镜像/提交身份、容器健康、restart count、SQLite quick check、HTTP 探针、Caddy validate |
| 执行器 | 非删除式源码同步、app 镜像构建、单容器 force-recreate、Caddy graceful reload |
| 扰动 | 构建失败、镜像启动失败、数据库锁、Caddy 配置错误、Cloudflare 缓存/链路时延 |
| 稳定性策略 | 旧服务先持续运行；候选先验证；切换有界；任一步失败立即回滚且不等待人工输入 |

### 13.3 GitHub 与既有经验复核

- 本项目 `873a6c3` 的首次边缘部署已证明：app/browser/cn-proxy 独立重启边界、共享 Caddy graceful reload 和持久卷保持方案可行。
- `82463cf`、`f2f4d3c` 的审查闭环证明：本次只需更新 app 源码、Compose 默认值和生产 Caddy 站点；browser 镜像与 CDP 网络无需变化。
- 沿用 Docker Compose 的“构建不影响运行容器，随后 `--no-deps --force-recreate --no-build app`”模式；沿用 Caddy 的“候选 validate 成功后 reload”模式。
- 服务器部署目录不是 Git 工作树，因此只同步 Git 跟踪内容，不使用 `--delete`，不覆盖 `.env`、secrets、备份或持久数据。

### 13.4 实施节点

- [x] 节点 1：只读核对服务器版本、容器健康/restart count、磁盘、NTP、Compose 展开、当前 Caddy 与部署标记；SQLite 与公网健康正常，Cloudflare 22 个网段与官方列表一致。
- [x] 节点 2：建立 SQLite、共享 Caddyfile、当前 Git 跟踪源码和旧 app 镜像回滚点；备份目录为 `/opt/new-api/icloud-code-gateway/backups/hardening-20260726T170444Z-91369cf`。
- [x] 节点 3：非删除式同步 `91369cf` 跟踪文件并逐文件校验；候选镜像 `sha256:906ffbefc34aff91ad762523cae37859b42c7d5a937acc3169e88edf865fe99e` 在独立临时 SQLite 卷上通过健康、完整性与 Unicode 认证验证。
- [x] 节点 4：服务器侧独立 60 秒 watchdog 仅替换 app，新容器 10 秒恢复健康；SQLite、内部/公网健康和 revision 通过，未触发回滚。
- [x] 节点 5：完整共享 Caddy 候选与挂载配置均通过 2.11.4 验证，原位写入后 graceful reload；inode、权限、容器 ID 和 `restart=0` 保持不变。
- [x] 节点 6：公网与内部闭环通过；分块超限 413、小分块正常到达路由、Unicode 错误口令 401、来源头可信链、SQLite/CDP、安全头、日志和磁盘均正常，四个容器均为 `restart=0`。
- [x] 节点 7：更新服务器部署标记、`OPERATIONS.md` 和云贝唯一连接手册，清理临时件；本地最终门禁为 `104 passed`、Ruff/格式/编译/diff/秘密扫描通过，最终记录提交并推送 GitHub `main`。

### 13.5 回滚边界

- app 失败：把部署前镜像重新标记为 Compose app 镜像，60 秒内 force-recreate，只恢复代码容器，不恢复或删除 SQLite 卷。
- Caddy 候选失败：不写正式文件；正式文件写入后验证失败则立即从备份原位恢复，不 reload 失败配置。
- 上线后业务异常：优先回滚 app 镜像与 Caddyfile，保留数据库和 browser profile 现场；远端 Apple Alias 不做任何写操作。

### 13.6 生产验证记录

- 2026-07-27：部署前 app/browser/cn-proxy/Caddy 均为 `healthy / restart=0`，NTP 正常，根卷可用 59GB；SQLite 在线备份与生产库 `quick_check=ok`。Cloudflare 配置的 22 个 IPv4/IPv6 网段与官方列表完全一致。
- 回滚目录为 `/opt/new-api/icloud-code-gateway/backups/hardening-20260726T170444Z-91369cf`；旧 app 镜像为 `sha256:36e201c7c852e3270cb3fbc3da16633e5d573d68c31fb837050b7ac1a0eeb204`，保留标签 `rollback-pre-91369cf-20260726T170444Z`。备份内保留旧源码、SQLite、Caddy、前后元数据和 watchdog 日志，最终清单哈希为 `cbcc6b9b0d7a06b536454ea167253428ac58a31365536fca0b224dcff27bc0ed`。
- 服务器非删除式同步 Git 提交 `91369cf1c54fb5161b4cfc5f8953c95e94878ac2`，69 个跟踪文件逐项 SHA-256 验证通过，`.env` 保持 `0600`。候选镜像在独立临时卷上通过健康、SQLite 与 Unicode 口令比较验证。
- 服务器侧独立 60 秒 watchdog 只 force-recreate app；新 app 在 10 秒内恢复健康，镜像为 `sha256:906ffbefc34aff91ad762523cae37859b42c7d5a937acc3169e88edf865fe99e`，没有触发回滚。browser、cn-proxy 与 Caddy 的容器 ID、启动时间和 restart count 均未变化。
- 共享 Caddy 候选只新增 Cloudflare 来源头可信边界，完整配置和挂载配置均通过运行中 Caddy 2.11.4 验证；原位写入保持 inode/权限，graceful reload 后 Caddy 容器 ID 不变且 `restart=0`。最终 Caddy SHA-256 为 `90dec04b2024eeabaa67f0dcde1d254e3ed02ce9f4bd6f9dc550f7c51f13c3f8`。
- 公网 `/healthz`、首页、管理员登录为 200，未登录 noVNC 为 303，中文错误口令为 401；安全响应头完整。应用直连 3 MiB 分块请求为 413，小分块表单为 401。直连源站伪造 XFF/`CF-Connecting-IP` 被丢弃，经 Cloudflare 请求恢复真实出口 `202.8.9.242`。
- 生产标记 `.icloud-code-gateway-deploy-sha` 为完整功能提交；新镜像保留 `latest/prod/release-91369cf`，旧镜像只保留专用 rollback 标签。候选容器、临时卷、候选标签、部署锁和一次性脚本均已清理，没有本轮悬空镜像；未修改 CDP 9222 网络归属，未执行 Apple/HME 远端写操作。
