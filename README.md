# comic 每日签到

这个目录是一个可以单独上传到 GitHub 的小项目。GitHub Actions 每天使用已有的
18comic Cookie 访问任务页，并核验金币、经验页面中的“每日登入”任务是否完成。

项目不会向网站提交用户名和密码，也不包含密码登录代码。

脚本不会自动点击广告、发布评论或回复、上传文件，也不会自动给作品点赞。

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

建议使用私有仓库。不要把 Cookie 或 Token 写进代码、README、`.env` 或工作流文件。

## 配置 Actions Secrets

打开 GitHub 仓库：

1. 进入 `Settings` → `Secrets and variables` → `Actions`。
2. 新建 Repository secret：`JM_USERNAME`，值为网站用户名；它只用于拼接任务页地址。
3. 新建 Repository secret：`JM_COOKIE`，值只填写 AVS 的值，不要包含 `AVS=` 前缀。
4. 登录 [PushPlus](https://www.pushplus.plus/)，复制你的用户 Token 或消息 Token。
5. 新建 Repository secret：`PUSHPLUS_TOKEN`，值为刚复制的 Token。
6. 进入 `Actions`，选择 `Daily comic check-in`。
7. 点击 `Run workflow` 手动测试一次。

`JM_COOKIE` 与密码具有同等敏感性，不要写进代码、README、工作流或 Actions 日志。
如果 Cookie 失效，需要从已登录的网页重新复制并更新 Secret。

每次运行都会发送一条 Markdown 格式的 PushPlus 通知，内容包括签到是否成功以及
金币、经验任务进度。Token 不要写进代码或工作流文件。

工作流默认每天 `16:17 UTC` 运行，对应中国标准时间次日 `00:17`。GitHub 的定时
任务可能有少量延迟，因此没有把时间设在整点。

## 本地测试

PowerShell：

```powershell
cd comic
python -m unittest discover -s tests -v
$env:JM_USERNAME = "你的用户名"
$env:JM_COOKIE = "你的AVS值"
$env:PUSHPLUS_TOKEN = "你的 PushPlus Token"
python checkin.py
```

测试结束后可清除当前 PowerShell 会话中的 Cookie：

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
- `无法解析任务页`：网站页面结构发生变化，需要更新脚本解析逻辑。

如果网站更换域名，可新增仓库变量或修改工作流传入 `JM_BASE_URL`；务必只使用
网站可信的 HTTPS 域名。
"# comic"  
