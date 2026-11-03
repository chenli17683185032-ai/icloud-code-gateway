# 当前验证码 API 调用配置

核对日期：2026-11-03。本文不包含真实邮箱、密钥或验证码。

- Base URL：`https://icloud.yunbay.xyz`
- 推荐接口：`POST /api/v1/verification-codes`
- Content-Type：`application/json`
- JSON 字段：`email` 为完整隐藏邮箱，`key` 为已有调用凭据。
- 无需 Cookie；不要使用 Apple ID 登录密码或 IMAP 应用专用密码作为 API key。

```bash
curl 'https://icloud.yunbay.xyz/api/v1/verification-codes' \
  -H 'Content-Type: application/json' \
  --data '{"email":"your-alias@icloud.com","key":"YOUR_API_KEY"}'
```

也可以用 GET，邮箱需 URL 编码，密钥放 Authorization 请求头：

```bash
curl 'https://icloud.yunbay.xyz/api/v1/mailboxes/your-alias%40icloud.com/verification-code' \
  -H 'Authorization: Bearer YOUR_API_KEY' \
  -H 'Accept: application/json'
```

`YOUR_API_KEY` 替换为现有 Alias Token（通常以 `icg_` 开头）；若管理员已配置共用 API 密码，当前 Worker 实现也接受该凭据。本文不导出、不轮换任何密钥。

## 返回结果

- HTTP 200、`status="found"`：从 `data.code` 取验证码；`data` 还含 `email`、`received_at`、`expires_at`。
- HTTP 200、`status="waiting"`：还没有有效验证码，验证码和时间字段为 `null`，按 `retry_after` 重试（当前为 5 秒）。
- HTTP 401：邮箱/密钥不匹配或缺少凭据。
- HTTP 422：参数格式不正确。
- HTTP 429：触发限流，遵守 `retry_after`（当前为 60 秒）。

接口只返回最新一个有效 GPT/Grok 验证码，不返回邮件正文、标题、发件人或附件。标准接口不提供 `wait`、`timeout`、`since` 或 `limit` 参数。错误处理应先检查 HTTP 状态码，不能假定所有内部错误都有 `data` 字段。

公网未认证 GET 和无效凭据 POST 均已验证返回 HTTP 401。此检查仅验证接口与鉴权门禁，不作为真实新邮件端到端验收。
