import sys

from aiohttp import web

from .config import ConfigError, load_config
from .proxy import create_app


def main():
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"webdav-proxy: {exc}", file=sys.stderr)
        sys.exit(1)
    web.run_app(create_app(config), host=config.host, port=config.port)


if __name__ == "__main__":
    main()
