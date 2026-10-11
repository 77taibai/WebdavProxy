import asyncio
import functools
import socket
from urllib.parse import urlsplit

from aiohttp import encode_basic_auth, web
from aiohttp.test_utils import TestClient, TestServer

from webdav_proxy.config import Config
from webdav_proxy.proxy import (
    WRITE_METHODS,
    _rewrite_multistatus,
    _strip_href_prefix,
    create_app,
)

PROXY_USER = "client"
PROXY_PASSWORD = "client-pass"
UPSTREAM_USER = "upstream"
UPSTREAM_PASSWORD = "upstream-pass"

FULL_CONTENT = b"0123456789abcdef" * 96
RANGE_CONTENT = FULL_CONTENT[100:200]

PROPFIND_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:multistatus xmlns:D="DAV:"><D:response><D:href>/</D:href>'
    "</D:response></D:multistatus>"
).encode()

PROPFIND_ROOT_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:multistatus xmlns:D="DAV:">'
    '<D:response><D:href>/test/</D:href></D:response>'
    '<D:response><D:href>/test/%E7%94%B5%E5%BD%B1.mkv</D:href></D:response>'
    '<D:response><D:href>/other/book.pdf</D:href></D:response>'
    "</D:multistatus>"
).encode()

CLIENT_AUTH = encode_basic_auth(PROXY_USER, PROXY_PASSWORD)


def basic_header(user, password):
    return encode_basic_auth(user, password)


def aio_test(func):
    @functools.wraps(func)
    def wrapper():
        asyncio.run(func())

    return wrapper


def make_config(read_only=False, upstream_url="http://upstream.invalid/dav/", root_path=""):
    return Config(
        upstream_url=upstream_url,
        upstream_root_path=root_path,
        upstream_user=UPSTREAM_USER,
        upstream_password=UPSTREAM_PASSWORD,
        proxy_user=PROXY_USER,
        proxy_password=PROXY_PASSWORD,
        read_only=read_only,
        host="127.0.0.1",
        port=8080,
        href_strip_prefix=urlsplit(upstream_url).path.rstrip("/") + root_path,
    )


def make_upstream(state):
    @web.middleware
    async def guard(request, handler):
        expected = basic_header(UPSTREAM_USER, UPSTREAM_PASSWORD)
        if request.headers.get("Authorization") != expected:
            state["auth_failures"] += 1
            return web.json_response({"error": "upstream auth"}, status=401)
        return await handler(request)

    async def propfind(request):
        state["propfind"] += 1
        return web.Response(
            status=207,
            body=PROPFIND_XML,
            headers={"Content-Type": "application/xml"},
        )

    async def big(request):
        state["big"] += 1
        state["client_hint"] = request.headers.get("X-Client-Hint", "")
        raise web.HTTPFound(state.get("redirect_target") or "/files/big.txt")

    async def big_file(request):
        state["range_seen"] = request.headers.get("Range", "")
        if state["range_seen"]:
            return web.Response(
                status=206, body=RANGE_CONTENT, headers={"Content-Type": "text/plain"}
            )
        return web.Response(
            status=200,
            body=FULL_CONTENT,
            headers={"Content-Type": "text/plain", "X-Webdav-Echo": "yes"},
        )

    async def write_handler(request):
        state["writes"].append(request.method)
        state["last_body"] = await request.read()
        return web.Response(status=201, text="created")

    async def any_method(request):
        state["methods"].append(request.method)
        state["last_qs"] = request.rel_url.query_string
        return web.Response(status=204)

    async def prefixed(request):
        state["last_path"] = request.path
        state["last_qs"] = request.rel_url.query_string
        if request.method == "PROPFIND":
            state["propfind_root"] += 1
            return web.Response(
                status=207, body=PROPFIND_ROOT_XML, headers={"Content-Type": "application/xml"}
            )
        return web.Response(text="prefixed")

    app = web.Application(middlewares=[guard])
    app.router.add_route("PROPFIND", "/", propfind)
    app.router.add_route("GET", "/big", big)
    app.router.add_route("GET", "/files/big.txt", big_file)
    app.router.add_route("*", "/write", write_handler)
    app.router.add_route("*", "/forward", any_method)
    app.router.add_route("*", "/test{tail:.*}", prefixed)
    return app


async def start_stack(read_only=False, upstream_url=None, upstream_root_path=""):
    state = {
        "writes": [],
        "methods": [],
        "propfind": 0,
        "big": 0,
        "range_seen": "",
        "client_hint": "",
        "auth_failures": 0,
        "last_body": None,
        "last_qs": "",
        "last_path": "",
        "propfind_root": 0,
        "auth_on_cross_host": "unset",
    }
    upstream_server = TestServer(make_upstream(state))
    await upstream_server.start_server()
    if upstream_url is None:
        upstream_url = str(upstream_server.make_url(""))
    proxy_server = TestServer(
        create_app(
            make_config(
                read_only=read_only,
                upstream_url=upstream_url,
                root_path=upstream_root_path,
            )
        )
    )
    await proxy_server.start_server()
    return TestClient(proxy_server), upstream_server, state


async def stop_stack(client, upstream_server):
    await client.close()
    await upstream_server.close()


@aio_test
async def test_healthz_without_auth():
    client, upstream, state = await start_stack()
    try:
        resp = await client.get("/healthz")
        assert resp.status == 200
        assert await resp.text() == "ok"
        assert state["propfind"] == 0
        assert state["auth_failures"] == 0
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_missing_credentials_returns_401():
    client, upstream, state = await start_stack()
    try:
        resp = await client.get("/dav/")
        assert resp.status == 401
        assert resp.headers["WWW-Authenticate"] == 'Basic realm="webdav-proxy"'
        assert state["propfind"] == 0
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_wrong_credentials_returns_401():
    client, upstream, state = await start_stack()
    try:
        resp = await client.get(
            "/dav/", headers={"Authorization": basic_header(PROXY_USER, "wrong-pass")}
        )
        assert resp.status == 401
        resp = await client.get(
            "/dav/", headers={"Authorization": basic_header("nobody", PROXY_PASSWORD)}
        )
        assert resp.status == 401
        assert state["propfind"] == 0
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_propfind_forwards_with_injected_upstream_auth():
    client, upstream, state = await start_stack()
    try:
        resp = await client.request(
            "PROPFIND", "/", headers={"Authorization": CLIENT_AUTH}
        )
        assert resp.status == 207
        assert await resp.read() == PROPFIND_XML
        assert resp.headers["Content-Type"].startswith("application/xml")
        assert state["propfind"] == 1
        assert state["auth_failures"] == 0
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_get_follows_302_and_returns_final_body():
    client, upstream, state = await start_stack()
    try:
        resp = await client.get("/big", headers={"Authorization": CLIENT_AUTH})
        assert resp.status == 200
        assert await resp.read() == FULL_CONTENT
        assert resp.headers["X-Webdav-Echo"] == "yes"
        assert state["big"] == 1
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_range_header_passes_through_redirect():
    client, upstream, state = await start_stack()
    try:
        resp = await client.get(
            "/big",
            headers={
                "Authorization": CLIENT_AUTH,
                "Range": "bytes=100-199",
                "X-Client-Hint": "42",
            },
        )
        assert resp.status == 206
        assert await resp.read() == RANGE_CONTENT
        assert state["range_seen"] == "bytes=100-199"
        assert state["client_hint"] == "42"
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_write_methods_forwarded_when_not_read_only():
    client, upstream, state = await start_stack()
    try:
        body = b"upload-payload" * 4096
        resp = await client.put(
            "/write", data=body, headers={"Authorization": CLIENT_AUTH}
        )
        assert resp.status == 201
        assert await resp.text() == "created"
        assert state["writes"] == ["PUT"]
        assert state["last_body"] == body
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_all_common_methods_forwarded():
    client, upstream, state = await start_stack()
    methods = [
        "OPTIONS",
        "GET",
        "HEAD",
        "PROPFIND",
        "REPORT",
        "PROPPATCH",
        "PUT",
        "DELETE",
        "MKCOL",
        "COPY",
        "MOVE",
        "LOCK",
        "UNLOCK",
        "POST",
    ]
    try:
        for method in methods:
            resp = await client.request(
                method, "/forward", headers={"Authorization": CLIENT_AUTH}
            )
            assert resp.status == 204, method
        assert state["methods"] == methods
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_read_only_blocks_writes_but_allows_reads():
    client, upstream, state = await start_stack(read_only=True)
    try:
        for method in WRITE_METHODS:
            resp = await client.request(
                method, "/write", headers={"Authorization": CLIENT_AUTH}
            )
            assert resp.status == 403, method
            assert await resp.json() == {"error": "read-only mode"}
        assert state["writes"] == []
        assert state["methods"] == []

        resp = await client.get("/big", headers={"Authorization": CLIENT_AUTH})
        assert resp.status == 200
        assert await resp.read() == FULL_CONTENT
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_cross_host_302_does_not_leak_upstream_credentials():
    other_app = web.Application()

    async def target(request):
        other_state["auth_seen"] = request.headers.get("Authorization", "<none>")
        return web.Response(body=FULL_CONTENT, headers={"Content-Type": "text/plain"})

    other_state = {"auth_seen": "unset"}
    other_app.router.add_get("/x.txt", target)
    other_server = TestServer(other_app)
    await other_server.start_server()

    client, upstream, state = await start_stack()
    try:
        state["redirect_target"] = str(other_server.make_url("/x.txt"))
        resp = await client.get("/big", headers={"Authorization": CLIENT_AUTH})
        assert resp.status == 200
        assert await resp.read() == FULL_CONTENT
        assert other_state["auth_seen"] == "<none>"
    finally:
        await stop_stack(client, upstream)
        await other_server.close()


@aio_test
async def test_query_string_forwarded():
    client, upstream, state = await start_stack()
    try:
        resp = await client.get(
            "/forward", params={"a": "1", "b": "two"}, headers={"Authorization": CLIENT_AUTH}
        )
        assert resp.status == 204
        assert state["last_qs"] == "a=1&b=two"
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_root_path_prefix_maps_client_paths():
    client, upstream, state = await start_stack(upstream_root_path="/test")
    try:
        resp = await client.get("/", headers={"Authorization": CLIENT_AUTH})
        assert resp.status == 200
        assert await resp.text() == "prefixed"
        assert state["last_path"] == "/test/"

        resp = await client.get("/abc", headers={"Authorization": CLIENT_AUTH})
        assert resp.status == 200
        assert state["last_path"] == "/test/abc"
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_root_path_prefix_preserves_query_string():
    client, upstream, state = await start_stack(upstream_root_path="/test")
    try:
        resp = await client.get(
            "/abc", params={"a": "1"}, headers={"Authorization": CLIENT_AUTH}
        )
        assert resp.status == 200
        assert state["last_path"] == "/test/abc"
        assert state["last_qs"] == "a=1"
    finally:
        await stop_stack(client, upstream)


@aio_test
async def test_root_path_rewrites_propfind_href_prefixes():
    client, upstream, state = await start_stack(upstream_root_path="/test")
    try:
        resp = await client.request(
            "PROPFIND", "/", headers={"Authorization": CLIENT_AUTH}
        )
        assert resp.status == 207
        assert state["last_path"] == "/test/"
        body = (await resp.read()).decode("utf-8")
        assert "<D:href>/</D:href>" in body
        assert "%E7%94%B5%E5%BD%B1.mkv" in body
        assert "<D:href>/other/book.pdf</D:href>" in body
    finally:
        await stop_stack(client, upstream)


def test_strip_href_prefix_variants():
    assert _strip_href_prefix("/test/", "/test") == "/"
    assert _strip_href_prefix("/test", "/test") == "/"
    assert (
        _strip_href_prefix("/test/%E7%94%B5%E5%BD%B1.mkv", "/test")
        == "/%E7%94%B5%E5%BD%B1.mkv"
    )
    assert _strip_href_prefix("/test/电影.mkv", "/test") == "/%E7%94%B5%E5%BD%B1.mkv"
    assert (
        _strip_href_prefix("http://h/dav/test/a.txt", "/dav/test") == "http://h/a.txt"
    )
    assert (
        _strip_href_prefix("http://h/dav/test/a.txt?op=1", "/dav/test")
        == "http://h/a.txt?op=1"
    )
    assert _strip_href_prefix("/other/file", "/test") == "/other/file"
    assert _strip_href_prefix("/testx/file", "/test") == "/testx/file"
    assert _strip_href_prefix("/keep/x", "") == "/keep/x"


def test_rewrite_multistatus_keeps_other_tags():
    xml = (
        '<D:multistatus><D:response><D:href>/test/a.txt</D:href>'
        "<D:displayname>a.txt</D:displayname></D:response></D:multistatus>"
    ).encode("utf-8")
    out = _rewrite_multistatus(xml, "/test").decode("utf-8")
    assert "<D:href>/a.txt</D:href>" in out
    assert "<D:displayname>a.txt</D:displayname>" in out


@aio_test
async def test_upstream_unreachable_returns_502():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
    dead_url = f"http://127.0.0.1:{dead_port}/dav/"

    client, upstream, state = await start_stack(upstream_url=dead_url)
    try:
        resp = await client.get("/file.txt", headers={"Authorization": CLIENT_AUTH})
        assert resp.status == 502
        payload = await resp.json()
        assert "error" in payload
    finally:
        await stop_stack(client, upstream)
