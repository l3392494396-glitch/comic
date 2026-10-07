# comic 自动签到

用于 [18comic](https://18comic.ink) 个人账号的每日自动签到。通过 GitHub Actions
定时运行，完成后可用 [PushPlus](https://www.pushplus.plus/) 推送签到结果。

## 功能

- **登录态自动刷新**：每次运行先用账号密码登录，拿到最新的 AVS 登录态，再交给
  签到脚本使用，因此不需要再手工更新 Cookie。
- **个人中心签到**：调用 `/ajax/user_daily_sign` 完成每日签到。
- **月度签到**：自动识别“开启本月签到”按钮并提交请求。
- **任务进度核验**：读取金币 / 经验任务页，确认“每日登入”已完成。
- **PushPlus 推送**：以 Markdown 推送签到明细；未配置 Token 时自动跳过，不影响
  签到结果。
- **Cookie 兜底**：无法自动登录时，可改用 `JM_COOKIE` 手工提供 AVS 登录态。

## 工作流程

1. `refresh_login.py` 使用 `JM_USERNAME` + `JM_PASSWORD` 登录，取得新的 AVS Cookie，
   写入后续步骤的环境变量 `JM_COOKIE`。
2. `checkin.py` 读取该登录态，完成个人中心签到、月度签到，并核验任务进度。
3. 若配置了 `PUSHPLUS_TOKEN`，将结果推送到 PushPlus。

## 配置

在仓库 `Settings → Secrets and variables → Actions` 中新增以下 Secrets：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `JM_USERNAME` | 是 | 登录用户名 |
| `JM_PASSWORD` | 推荐 | 登录密码，用于每次自动刷新登录态 |
| `JM_COOKIE` | 否 | 仅填 `AVS` 的值，不含 `AVS=` 前缀；无法自动登录时的兜底 |
| `PUSHPLUS_TOKEN` | 否 | PushPlus 推送 Token，不配置则跳过推送 |

配置了 `JM_PASSWORD` 就会优先走账号密码登录；否则必须提供 `JM_COOKIE`。

## 本地运行

```bash
python -m pip install -r requirements.txt
cp .env.example .env      # 按需填写，或直接使用环境变量
python refresh_login.py   # 打印可用的 JM_COOKIE
python checkin.py
```

可用的环境变量：

- `JM_USERNAME`：用户名
- `JM_PASSWORD`：密码
- `JM_COOKIE`：AVS 登录态（备选，`refresh_login.py` 也会读取它作为兜底）
- `JM_BASE_URL`：自定义站点地址，默认 `https://18comic.ink`
- `JM_TIMEOUT`：单次请求超时秒数（1–120），默认 30

`.env` / `.env.local` 会被 `checkin.py` 自动加载，但不会覆盖已经存在的系统环境变量。

## 常见问题

- **提示“登录态可能已失效”**：登录态过期。配置 `JM_PASSWORD` 后每次运行都会自动
  刷新，即可避免；同时请确认账号密码仍然有效。
- **无法连接站点**：若本地 DNS 被拦截，`checkin.py` 会在证书校验通过的前提下自动
  尝试 Cloudflare 直连；也可用 `JM_BASE_URL` 指定可用镜像。
- **推送失败**：仅影响通知，不影响签到结果；检查 `PUSHPLUS_TOKEN` 是否正确。
- **定时任务被自动停用**：GitHub 会在仓库长期无提交后暂停 schedule，可在 Actions
  页面手动启用或手动触发一次。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试使用注入的假会话，不会访问真实站点。
