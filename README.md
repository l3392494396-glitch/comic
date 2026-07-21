# comic 每日签到

这个目录是一个可以单独上传到 GitHub 的小项目。GitHub Actions 每天使用账号密码
登录 18comic，取得新的 AVS 登录态后调用个人中心签到，并核验金币、经验页面中的
“每日登入”任务是否完成。

脚本只会提交个人中心的每日签到；不会自动点击广告、发布评论或回复、上传文件，
也不会自动给作品点赞。

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

建议使用私有仓库。不要把密码、Cookie 或 Token 写进代码、README 或工作流文件。

## 配置 Actions Secrets

打开 GitHub 仓库：

1. 进入 `Settings` → `Secrets and variables` → `Actions`。
2. 新建 Repository secret：`JM_USERNAME`，值为网站用户名。
3. 新建 Repository secret：`JM_PASSWORD`，值为网站密码。
4. 登录 [PushPlus](https://www.pushplus.plus/)，复制你的用户 Token 或消息 Token。
5. 新建 Repository secret：`PUSHPLUS_TOKEN`，值为刚复制的 Token。
6. 进入 `Actions`，选择 `Daily comic check-in`。
7. 点击 `Run workflow` 手动测试一次。

账号密码只通过 HTTPS 提交给服务器返回的登录表单，不会写入日志。请只把密码放在
GitHub Actions Secrets 或本地环境文件中，不要提交到仓库。

每次运行都会发送一条 Markdown 格式的 PushPlus 通知，内容包括个人中心签到结果
以及金币、经验任务进度。当天已经签到时会按成功处理。Token 不要写进代码或工作流文件。

个人中心接口已确认签到成功后，金币或经验任务页因页面改版而无法解析时只会作为
核验警告写入日志和通知，不会把已经成功的签到判定为失败。

脚本每次运行都会重新登录，并启用网站的长期登录选项，然后使用站点返回的 `AVS`
完成签到和任务核验。服务器返回 HTTPS 跳转时，脚本会继续访问目标域名；非 HTTPS
跳转仍会被拒绝。

工作流默认每天 `16:17 UTC` 运行，对应中国标准时间次日 `00:17`。GitHub 的定时
任务可能有少量延迟，因此没有把时间设在整点。

## 本地测试

PowerShell：

```powershell
cd comic
python -m unittest discover -s tests -v
python checkin.py
```

本地运行时，脚本会自动读取项目根目录的 `.env`；如果 `.env` 不存在，则读取
`.env.example`。文件格式如下：

```dotenv
JM_USERNAME=你的用户名
JM_PASSWORD=你的网站密码
PUSHPLUS_TOKEN=你的PushPlus Token
JM_BASE_URL=https://jmcomic-zzz.one
```

已经存在的系统环境变量不会被文件覆盖，因此 GitHub Actions Secrets 仍然优先。

旧的 `JM_COOKIE` 配置仍可作为兼容方式使用，只填写 AVS 值且不要带 `AVS=`；同时
配置 `JM_PASSWORD` 时，脚本优先使用账号密码重新登录。

如果改为手动设置 PowerShell 环境变量，测试结束后可清除当前会话中的密码：

```powershell
Remove-Item Env:JM_PASSWORD
```

项目使用 `curl_cffi` 模拟 Chrome 的 TLS/HTTP2 请求指纹，以降低站点将 GitHub
Actions 请求误判为爬虫并返回 HTTP 403 的概率。安装依赖：

```powershell
python -m pip install --requirement requirements.txt
```

## 常见失败

- `配置错误`：检查 `JM_USERNAME`、`JM_PASSWORD` 和 `PUSHPLUS_TOKEN` 的名称。
- `账号密码登录失败`：检查用户名、密码，或网站是否显示 Cloudflare 验证页面。
- `任务进度无法读取`：网站页面结构发生变化；不影响已经由个人中心接口确认的签到。

如果网站更换域名，可新增仓库变量或修改工作流传入 `JM_BASE_URL`；务必只使用
网站可信的 HTTPS 域名。
