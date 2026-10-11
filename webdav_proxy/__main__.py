import asyncio
import sys

from aiohttp import web

from .config import ConfigError, load_config
from .proxy import create_app, loop_exception_handler


def main():
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"webdav-proxy: {exc}", file=sys.stderr)
        sys.exit(1)
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(loop_exception_handler)
    try:
        web.run_app(
            create_app(config), host=config.host, port=config.port, loop=loop
        )
    finally:
        loop.close()


if __name__ == "__main__":
    main()
