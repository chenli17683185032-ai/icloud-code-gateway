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

## 14. 历史 Alias 导入与 iCloud 管理闭环

### 14.1 目标与验收指标

目标是把现有网关从“只能创建新 Alias”扩展为可管理当前 Apple 账户全部 Hide My Email Alias 的管理面，同时保持远端 Apple 状态为唯一事实源。

- 保存或捕获有效 HME Session 后，自动把完整远端 Alias 快照导入本地；手动刷新仍可重复执行且不产生重复记录。
- 生产实测的 107 条历史 Alias（97 条活动、10 条失活）应全部可见；导入时保留已有本地标签、备注、发件人过滤和访问密钥。
- 任一畸形、重复、空且不可信的远端快照均拒绝对账，不把本地 Alias 或访问密钥批量失活。
- 所有活动历史 Alias 均可签发或轮换访问密钥；失活 Alias 不得签发密钥。
- 活动 Alias 可从管理页请求 Apple 远端停用；只有再次读取 Apple 列表确认 `isActive=false` 后，才把本地状态改为失活并撤销访问密钥。
- 失活 Alias 可请求 Apple 远端恢复；只有再次读取 Apple 列表确认 `isActive=true` 后，才恢复本地活动状态。
- 永久删除只允许已失活 Alias，管理员必须输入完整 Alias 邮箱确认；只有 Apple 列表确认远端 ID 已消失后，才删除本地记录。
- 远端写失败、状态未确认、Session 失效或代理异常时，本地状态保持不变并返回有界错误；不自动重试远端写请求。
- CSRF、管理员会话、确认字段、动作状态约束和错误映射均有直接回归测试。
- 全量测试、Ruff、格式、Python 编译、`git diff --check` 和秘密扫描通过。
- 部署前完成 SQLite 在线备份；构建期间旧 app 持续服务，只替换 app，60 秒 watchdog 失败自动回滚；browser、cn-proxy、Caddy 和 CDP 网络归属不变。
- 部署后只执行 HME list 读取和本地导入，不自动停用、恢复或删除任何真实 Apple Alias；最终更新 `OPERATIONS.md`、云贝唯一连接手册并推送 GitHub `main`，本地工作树保持干净。

### 14.2 控制结构与扰动

| 控制元素 | 本轮对象 |
| --- | --- |
| 目标 | 完整发现历史 Alias，并对远端生命周期和本地访问密钥建立一致管理 |
| 被控对象 | Apple HME Alias、SQLite Alias 映射、访问密钥、管理端操作状态 |
| 测量 | `/v2/hme/list` 完整快照、`anonymousId`、`isActive`、本地状态/密钥计数、HTTP 回归探针 |
| 控制器 | 快照校验与幂等对账、远端动作后读回确认、显式确认、状态机约束、审计事件 |
| 执行器 | HME deactivate/reactivate/delete、SQLite upsert/state/delete、访问密钥签发/撤销 |
| 扰动 | Apple 私有接口变化、Session 过期、代理时延、响应成功但状态传播延迟、重复点击、并发签发、畸形快照 |
| 稳定性策略 | 远端写不盲重试；读回未确认则不改本地；不确定快照失败关闭；部署失败 60 秒内回滚 |

### 14.3 GitHub 经验与生产测量

- GitHub 同类实现 `Yimikami/icloud-hme-manager`、`fewhnhouse/hide-my-email` 和 `banana2556/hme-manager` 均使用 `/v2/hme/list` 返回 `result.hmeEmails`，使用 `{anonymousId}` 调用 `/v1/hme/deactivate`、`/v1/hme/reactivate` 与 `/v1/hme/delete`。
- 多个实现明确永久删除只适用于已经停用的 Alias；因此管理端把“停用”和“永久删除”分成两个状态受限动作，不把普通删除按钮直接映射为不可逆操作。
- 2026-07-27 对生产执行只读测量：Apple 一次返回 107 条、无分页字段，字段完整且远端 ID/邮箱均无缺失；其中 97 条活动、10 条失活，本地当前为 0 条。缺口确认是 Session 保存只验证列表却不导入，以及管理端缺少远端生命周期操作，而不是 Apple 分页或过滤。
- 当前数据库已经支持按邮箱幂等导入、保留本地配置、活动 Alias 签发/轮换密钥以及失活时撤销密钥；本轮复用这些边界，只增加完整快照校验、自动导入、远端动作确认和本地永久删除。

### 14.4 实施节点

- [x] 节点 1：核对本地 `main`、GitHub 相近实现和生产只读 HME 快照；确认远端 107 条、本地 0 条以及接口状态机。
- [x] 节点 2：补充 HME 客户端、服务层、数据库和 Web 回归测试，覆盖自动导入、历史 Alias 发 key、畸形/部分快照拒绝、停用/恢复/永久删除的顺序与确认边界。
- [x] 节点 3：实现客户端与服务状态机，复用统一快照校验/对账路径；补充本地记录删除能力和审计结果，远端写请求不自动重试。
- [x] 节点 4：扩展管理列表、操作按钮和确认交互，调整桌面/移动布局与操作文案；更新 README 和运维说明。
- [x] 节点 5：运行全量测试、静态检查和本地 HTTP/UI 验收；确认无凭据进入 Git 跟踪内容。使用 107 条隔离演示数据验证 1440×900、820×900、390×844：无水平溢出、按钮裁切或重叠；原生 `prompt()` 兼容缺陷已改为站内删除确认框，错误邮箱只显示本地校验且不发送请求，控制台错误为 0。
- [x] 节点 6：功能提交 `20f17f5` 已推送 GitHub `main`；服务器完成部署前备份，独立 60 秒 watchdog 只替换 app，新容器 10 秒恢复健康且未触发回滚。
- [x] 节点 7：生产只读 HME 同步确认本地 107 条、97 条活动、10 条失活、0 条已配 key；未调用真实停用/恢复/删除。两份运维记录随本次收口提交更新，服务器与本地临时件已清理。

### 14.5 状态机与回滚边界

| 当前状态 | 允许动作 | 成功反馈 | 本地结果 |
| --- | --- | --- | --- |
| 活动 | 签发/轮换 key、撤销 key、停用 | 同一远端 ID 为 `isActive=false` | 失活并撤销 key |
| 失活 | 恢复、永久删除 | 恢复后为 `isActive=true`；删除后远端 ID 不存在 | 恢复活动；或删除本地记录 |
| 任意不确定 | 无状态变更 | 快照畸形、目标状态未确认、网络/Session 错误 | 保持原状态，记录失败 |

- 远端写成功但读回暂未确认时，不执行第二次写请求；管理员稍后使用只读刷新收敛状态，避免重复停用或重复删除。
- app 上线失败时恢复部署前镜像，保留 SQLite 和 browser profile；功能上线后若仅 UI/路由异常，同样只回滚 app，不恢复数据库备份，除非 SQLite 完整性检查失败。
- 生产验收不选择任何真实 Alias 执行破坏性动作；真实停用或永久删除只由管理员在管理页明确选择并确认。

### 14.6 生产结果

- 2026-07-27：功能提交 `20f17f5907e03a8a8cdef25178c24a0b904a16f7` 已部署。新 app 镜像为 `sha256:52a53fadd5bca55a1db32209fa84ccb4513e37ffc7bcfe119e4f1c81d0138f28`，保留 `latest/prod/release-20f17f5`；部署前镜像保留 `rollback-pre-20f17f5-20260726T184755Z`。
- HME Session 过期后由持久 Chromium 中的现有登录态重新捕获，随后只读同步一次导入全部 107 条历史 Alias；重复同步仍为 107 条，其中 97 条活动、10 条失活、0 条已配 key，SQLite `quick_check=ok`。
- 管理页已显示全部 107 条记录；活动 Alias 可以签发或轮换 key，失活 Alias 需要先恢复。生产只验证动作前置条件和只读收敛，没有选择真实 Alias 执行停用、恢复或永久删除。
- 60 秒 watchdog 只替换 app，10 秒恢复健康；browser、cn-proxy、共享 Caddy 均未重启。四个生产容器最终为 `healthy / restart=0 / OOM=false`，部署标记已写入完整功能提交，项目候选容器、卷和镜像均为 0。
- 回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/history-management-20260726T184755Z-20f17f5`，最终清单文件 SHA-256 为 `391758c46cfb520a26434463ce3395528eb004667892810d009694570b0ff5b4`，清单内全部文件复验通过。CDP 9222 网络边界、共享 Caddy 配置和正式数据卷均未改变。

## 15. 验证码查询记录专栏

### 15.1 目标、边界与验收指标

目标是在现有管理员平台增加独立的“查询记录”专栏，让管理员确认哪些隐藏邮箱曾被用户查询、查询结果和时间，同时不扩大公开接口返回面，也不保存验证码、访问 key、邮件正文或原始客户端 IP。

- 管理页导航增加“查询记录”锚点；独立全宽栏目展示最近 100 条 `code_lookup` 事件，继续沿用现有 7 天自动保留期。
- 每条记录展示 Alias 邮箱、结果、脱敏来源指纹和查询时间；结果至少区分“已返回验证码”“暂无验证码”“IMAP 未配置/失效/错误”和“无效 Key”。
- 已匹配 Alias 的事件保存加密邮箱快照；Alias 后续永久删除时，既有查询记录仍能显示当时邮箱。旧事件在上线迁移时从仍存在的 Alias 回填快照。
- 无效 Key 没有可归属 Alias，显示“未匹配邮箱”，不能记录或回显用户输入的 key。
- 只允许管理员已认证页面读取查询记录，不新增公开查询历史 API，不把邮箱、来源指纹或审计数据发送给普通访客。
- 列表查询有明确上限并使用 `(event_type, id)` 索引；移动端不横向溢出，长邮箱、结果和时间不重叠。
- 生产迁移前完成 SQLite 在线备份；构建期间旧 app 持续服务，只替换 app，60 秒 watchdog 失败自动恢复旧镜像。
- 全量测试、Ruff、格式、Python/JS 语法、两套 Compose 展开、`git diff --check`、秘密扫描和桌面/移动 UI 验收全部通过。
- 上线验收只读取现有审计事件，不发起真实公开验证码查询，也不调用 Apple Alias 写接口。

### 15.2 控制结构与稳定性约束

| 控制元素 | 本轮对象 |
| --- | --- |
| 目标 | 管理员可可靠识别哪些 Alias 被查询过，同时维持敏感信息最小化 |
| 被控对象 | 公开验证码查询、SQLite `audit_events`、管理员查询记录列表 |
| 测量 | `code_lookup` 事件数、可归属 Alias 数、outcome、创建时间、数据库完整性、页面布局 |
| 控制器 | 既有 `_audit_lookup`、加密 Alias 快照、保留期清理、限量倒序查询、管理员会话边界 |
| 执行器 | 审计事件写入、兼容性列迁移/回填、管理模板渲染 |
| 扰动 | 5 秒轮询造成重复 `no_code`、Alias 被永久删除、无效 key、长邮箱、日志量突增、部署回滚 |
| 稳定性策略 | 不复制验证码/key；列表有界；迁移为可空加法；旧镜像忽略新列；部署失败只回滚 app |

### 15.3 GitHub 经验与生产基线

- `goauthentik/authentik` 的 `Event` 模型把 `action`、清洗后的 `context`、`client_ip` 和 `created` 分开保存，并为动作、时间和来源建立索引；本项目沿用“结构化事件 + 有界保留”，但继续只保存不可逆 IP 摘要。
- `django/django` 的管理员 `LogEntry` 同时保存对象 ID 与 `object_repr` 快照，使对象删除后审计记录仍可辨识；本项目对应保存加密的 Alias 邮箱快照，不保存访问 key 或验证码。
- 2026-07-27 生产只读测量：schema version 为 1，`audit_events` 现有列为 `id/event_type/alias_id/outcome/ip_digest/created_at`；已有 17 条 `code_lookup`，涉及 2 个 Alias，其中 `found=2`、`no_code=15`。
- 当前公开查询路径已对有效、无效、未配置、IMAP 失败和未找到验证码写入 `code_lookup`；本轮复用该闭环，不增加第二套日志表，也不改变公开响应契约和限流语义。

### 15.4 最小充分模型

- `audit_events` 增加可空 `alias_email_blob BLOB`。写入有 `alias_id` 的事件时，在同一事务中复制 Alias 已有的 AES-GCM 邮箱密文；读取时继续使用 `alias-email` purpose 解密。
- 启动时通过 `PRAGMA table_info(audit_events)` 检测列，缺失才执行 `ALTER TABLE`；随后为旧事件按 `alias_id` 回填现存 Alias 的 `email_blob`，并幂等创建 `audit_events_type_id_idx(event_type, id DESC)`。
- 该变更保持 `schema_version=1`：新增列可空且旧代码只显式读取既有列，部署前镜像可直接回滚并忽略新列，不需要为了普通应用回滚恢复 SQLite。
- 数据库提供单一 `list_code_lookup_events(limit=100)` 读取方法，只返回 `id/alias_id/alias_email/outcome/ip_digest/created_at`；解密失败按数据库错误处理，避免静默显示错误归属。
- `GatewayService.dashboard()` 同时返回查询列表与当前显示条数、可归属 Alias 数；不把原始审计密文暴露给模板。

### 15.5 实施节点

- [x] 节点 1：核对本地 `main`、现有审计闭环、GitHub 成熟实现和生产只读基线，确定使用现有表的兼容性加法迁移。
- [x] 节点 2：数据库、服务和 Web 回归测试已补齐，覆盖 v1 迁移/回填、新事件快照、Alias 删除后记录仍可辨识、无效 key 无邮箱、dashboard 统计和独立管理栏目；新增测试先在旧实现上 4/4 失败。
- [x] 节点 3：已实现 `audit_events` 加密快照列、幂等索引/回填、查询列表和 dashboard 数据；4 个定向测试通过，公开查询响应与限流代码未改变。
- [x] 节点 4：已增加管理导航和“查询记录”栏目，提供邮箱/结果/来源指纹/北京时间及空状态；1440×900、820×900、390×844 均无横向溢出或内容重叠，导航定位不会被粘性顶栏遮挡。
- [x] 节点 5：README/运维说明已更新；`116 passed`，Ruff、格式、Python/JS 语法、两套 Compose、`git diff --check` 和秘密扫描全部通过。三视口 UI 无溢出/重叠，控制台错误为 0，固定 UTC 样本正确显示为北京时间且 HTML 不含验证码、Key 或原始 IP。
- [x] 节点 6：功能提交 `bd1b4fe` 已推送 GitHub `main`；服务器完成 SQLite/源码备份后，用独立 60 秒 watchdog 只替换 app，新容器约 10 秒恢复健康且未回滚。
- [x] 节点 7：生产只读核对迁移列、索引、17 条既有查询记录及管理页展示；补充修正三份空证据，更新 `OPERATIONS.md`、本计划和云贝唯一连接手册，并清理本地/服务器临时件。本节随最终纯文档提交推送 GitHub `main`。

### 15.6 测试矩阵

| 层级 | 必测闭环 |
| --- | --- |
| 数据库 | 旧 v1 库在线增加列与索引；旧事件回填；新事件保存快照；Alias 删除后快照保留；无效 key 事件为空邮箱；limit 夹在 1–500 |
| 服务 | `found/no_code/not_configured/imap_invalid/imap_error/invalid_key` 映射不变；dashboard 查询条数和 Alias 去重计数正确 |
| Web | 未登录仍 303；管理员页面包含查询记录导航、列标题、结果文案、邮箱和脱敏指纹；公开 API 不增加字段 |
| 安全 | HTML/日志/数据库审计字段不含验证码、访问 key、原始 IP；现有 7 天清理仍覆盖新增列 |
| UI | 0 条、17 条和 100 条记录；长邮箱；桌面/平板/手机无横向溢出、裁切、重叠或控制台错误 |
| 部署 | SQLite `quick_check=ok`；迁移前后事件计数守恒；四容器健康且 `restart=0`；只替换 app；公网健康正常 |

### 15.7 回滚边界

- 新列和索引均为向后兼容的加法，普通功能回滚只恢复部署前 app 镜像；旧代码继续使用原六列，不删除新列、不恢复数据库。
- 只有启动迁移导致 SQLite `quick_check` 失败或事件计数异常时，才停止写入并使用部署前在线备份恢复数据库；不得把备份恢复作为普通 UI 回滚步骤。
- 查询专栏异常不能影响公开验证码接口；若上线后仅模板或列表读取失败，watchdog/人工回滚只替换 app，browser、cn-proxy、Caddy、CDP 网络和 Apple 会话均保持不变。

### 15.8 生产结果

- 2026-07-27：功能提交 `bd1b4fe97897b47492263c9d1509e0aa3d35ea9a` 已部署，新 app 镜像为 `sha256:4944e85e1a65172c93454567516063867c44c3f2f0743a761bcf4e9ed82fa003`。回滚与审计目录为 `/opt/new-api/icloud-code-gateway/backups/query-history-20260727T045045Z-bd1b4fe`。
- 60 秒 watchdog 只替换 app，新容器约 10 秒恢复健康；browser、cn-proxy 和共享 Caddy 未重启。生产迁移保持 `schema_version=1`，新增 `alias_email_blob` 与 `audit_events_type_id_idx`，原 17 条 `code_lookup` 全部回填加密邮箱快照且计数守恒。
- 管理页生产验收显示 17 行查询记录；数据库和隔离候选均为 `quick_check=ok`。补充验收已把先前因 `docker exec` 缺少 `-i` 而为空的 `candidate-database.txt`、`candidate-page.txt`、`production-page.txt` 重建为非空证据，最终清单 SHA-256 为 `38a2f2bc832681d8a4f6c54fc112e61c22aa0e797aa280666a258cf24ae22119`。
- 生产部署标记继续保持功能提交 `bd1b4fe`；候选容器、候选卷和远程补充脚本均已清理，不制造真实验证码查询，也未修改 browser、cn-proxy、Caddy、CDP 网络或 Apple Alias。

## 16. 创建失败现场恢复

### 16.1 目标与验收指标

目标是在不重复调用 Apple 创建接口的前提下，查清管理页“创建失败，未完成的 Alias 可通过对账恢复”的真实状态，恢复有效 HME Session，并证明远端 Alias 与本地数据库重新收敛。

- 生产日志和审计只读取时间、HTTP 状态、结果与数量，不输出 Alias 邮箱、Cookie、访问 key、验证码或上游响应正文。
- 对两次失败请求建立因果链：请求是否到达 app、服务层是否记录失败、本地是否新增 Alias、Apple 列表能否读取。
- 若 Apple 列表可读且远端多于本地，只执行一次幂等 `sync_aliases()`；若列表不可读，禁止盲目对账和再次创建。
- 优先复用唯一持久 Chromium profile 捕获新 Session；捕获的新 Session 必须先通过 HME list 验证，验证成功后才能原子保存并对账。
- 恢复后远端有效 ID 集与本地远端 ID 集差异均为 0，SQLite `quick_check=ok`，app/browser/cn-proxy/Caddy 保持 `healthy / restart=0`。
- 不以真实创建请求作为恢复验收探针，避免用户已经点击两次后再产生第三个 Alias；以只读 list、幂等 sync 和管理页状态作为闭环证据。
- 补齐查询记录部署证据文件，更新 `OPERATIONS.md` 和云贝唯一连接手册，最终推送 GitHub `main` 并清理本轮临时件。

### 16.2 控制结构与扰动

| 控制元素 | 本轮对象 |
| --- | --- |
| 目标 | 恢复 Alias 创建控制链，同时避免重复创建或错误对账 |
| 被控对象 | 已保存 HME Session、持久 Chromium 登录态、Apple Alias 集、本地 SQLite 映射 |
| 测量 | 创建 HTTP 状态、`alias_create` 审计、HME list、远端/本地 ID 集、容器健康与数据库完整性 |
| 控制器 | 失败关闭、捕获前置验证、幂等对账、只读集合比较、单次状态机执行 |
| 执行器 | 持久浏览器 Session 捕获、`save_hme_session()`、必要时 `sync_aliases()` |
| 扰动 | Apple Session 过期、浏览器 Cookie 与 API Session 生命周期不同、部署切换时延、用户重复点击、Apple 私有接口错误 |
| 稳定性策略 | 不确定时不写远端；先恢复测量通道，再收敛本地状态；不使用创建动作做健康探针 |

### 16.3 GitHub 经验与生产基线

- 前述 `Yimikami/icloud-hme-manager`、`fewhnhouse/hide-my-email` 与 `banana2556/hme-manager` 均依赖短期 Apple Web Session 调用 HME 私有接口；浏览器仍登录不代表先前捕获的请求头与令牌仍可用，因此 Session 过期应通过浏览器重新捕获，而不是重试写请求。
- 同类实现继续以 `/v2/hme/list` 作为当前 Alias 集的事实来源。本项目沿用“写操作不自动重试、失败后先 list 对账”的边界，避免 reserve 已成功但客户端超时造成重复 Alias。
- 2026-07-27 现场测量：两次 `POST /admin/api/aliases` 均到达 app 并返回 502，对应两条 `alias_create=failed`；本地仍为 107 条（97 活动、10 失活），SQLite 完整。
- 同时直接读取 HME list 返回 `HmeSessionError`；持久 Chromium 仍位于 iCloud+ 页面，认证 Cookie 名集合完整。当前最小充分解释是已保存 HME Session 失效，而不是查询记录功能修改了创建路径。

### 16.4 实施节点

- [x] 节点 1：只读核对生产请求、审计、本地 Alias 计数、数据库完整性和 HME list；确认两次 502 均由失效 Session 导致，未发现本地新增 Alias。
- [x] 节点 2：通过生产 app 的既有捕获状态机复用持久 Chromium，状态依次为 `idle -> starting -> waiting_login -> verifying -> captured`；新 HME Session 已通过 list 验证并保存，未要求人工重新登录。
- [x] 节点 3：捕获保存时已执行一次幂等对账；随后只读复核 Apple 为 108 条（98 活动、10 失活），本地同为 108 条，双向 ID 集差异、重复 ID/邮箱和畸形项均为 0。相较故障前基线新增 1 条，证明两次 502 中一次已在 Apple 侧完成创建并由本次对账恢复；未再调用显式同步或创建接口。
- [x] 节点 4：管理页已显示 108 条 Alias 和 17 条查询记录，HME 为已配置；SQLite、内部/公网健康和 CDP 均正常，app/browser/cn-proxy/Caddy 全部 `healthy / restart=0 / OOM=false`，未再次创建真实 Alias。
- [x] 节点 5：查询记录部署补充验收通过；生产与隔离候选的数据库/页面证据均非空且断言通过，最终 SHA-256 清单复验成功，候选容器、卷和远程脚本已清理。
- [x] 节点 6：实施计划、`OPERATIONS.md` 和云贝唯一连接手册已更新；本节随最终纯文档提交推送 GitHub `main`，服务器部署标记继续指向功能提交 `bd1b4fe`。
- [x] 节点 7：服务器候选容器/卷和一次性脚本已删除；本机 5 个任务临时路径已移入独立废纸篓目录。最终提交推送后复核工作树干净且 `main == origin/main`。

### 16.5 回滚与停止条件

- 捕获器只有在新 Session 的 HME list 验证成功后才调用 `set_secret`，因此捕获、解析或验证失败时不需要数据库回滚；保留现有 Alias、key 和浏览器 profile 现场。
- 远端 list 为空、字段畸形、ID 重复或本地已知集合不完整时拒绝对账，不把 107 条本地记录批量失活。
- 远端/本地集合一致时不执行额外同步；集合不一致时只执行一次 `sync_aliases()`，同步后仍不一致则停止，不调用 create/deactivate/reactivate/delete。
- 浏览器需要 Apple 人工登录时，捕获状态停留在 `waiting_login` 是明确的人机边界；不得通过保存密码、自动输入 2FA 或删除 profile 绕过。

### 16.6 现场结果

- 2026-07-27 12:52（Asia/Shanghai）两次创建请求均到达生产 app 并返回 502，对应两条未关联 Alias 的 `alias_create=failed`；故障前本地仍为 107 条，数据库完整。旧 Session 的 HME list 同时返回 `HmeSessionError`。
- 持久 Chromium 仍在 iCloud+ 页面且认证 Cookie 名集合完整。13:01 通过生产 app 的捕获状态机无人工登录完成新 Session 捕获；`save_hme_session()` 先验证 Apple list，再保存 Session 并执行幂等对账。
- 恢复后 Apple 与本地均为 108 条（98 活动、10 失活），双向远端 ID 差异、重复 ID/邮箱和畸形项均为 0。相较故障前新增 1 条，说明两次 502 中一次在 Apple 侧实际完成创建；该恢复项没有自动签发 key，应在管理页刷新后对该活动项手工签发，不能通过再次点击“创建”来补 key。
- 本轮没有调用第三次 create，也没有调用 deactivate/reactivate/delete；公网健康为 200，部署标记仍为 `bd1b4fe`，四个正式容器保持 `healthy / restart=0 / OOM=false`。
- 最终本地门禁为 `116 passed`；Ruff、格式、Python 编译、JS 语法、两套 Compose、`git diff --check` 和新增文档秘密扫描全部通过。

## 17. 管理员密钥查看与全 Alias 验证码面板

### 17.1 目标与验收指标

目标是把管理员工作台补全为可直接运维的 iCloud 管理面：管理员可以随时查看和复制当前有效访问 key，也可以不依赖 key 查看所有已导入 Alias 最近 5 分钟收到的验证码。

- 新签发或轮换的访问 key 使用现有 32 字节主密钥进行 AES-GCM 加密保存，同时继续保存 SHA-256 哈希供公开查询校验；公开 API 不改为解密校验。
- 管理员通过显式“查看密钥”动作取得完整 key；接口必须同时验证管理员会话与 CSRF，响应禁止缓存，管理页初始 HTML 不包含完整 key。
- 旧数据库中只有哈希的既有 key 无法反解，页面明确显示“轮换后可查看”；不得自动轮换现有 key，避免用户侧凭据突然失效。
- 撤销、Alias 失活、远端列表移除或永久删除时，同一事务边界内清除 key 哈希、提示和密文；失效 key 不得继续查看或用于公开查询。
- 管理员验证码面板覆盖全部本地 Alias，包括没有 key 的 Alias；只读取最近 300 秒至未来 60 秒内、收件人精确匹配受管 Alias 且包含 6 位验证码的邮件。
- 一次管理员刷新只建立一个 IMAP 会话，执行一次时间窗 SEARCH 和批量 FETCH；最多扫描最近 500 封候选邮件并返回最多 500 条结果，按收件时间倒序。
- 验证码不写入 SQLite、审计日志、服务器日志或 URL；只在管理员专用 JSON 响应和当前页面 DOM 中短暂存在，关闭/刷新页面即消失。
- 管理员验证码读取与公开 key 查询共用既有有界 IMAP 并发槽和 20 秒总超时；繁忙、凭据失效、网络错误均失败关闭，不无限等待。
- 桌面、平板和手机视口均可查看/复制 key 与验证码，无横向溢出、操作按钮重叠或长邮箱/密钥裁切。
- 全量测试、Ruff、格式、Python/JS 语法、两套 Compose、diff 与秘密扫描通过；部署只替换 app，60 秒内失败自动恢复旧镜像，browser/cn-proxy/Caddy 不重启。

### 17.2 工程控制结构

| 控制元素 | 本轮对象 |
| --- | --- |
| 目标 | 管理员可观测当前 key 与全 Alias 验证码，同时保持公开访问隔离 |
| 被控对象 | 访问 key 生命周期、SQLite 密文、IMAP 最近邮件、管理员页面临时状态 |
| 测量 | key 哈希/密文一致性、管理员会话/CSRF、IMAP INTERNALDATE、精确收件人、容器健康 |
| 控制器 | AES-GCM 用途绑定、显式 reveal、一次 SEARCH + 批量 FETCH、500 条上界、并发槽与总超时 |
| 执行器 | key 签发/轮换/撤销、管理员 reveal API、管理员 recent-codes API、DOM 渲染/复制 |
| 扰动 | 旧 key 明文已丢失、邮件量突增、IMAP 时延/凭据失效、重复刷新、长邮箱和移动端窄屏 |
| 稳定性策略 | 不自动轮换；不持久化 OTP；秘密按需读取；扫描和响应有界；部署失败只回滚 app |

### 17.3 GitHub 经验与采用结论

- `bitwarden/clients` 的 Web 客户端把敏感值“切换可见”和“复制到剪贴板”作为显式用户动作，不把秘密默认展开；本项目对应在 Alias 操作区提供眼睛图标，并复用既有密钥模态框和复制按钮。
- `hashicorp/vault` 的 KV 界面明确提示 reveal 会暴露 secret values，并为 reveal 行为保留直接测试；本项目同样让完整 key 只经管理员专用 reveal 接口返回，初始 dashboard 仍只含末 4 位和可恢复状态。
- `axllent/mailpit` 的消息 API 使用 `limit/start` 有界读取最近消息；本项目不引入邮件持久化，而是在一次 IMAP 时间窗扫描中设置 500 封/500 条硬上限。
- `pwnapplehat/icloud-hide-my-email` 使用 `BODY.PEEK[]` 避免把验证码邮件标为已读；本项目继续沿用只读 SELECT、`BODY.PEEK[]` 和 `INTERNALDATE`，并复用现有批量 FETCH 回退路径。
- `Yimikami/icloud-hme-manager` 与 `fewhnhouse/hide-my-email` 继续以 `/v2/hme/list` 管理完整 Alias 集；管理员验证码扫描以本地已对账的完整 Alias 集为允许列表，不从邮件头动态扩张管理范围。

### 17.4 最小充分模型

#### 访问 key

- `aliases` 增加可空 `access_key_blob BLOB`，保持 `schema_version=1`。旧镜像显式读取既有列，可忽略新增列；功能回滚不删除该列。
- 签发时一次生成 key，并在同一事务中写入 `SHA-256 hash + hint + AES-GCM blob`。密文 AAD 使用 `alias-access-key:{alias_id}`，防止跨 Alias 交换密文。
- 读取时解密、校验 key 格式并重新计算哈希；密文与哈希不一致按数据库损坏失败，不返回可疑值。
- Alias 字典只新增 `access_key_recoverable: bool`，不携带明文。完整 key 仅由 `reveal_access_key(alias_id)` 返回。

#### 管理员验证码

- IMAP 层新增 `find_recent_codes(aliases, ...)`：一次 window SEARCH，取 UID 倒序最多 500 条，按既有 25 条批次 FETCH，逐封校验 INTERNALDATE/Date、受管 Alias 精确收件人和 6 位码。
- 返回值包含匹配 Alias、验证码、UID、UTC 收件时间和是否因 500 条上界截断；不返回邮件正文、主题或完整发件人。
- 服务层把 Alias 邮箱映射回本地 ID/标签并转换北京时间；管理员扫描写一条不含验证码的 `admin_code_scan` 审计事件。
- `POST /admin/api/codes/recent` 要求管理员会话与 CSRF；`POST /admin/api/aliases/{id}/key/reveal` 同样要求管理员会话与 CSRF。两者均继承 `Cache-Control: no-store`。

### 17.5 前端交互

- 管理导航增加“验证码”锚点；Alias 列表中可恢复 key 显示眼睛图标，旧哈希-only key 显示“轮换后可查看”。
- 点击眼睛图标后调用 reveal API，并在现有密钥模态框中显示完整 key、Alias 和复制按钮；关闭后清空 DOM。
- 新增独立“验证码”全宽栏目，只有管理员点击刷新按钮才读取 IMAP。结果表显示 Alias、6 位码、北京时间和复制按钮；空、繁忙、未配置与错误状态在同一固定区域反馈。
- 结果使用 `textContent` 构建，不拼接 HTML；刷新前清空上一批验证码，页面卸载后不保留到 localStorage/sessionStorage/cookie。

### 17.6 实施节点

- [x] 节点 1：核对当前 Git/main、数据库 key 生命周期、IMAP 批量读取、管理 API/模板与 GitHub 同类经验；确认旧 key 不可逆，且一次 window SEARCH + 批量 FETCH 可覆盖全 Alias。
- [x] 节点 2：数据库、IMAP、服务与 Web 回归测试已增加；旧实现产生 7 个定向失败，service/web 另因缺少 `RecentOtpBatch` 在收集阶段失败，证明密文迁移、reveal、全 Alias 单次扫描和管理员接口均尚不存在。
- [x] 节点 3：已实现 `access_key_blob` 兼容迁移、签发/撤销/失活清理和校验式 reveal；数据库与 IMAP 定向测试共 43 项通过，旧 key 保持 hash-only 且不自动轮换。
- [x] 节点 4：已实现一次 IMAP 会话的全 Alias recent-code 扫描、服务映射、无明文审计和管理员 API；无 key Alias 已由服务/Web 回归测试覆盖，公开 `/api/code` 合同保持不变。
- [x] 节点 5：已实现管理员显式查看 key、旧 key“轮换后可查看”提示和验证码栏目，具备响应式布局、加载/空/错误/截断状态、复制与会话过期跳转；四个核心模块共 81 项测试通过。
- [x] 节点 6：README/运维说明已更新；全量 `125 passed`，Ruff/格式、Python/JS 语法、两套 Compose、diff 与秘密扫描通过。1440px、768px、390px 浏览器验收均无横向溢出、按钮重叠或控制台错误；初始 HTML 不含完整 key/验证码，审计不含 OTP。
- [x] 节点 7：功能提交 `312e8ba809d5cf9799fb54780ebe6dc2902fa20f` 已推送 GitHub `main`。部署前完成 SQLite/源码/旧镜像备份；隔离候选迁移与 key 加密闭环通过后，60 秒 watchdog 只替换 app，约 22.4 秒恢复 healthy，公网最长连续非 200 为 16.243 秒，未触发回滚。
- [x] 节点 8：生产新增 `access_key_blob` 后 `quick_check=ok`，115 个 Alias、4 条旧 key 哈希指纹和 17 条公开查询记录均保持；旧 key reveal 返回 409 且未自动轮换。管理员验证码接口实际扫描 44 封候选、返回 0 条、未截断；未制造验证码、未执行 Apple/HME 写操作，browser/cn-proxy/Caddy 未重启。
- [x] 节点 9：`OPERATIONS.md`、本计划和云贝唯一连接手册已更新，最终记录提交已纳入 GitHub `main`。服务器候选容器/卷、release、候选标签和部署锁均已清理，部署标记保持实际功能提交 `312e8ba809d5cf9799fb54780ebe6dc2902fa20f`。

### 17.7 测试矩阵

| 层级 | 必测闭环 |
| --- | --- |
| 数据库 | v1 旧库增加可空密文列；新 key 可解密且 hash 一致；轮换替换；撤销/失活清除；旧 key 返回不可恢复；密文交换/损坏失败 |
| IMAP | 多 Alias/无 key Alias；一条 Alias 多封验证码；精确收件人；299/300/301 秒；未来 60/61 秒；一次 SEARCH；批量 FETCH；500 条截断；超时/登录失败 |
| 服务 | 管理员扫描不要求 key；映射 ID/标签和北京时间；IMAP 错误映射；并发槽释放；审计不含 OTP；公开 lookup 行为不变 |
| Web | reveal/recent-codes 未登录 401、无/错 CSRF 403；旧 key 409；成功 JSON no-store；dashboard 不含 key；公开 API 不新增字段 |
| 前端 | 眼睛/复制、旧 key 提示、验证码刷新/空/错误/截断、重复刷新清空旧值、会话过期跳登录、键盘/移动端可达 |
| 部署 | SQLite `quick_check=ok`；旧两条 key hash/hint 保留且 blob 为空；新列迁移兼容回滚；四容器健康且只替换 app |

### 17.8 回滚与停止条件

- 功能回滚只恢复旧 app 镜像；新增可空列由旧代码忽略，不恢复 SQLite 备份，也不删除已经写入的 key 密文。
- 只有迁移后 `quick_check` 失败、现有 key hash/hint 计数变化或密文一致性断言失败时才回滚数据库备份。
- 生产不自动轮换旧 key；管理员需要查看旧 key 时必须明确点击轮换，并接受旧 key 立即失效的既有语义。
- 管理员验证码扫描超时、截断或 IMAP 异常时停止当前读取，不增加扫描上界、不启动逐 Alias 连接、不把验证码落库重试。
- 部署只替换 app；browser、cn-proxy、Caddy、CDP 网络、Apple Session 和 Alias 远端状态不在本轮变更范围。

## 18. `ic-veilmail` 对照后的增量实施计划（2026-07-30）

### 18.1 目标与性能指标

在不扩大公网数据面、不缓存邮件正文、不改变访问密钥协议的前提下，吸收
`Redmig110/ic-veilmail` 已经经过提交演化验证的两项经验：验证码语境判定和 Junk
邮箱只读检索。增量闭环仍以“一个访问密钥只返回一个 Alias 在五分钟内的最新六位
数字验证码”为目标。

验收指标：

- 只接受与验证码语义词相邻的独立六位数字；普通通知中的日期、账号、订单号或其他
  无验证码语境的六位数字不得返回。
- 同一封邮件有多个六位数字时，返回与验证码语义距离最近的候选，不按正文出现顺序
  盲选。
- 保持现有六位纯数字响应契约，不扩展到参考项目支持的 4-8 位或字母数字码。
- 管理员可选配一个 Junk 文件夹；查询在同一 IMAP 登录、同一总截止时间内只读扫描
  主文件夹和 Junk 文件夹，并以 `INTERNALDATE` 选择跨文件夹最新结果。
- 旧的加密 IMAP 配置没有 Junk 字段时可直接加载，现有 `INBOX` 行为不变。
- 配置保存前必须只读验证所有已配置文件夹；查询阶段至少一个文件夹可读时允许降级，
  全部不可读时失败关闭；任何读取都不得把邮件标为已读。
- 管理员批量扫描在每个已配置文件夹各做一次时间窗搜索，但共享一个 500 封扫描预算，
  不因增加 Junk 而把已有上限翻倍。
- 不引入常驻 IMAP IDLE、邮件/OTP 落库、SMTP 或发信能力；不修改或调用项目既有的
  HME 停用、恢复和删除能力。
- 全量测试、Ruff、Python 编译和 `git diff --check` 全部通过，敏感信息扫描无新增命中。

### 18.2 工程控制抽象

| 控制元素 | 本次增量对象 |
| --- | --- |
| 目标 | 降低验证码误码率，并覆盖可能进入 Junk 的有效邮件 |
| 被控对象 | 主 IMAP 文件夹、可选 Junk 文件夹、邮件中的六位数字候选 |
| 控制器 | 语境评分器、跨文件夹只读扫描器、总预算分配器、总截止时间 |
| 测量 | 精确 Alias、sender filter、`INTERNALDATE`、验证码语义距离、文件夹可读性 |
| 执行 | 返回最新合格验证码，或返回等待/不可用状态 |
| 扰动 | 营销邮件数字、账号/订单号、垃圾邮件分类、文件夹命名差异、单文件夹故障、IMAP 时延 |
| 稳定性 | 旧配置兼容、保存前验证、至少一个文件夹可读才降级、全局超时、不持久化邮件 |

最小充分闭环：

```text
旧 IMAP 配置启动
  -> INBOX 中普通六位数字不被识别
  -> INBOX 中语境明确的验证码可识别
  -> 配置并验证 Junk 文件夹
  -> Junk 中对应 Alias 的新验证码可识别
  -> INBOX/Junk 同时有验证码时返回 INTERNALDATE 较新者
  -> 任一文件夹暂时不可用时由另一文件夹维持服务
  -> 两个文件夹均不可用时有界失败关闭
```

### 18.3 GitHub 对照结论

采用：

- `2f94a55` 专门修复收件箱验证码误判，证明“必须有验证码语境、排除普通数字”是实际
  需求，不是推测性优化；本项目保留更窄的六位数字协议并补充候选距离排序。
- 参考项目把 Junk 作为独立可选邮箱同步；本项目只借鉴“额外只读检索”边界，不复制
  邮件缓存、删除或管理界面。
- 参考项目对中国区/全球区、持久浏览器 Cookie 和本地标签的处理用于交叉核对；当前
  项目已有更严格的动态 HME host 捕获、Origin 一致性和加密存储，无需替换。

拒绝：

- IMAP IDLE + 全量数据库同步：会新增常驻连接、跨线程重连状态机和邮件正文存储，当前
  五秒有界轮询尚未证明需要扩大该故障边界。
- 4-8 位及字母数字验证码：与当前“六位数字码”业务协议冲突。
- SMTP/iCloud 发信、回复、附件、远程图片、邮件删除和 Mail Route：超出验证码网关的
  最小充分模型；参考项目的 Alias 生命周期实现也不替换当前项目已有的确认式控制链。
- 参考项目的本地明文 JSON/Cookie 文件：其服务只绑定回环；当前公网部署必须继续使用
  AES-GCM 加密设置与管理员认证边界。

### 18.4 实施节点与循环状态

#### 节点 J：基线与差异分析

- [x] 克隆并检查参考仓库源码、README、测试和完整提交历史。
- [x] 对照 HME、Cookie 捕获、IMAP、验证码提取、邮件同步和 Web 安全边界。
- [x] 运行当前项目全量测试，确认修改前基线通过且工作树干净。

验证：参考项目的可采用经验能映射到当前控制目标，拒绝项有明确边界理由。

#### 节点 K：先建立失败样本

- [x] 增加无验证码语境的六位账号/订单号误判测试。
- [x] 增加同一邮件多候选、中文语境、反向语境和 HTML 正文测试。
- [x] 增加旧配置兼容、Junk 配置校验、跨文件夹最新结果和单/双文件夹故障测试。

验证：新增测试在旧实现上准确暴露缺口，不依赖真实邮箱或网络。

#### 节点 L：最小实现

- [x] 将验证码提取改为“严格六位候选 + 语境窗口 + 距离排序”，无合格语境返回空。
- [x] 为 `ImapConfig` 增加向后兼容的可选 `junk_folder`，秘密序列化保持结构化数据。
- [x] 在一次登录和一次总截止时间内扫描两个只读文件夹，跨文件夹按收件时间选最新。
- [x] 与远端新增的组合 SEARCH、25 封批量 FETCH、`WITHIN` 和管理员全 Alias 扫描合并，
  两个文件夹公平共享既有 500 封总预算。
- [x] 管理页增加可选 Junk 文件夹字段，空值继续保留单文件夹行为。

验证：节点 K 全部测试通过，既有 IMAP、API 和管理配置测试无回归。

#### 节点 M：门禁、文档与交付

- [x] 更新 README、测试矩阵和本计划节点状态，不记录任何真实凭据。
- [x] 在最新远端基线上运行全量测试、Ruff、compileall、`git diff --check` 和敏感信息扫描。
- [x] 审查合并后的变更规模、向后兼容性和许可证边界；清理本任务临时件与测试缓存。
- [x] 功能提交 `46c8bbb` 已推送 GitHub `main`。
- [x] 本次未获生产部署授权，不访问生产服务器；后续如需部署，按既有运维手册执行
  最小停机发布、线上健康检查和运维记录回写。

验证：本地 `main`、GitHub `main` 和交付记录一致；未获生产配置与闭环验证前不声称
服务器已更新。

### 18.5 回滚与不确定性

- 本次加密配置只新增可选字段，无 SQLite schema 迁移；回滚旧代码时未知字段会被忽略。
- 如果严格语境规则对某个真实服务产生漏码，先用脱敏邮件样本补回归测试，再扩充最小
  语义词集合；不恢复“任意六位数字即验证码”的高误码策略。
- Junk 名称由管理员显式配置，不猜测 `Junk`、`Spam` 或本地化名称。
- 单文件夹故障降级只影响该次查询，不修改持久配置；配置错误仍需管理员重新保存验证。
- 本次不直接访问 Apple 写接口、真实 IMAP 邮箱或生产服务器。

### 18.6 本次实施记录

- 2026-07-30：完成参考仓库只读研究。确认当前项目在公网认证、凭据加密、Alias 精确
  匹配、总超时和 HME Session 白名单上更强；选定验证码语境与可选 Junk 只读检索两项
  增量，拒绝全量邮件控制台和常驻 IDLE。当前进入节点 K，尚未修改业务代码。
- 2026-07-30：完成节点 K。新增 IMAP 测量器失败样本后，旧实现结果为 22 项通过、9 项
  按预期失败；服务配置与管理页回显各有 1 项预期失败。失败面只覆盖语境误判、候选
  排序、Junk 配置/跨文件夹扫描和故障降级。测试命令需使用 `PYTHONPATH=.`；当前进入
  节点 L。
- 2026-07-30：完成节点 L。验证码改为严格六位语境距离判定；可选 Junk 与主文件夹在
  一次登录和同一总截止时间内只读扫描，跨文件夹按 `INTERNALDATE` 选新，任一文件夹
  可读时降级、全部失败时保留具体脱敏错误。补充 modified UTF-7 与引用处理，支持带
  空格、引号、`&` 和中文的文件夹名。管理表单完成保存与回显；全量 99 项测试通过，
  当前进入节点 M。
- 2026-07-30：旧基线上的初轮门禁为全量 99 项测试、Ruff、compileall、
  `git diff --check` 和高置信凭据扫描通过；管理页在 `1440x900`、`390x844` 和
  `360x800` 视口无横向溢出、控件重叠或控制台错误。刷新远端时发现 `origin/main`
  已前进到 `21f81c5`，包含管理员全 Alias 验证码扫描等生产功能，因此该轮门禁不再作为
  最终交付证据，节点 M 重新打开。
- 2026-07-30：本地 `main` 已快进到 `21f81c5` 并完成冲突整合。保留远端组合 SEARCH、
  25 封批量 FETCH、`WITHIN`、管理员验证码面板和 500 封硬上限；新增跨文件夹公平预算，
  两个文件夹仍只用一个登录和一个总截止时间。合并后 IMAP 47 项、服务与 Web 41 项
  定向测试及 Ruff 通过，当前进入最新基线的全量门禁。
- 2026-07-30：完成最新基线的节点 M 本地门禁。全量 `145 passed`，Ruff、compileall、
  JavaScript 语法、`git diff --check` 和高置信凭据扫描通过；唯一警告为既有 Starlette
  TestClient/httpx 弃用提示。管理页在 `1440x900`、`390x844` 和 `360x800` 复验，新增
  Junk 字段可见且与提交按钮无重叠，无横向溢出、文字裁切、控件重叠或浏览器告警；
  当前只待提交并推送 GitHub `main`。
- 2026-07-30：功能提交 `46c8bbb1515936119314cf899eaca6ad0b016255` 已直接推送
  GitHub `main`；本次未访问生产服务器、真实 Apple/IMAP 或任何远程写接口，节点 M
  完成。本记录随独立收口提交推送，不改变功能提交内容。

## 19. `46c8bbb` 生产部署计划（2026-07-31）

### 19.1 目标与性能指标

把已经通过本地门禁的功能提交
`46c8bbb1515936119314cf899eaca6ad0b016255` 部署到
`icloud.yunbay.xyz`，使生产 IMAP 读取具备严格验证码语境判定和可选 Junk 文件夹支持。
本轮只替换 `app`，不改变 browser、cn-proxy、共享 Caddy、网络、正式数据卷或 Apple
远端状态。

验收指标：

- 构建和候选验证期间旧 app 持续服务；正式切换后 60 秒内恢复 healthy，公网连续不可用
  时间小于 60 秒，否则由独立 watchdog 自动恢复部署前镜像。
- 部署前后 SQLite 均为 `quick_check=ok`，Alias、访问密钥和审计记录计数及脱敏指纹守恒；
  本次没有 schema 迁移。
- 隔离候选能加载没有 `junk_folder` 的旧加密 IMAP 配置，`/healthz` 正常，管理页包含
  Junk 字段，响应和日志不暴露任何配置明文、访问密钥或验证码。
- browser、cn-proxy、共享 Caddy 的容器 ID 和 restart count 保持不变；所有正式容器最终
  为 healthy、`OOMKilled=false`。
- 公网 `/healthz`、首页和管理员登录页正常；生产部署标记和 release 镜像指向功能提交
  `46c8bbb`。
- 不执行 Apple/HME 写操作，不制造真实验证码，不主动轮换访问密钥；IMAP 只做配置兼容
  和界面静态闭环，不读取或记录邮件正文。

### 19.2 工程控制结构

| 控制元素 | 本次部署对象 |
| --- | --- |
| 目标 | 让生产代码收敛到已验证功能提交，同时保持服务和数据稳定 |
| 被控对象 | app 镜像、app 容器、SQLite 卷、部署标记 |
| 控制器 | 隔离候选、app-only force-recreate、60 秒独立 watchdog、自动回滚 |
| 测量 | health、HTTP 状态、容器 ID/restart/OOM、数据库完整性/计数、镜像摘要 |
| 执行 | 构建候选、固定 release/rollback 标签、只替换 app、失败时恢复旧镜像 |
| 扰动 | 构建时延、启动时延、SQLite 锁、Host 校验、代理/Apple/IMAP 上游波动 |
| 稳定性 | 先备份和隔离验证；切换有界；旧镜像独立保留；不触碰无关容器和远端写接口 |

最小充分闭环：

```text
测量生产基线
  -> 在线备份 SQLite、源码和镜像元数据
  -> 旧 app 在线时构建候选镜像
  -> 用隔离数据库/配置启动候选并验证旧配置兼容
  -> 启动独立 60 秒 watchdog
  -> 只 force-recreate app
  -> Host-aware health 与公网 health 恢复
  -> 校验数据守恒和无关容器未变化
  -> 成功则固定 release 并清理临时件
  -> 失败则 watchdog 恢复旧镜像并复验 health
```

### 19.3 实施节点与循环状态

#### 节点 P：计划固化与只读预检

- [x] 确认本地 `main`、GitHub `main` 和功能提交存在，工作树在计划编辑前干净。
- [x] 定位云贝唯一 SSH 密钥和连接手册，不读取或回显私钥内容。
- [x] 本节部署计划已由提交 `dbda19c` 推送到 GitHub `main`。
- [x] 只读采集生产部署标记、源码/镜像、容器健康/restart/OOM、磁盘、时间同步、
  `.env` 权限、SQLite 完整性与脱敏计数、内外 health 基线。

停止条件：SSH 主机身份不匹配、`.env` 权限不是 `0600`、SQLite 非 `ok`、磁盘不足、
时间未同步、任一正式容器基线异常，或生产版本无法与现有 release/rollback 镜像对应。

#### 节点 Q：备份与隔离候选

- [x] 创建权限 `0700` 的时间戳审计目录，在线备份 SQLite 并验证 `quick_check=ok`。
- [x] 保存部署前源码、Compose/部署标记、容器/镜像/数据计数元数据及 SHA-256 清单；
  `.env` 不复制、不输出，正式卷不删除。
- [x] 给当前 app 镜像增加唯一 `rollback-pre-46c8bbb-<timestamp>` 标签。
- [x] 从 Git 功能提交清单上传精确源码归档，不覆盖 `.env`、备份或持久数据；旧 app
  保持在线，正式源码在切换成功后才同步。
- [x] 构建唯一候选镜像，在独立临时卷/容器中验证启动、Host-aware `/healthz`、SQLite
  `quick_check`、旧 IMAP 配置兼容、管理页 Junk 字段及秘密不外泄。

停止条件：备份或清单失败、候选镜像无法唯一标识、候选启动/兼容断言失败、日志或页面
出现秘密值。停止时保留旧 app 在线，只清理本轮候选临时件。

#### 节点 R：有界切换与自动回滚

- [x] 启动不依赖当前 SSH 会话的 60 秒 watchdog；其失败路径把 Compose app 镜像恢复为
  rollback 标签并只 force-recreate app。
- [x] 将候选固定到 Compose app 镜像标签，只执行
  `docker compose up -d --no-deps --force-recreate app`，记录切换起止和 250ms 公网采样。
- [x] 在 60 秒内确认 app healthy、内部 Host-aware `/healthz` 和公网 `/healthz` 为 200，
  随后写成功信号终止 watchdog；任何一项失败都不得等待人工操作。
- [x] 未触发回滚；watchdog 接受成功信号并退出，旧镜像继续由唯一 rollback 标签保留。

#### 节点 S：生产验收、记录与清理

- [x] 确认公网健康、首页、管理员登录页、响应安全头、部署标记和运行镜像。
- [x] 复验 SQLite `quick_check=ok`、数据计数/脱敏指纹守恒；确认旧 IMAP 配置仍可加载，
  管理页面出现 Junk 字段且没有秘密出现在 HTML 或 app 日志。
- [x] 对比 browser、cn-proxy、Caddy 的容器 ID/restart/OOM，证明只有 app 被替换。
- [x] 固定 `release-46c8bbb` 与回滚标签，生成并复验最终 SHA-256 清单。
- [x] 清理候选容器/卷/标签、一次性脚本和部署锁；保留正式 release、唯一 rollback 和审计
  目录，复验正式服务不受清理影响。
- [x] 把实际镜像、停机时长、健康/数据结果和回滚位置写回本节、`OPERATIONS.md` 与云贝
  唯一连接手册；提交并推送 GitHub `main`，本地工作树恢复干净。

### 19.4 回滚与不确定性

- 普通启动、路由或页面回归只恢复部署前 app 镜像，不恢复 SQLite；本次没有 schema
  迁移，旧代码会忽略加密 IMAP 配置中未知的可选 `junk_folder` 字段。
- 只有 SQLite 完整性失败、数据计数或脱敏指纹变化时才使用部署前在线备份；恢复前先保留
  故障现场，避免把错误恢复变成第二次数据破坏。
- 生产现有配置未必设置 Junk 文件夹；本轮只证明旧配置兼容并上线字段，不猜测或自动写入
  文件夹名，也不为验收连接真实邮箱读取邮件。
- Host 校验会拒绝容器别名作为 HTTP Host；内部主动检查必须发送
  `Host: icloud.yunbay.xyz`，不能把预期 400 误判为 app 故障。
- SSH 会话断开不应使发布悬停：切换前 watchdog 必须独立运行，成功或失败都在 60 秒内
  收敛到一个可健康服务的固定镜像。

### 19.5 本次部署记录

- 2026-07-31：用户已明确授权直接部署生产。部署计划建立时，本地和 GitHub `main` 均为
  `e1c84b8`，目标功能提交为 `46c8bbb`，服务器尚未连接或修改；当前进入节点 P。
- 2026-07-31：节点 P 通过。计划提交 `dbda19c` 和 SHA 校正提交 `fcb77a8` 已推送
  `main`；生产标记为 `312e8ba`，69 个跟踪文件聚合 SHA-256 与该 Git 对象完全一致。
  `.env=0600`，NTP 已同步，根卷可用 45GB；app/browser/cn-proxy/Caddy 均为
  `healthy / restart=0 / OOM=false`，内部及服务器/本机公网探针均为 200，近 30 分钟 app
  错误日志为 0。SQLite `quick_check=ok`，基线为 159 个 Alias（148 活动、11 失活）、
  41 个 key hash、37 个可查看 key 密文、262 条审计和 2 条设置；当前进入节点 Q。
- 2026-07-31：节点 Q 通过。审计目录为
  `/opt/new-api/icloud-code-gateway/backups/imap-junk-20260730T161519Z-46c8bbb`，SQLite
  在线备份为 `quick_check=ok`；旧镜像固定为
  `rollback-pre-46c8bbb-20260730T161519Z`。目标 Git 归档 SHA-256 为
  `679ded7d570c2636cf2dc7fd6da2bf94f615307ebae1738715208986e93bc5f4`，70 个文件构建
  的候选镜像为 `sha256:eb34a40827f3a046f430a4e150b7ef43be165135da41fc68cccd3ab50b14daa3`。
  隔离候选 7 秒 healthy，旧 IMAP 配置成功加载且 Junk 默认为空；SQLite、Alias/key
  计数和聚合指纹保持，管理页 Junk 字段存在，HTML/日志秘密命中为 0。生产 app 全程仍
  为原容器和原镜像且 healthy，当前进入节点 R。
- 2026-07-31：节点 R 通过。独立 watchdog 在切换前启动；只对 app 执行 force-recreate，
  新容器 `783385aad7d3dc0c62fa1694b57c222d71c98fedb068677369cc251cb1d05767`
  使用候选镜像，`19.121` 秒内完成 healthy、内部 Host-aware health、公网 health、源码清单
  和部署标记闭环，watchdog 返回 `accepted` 且未回滚。250ms 本机采样器因其持久 TLS
  连接被 Cloudflare 全程 reset 而无效，不作为停机证据；切换控制窗口 `19.121` 秒作为
  公网不可用的保守上界，后续独立本机 10 次探针及服务器复验均为 200。
- 2026-07-31：节点 S 生产验收通过。镜像 revision、服务器标记和 70 文件源码均为真实
  Git 对象 `46c8bbb1515936119314cf899eaca6ad0b016255`；SQLite `quick_check=ok`，159 个
  Alias（148 活动、11 失活）、41 个 key hash、37 个 key 密文、262 条审计和设置形状
  全部守恒。旧 IMAP 配置加载且 Junk 默认为空，安装模板包含 Junk 字段，日志秘密/严重
  错误命中为 0；browser、cn-proxy、Caddy 的 ID、restart 和 OOM 状态均未改变。当前只待
  候选清理、最终清单和三处运维记录收口。
- 2026-07-31：节点 S 收口完成。新镜像固定为 `latest/prod/release-46c8bbb`，旧镜像
  `sha256:d2963e4f4330deab82d004927d88d13eb68581b645d4341bb8f535dc21ec1461`
  只保留 `rollback-pre-46c8bbb-20260730T161519Z`。候选容器、卷、候选/旧 release 标签、
  解压源码、一次性 watchdog 和部署锁均已清理；清理后内外 health、SQLite 和四容器状态
  复验通过，根卷可用 45GB。审计目录保留 34 个权限受限文件，最终清单 SHA-256 为
  `6f160aaf1d89d69257153b1c485ebcb6f27e18bee4c4ae8a387bba8d42bcda57`，清单内文件全部
  复验通过。本计划、`OPERATIONS.md` 和云贝唯一连接手册已同步，最终记录提交推送
  GitHub `main` 后本轮闭环完成。

## 20. 10:30 验证码未返回事故诊断计划（2026-07-31）

### 20.1 目标与性能指标

定位 2026-07-31 10:30（Asia/Shanghai）前后“QQ 邮箱已经收到验证码，但公网查询持续
返回无验证码”的直接原因，并区分时间窗口、Alias/发件人过滤、IMAP 文件夹、邮件解析和
新语境规则五类故障。当前用户要求查明原因，本节只授权只读诊断，不改代码、配置、镜像
或生产数据。

验收指标：

- 以生产审计确认 10:25-10:37 内查询次数、结果和目标 Alias 的脱敏关联，不输出 IP、
  Alias、访问密钥或验证码。
- 以只读 IMAP 会话确认 10:30 邮件是否位于已配置文件夹、是否精确投递到受管 Alias、
  是否命中 sender filter，以及 `INTERNALDATE` 相对每次查询是否处于 300 秒窗口。
- 在内存中对同一封邮件执行当前提取器，只输出“存在独立六位数字/存在已知语境/当前规则
  是否提取”等布尔量和距离，不输出主题、正文、发件人、收件人或验证码。
- 检查服务器 NTP、app health、IMAP 登录/只读选择和日志；不得把基础设施故障误归因于
  解析规则。
- 得到一条可重复的因果链；证据不足时明确还缺什么，不猜测、不直接上线修复。

### 20.2 工程控制结构

| 控制元素 | 本次事故对象 |
| --- | --- |
| 目标 | 邮件到达后，五分钟内由正确 key 返回对应 Alias 的六位验证码 |
| 被控对象 | 访问 key、Alias、IMAP 邮件、时间窗、验证码提取器 |
| 测量 | 审计时间/结果、`INTERNALDATE`、收件人/发件人匹配、数字候选、语境距离 |
| 控制器 | key 映射、IMAP SEARCH/FETCH、时间过滤、sender filter、语境评分 |
| 执行 | 返回验证码或稳定的 `no_code` 状态 |
| 扰动 | 邮件延迟、QQ 文件夹分类、HTML/编码、服务文案变化、客户端重复刷新、时钟偏差 |
| 稳定性 | 全程只读、秘密不出容器、先关联审计再读取目标时段、结论可由脱敏布尔证据复现 |

最小充分闭环：

```text
10:30 邮件元数据存在
  -> 收件 Alias 与 key 映射一致
  -> 查询发生在邮件的 300 秒有效窗内
  -> 已配置文件夹和 sender filter 均允许该邮件
  -> 邮件含独立六位数字
  -> 当前语境提取器返回或拒绝
  -> 审计结果与提取结果一致
  -> 唯一定位到时间、过滤、文件夹或解析控制器
```

### 20.3 GitHub 对照与初始假设

- `Redmig110/ic-veilmail` 的 `2f94a551345407dbb937b519076d83006a868483` 在要求验证码
  语境的同时新增“认证码、确认码、临时码、一次性、confirmation”等词，并补真实正/反
  样本；这说明语境控制必须在误码率和漏码率之间共同校准，不能只收紧门限。
- 当前生产提交 `46c8bbb` 同时上线了严格语境判定和可选 Junk，但生产旧配置的 Junk 为空。
  初始高概率假设是新词表不覆盖该真实服务文案；次高概率假设是邮件位于未配置文件夹。
- 由于当前时间 10:37，10:30 邮件已经超过公开接口 300 秒窗口；这能解释“现在刷新不到”，
  但不能解释用户在 10:30 至 10:35 内已经持续刷新，因此必须用审计时间线验证。

### 20.4 实施节点与循环状态

#### 节点 T：基线与审计时间线

- [x] 确认事故报告时间为 10:37，本地/GitHub `main` 均为 `70ef367`，工作树此前干净。
- [x] 复核 GitHub 参考修复 `2f94a55` 的语境词扩充与防误码边界。
- [ ] 推送本事故计划到 GitHub `main`。
- [ ] 只读核对生产版本、NTP、容器健康、IMAP 配置形状及 10:25-10:37 查询审计聚合。

#### 节点 U：目标邮件与控制链复现

- [ ] 用一次只读 IMAP 登录扫描 10:25-10:37，不把邮件标为已读，不输出邮件内容或身份。
- [ ] 将时段邮件收件人哈希与本地 Alias 关联，确认目标邮件、Alias 状态、key 和 sender filter。
- [ ] 对目标邮件复现数字候选、语境、时间窗和当前提取结果，并与查询审计逐次对齐。

#### 节点 V：结论与收口

- [ ] 给出唯一根因或按证据排序的剩余分支，说明是否由 `46c8bbb` 引入。
- [ ] 更新本节实施记录和必要的运维记录，提交并推送 GitHub `main`，清理诊断临时件。
- [ ] 除非用户随后明确要求修复，否则不改生产代码、配置或镜像。

### 20.5 停止条件与隐私边界

- IMAP 登录失败、目标时段有多封不可区分邮件或没有对应查询审计时，只报告证据缺口，不
  扩大到全天邮件、不输出原文，也不尝试制造新验证码。
- 所有邮件内容只在生产容器内存中参与布尔判定；输出不得包含 OTP、邮箱、Alias、主题、
  发件人、访问 key、Cookie、IMAP 密码或正文片段。
- 诊断不得改变 SEEN 标志、IMAP 配置、Alias 状态、sender filter、访问 key、审计数据或
  Apple/HME 远端状态。

### 20.6 本次诊断记录

- 2026-07-31 10:37：收到事故报告。10:30 邮件已在 QQ 邮箱可见，但用户在网站持续刷新
  未得到验证码。当前尚未连接生产；进入节点 T。
