# 代码审计与加固记录

审计对象：[Lappercn/gemini-pro](https://github.com/Lappercn/gemini-pro) `main`（提交 `2ce445c`）。
方法：通读 Python 后端与部署脚本，抽查浏览器驱动与前端打包产物，用 mock 后端拉起真实服务做回归测试。未在真实 Google 页面上联调，因此浏览器侧（`worker.py`、`backends/browser.py`）的行为只做了静态审阅。

## 1. 工作原理

FastAPI 网关把 OpenAI `/v1/chat/completions` 请求放进按租户轮转的公平队列，由线程池里的 Slot 调度一个共享 Chromium（Playwright）。浏览器打开 Google 搜索的 AI 模式页面（`udm=50`），读取答案后按 OpenAI 格式返回。遇到 `/sorry` 验证码页时，调用 YesCaptcha 付费打码服务拿到令牌。

## 2. 使用层面的风险（代码修不了）

| 编号 | 风险 | 说明 |
| :-- | :-- | :-- |
| R1 | 合规 | 通过伪装浏览器指纹（抹掉 `navigator.webdriver`、伪造 Windows UA、`AutomationControlled`）与付费破解 reCAPTCHA Enterprise 来获得免登录访问，违反 Google 服务条款。Google 可随时封堵，出口 IP 可能被长期风控。 |
| R2 | 隐私 | 提问以 `?q=` 形式发往 Google，也可能留在出口代理日志中。不要提交个人隐私、患者信息或机密。 |
| R3 | 稳定性 | 完全依赖 Google 页面 DOM 与内部接口，页面改版即失效；数据中心 IP 更容易触发验证码，打码成本随之上升。 |
| R4 | 供应链 | `proxy_svc/static` 是预编译的 Vue 包，无法证明与 `web/src` 一致。已核对其中没有外部请求（仅 `ys666.site` 赞助链接与 W3C 命名空间），仍建议自行 `npm ci && npm run build` 重新构建。 |
| R5 | 许可 | 上游没有 LICENSE，默认保留所有权利。私用自担风险，公开再分发前请联系作者。 |

## 3. 缺陷与漏洞清单

| 编号 | 严重度 | 位置 | 问题 | 处理 |
| :-- | :-- | :-- | :-- | :-- |
| B1 | 阻断 | `proxy_svc/backends/__init__.py` 等 | 所有 `__init__.py` 缺失，`make_backend` 未定义，服务启动即 `ImportError`。根因是 `.gitignore` 的 `_*.py` 把 `__init__.py` 一并忽略。 | 已补文件，`.gitignore` 增加 `!__init__.py` |
| S1 | 高 | `app.py` `_ident` | 未配置密钥时无鉴权且所有人都是管理员；README 示例监听 `0.0.0.0`，`/admin/keys` 可读出全部租户密钥，`/admin/settings`、压测、暂停池同样对公网开放。 | 默认返回 503，需显式 `ALLOW_NO_AUTH=1` |
| S2 | 高 | `app.py` `/admin/bootstrap` | 无需鉴权，首个访问者即可领走管理员密钥。 | 默认关闭，需 `ALLOW_BOOTSTRAP=1` |
| S3 | 高 | `deploy/gemini-pro.service` | 以 root 运行 Chrome，且启动参数含 `--no-sandbox` 与关闭站点隔离，渲染进程被利用即得 root。 | 改为专用低权限用户并加 systemd 沙箱。`--no-sandbox` 属浏览器驱动代码，未改，残余风险见 §5 |
| S4 | 中 | `config.py` `save_store` | `settings.json` 明文保存管理员与租户密钥，权限为默认 umask。 | 以 0600 写入，服务 `UMask=0077` |
| S5 | 中 | `deploy/ssh_run.py` `upload.py` | `AutoAddPolicy` 不校验主机指纹，易被中间人利用，且靠明文密码。 | 已删除，由 `install.sh` 取代 |
| S6 | 中 | `app.py` `/health` | 匿名可读租户名、队列、内存、线程数。 | 匿名仅返回精简字段 |
| S7 | 中 | `app.py` | `timeout` 无上限（可传 1e9 长期占住连接）；`/ask/batch` 单条问题不校验长度；无请求体上限。 | `timeout` 限 0–600 秒，单条 1–60000 字，`MAX_BODY_BYTES` 默认 8MB |
| S8 | 低 | `config.py` `whois` | 非 ASCII 密钥使 `hmac.compare_digest` 抛 `TypeError`，返回 500。 | 改为字节比较，返回 401 |
| S9 | 低 | `config.py` `whois` | 已禁用的租户密钥仍能通过鉴权。 | 鉴权阶段即拒绝 |
| S10 | 低 | `app.py` `panel_asset` | 用 `startswith` 判断目录，同前缀的兄弟目录可绕过。 | 改用 `os.path.commonpath` |
| S11 | 低 | 全局 | 无鉴权失败限速。密钥足够长时风险有限。 | 未做，建议前置 Caddy 或 fail2ban |
| S12 | 低 | `config.py` | 默认保存每条提问前 80 字到统计与面板。 | 部署脚本默认 `METRICS_PREVIEW_CHARS=0` |
| F1 | 高 | `config.py` `whois` `validate` | 只配置 `API_KEY` 时（最常见的单密钥配置），该密钥被判为普通租户而不是管理员，管理员接口与控制台一律 403；`validate()` 还会误报“API_KEY 与某个 USERS 密钥重复”。 | 先判断管理员密钥，重复校验跳过自动生成的默认租户 |
| F2 | 中 | `app.py` `/v1/chat/completions` | 把 OpenAI 标准字段 `user` 当作租户切换：非管理员得 403，管理员写了不存在的名字得 404，导致常带 `user` 的客户端无法调用。 | 仅管理员指向已存在租户时生效，其余忽略 |
| F3 | 中 | `config.py` `_apply_store` | `settings.json` 里的 `api_key` 覆盖环境变量 `API_KEY`，改环境变量无法轮换密钥。 | 环境变量优先 |
| F4 | 低 | `config.py` `load_store` | `settings.json` 内容异常会让服务启动失败。 | 捕获并记录错误 |
| F5 | 低 | `proxy_svc/run.sh` | mock 后端也强制要求 `YES_KEY`。 | 仅 browser 后端要求 |
| F6 | 低 | `deploy/install_chrome.sh` | 缺 `apt-get update` 与架构判断。 | 已补 |
| F7 | 提示 | `app.py` | `stream=true` 是整段答案一次性发出的“假流式”，错误以 HTTP 200 加 `[Error: …]` 文本返回。 | 未改，避免改变客户端行为 |
| F8 | 提示 | `config.py` `machine_capacity` | 内存预算取 `max(可用, 总内存-350MB)`，小内存机器会高估并发。 | 安装脚本按内存限制 `MAX_WORKERS`，并设 cgroup 内存上限与 swap |
| F9 | 提示 | `fingerprint.py` | UA 硬编码为 Chrome/157。`apt` 自动升级 Chrome 后主版本不一致时，页面可能只返回空壳（`BLOCKED`/`UNAVAILABLE`）。 | 属反检测逻辑，按约定未改 |

## 4. 有意不做的事

`fingerprint.py`、`solver.py`，以及 `worker.py` 与 `backends/browser.py` 中的指纹伪装和验证码处理，只做了审阅，没有增强。本次加固不扩展任何绕过 Google 风控的能力。

## 5. 残余风险

- Chrome 仍以 `--no-sandbox` 运行，并默认关闭站点隔离（`PW_SHARE_RENDERER=1`）。已限制在无登录权限的专用用户和 systemd 沙箱内；若更看重隔离，设 `PW_SHARE_RENDERER=0`，会多占内存。
- 服务只用 HTTP 对外时密钥是明文传输，生产请使用 `--domain` 走 HTTPS。
- 浏览器侧逻辑没有在真实页面上回归过。

## 6. VPS 部署评估

- 规格：x86_64，Ubuntu 22.04 / 24.04。2 核 4GB 较合适；2GB 可运行，需要 swap 并把并发控制在 2 以内；磁盘 10GB 以上。
- 网络：服务器需能直连 Google。数据中心 IP 更容易被要求验证码，可用 `EGRESS_PROXY` 指定更干净的出口。
- 暴露面：只开放 443（Caddy 反代），内部端口默认 8888，仅监听本机，可用 `--port` 修改（8888 也是宝塔面板、Jupyter 的默认端口，被占用时安装脚本会直接报错）。云厂商防火墙需另行放行。
- 成本：YesCaptcha 按次计费，遇到验证码才会消耗。
- 运维：日志用 `journalctl -u gemini-pro`，备份 `/var/lib/gemini-pro`，更新用 `install.sh --update`。

## 7. 验证

`tests/test_smoke_mock.py` 用 mock 后端拉起真实服务，覆盖 B1、S1、S2、S6–S10、F1–F4，不访问外网。S4（文件权限）的用例只在 POSIX 系统上运行：

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

`deploy/install.sh` 通过了 shellcheck；尚未在真实 Ubuntu 主机上端到端运行，首次使用请先用 `--mock` 在测试机上走一遍。
