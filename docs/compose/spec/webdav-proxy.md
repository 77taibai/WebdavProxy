---
feature: webdav-proxy
status: delivered
updated: 2026-10-11
branch: feature/webdav-proxy
commits: e2db059..fd74066
---

# WebDAV 代理程序

## Report

**What was built** — 一个基于 Python/aiohttp 的 WebDAV 中间代理。客户端以 Basic 认证访问代理（`PROXY_USER`/`PROXY_PASSWORD`），代理用环境变量配置的上游凭据（`UPSTREAM_URL`/`UPSTREAM_USER`/`UPSTREAM_PASSWORD`）转发全部 WebDAV 方法，双向流式、不落盘。上游 302/301/307/308 由代理自行跟随（跨域跳转时 auth 由 aiohttp 自动剥离，实测证实），客户端永远只看到最终响应，`Range` 断点续传经跳转后可用。`READ_ONLY=true` 时九种写方法被 403 拦截且不触达上游。配置支持 `.env`（自实现解析，真实环境变量优先）；启动时缺必填变量报错退出。附带 `python:3.12-slim` 非 root Dockerfile（SaaS `PORT` 注入兼容）、`/healthz` 免认证健康检查、中文 README 与 `.env.example`。

**Verification** — `.\.venv\Scripts\python.exe -m pytest tests -q` → 38 passed（含配置/认证/302 跟随/跨域不泄凭据/Range 透传/流式上传/全 14 方法转发/只读 403/502/查询串转发）；实测探针证实跨域 302 剥离 `Authorization`、流式 body 的 PUT 遇重定向按已知限制返回 502。独立评审子代理结论：Spec compliance PASS、Correctness PASS、Consistency PASS → APPROVED；其提出的 4 个 minor（prepare 泄漏、流式中断静默、`OPTIONS *` ValueError→500、共享 CookieJar）与 2 项测试补强已全部修复并重测通过。

**Journey log**
- `OPTIONS *`（星形 request-target）会让 `raw_path` 拼进上游 URL 触发 yarl 的 `ValueError`；在 502 捕获集中加 `ValueError` 兜底。
- 带流式 body 的写方法被上游重定向时，aiohttp 无法重放请求体 → 502；不要为可重放而全文缓冲上传体（大文件内存爆）。
- yarl 的 `query_string` 在服务端会解码再编码，断言查询串时用 ASCII 参数避免编码假设。
- 编辑测试文件时切走了 502 测试的函数定义行，靠 pytest 立即失败暴露——重审编辑后必须跑套件。
- aiohttp `DummyCookieJar` 可避免代理会话间共享上游 cookie 造成跨客户端状态渗透。

## [S1] Problem

用户（WebDAV 客户端）需要一个中间代理：客户端把请求发给代理，代理转发给上游 WebDAV 服务器。两个核心痛点：

1. 上游（如 Alist 等网盘挂载方案）对下载路径常返回 302 跳转到异地直链；标准代理会把 302 原样传回，而多数客户端只支持 Basic 认证的 WebDAV，跟随 302 时会丢失认证或干脆不支持。需要代理本身跟随 302，把最终内容流式返回给客户端。
2. 需要可选的只读模式：仅允许读操作，拒绝一切会修改上游数据的请求。

代理自身的认证凭据与上游凭据必须隔离，全部通过环境变量或 `.env` 提供，并以 Docker 镜像形态交付，便于 SaaS 平台部署。

## [S2] Design

### 架构

- 技术栈：Python 3.12+ / aiohttp（服务端+客户端均为异步，天然支持双向流式转发）。
- 单进程单应用；`webdav_proxy` 包 + `python -m webdav_proxy` 入口。
- 配置模块 `webdav_proxy/config.py`：读取环境变量，若存在 `.env`（工作目录）则加载，已存在的真实环境变量优先于 `.env`。`.env` 解析自实现（`KEY=VALUE`、忽略空行与 `#` 注释、支持 `export ` 前缀与引号包裹），无额外依赖。

### 配置契约（环境变量）

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `UPSTREAM_URL` | 是 | — | 上游 WebDAV 基地址，如 `https://dav.example.com/dav/` |
| `UPSTREAM_USER` | 否 | 空 | 上游 Basic 认证用户名 |
| `UPSTREAM_PASSWORD` | 否 | 空 | 上游 Basic 认证密码 |
| `PROXY_USER` | 是 | — | 代理对客户端要求的 Basic 用户名 |
| `PROXY_PASSWORD` | 是 | — | 代理对客户端要求的 Basic 密码 |
| `READ_ONLY` | 否 | `false` | `true`/`1`/`yes`（不区分大小写）时启用只读 |
| `PORT` | 否 | `8080` | 监听端口，绑定 `127.0.0.1`（本地模式）；Docker 内绑 `0.0.0.0` 由 `HOST` 控制：`HOST` 默认 `0.0.0.0`（容器约定），本地运行可设 `127.0.0.1` |
| `HOST` | 否 | `0.0.0.0` | 监听地址 |

- 缺少 `UPSTREAM_URL`、`PROXY_USER`、`PROXY_PASSWORD` 任一必填项时启动失败并退出，打印缺失变量名。
- `HOST`/`PORT` 缺一不判错（均有默认）。

### 认证（客户端 → 代理）

- 代理要求 HTTP Basic 认证，凭据与 `PROXY_USER`/`PROXY_PASSWORD` 比对（恒定时间比较）。
- 缺失或错误凭据返回 `401` + `WWW-Authenticate: Basic realm="webdav-proxy"`。
- `GET /healthz` 免认证，返回 `200` 文本 `ok`，供 SaaS 健康检查。

### 转发（代理 → 上游）

- 允许方法：`OPTIONS, GET, HEAD, PROPFIND, REPORT, PROPPATCH, PUT, DELETE, MKCOL, COPY, MOVE, LOCK, UNLOCK, POST` 之外的未知方法也原样转发（WebDAV 扩展方法兼容）。
- 请求转发规则：
  - 上游 URL = `UPSTREAM_URL` 基地址拼接请求路径与查询串。
  - 复制客户端请求头，剔除逐跳头（`Connection, Keep-Alive, Proxy-Authenticate, Proxy-Authorization, TE, Trailer, Transfer-Encoding, Upgrade`）及 `Host, Content-Length, Authorization, Accept-Encoding`（由客户端库自动生成）。
  - `Authorization` 替换为上游凭据（若配置了 `UPSTREAM_USER`）；客户端的原凭据永不透传。
  - 请求体流式转发（`request.content` 直接作为客户端 body），支持大文件上传，不整体驻留内存。
- 响应回传规则：
  - 回传上游状态码与响应头，剔除逐跳头及 `Content-Length, Transfer-Encoding, Content-Encoding, Connection`（由服务端按实际流重设）。
  - 响应体流式回传（分块迭代客户端响应流）。

### 302 跟随

- 对所有方法使用"跟随重定向"的客户端请求（`allow_redirects=True`），代理自己完成 302/301/307/308 链的跳转，最终把目标响应（状态码、头、体）作为代理响应返回——客户端永远看不到 3xx。
- 上游凭据仅在跳转到"同源"目标时携带；跳转到其他域名的直链不带 Authorization（防止凭据泄露），该行为由客户端库 `allow_redirects` 的默认凭据剥离策略保证。
- 下载场景：客户端 Header 中的 `Range` 原样透传，断点续传/分块下载经跳转后仍然可用（目标返回 206 时原样回传）。
- 已知限制（实测确认）：带流式请求体的写方法（如 PUT）若被上游重定向，aiohttp 客户端无法重放已消费的流式 body，此时代理返回 `502` 及明确错误信息；不会静默丢数据。下载链路（GET/HEAD 无 body）不受影响；为可重放而把上传体全文缓冲进内存会破坏大文件上传的流式性，故不采用。

### 只读模式

- `READ_ONLY=true` 时，以下写方法返回 `403 Forbidden`（带 JSON 错误说明），请求不会被转发：`PUT, DELETE, MKCOL, COPY, MOVE, PROPPATCH, LOCK, UNLOCK, POST`。
- 读方法（`GET, HEAD, OPTIONS, PROPFIND, REPORT`）与其他未列出方法正常转发。

### 错误行为

- 上游连接失败/超时：返回 `502 Bad Gateway`，body 为 JSON `{"error": "..."}`；不打断服务进程。
- 连接超时 10s、读超时 300s（大文件下载容忍）。总超时不设上限。
- 上游凭据、代理凭据不写日志；日志仅含方法、路径、状态码、耗时。

### Docker / SaaS 部署

- `Dockerfile`：`python:3.12-slim` 基础镜像，仅装 `requirements.txt`（aiohttp），非 root 用户运行，`EXPOSE 8080`，`CMD ["python", "-m", "webdav_proxy"]`。
- `.dockerignore` 排除 `.venv/`、`.git/`、`.env`、测试等。
- 配置一律经环境变量注入（SaaS 平台标准方式），不把 `.env` 打进镜像。
- `requirements.txt` 固定 aiohttp 版本。

## [S3] Out of Scope

- HTTPS/TLS 终结（由 SaaS 平台网关或反向代理负责）。
- 多代理账号及按账号分级的读写权限（已决策为全局 `READ_ONLY` 开关）。
- 缓存、上传/下载限速、请求计费或配额。
- 对上游响应的任何内容改写（如重写 PROPFIND 中返回的 URL 字段）。
- WebDAV 客户端兼容性专项测试（仅覆盖常见方法与 GET 下载链路）。

## Tasks

- [x] T1: 配置模块与 `.env` 加载 — acceptance: 缺必填变量时启动报错并指明缺失项；`.env` 与真实环境变量优先级正确（covers: S2 配置契约）
- [x] T2: 代理核心（认证 + 全方法转发 + 流式双向 + 302 跟随 + Range 透传）— acceptance: 所有方法按转义规则转发，302 后内容直达客户端（covers: S2 认证/转发/302 跟随）
- [x] T3: 只读模式拦截 — acceptance: 写方法 403 且不触达上游，读方法正常（covers: S2 只读模式）
- [x] T4: 错误处理与日志 — acceptance: 上游不可达返回 502，凭据不落日志（covers: S2 错误行为）
- [x] T5: pytest 测试（假上游 + 端到端行为断言）— acceptance: 全部通过，覆盖认证/只读/302/流式/异常（depends: T1,T2,T3）
- [x] T6: Dockerfile + .dockerignore + requirements.txt + README + .env.example — acceptance: 文件齐备，无密钥入镜像（covers: S2 Docker / SaaS 部署；depends: T2）
