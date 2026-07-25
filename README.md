# comic 每日签到

这个目录是一个可以单独上传到 GitHub 的小项目。脚本每天使用已有的 AVS Cookie
调用个人中心签到，然后打开“签到活动”页执行“开启本月签到”，最后核验金币、
经验页面中的“每日登入”任务是否完成。

脚本只会提交个人中心每日签到和月度日历签到；不会自动点击广告、发布评论或回复、
上传文件，也不会自动给作品点赞。

## 文件结构

```text
comic/
├── .github/workflows/main.yml
├── tests/test_checkin.py
├── .env.example
├── .gitignore
├── checkin.py
├── requirements.txt
└── README.md
```

## 上传到 GitHub

请将 **comic 文件夹里的内容** 放在新 GitHub 仓库的根目录。工作流必须位于
仓库根目录的 `.github/workflows/` 下，不能再套一层 `comic/`。

建议使用私有仓库。不要把 Cookie 或 Token 写进代码、README 或工作流文件。

## 配置 Actions Secrets

打开 GitHub 仓库：

1. 进入 `Settings` → `Secrets and variables` → `Actions`。
2. 新建 Repository secret：`JM_USERNAME`，值为网站用户名。
3. 新建 Repository secret：`JM_COOKIE`，值填写 `AVS=你的登录Cookie`。
4. 登录 [PushPlus](https://www.pushplus.plus/)，复制你的用户 Token 或消息 Token。
5. 新建 Repository secret：`PUSHPLUS_TOKEN`，值为刚复制的 Token。
6. 进入 `Actions`，选择 `Daily comic check-in`。
7. 点击 `Run workflow` 手动测试一次。

`JM_COOKIE` 与密码具有同等敏感性，只能放在 GitHub Actions Secrets 或本地
`.env.local` 中，不要提交到仓库。

每次运行都会发送一条 Markdown 格式的 PushPlus 通知，内容包括个人中心签到、
月度签到以及金币、经验任务进度。当天已经签到时会按成功处理。

个人中心接口已确认签到成功后，金币或经验任务页因页面改版而无法解析时只会作为
核验警告写入日志和通知，不会把已经成功的签到判定为失败。

脚本不会提交账号密码，只使用 Secret 中的 `AVS` 完成签到和任务核验。网页返回
HTTPS 跳转时脚本会继续访问目标域名；非 HTTPS 跳转仍会被拒绝。月度签到按钮
对应的请求额外限制为同域地址，避免 Cookie 被发送到站外。

如果本机 DNS 将跳转域名解析为已确认的异常地址 `182.43.124.7`，脚本会从该域名
的 IPv6 记录中恢复对应的 Cloudflare IPv4 后直连。此过程不使用代理，并且脚本
明确忽略系统及环境变量中的代理设置；连接仍按原域名执行 HTTPS 证书校验，正常
DNS 解析不会被改写。

工作流默认每天 `16:17 UTC` 运行，对应中国标准时间次日 `00:17`。GitHub 的定时
任务可能有少量延迟，因此没有把时间设在整点。

## 本地测试

PowerShell：

```powershell
cd comic
python -m unittest discover -s tests -v
python checkin.py
```

本地运行时，脚本会自动读取项目根目录的 `.env.local`；如果不存在，则读取
`.env`。文件格式如下：

```dotenv
JM_USERNAME=你的用户名
JM_COOKIE=AVS=你的登录Cookie
PUSHPLUS_TOKEN=你的PushPlus Token
```

已经存在的系统环境变量不会被文件覆盖，因此 GitHub Actions Secrets 仍然优先。

`JM_COOKIE` 也兼容只填写 AVS 值，脚本会自动补上 `AVS=`；如果粘贴了完整 Cookie，
脚本只保留其中的 `AVS`。

如果改为手动设置 PowerShell 环境变量，测试结束后可清除当前会话中的 Cookie：

```powershell
Remove-Item Env:JM_COOKIE
```

项目使用 `curl_cffi` 模拟 Chrome 的 TLS/HTTP2 请求指纹，以降低站点将 GitHub
Actions 请求误判为爬虫并返回 HTTP 403 的概率。安装依赖：

```powershell
python -m pip install --requirement requirements.txt
```

## 常见失败

- `配置错误`：检查 `JM_USERNAME`、`JM_COOKIE` 和 `PUSHPLUS_TOKEN` 的名称。
- `登录态未生效`：`AVS` 已过期，需要重新登录网站并更新 Cookie。
- `缺少“开启本月签到”按钮`：签到活动页面结构发生变化，需要更新脚本解析逻辑。
- `任务进度无法读取`：网站页面结构发生变化；不影响已经由个人中心接口确认的签到。

脚本固定从 `18comic.ink` 开始访问。
