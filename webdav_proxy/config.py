import os
from dataclasses import dataclass
from urllib.parse import urlsplit

REQUIRED_KEYS = ("UPSTREAM_URL", "PROXY_USER", "PROXY_PASSWORD")

_TRUE_VALUES = frozenset({"true", "1", "yes"})
_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8080


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    upstream_url: str
    upstream_root_path: str
    upstream_user: str
    upstream_password: str
    proxy_user: str
    proxy_password: str
    read_only: bool
    host: str
    port: int
    href_strip_prefix: str = ""


def _parse_line(line):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    if "=" not in stripped:
        return None
    key, _, value = stripped.partition("=")
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if not key:
        return None
    return key, value


def load_dotenv_file(path):
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8-sig") as fh:
        for raw_line in fh:
            parsed = _parse_line(raw_line)
            if parsed is not None:
                values[parsed[0]] = parsed[1]
    return values


def _parse_bool(raw):
    return raw.strip().lower() in _TRUE_VALUES


def load_config(env=None, env_file=None):
    env = os.environ if env is None else env
    if env_file is None:
        env_file = os.path.join(os.getcwd(), ".env")
    dotenv = load_dotenv_file(env_file)

    def get(key):
        if key in env:
            return env[key]
        return dotenv.get(key)

    missing = [key for key in REQUIRED_KEYS if not get(key)]
    if missing:
        raise ConfigError(
            "Missing required environment variables: " + ", ".join(missing)
        )

    port_raw = get("PORT")
    try:
        port = int(port_raw) if port_raw else _DEFAULT_PORT
    except ValueError:
        raise ConfigError(f"Invalid PORT value: {port_raw!r}") from None

    root_path = (get("UPSTREAM_ROOT_PATH") or "").strip()
    if root_path and not root_path.startswith("/"):
        root_path = "/" + root_path
    root_path = root_path.rstrip("/")

    upstream_url = get("UPSTREAM_URL")
    href_strip_prefix = urlsplit(upstream_url).path.rstrip("/") + root_path

    return Config(
        upstream_url=upstream_url,
        upstream_root_path=root_path,
        upstream_user=get("UPSTREAM_USER") or "",
        upstream_password=get("UPSTREAM_PASSWORD") or "",
        proxy_user=get("PROXY_USER"),
        proxy_password=get("PROXY_PASSWORD"),
        read_only=_parse_bool(get("READ_ONLY") or ""),
        host=get("HOST") or _DEFAULT_HOST,
        port=port,
        href_strip_prefix=href_strip_prefix,
    )
