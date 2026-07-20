# comic 每日签到

这个目录是一个可以单独上传到 GitHub 的小项目。GitHub Actions 每天登录一次
18comic，并核验金币、经验页面中的“每日登入”任务是否已经完成。

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

建议使用私有仓库。不要把账号密码写进代码、README、`.env` 或工作流文件。

## 配置 Actions Secrets

打开 GitHub 仓库：

1. 进入 `Settings` → `Secrets and variables` → `Actions`。
2. 新建 Repository secret：`JM_USERNAME`，值为网站用户名。
3. 推荐新建 Repository secret：`JM_COOKIE`，值为已登录网页请求中的完整
   `Cookie` 请求头（只复制冒号后面的值）。配置后会优先使用 Cookie 登录。
4. 如果不使用 Cookie，则新建 Repository secret：`JM_PASSWORD`，值为网站密码。
5. 登录 [PushPlus](https://www.pushplus.plus/)，复制你的用户 Token 或消息 Token。
6. 新建 Repository secret：`PUSHPLUS_TOKEN`，值为刚复制的 Token。
7. 进入 `Actions`，选择 `Daily comic check-in`。
8. 点击 `Run workflow` 手动测试一次。

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
$env:JM_COOKIE = "AVS=你的登录Cookie; 其他Cookie=值"
$env:PUSHPLUS_TOKEN = "你的 PushPlus Token"
python checkin.py
```

测试结束后可清除当前 PowerShell 会话中的密码：

```powershell
Remove-Item Env:JM_PASSWORD
```

项目使用 `curl_cffi` 模拟 Chrome 的 TLS/HTTP2 请求指纹，以降低站点将 GitHub
Actions 请求误判为爬虫并返回 HTTP 403 的概率。安装依赖：

```powershell
python -m pip install --requirement requirements.txt
```

## 常见失败

- `配置错误`：检查两个 Actions Secrets 的名称是否完全正确。
- `登录失败`：密码不正确、账号需要网页验证，或账号被冻结。
- `没有返回 JSON`：网站维护、域名失效或触发了网站风控。
- `无法解析任务页`：网站页面结构发生变化，需要更新脚本解析逻辑。

如果网站更换域名，可新增仓库变量或修改工作流传入 `JM_BASE_URL`；务必只使用
网站可信的 HTTPS 域名。
"# comic"  
