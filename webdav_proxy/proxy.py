import asyncio
import base64
import hmac
import logging
import re
import time
from urllib.parse import quote, unquote, urlsplit

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

_MULTISTATUS_METHODS = frozenset({"PROPFIND", "REPORT"})

_HREF_RE = re.compile(
    rb"<(?P<open>[A-Za-z0-9_.]*:?)href[^>]*>(?P<value>[^<]*)</(?P<close>[A-Za-z0-9_.]*:?)href\s*>",
    re.IGNORECASE,
)

_URL_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/]*)(/.*)$", re.DOTALL)

_CLIENT_ABORT_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

_CLIENT_ABORT_MESSAGE = "Cannot write to closing transport"


def _is_client_abort(exc):
    return isinstance(exc, _CLIENT_ABORT_ERRORS) or (
        isinstance(exc, RuntimeError) and _CLIENT_ABORT_MESSAGE in str(exc)
    )


def loop_exception_handler(loop, context):
    exc = context.get("exception")
    message = str(context.get("message", ""))
    if (
        _is_client_abort(exc)
        and ("connection_lost" in message or "shutdown" in message)
    ):
        logger.debug("client connection dropped: %s", exc)
        return
    loop.default_exception_handler(context)

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


def _strip_href_prefix(value, prefix):
    if not prefix:
        return value
    decoded = unquote(value)
    path_part, query_sep, query = decoded.partition("?")
    host = ""
    url_match = _URL_RE.match(path_part)
    if url_match:
        host = url_match.group(1)
        path = url_match.group(2)
    else:
        path = path_part
    if path == prefix:
        new_path = "/"
    elif path.startswith(prefix + "/"):
        new_path = path[len(prefix):]
    else:
        return value
    new_path = quote(new_path, safe="/@&=+$,;~*'()!-._")
    return host + new_path + (query_sep + query if query_sep else "")


def _rewrite_multistatus(body, prefix):
    out = bytearray()
    pos = 0
    for match in _HREF_RE.finditer(body):
        out += body[pos:match.start()]
        value_bytes = match.group("value")
        try:
            value = value_bytes.decode("utf-8")
        except UnicodeDecodeError:
            value = value_bytes.decode("latin-1")
        out += body[match.start():match.start("value")]
        out += _strip_href_prefix(value, prefix).encode("utf-8")
        out += body[match.end("value"):match.end()]
        pos = match.end()
    out += body[pos:]
    return bytes(out)


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

    target = (
        config.upstream_url.rstrip("/")
        + config.upstream_root_path
        + request.raw_path
    )

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

    if (
        request.method in _MULTISTATUS_METHODS
        and upstream.status == 207
        and config.href_strip_prefix
    ):
        raw_body = await upstream.content.read()
        upstream.release()
        body = _rewrite_multistatus(raw_body, config.href_strip_prefix)
        logger.info(
            "%s %s -> %s (href rewritten, %.3fs)",
            request.method,
            request.raw_path,
            upstream.status,
            time.perf_counter() - started,
        )
        return web.Response(status=207, body=body, headers=out_headers)

    response = web.StreamResponse(status=upstream.status, headers=out_headers)
    try:
        await response.prepare(request)
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
        await response.write_eof()
    except asyncio.CancelledError:
        response.force_close()
        raise
    except Exception as exc:
        response.force_close()
        if _is_client_abort(exc):
            logger.info(
                "%s %s -> client aborted transfer",
                request.method,
                request.raw_path,
            )
        else:
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
