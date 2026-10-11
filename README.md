# WebDAV 代理

一个轻量的 WebDAV 中间代理：客户端把请求发给代理，代理转发给上游 WebDAV 服务器，本身跟随 302 重定向把最终内容流式返回（解决上游 302 跳转直链导致客户端认证丢失的问题），并支持可选的只读模式，以 Docker 镜像形态交付，便于 SaaS 平台部署。

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `UPSTREAM_URL` | 是 | — | 上游 WebDAV 基地址，如 `https://dav.example.com/dav/` |
| `UPSTREAM_ROOT_PATH` | 否 | 空 | 上游根路径前缀：代理把它拼在请求路径前。设为 `/test` 时客户端访问 `/` 命中上游 `/test/`、访问 `/abc` 命中上游 `/test/abc`；留空不做前缀映射 || `UPSTREAM_USER` | 否 | 空 | 上游 Basic 认证用户名 |
| `UPSTREAM_PASSWORD` | 否 | 空 | 上游 Basic 认证密码 |
| `PROXY_USER` | 是 | — | 代理对客户端要求的 Basic 用户名 |
| `PROXY_PASSWORD` | 是 | — | 代理对客户端要求的 Basic 密码 |
| `READ_ONLY` | 否 | `false` | 设为 `true`/`1`/`yes`（不区分大小写）时启用只读模式 |
| `PORT` | 否 | `8080` | 监听端口 |
| `HOST` | 否 | `0.0.0.0` | 监听地址；容器内保持默认 `0.0.0.0`，本地运行可设为 `127.0.0.1` |

缺少任一必填变量（`UPSTREAM_URL`、`PROXY_USER`、`PROXY_PASSWORD`）时启动失败并退出，打印缺失的变量名。

根路径前缀说明：设置 `UPSTREAM_ROOT_PATH` 后，代理会同步重写 `PROPFIND`/`REPORT` 的 207 响应中的 `<D:href>` 链接（剥去「上游路径 + 前缀」整段，还原为客户端视角的相对路径），保证客户端目录视图与代理路径一致；`GET`/`PUT` 等其他响应内容不做任何改写。

## 本地运行

```powershell
python -m webdav_proxy
```

本地运行时建议设置 `$env:HOST = "127.0.0.1"`（默认 `0.0.0.0` 是容器约定）。也可在项目目录放置 `.env` 文件提供配置，真实环境变量优先于 `.env`。

## Docker 构建与运行

```powershell
# 构建
docker build -t webdav-proxy .

# 运行（凭据通过 --env-file 注入，不会打进镜像）
docker run --rm -p 8080:8080 --env-file .env webdav-proxy
```

容器以非 root 用户运行；`.dockerignore` 已排除 `.env`，运行时配置全部来自平台注入的环境变量。容器内默认 `HOST=0.0.0.0`、`PORT=8080`，与 `EXPOSE 8080` 及上面的 `-p 8080:8080` 对应；SaaS 平台通常会注入自己的 `PORT`，镜像会自动遵循。

## 健康检查

`GET /healthz` 免认证，返回 `200` 与文本 `ok`，供 SaaS 平台 / 负载均衡器健康检查使用：

```powershell
Invoke-WebRequest http://127.0.0.1:8080/healthz
```

## 只读模式

设置 `READ_ONLY=true` 后，以下写方法会被拦截并返回 `403 Forbidden`（带 JSON 错误说明），请求不会转发到上游：

`PUT`、`DELETE`、`MKCOL`、`COPY`、`MOVE`、`PROPPATCH`、`LOCK`、`UNLOCK`、`POST`

读方法（`GET`、`HEAD`、`OPTIONS`、`PROPFIND`、`REPORT`）与其他未列出的方法正常转发。

## 注意事项

- **凭据隔离**：代理自身对客户端要求的 Basic 认证（`PROXY_USER`/`PROXY_PASSWORD`）与上游凭据（`UPSTREAM_USER`/`UPSTREAM_PASSWORD`）完全独立；客户端的 `Authorization` 头永不透传给上游。
- **TLS**：代理仅提供 HTTP，HTTPS/TLS 终结由 SaaS 平台网关或反向代理负责。
- **已知限制**：上游对带请求体的写方法（如 PUT）返回重定向时，重定向无法携带已消费的流式上传体，代理将返回 `502` 并给出明确错误信息；下载链路（GET/HEAD）的 302 跟随不受影响。
- **日志**：仅记录方法、路径、状态码、耗时；任何凭据均不落日志。
