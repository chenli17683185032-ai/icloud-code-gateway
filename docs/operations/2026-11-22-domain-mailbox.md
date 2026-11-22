# 新增域名邮箱（2026-11-22）

本文仅记录邮箱配置和验收，不包含登录密码、API Token、邮件正文或验证码。

## 操作内容

- 目标：为现有 `tokenopen.top` 域名新增 `codes@tokenopen.top`。
- 只读核查确认：Cloudflare Email Routing 已启用，DNS 记录已锁定。
- 原有 `subteam@tokenopen.top` 路由指向 `icloud-mailbox-worker`。
- 域名另有一条已启用的全收路由，指向独立的 `grok-moemail-email-receiver-worker`。全收路由不等于已在 iCloud 收件箱登记所有地址。
- 已确认当前账户存在已验证的收件目标，可新增精确地址转发规则。
- 本操作不需要修改或重新部署 Worker，也不需要调整 MX、密钥、iCloud 转发设置或数据库。
- 操作计划已先以提交 `1c414d3` 上传 GitHub 分支 `codex/add-domain-mailbox-20261122`，随后保存新地址规则。
- 新增精确地址规则：`codes@tokenopen.top` → 当前账户既有、已验证的主 Gmail。完整个人收件地址仅保存在 Cloudflare 配置及本地私有截图中。
- 新地址的邮件在该 Gmail 收件箱查看，不进入 iCloud 接码后台。

## 遇到的坑

- iCloud Worker 当前只接受配置的固定域名收件地址。仅将新地址路由到该 Worker，不能证明邮件能够入库。
- 精确地址规则和全收规则属于不同的匹配层次。新增规则必须核对已有路由，避免覆盖原地址或误改全收行为。
- 控制台规则生效与真实邮件送达是不同的验收层次，不能将“活跃”状态描述为已完成新邮件端到端测试。

## 获得的收获

- 新增域名邮箱应先确认现有 Email Routing、收件服务和查看入口，再决定是否需要改代码。
- 使用已验证的既有收件目标，可以让新地址直接进入用户已有的收件箱。
- 无需部署应用的邮箱规则变更，也应保留本地及 GitHub 操作记录。

## 验收结果

- 北京时间 2026-11-22，Cloudflare 控制台保存成功，并显示 `codes@tokenopen.top` 为“活跃”；规则类型为发送到电子邮件，目标为选定的已验证主 Gmail。
- 同一规则列表显示原 `subteam@tokenopen.top` → `icloud-mailbox-worker` 规则及全收规则均保持“活跃”。
- 仅新增一条精确地址转发规则，未更新 Worker、DNS、原有收件规则、访问密钥或数据库。
- 配置截图保存在本地忽略目录 `outputs/domain-mailbox-20261122/codes-active.jpg`，不上传包含个人收件地址的截图。
- 本次未发送测试邮件，不将控制台检查视为真实新邮件送达验收。

## 回退说明

如需取消新地址的 Gmail 转发，可在 Cloudflare Email Routing 中停用本次新增的 `codes@tokenopen.top` 精确规则。停用后该地址将重新受既有全收规则处理，不等于停止该地址的一切收件。回退操作不涉及原 `subteam@tokenopen.top` 规则或全收规则。
