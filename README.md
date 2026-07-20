# comic 每日签到

这个目录是一个可以单独上传到 GitHub 的小项目。GitHub Actions 每天登录一次
18comic，并核验金币、经验页面中的“每日登入”任务是否已经完成。

脚本不会自动点击广告、发布评论或回复、上传文件，也不会自动给作品点赞。

## 文件结构

```text
comic/
├── .github/workflows/daily-checkin.yml
├── tests/test_checkin.py
├── .env.example
├── .gitignore
├── checkin.py
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
3. 新建 Repository secret：`JM_PASSWORD`，值为网站密码。
4. 进入 `Actions`，选择 `Daily comic check-in`。
5. 点击 `Run workflow` 手动测试一次。

工作流默认每天 `16:17 UTC` 运行，对应中国标准时间次日 `00:17`。GitHub 的定时
任务可能有少量延迟，因此没有把时间设在整点。

## 本地测试

PowerShell：

```powershell
cd comic
python -m unittest discover -s tests -v
$env:JM_USERNAME = "你的用户名"
$env:JM_PASSWORD = "你的密码"
python checkin.py
```

测试结束后可清除当前 PowerShell 会话中的密码：

```powershell
Remove-Item Env:JM_PASSWORD
```

项目只使用 Python 标准库，不需要安装第三方依赖。

## 常见失败

- `配置错误`：检查两个 Actions Secrets 的名称是否完全正确。
- `登录失败`：密码不正确、账号需要网页验证，或账号被冻结。
- `没有返回 JSON`：网站维护、域名失效或触发了网站风控。
- `无法解析任务页`：网站页面结构发生变化，需要更新脚本解析逻辑。

如果网站更换域名，可新增仓库变量或修改工作流传入 `JM_BASE_URL`；务必只使用
网站可信的 HTTPS 域名。
"# comic"  
