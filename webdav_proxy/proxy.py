import asyncio
import base64
import hmac
import logging
import time

from aiohttp import (
    ClientError,
    ClientSession,
    ClientTimeout,
    DummyCookieJar,
    web,
)
from multidict import CIMultiDict

from .config import Config

logger = logging.getLogger(__name__)

_REALM = 'Basic realm="webdav-proxy"'

WRITE_METHODS = frozenset(
    {"PUT", "DELETE", "MKCOL", "COPY", "MOVE", "PROPPATCH", "LOCK", "UNLOCK", "POST"}
)

_STRIP_REQUEST_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "authorization",
        "accept-encoding",
    }
)

_STRIP_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "content-length",
        "content-encoding",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "upgrade",
    }
)

_CLIENT_TIMEOUT = ClientTimeout(connect=10, sock_read=300)

_CONFIG_KEY = web.AppKey("config", Config)
_SESSION_KEY = web.AppKey("upstream_session", ClientSession)


def _basic_token(user, password):
    raw = f"{user}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _is_authenticated(config, request):
    header = request.headers.get("Authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded.strip():
        return False
    try:
        decoded = base64.b64decode(encoded.strip()).decode("utf-8")
    except ValueError:
        return False
    user, sep, password = decoded.partition(":")
    if not sep:
        return False
    user_ok = hmac.compare_digest(
        user.encode("utf-8"), config.proxy_user.encode("utf-8")
    )
    pass_ok = hmac.compare_digest(
        password.encode("utf-8"), config.proxy_password.encode("utf-8")
    )
    return user_ok and pass_ok


@web.middleware
async def auth_middleware(request, handler):
    if request.method == "GET" and request.path == "/healthz":
        return await handler(request)
    if not _is_authenticated(request.app[_CONFIG_KEY], request):
        return web.Response(status=401, headers={"WWW-Authenticate": _REALM})
    return await handler(request)


async def healthz(request):
    return web.Response(text="ok")


async def _forward(request):
    config = request.app[_CONFIG_KEY]
    started = time.perf_counter()

    if config.read_only and request.method in WRITE_METHODS:
        return web.json_response({"error": "read-only mode"}, status=403)

    target = config.upstream_url.rstrip("/") + request.raw_path

    upstream_headers = CIMultiDict()
    for name, value in request.headers.items():
        if name.lower() not in _STRIP_REQUEST_HEADERS:
            upstream_headers.add(name, value)
    if config.upstream_user:
        upstream_headers["Authorization"] = _basic_token(
            config.upstream_user, config.upstream_password
        )

    session = request.app[_SESSION_KEY]
    try:
        upstream = await session.request(
            request.method,
            target,
            headers=upstream_headers,
            data=request.content if request.can_read_body else None,
            allow_redirects=True,
        )
    except (ClientError, asyncio.TimeoutError, ValueError) as exc:
        message = str(exc) or type(exc).__name__
        logger.warning("%s %s -> 502 (%s)", request.method, request.raw_path, message)
        return web.json_response({"error": message}, status=502)

    out_headers = CIMultiDict()
    for name, value in upstream.headers.items():
        if name.lower() not in _STRIP_RESPONSE_HEADERS:
            out_headers.add(name, value)

    response = web.StreamResponse(status=upstream.status, headers=out_headers)
    try:
        await response.prepare(request)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
    except Exception as exc:
        response.force_close()
        logger.warning(
            "%s %s -> stream aborted (%s)",
            request.method,
            request.raw_path,
            str(exc) or type(exc).__name__,
        )
    finally:
        upstream.close()

    logger.info(
        "%s %s -> %s (%.3fs)",
        request.method,
        request.raw_path,
        upstream.status,
        time.perf_counter() - started,
    )
    return response


async def _make_client_session(app):
    app[_SESSION_KEY] = ClientSession(
        timeout=_CLIENT_TIMEOUT, cookie_jar=DummyCookieJar()
    )
    yield
    await app[_SESSION_KEY].close()


def create_app(config):
    app = web.Application(middlewares=[auth_middleware])
    app[_CONFIG_KEY] = config
    app.cleanup_ctx.append(_make_client_session)
    app.router.add_get("/healthz", healthz)
    app.router.add_route("*", "/{path_info:.*}", _forward)
    return app
