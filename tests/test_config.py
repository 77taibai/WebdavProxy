import pytest

from webdav_proxy.__main__ import main
from webdav_proxy.config import ConfigError, load_config, load_dotenv_file


BASE_ENV = {
    "UPSTREAM_URL": "https://dav.example.com/dav/",
    "PROXY_USER": "alice",
    "PROXY_PASSWORD": "wonderland",
}

_NO_ENV_FILE = "no-such-.env-in-tests"


def load(env=None, env_file=_NO_ENV_FILE):
    return load_config(env=env, env_file=env_file)

DOTENV_TEXT = "\n".join(
    [
        "# webdav proxy configuration",
        "",
        'export UPSTREAM_URL="https://dav.example.com/dav/"',
        "PROXY_USER=bob",
        "PROXY_PASSWORD='p@ss word'",
        "READ_ONLY=yes",
        "PORT=9090",
        "HOST=127.0.0.1",
        "",
    ]
)


def make_env(**overrides):
    env = dict(BASE_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def write_env_file(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return str(path)


class TestRequiredVariables:
    def test_missing_all_lists_every_variable(self):
        with pytest.raises(ConfigError) as excinfo:
            load(env={})
        message = str(excinfo.value)
        assert "UPSTREAM_URL" in message
        assert "PROXY_USER" in message
        assert "PROXY_PASSWORD" in message

    def test_missing_upstream_url(self):
        with pytest.raises(ConfigError) as excinfo:
            load(env=make_env(UPSTREAM_URL=None))
        assert "UPSTREAM_URL" in str(excinfo.value)

    def test_missing_proxy_user(self):
        with pytest.raises(ConfigError) as excinfo:
            load(env=make_env(PROXY_USER=None))
        assert "PROXY_USER" in str(excinfo.value)

    def test_missing_proxy_password(self):
        with pytest.raises(ConfigError) as excinfo:
            load(env=make_env(PROXY_PASSWORD=None))
        assert "PROXY_PASSWORD" in str(excinfo.value)

    def test_empty_value_counts_as_missing(self):
        with pytest.raises(ConfigError):
            load(env=make_env(PROXY_PASSWORD=""))


class TestDefaultsAndOverrides:
    def test_defaults(self):
        config = load(env=make_env())
        assert config.upstream_url == "https://dav.example.com/dav/"
        assert config.upstream_user == ""
        assert config.upstream_password == ""
        assert config.proxy_user == "alice"
        assert config.proxy_password == "wonderland"
        assert config.read_only is False
        assert config.host == "0.0.0.0"
        assert config.port == 8080

    @pytest.mark.parametrize("raw", ["true", "TRUE", "True", "1", "yes", "YES", "Yes"])
    def test_read_only_truthy(self, raw):
        assert load(env=make_env(READ_ONLY=raw)).read_only is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off", "", "random"])
    def test_read_only_falsy(self, raw):
        assert load(env=make_env(READ_ONLY=raw)).read_only is False

    def test_invalid_port_raises(self):
        with pytest.raises(ConfigError):
            load(env=make_env(PORT="not-a-number"))


class TestRootPath:
    def test_default_is_empty(self):
        config = load(env=make_env())
        assert config.upstream_root_path == ""
        assert config.href_strip_prefix == "/dav"

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("test", "/test"),
            ("/test", "/test"),
            ("/test/", "/test"),
            ("a/b/", "/a/b"),
            (" ", ""),
        ],
    )
    def test_normalization(self, raw, expected):
        assert load(env=make_env(UPSTREAM_ROOT_PATH=raw)).upstream_root_path == expected

    def test_href_strip_prefix_combines_url_path_and_root(self):
        config = load(env=make_env(UPSTREAM_ROOT_PATH="/test/"))
        assert config.href_strip_prefix == "/dav/test"


class TestDotenv:
    def test_parses_export_quotes_and_comments(self, tmp_path):
        env_file = write_env_file(tmp_path, DOTENV_TEXT)
        config = load(env={}, env_file=env_file)
        assert config.upstream_url == "https://dav.example.com/dav/"
        assert config.proxy_user == "bob"
        assert config.proxy_password == "p@ss word"
        assert config.read_only is True
        assert config.port == 9090
        assert config.host == "127.0.0.1"

    def test_inline_comments_are_part_of_value(self, tmp_path):
        env_file = write_env_file(tmp_path, "TOKEN=a b # keep me\n")
        assert load_dotenv_file(env_file)["TOKEN"] == "a b # keep me"

    def test_blank_and_malformed_lines_are_skipped(self, tmp_path):
        env_file = write_env_file(
            tmp_path, "\n   \n# only a comment\nNO_EQUALS_HERE\nOK=1\n"
        )
        assert load_dotenv_file(env_file) == {"OK": "1"}

    def test_env_file_missing_is_ok(self, tmp_path):
        config = load(env=make_env(), env_file=str(tmp_path / "absent.env"))
        assert config.port == 8080

    def test_real_environment_wins_over_dotenv(self, tmp_path):
        env_file = write_env_file(tmp_path, DOTENV_TEXT)
        config = load_config(
            env={"PROXY_USER": "carol", "PORT": "7777"}, env_file=env_file
        )
        assert config.proxy_user == "carol"
        assert config.port == 7777
        assert config.proxy_password == "p@ss word"


class TestMainEntry:
    def test_missing_config_exits_nonzero_with_stderr(self, monkeypatch, tmp_path, capsys):
        for key in ("UPSTREAM_URL", "PROXY_USER", "PROXY_PASSWORD"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1
        assert "UPSTREAM_URL" in capsys.readouterr().err
