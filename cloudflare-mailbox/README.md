# Cloudflare 隐邮收件箱

这个子项目使用 Cloudflare Email Workers、D1 和 Workers Static Assets，替代当前 QQ 邮箱与 IMAP 读取链路。iCloud Hide My Email 把邮件转发到你的域名地址，Email Worker 解析原始收件 Alias、正文和验证码，加密写入 D1。普通用户通过“隐藏邮箱 + Token”只查看 GPT/Grok 验证码；操作员通过独立 `/admin/` 后台查看全部 Alias 的完整邮件。

## 运行边界

- 本地 `control` 继续负责 Apple 登录、Alias 创建/停用/删除和 Token 签发。
- Cloudflare Worker 接收域名邮件、保存 D1、提供查询网页和 `/control/v1/*` 同步接口。
- 不再需要 QQ 邮箱、IMAP、云端 Chromium、`cn-proxy` 或常驻 VPS edge。
- “服务器 2”可以作为部署机运行 Wrangler，但 Worker 与 D1 实际运行在 Cloudflare 网络，不是 VPS 进程。

## 当前生产配置

- 用户主入口：`https://icloud.yunbay.xyz`
- 维护入口：`https://mailbox.yunbay.xyz`
- 域名收件地址：`otp@yunbay.xyz`
- Email Routing：`otp@yunbay.xyz → icloud-mailbox-worker`
- D1：`icloud-mailbox`（APAC）
- 旧链接兼容：`/#key=<Token>` 会通过全局 HMAC 索引自动找到邮箱；新链接继续使用 `/#email=<邮箱>&key=<Token>`。
- 操作员后台：`https://icloud.yunbay.xyz/admin/`，只输入操作员 Token，无需逐个输入邮箱。
- 原 `icloud.yunbay.xyz` DNS/VPS 保留在 Worker route 后方；移除该 route 即可恢复旧站，不需要重建 DNS。
- iCloud Hide My Email 会把 OpenAI/Grok 的发件地址改写成 Apple `@icloud.com` 中继地址。Worker 优先识别原始官方域名；遇到 Apple 中继时，要求发件显示名、标题、正文中至少两处品牌证据才公开验证码，避免只靠一个可伪造显示名放行。

## 保存的数据

- `aliases`：邮箱 HMAC、Token HMAC、状态及加密 Alias 元数据。
- `messages`：加密发件人、标题、纯文本正文、验证码，以及明文分类、保留级别和收件/过期时间。
- `auth_rate_limits`：不可逆来源指纹和分钟窗口计数。
- 不保存明文 Token，不保存 HTML，不保存附件。验证码和普通邮件默认 24 小时清理；会员开通、付款、封号、申诉、工单和售后支持等重要非验证码邮件长期保存。

## 本地开发

要求 Node.js 20+。

```bash
cd cloudflare-mailbox
npm ci
cp .dev.vars.example .dev.vars
```

为三个 32 字节密钥分别生成值：

```bash
openssl rand -base64 32
```

把测试值写入 `.dev.vars`，然后：

```bash
npm run db:migrate:local
npm run dev
```

默认地址由 Wrangler 输出，通常是 `http://127.0.0.1:8787`。

完整门禁：

```bash
npm run check
```

## 在服务器 2 上部署

服务器 2 只需要 Node.js、Git 和 Wrangler 的 Cloudflare 授权，不需要 Docker、SQLite 服务或常驻守护进程。

```bash
git clone https://github.com/chenli17683185032-ai/icloud-code-gateway.git
cd icloud-code-gateway/cloudflare-mailbox
npm ci
npx wrangler login
```

无浏览器服务器也可以使用受限的 `CLOUDFLARE_API_TOKEN`，不要把 Token 写入仓库、命令历史或运维文档。

### 1. 创建 D1

```bash
npx wrangler d1 create icloud-mailbox
```

把输出的真实 `database_id` 替换进 `wrangler.jsonc`，不要继续使用全零占位 ID。

### 2. 写入 Worker secrets

`CONTROL_PLANE_TOKEN` 必须与本地 control 当前使用的值完全一致。其余三个值分别运行一次 `openssl rand -base64 32` 生成。

```bash
npx wrangler secret put CONTROL_PLANE_TOKEN
npx wrangler secret put LOOKUP_HMAC_KEY
npx wrangler secret put DATA_ENCRYPTION_KEY
npx wrangler secret put SESSION_SIGNING_KEY
npx wrangler secret put OPERATOR_ACCESS_TOKEN
```

前三个加密/HMAC key 一旦更换，既有 Token 摘要、加密邮件或登录会话会失效。操作员 Token 轮换只会使后台会话失效。生产轮换前必须先设计迁移，不要直接覆盖前三个 key。

### 3. 设置收件地址和保留时间

在 `wrangler.jsonc` 中设置：

```jsonc
"INBOX_ADDRESS": "otp@your-domain.com",
"EMAIL_RETENTION_SECONDS": "86400",
"MAX_EMAIL_BYTES": "5000000"
```

### 4. 迁移并部署

```bash
npm run db:migrate:remote
npm run deploy
```

随后在 Cloudflare Dashboard 为 Worker 绑定查询站点的自定义域名，例如 `mailbox.your-domain.com`。

### 5. 配置 Cloudflare Email Routing

1. 在域名的 Email Routing 中启用 Cloudflare MX 记录。
2. 创建地址 `otp@your-domain.com`，目标选择 `Send to a Worker`。
3. 选择本项目部署出的 `icloud-mailbox-worker`。
4. 第一封测试邮件到达后，确认 Apple 转发保留了以下任一原始收件头：`To`、`Delivered-To`、`X-Original-To`、`X-Apple-Original-Recipient`。

如果所有原始 iCloud Alias 头都被中间服务改写成 `otp@your-domain.com`，Worker 无法判断邮件属于哪个隐藏邮箱，不能正式切换。

### 6. 修改 iCloud 转发目标

先把 `otp@your-domain.com` 加入 Apple 账户并完成验证，再在 iCloud Hide My Email 设置中把转发目标切到该地址。建议先用一个 Alias 做真实验证码测试，不要一次切完后立即删除 QQ 回滚路径。

### 7. 让本地 control 同步到 Worker

在本机受限的 `icloud-control-plane.env` 中加入：

```dotenv
ICLOUD_GATEWAY_EDGE_BASE_URL=https://mailbox.your-domain.com
ICLOUD_GATEWAY_PUBLIC_BASE_URL=https://mailbox.your-domain.com
ICLOUD_GATEWAY_EDGE_SYNC_ENABLED=1
```

重新启动本地 control，然后点击“同步到云端”。现有五个 `/control/v1/*` 合同保持不变，所有 Alias 和已有 Token 会写入 D1。新生成的取码链接格式为：

```text
https://mailbox.your-domain.com/#email=<隐藏邮箱>&key=<Token>
```

邮箱和 Token 都在 URL fragment 中，不会进入 Cloudflare HTTP 请求日志；页面建立 HttpOnly 会话后会立即清除 fragment。

### 8. 下线 QQ 与旧 VPS edge

真实收信、正文显示、验证码提取、Token 轮换和 Alias 停用全部验证通过后：

1. 从本机凭据文件移除 `ICLOUD_GATEWAY_IMAP_*`，本地控制台不再登录 QQ。
2. 保留 QQ 转发回滚至少 24 小时，再决定是否删除旧配置。
3. 旧 VPS 上的 iCloud `app/browser/cn-proxy` 不再承担收信，可按现有运维手册备份后下线。

## 用户接口

- `POST /api/session`：邮箱 + Token 建立 15 分钟 HttpOnly 会话。
- `GET /api/session`：无错误恢复当前会话状态。
- `GET /api/messages`：只返回当前邮箱的 GPT/Grok 验证码和时间。
- `POST /api/logout`：结束查询会话。

普通用户响应不包含发件人、标题、正文或其他类别邮件。

## 操作员接口

- `POST /api/operator/session`：只使用操作员 Token 建立后台会话。
- `GET /api/operator/session`：恢复后台会话。
- `GET /api/operator/messages`：返回全部 Alias 的完整邮件。
- `POST /api/operator/logout`：退出后台。

用户页与后台都每 3 秒刷新一次；自动刷新在数据未变化时不重绘，有新邮件时静默更新，只有手动刷新播放一次轻微反馈。

## 控制面接口

- `POST /control/v1/aliases`
- `POST /control/v1/aliases/by-email/{email}/key`
- `DELETE /control/v1/aliases/by-email/{email}/key`
- `POST /control/v1/aliases/by-email/{email}/state`
- `DELETE /control/v1/aliases/by-email/{email}`

所有控制面接口都要求 `Authorization: Bearer <CONTROL_PLANE_TOKEN>`。

## 回滚

Cloudflare 切换失败时：

1. 把 iCloud Hide My Email 转发目标切回原 QQ/IMAP 邮箱。
2. 把本机 `ICLOUD_GATEWAY_IMAP_ENABLED` 改回 `1` 并重启 control。
3. 从 `wrangler.jsonc` 删除 `icloud.yunbay.xyz/*` Worker route 后重新部署；原 DNS/VPS 会重新接管旧域名。
4. 本地 edge/public URL 继续使用 `https://icloud.yunbay.xyz`，执行一次旧 edge 同步。
5. D1 数据保留用于取证，不要在故障处理中直接删除数据库或覆盖加密密钥。
