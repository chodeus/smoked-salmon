"""Tests for what happens to a torrent AFTER a successful upload.

Covers salmon.uploader.torrent_client (URL parsing, client login, torrent
injection) and salmon.uploader.seedbox (UploadManager task queueing/execution,
path resolution). All network-touching seams (qbittorrentapi, transmission_rpc,
DelugeRPCClient, xmlrpc.client.Server, rclone subprocess) are replaced with
fakes; no real connections are made.
"""

import base64
import subprocess
from types import SimpleNamespace

import pytest
import qbittorrentapi

from salmon import cfg
from salmon.config.validations import Seedbox
from salmon.uploader import seedbox as seedbox_module
from salmon.uploader import torrent_client as tc
from salmon.uploader.torrent_client import (
    DelugeClient,
    QBittorrentClient,
    RuTorrentClient,
    TorrentClient,
    TorrentClientGenerator,
    TransmissionClient,
)

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

ALL_CLIENT_CLASSES = (QBittorrentClient, TransmissionClient, DelugeClient, RuTorrentClient)


@pytest.fixture
def no_login(monkeypatch):
    """Replace every client class's login with a recorder so no network happens."""
    logins: list[str] = []

    def fake_login(self):
        logins.append(type(self).__name__)
        return f"session-{type(self).__name__}"

    for cls in ALL_CLIENT_CLASSES:
        monkeypatch.setattr(cls, "login", fake_login)
    return logins


@pytest.fixture
def click_messages(monkeypatch):
    """Capture click.secho output (torrent_client and seedbox share the module)."""
    messages: list[str] = []
    monkeypatch.setattr(tc.click, "secho", lambda message, **kwargs: messages.append(str(message)))
    return messages


def make_client(monkeypatch, cls, fake_session):
    """Instantiate a TorrentClient subclass with login stubbed to a fake session."""
    monkeypatch.setattr(cls, "login", lambda self: fake_session)
    return cls()


def make_seedbox(**overrides) -> Seedbox:
    defaults = dict(
        name="box",
        enabled=True,
        url="remote",
        type="rclone",
        directory="/seedbox/music",
        torrent_client="qbittorrent+http://u:p@127.0.0.1:8080",
        label="salmon",
        add_paused=False,
    )
    defaults.update(overrides)
    return Seedbox(**defaults)


class FakeInjectClient:
    """Stands in for a logged-in TorrentClient inside UploadManager's cache."""

    def __init__(self, url: str = "") -> None:
        self.url = url
        self.added: list[tuple] = []
        self.add_error: Exception | None = None
        self.add_result = True

    def add_to_downloader(self, remote_folder, torrent, is_paused, label):
        if self.add_error is not None:
            raise self.add_error
        self.added.append((remote_folder, torrent, is_paused, label))
        return self.add_result


@pytest.fixture
def fake_parse(monkeypatch):
    """Replace TorrentClientGenerator.parse_libtc_url with a recording factory."""
    state = SimpleNamespace(calls=[], created={}, fail_urls=set())

    def parse(url: str) -> FakeInjectClient:
        state.calls.append(url)
        if url in state.fail_urls:
            raise ConnectionError(f"cannot reach {url}")
        client = FakeInjectClient(url)
        state.created[url] = client
        return client

    monkeypatch.setattr(seedbox_module.TorrentClientGenerator, "parse_libtc_url", parse)
    return state


@pytest.fixture
def recorded_transfers(monkeypatch):
    """Replace the module-level transfer/injection coroutines with recorders."""
    state = SimpleNamespace(rclone=[], inject=[], rclone_error=None, inject_fail_urls=set())

    async def fake_rclone(seedbox, remote_folder, path):
        if state.rclone_error is not None and seedbox.name in state.rclone_error[0]:
            raise state.rclone_error[1]
        state.rclone.append((seedbox, remote_folder, path))

    async def fake_inject(client, shell_path, torrent_path, label, add_paused):
        state.inject.append((client, shell_path, torrent_path, label, add_paused))
        return getattr(client, "url", "") not in state.inject_fail_urls

    monkeypatch.setattr(seedbox_module, "_rclone_upload_folder", fake_rclone)
    monkeypatch.setattr(seedbox_module, "_add_to_downloader", fake_inject)
    return state


def make_manager(monkeypatch, boxes: list[Seedbox]) -> seedbox_module.UploadManager:
    monkeypatch.setattr(cfg, "seedbox", boxes)
    return seedbox_module.UploadManager()


# ---------------------------------------------------------------------------
# TorrentClientGenerator.parse_libtc_url
# ---------------------------------------------------------------------------


def test_parse_libtc_url_qbittorrent_http_with_credentials(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("qbittorrent+http://admin:hunter2@127.0.0.1:8080")

    assert isinstance(client, QBittorrentClient)
    assert client.username == "admin"
    assert client.password == "hunter2"
    assert client.url == "http://127.0.0.1:8080"
    assert client.scheme is None
    assert client.host is None
    assert client.port is None


def test_parse_libtc_url_qbittorrent_https_secure_url(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("qbittorrent+https://admin:pw@qbit.example:443")

    assert isinstance(client, QBittorrentClient)
    assert client.url == "https://qbit.example:443"


def test_parse_libtc_url_transmission_http_without_credentials(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("transmission+http://127.0.0.1:9091")

    assert isinstance(client, TransmissionClient)
    assert client.username is None
    assert client.password is None
    assert client.scheme == "http"
    assert client.host == "127.0.0.1"
    assert client.port == 9091
    assert client.url is None


def test_parse_libtc_url_transmission_https_with_credentials(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("transmission+https://tm:secret@seed.example:443")

    assert isinstance(client, TransmissionClient)
    assert client.scheme == "https"
    assert client.host == "seed.example"
    assert client.port == 443
    assert client.username == "tm"
    assert client.password == "secret"


def test_parse_libtc_url_transmission_without_port_leaves_port_none(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("transmission+http://transmission.example")

    assert client.host == "transmission.example"
    assert client.port is None


def test_parse_libtc_url_deluge_with_credentials(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("deluge://user:password@127.0.0.1:58664")

    assert isinstance(client, DelugeClient)
    assert client.username == "user"
    assert client.password == "password"
    assert client.host == "127.0.0.1"
    assert client.port == 58664
    # Pinned oddity: with no "+" in the scheme, the client name itself becomes
    # the scheme attribute. Harmless because DelugeClient never reads it.
    assert client.scheme == "deluge"


def test_parse_libtc_url_rutorrent_keeps_rpc_path_in_url(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("rutorrent+http://rutorrent.example:9380/plugins/rpc/rpc.php")

    assert isinstance(client, RuTorrentClient)
    assert client.url == "http://rutorrent.example:9380/plugins/rpc/rpc.php"
    assert client.username is None
    assert client.password is None


def test_parse_libtc_url_percent_encoded_password_is_unquoted(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("qbittorrent+http://admin:p%40ss%20w0rd%2F%3A@host:8080")

    assert client.username == "admin"
    assert client.password == "p@ss w0rd/:"


def test_parse_libtc_url_password_with_raw_colon_survives(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("deluge://user:pa:ss@host:58846")

    assert client.username == "user"
    assert client.password == "pa:ss"


def test_parse_libtc_url_unsupported_client_raises_keyerror(no_login) -> None:
    with pytest.raises(KeyError, match="foobar"):
        TorrentClientGenerator.parse_libtc_url("foobar+http://host:1234")


def test_parse_libtc_url_garbage_string_raises_keyerror(no_login) -> None:
    with pytest.raises(KeyError):
        TorrentClientGenerator.parse_libtc_url("not a url at all")


def test_parse_libtc_url_non_numeric_port_raises_valueerror(no_login) -> None:
    with pytest.raises(ValueError):
        TorrentClientGenerator.parse_libtc_url("transmission+http://host:notaport")


def test_parse_libtc_url_does_not_log_credentials(no_login, click_messages) -> None:
    TorrentClientGenerator.parse_libtc_url("qbittorrent+http://admin:hunter2@127.0.0.1:8080")

    joined = "\n".join(click_messages)
    assert "hunter2" not in joined
    assert "admin:" not in joined
    assert "****" in joined


def test_parse_libtc_url_constructor_logs_in_immediately(no_login) -> None:
    client = TorrentClientGenerator.parse_libtc_url("transmission+http://127.0.0.1:9091")

    # Login is a synchronous constructor side effect, not deferred to first use.
    assert no_login == ["TransmissionClient"]
    assert client.client == "session-TransmissionClient"


def test_base_torrent_client_constructor_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        TorrentClient()


# ---------------------------------------------------------------------------
# QBittorrentClient
# ---------------------------------------------------------------------------


def test_qbittorrent_login_success_passes_url_and_credentials(monkeypatch) -> None:
    instances = []

    class FakeQbt:
        def __init__(self, host, username, password):
            self.host = host
            self.username = username
            self.password = password
            self.logged_in = False
            instances.append(self)

        def auth_log_in(self):
            self.logged_in = True

    monkeypatch.setattr(tc.qbittorrentapi, "Client", FakeQbt)
    client = QBittorrentClient(username="u", password="p", url="http://h:8080")

    assert client.client is instances[0]
    assert client.client.logged_in is True
    assert client.client.host == "http://h:8080"
    assert client.client.username == "u"
    assert client.client.password == "p"


def test_qbittorrent_login_failed_credentials_returns_none(monkeypatch) -> None:
    class FakeQbt:
        def __init__(self, host, username, password):
            pass

        def auth_log_in(self):
            raise qbittorrentapi.LoginFailed("bad credentials")

    monkeypatch.setattr(tc.qbittorrentapi, "Client", FakeQbt)
    client = QBittorrentClient(username="u", password="wrong", url="http://h:8080")

    assert client.client is None


def test_qbittorrent_login_connection_error_returns_none(monkeypatch) -> None:
    class FakeQbt:
        def __init__(self, host, username, password):
            raise qbittorrentapi.APIConnectionError("no route to host")

    monkeypatch.setattr(tc.qbittorrentapi, "Client", FakeQbt)
    client = QBittorrentClient(url="http://h:1")

    assert client.client is None


def test_qbittorrent_login_unexpected_error_returns_none(monkeypatch) -> None:
    class FakeQbt:
        def __init__(self, host, username, password):
            raise RuntimeError("boom")

    monkeypatch.setattr(tc.qbittorrentapi, "Client", FakeQbt)
    # Like the other clients, unexpected login errors are caught so the
    # constructor never blows up; the client is simply left as None.
    client = QBittorrentClient(url="http://h:8080")

    assert client.client is None


def test_qbittorrent_add_to_downloader_passes_savepath_category_paused(monkeypatch) -> None:
    calls = []
    fake = SimpleNamespace(torrents_add=lambda **kwargs: calls.append(kwargs))
    client = make_client(monkeypatch, QBittorrentClient, fake)

    result = client.add_to_downloader("/save/here", b"torrent-bytes", is_paused=True, label="salmon")

    assert result is True
    assert calls == [
        {"torrent_files": b"torrent-bytes", "save_path": "/save/here", "is_paused": True, "category": "salmon"}
    ]


def test_qbittorrent_add_to_downloader_reports_client_error(monkeypatch, click_messages) -> None:
    def broken_add(**kwargs):
        raise qbittorrentapi.APIConnectionError("connection dropped")

    client = make_client(monkeypatch, QBittorrentClient, SimpleNamespace(torrents_add=broken_add))

    result = client.add_to_downloader("/save", b"t", is_paused=False, label="")

    assert result is False
    assert any("Failed to add torrent" in m for m in click_messages)


@pytest.mark.parametrize("cls", ALL_CLIENT_CLASSES)
def test_add_to_downloader_returns_false_when_login_failed(monkeypatch, cls) -> None:
    client = make_client(monkeypatch, cls, None)

    # A failed login leaves .client as None; injection does nothing and fails.
    assert client.add_to_downloader("/save", b"t", is_paused=False, label="x") is False


# ---------------------------------------------------------------------------
# TransmissionClient
# ---------------------------------------------------------------------------


def test_transmission_login_defaults_to_http_localhost_9091(monkeypatch) -> None:
    kwargs_seen = []

    class FakeTm:
        def __init__(self, **kwargs):
            kwargs_seen.append(kwargs)

    monkeypatch.setattr(tc.transmission_rpc, "Client", FakeTm)
    client = TransmissionClient()

    assert isinstance(client.client, FakeTm)
    assert kwargs_seen == [
        {"protocol": "http", "host": "localhost", "port": 9091, "username": None, "password": None, "timeout": 60}
    ]


def test_transmission_login_https_uses_explicit_host_and_port(monkeypatch) -> None:
    kwargs_seen = []

    class FakeTm:
        def __init__(self, **kwargs):
            kwargs_seen.append(kwargs)

    monkeypatch.setattr(tc.transmission_rpc, "Client", FakeTm)
    TransmissionClient(username="tm", password="pw", scheme="https", host="seed.example", port=443)

    assert kwargs_seen[0]["protocol"] == "https"
    assert kwargs_seen[0]["host"] == "seed.example"
    assert kwargs_seen[0]["port"] == 443
    assert kwargs_seen[0]["username"] == "tm"
    assert kwargs_seen[0]["password"] == "pw"


def test_transmission_login_error_returns_none(monkeypatch) -> None:
    class FakeTm:
        def __init__(self, **kwargs):
            raise ConnectionError("refused")

    monkeypatch.setattr(tc.transmission_rpc, "Client", FakeTm)
    client = TransmissionClient(host="h", port=9091)

    assert client.client is None


def test_transmission_add_to_downloader_wraps_label_in_list(monkeypatch) -> None:
    calls = []
    fake = SimpleNamespace(add_torrent=lambda **kwargs: calls.append(kwargs) or "torrent-obj")
    client = make_client(monkeypatch, TransmissionClient, fake)

    result = client.add_to_downloader("/dl", b"bytes", is_paused=True, label="salmon")

    assert result is True
    assert calls == [{"torrent": b"bytes", "download_dir": "/dl", "paused": True, "labels": ["salmon"]}]


def test_transmission_add_to_downloader_empty_label_sends_none(monkeypatch) -> None:
    calls = []
    fake = SimpleNamespace(add_torrent=lambda **kwargs: calls.append(kwargs))
    client = make_client(monkeypatch, TransmissionClient, fake)

    client.add_to_downloader("/dl", b"bytes", is_paused=False, label="")

    assert calls[0]["labels"] is None
    assert calls[0]["paused"] is False


def test_transmission_add_to_downloader_reports_error(monkeypatch, click_messages) -> None:
    def broken(**kwargs):
        raise RuntimeError("rpc down")

    client = make_client(monkeypatch, TransmissionClient, SimpleNamespace(add_torrent=broken))

    assert client.add_to_downloader("/dl", b"b", is_paused=False, label="x") is False
    assert any("Failed to add torrent" in m for m in click_messages)


# ---------------------------------------------------------------------------
# DelugeClient
# ---------------------------------------------------------------------------


class FakeDelugeRPC:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []
        self.add_result = "hash123"
        self.add_error: Exception | None = None
        self.label_errors: list[Exception] = []

    def call(self, method, *args):
        self.calls.append((method, args))
        if method == "core.add_torrent_file":
            if self.add_error is not None:
                raise self.add_error
            return self.add_result
        if method == "label.set_torrent" and self.label_errors:
            raise self.label_errors.pop(0)
        return None

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]


def test_deluge_login_success_when_connected(monkeypatch) -> None:
    kwargs_seen = []

    class FakeRPC:
        def __init__(self, **kwargs):
            kwargs_seen.append(kwargs)
            self.connected = False

        def connect(self):
            self.connected = True

    monkeypatch.setattr(tc, "DelugeRPCClient", FakeRPC)
    client = DelugeClient(username="u", password="p", host="10.0.0.2", port=58846)

    assert isinstance(client.client, FakeRPC)
    assert kwargs_seen == [{"host": "10.0.0.2", "port": 58846, "username": "u", "password": "p"}]


def test_deluge_login_defaults_localhost_58846(monkeypatch) -> None:
    kwargs_seen = []

    class FakeRPC:
        def __init__(self, **kwargs):
            kwargs_seen.append(kwargs)
            self.connected = False

        def connect(self):
            self.connected = True

    monkeypatch.setattr(tc, "DelugeRPCClient", FakeRPC)
    DelugeClient()

    assert kwargs_seen[0]["host"] == "localhost"
    assert kwargs_seen[0]["port"] == 58846


def test_deluge_login_not_connected_returns_none(monkeypatch) -> None:
    class FakeRPC:
        def __init__(self, **kwargs):
            self.connected = False

        def connect(self):
            pass

    monkeypatch.setattr(tc, "DelugeRPCClient", FakeRPC)
    assert DelugeClient(host="h", port=1).client is None


def test_deluge_login_connect_error_returns_none(monkeypatch) -> None:
    class FakeRPC:
        def __init__(self, **kwargs):
            pass

        def connect(self):
            raise TimeoutError("timeout")

    monkeypatch.setattr(tc, "DelugeRPCClient", FakeRPC)
    assert DelugeClient(host="h", port=1).client is None


def test_deluge_add_to_downloader_sends_base64_payload_and_options(monkeypatch) -> None:
    rpc = FakeDelugeRPC()
    client = make_client(monkeypatch, DelugeClient, rpc)

    result = client.add_to_downloader("/dl/music", b"raw-torrent", is_paused=True, label="")

    assert result is True
    method, args = rpc.calls[0]
    assert method == "core.add_torrent_file"
    assert args[0].endswith(".torrent")
    assert args[1] == base64.b64encode(b"raw-torrent")
    assert args[2] == {"download_location": "/dl/music", "add_paused": True}
    # No label given -> no label RPCs at all.
    assert rpc.methods() == ["core.add_torrent_file"]


def test_deluge_add_to_downloader_sets_label(monkeypatch) -> None:
    rpc = FakeDelugeRPC()
    client = make_client(monkeypatch, DelugeClient, rpc)

    client.add_to_downloader("/dl", b"t", is_paused=False, label="salmon")

    assert rpc.methods() == ["core.add_torrent_file", "label.set_torrent"]
    assert rpc.calls[1][1] == ("hash123", "salmon")


def test_deluge_add_creates_missing_label_and_retries(monkeypatch) -> None:
    rpc = FakeDelugeRPC()
    rpc.label_errors = [Exception("Unknown Label")]
    client = make_client(monkeypatch, DelugeClient, rpc)

    result = client.add_to_downloader("/dl", b"t", is_paused=False, label="new-label")

    assert result is True
    assert rpc.methods() == ["core.add_torrent_file", "label.set_torrent", "label.add", "label.set_torrent"]
    assert rpc.calls[2][1] == ("new-label",)


def test_deluge_add_other_label_error_does_not_create_label(monkeypatch, click_messages) -> None:
    rpc = FakeDelugeRPC()
    rpc.label_errors = [Exception("permission denied")]
    client = make_client(monkeypatch, DelugeClient, rpc)

    result = client.add_to_downloader("/dl", b"t", is_paused=False, label="salmon")

    # Torrent stays added; label failure is only logged.
    assert result is True
    assert rpc.methods() == ["core.add_torrent_file", "label.set_torrent"]
    assert any("Failed to set label" in m for m in click_messages)


def test_deluge_add_to_downloader_reports_add_error(monkeypatch, click_messages) -> None:
    rpc = FakeDelugeRPC()
    rpc.add_error = RuntimeError("daemon gone")
    client = make_client(monkeypatch, DelugeClient, rpc)

    assert client.add_to_downloader("/dl", b"t", is_paused=False, label="x") is False
    assert any("Failed to add torrent" in m for m in click_messages)


# ---------------------------------------------------------------------------
# RuTorrentClient
# ---------------------------------------------------------------------------


class FakeRuTorrentServer:
    def __init__(self):
        self.raw_verbose_calls: list[tuple] = []
        self.raw_start_verbose_calls: list[tuple] = []
        self.error: Exception | None = None
        outer = self

        class Load:
            def raw_verbose(self, *args):
                if outer.error is not None:
                    raise outer.error
                outer.raw_verbose_calls.append(args)

            def raw_start_verbose(self, *args):
                if outer.error is not None:
                    raise outer.error
                outer.raw_start_verbose_calls.append(args)

        self.load = Load()


def test_rutorrent_login_success_queries_version(monkeypatch) -> None:
    urls = []

    class FakeServer:
        def __init__(self, url):
            urls.append(url)
            self.system = SimpleNamespace(client_version=lambda: "0.9.8")

    monkeypatch.setattr(tc.xmlrpc.client, "Server", FakeServer)
    client = RuTorrentClient(url="http://rt.example/plugins/rpc/rpc.php")

    assert isinstance(client.client, FakeServer)
    assert urls == ["http://rt.example/plugins/rpc/rpc.php"]


def test_rutorrent_login_error_returns_none(monkeypatch) -> None:
    class FakeServer:
        def __init__(self, url):
            raise ConnectionError("http 502")

    monkeypatch.setattr(tc.xmlrpc.client, "Server", FakeServer)
    assert RuTorrentClient(url="http://rt.example").client is None


def test_rutorrent_add_started_sends_directory_and_label_commands(monkeypatch) -> None:
    server = FakeRuTorrentServer()
    client = make_client(monkeypatch, RuTorrentClient, server)

    result = client.add_to_downloader("/dl/music", b"raw", is_paused=False, label="salmon")

    assert result is True
    assert server.raw_verbose_calls == []
    (args,) = server.raw_start_verbose_calls
    assert args[0] == ""
    assert isinstance(args[1], tc.xmlrpc.client.Binary)
    assert args[1].data == b"raw"
    assert list(args[2:]) == ["print=d.hash=", "d.directory.set=/dl/music", "d.custom1.set=salmon"]


def test_rutorrent_add_paused_uses_raw_verbose(monkeypatch) -> None:
    server = FakeRuTorrentServer()
    client = make_client(monkeypatch, RuTorrentClient, server)

    client.add_to_downloader("/dl", b"raw", is_paused=True, label="")

    assert server.raw_start_verbose_calls == []
    (args,) = server.raw_verbose_calls
    # Empty label -> no d.custom1.set command.
    assert list(args[2:]) == ["print=d.hash=", "d.directory.set=/dl"]


def test_rutorrent_add_reports_error(monkeypatch, click_messages) -> None:
    server = FakeRuTorrentServer()
    server.error = RuntimeError("xmlrpc fault")
    client = make_client(monkeypatch, RuTorrentClient, server)

    assert client.add_to_downloader("/dl", b"raw", is_paused=False, label="x") is False
    assert any("Failed to add torrent" in m for m in click_messages)


# ---------------------------------------------------------------------------
# seedbox._resolve_shell_path
# ---------------------------------------------------------------------------


def test_resolve_shell_path_without_override_returns_remote_folder() -> None:
    assert seedbox_module._resolve_shell_path("/downloads/music", []) == "/downloads/music"
    assert seedbox_module._resolve_shell_path("/downloads/music", ["-P", "--checksum"]) == "/downloads/music"


def test_resolve_shell_path_plain_override_replaces_remote_folder() -> None:
    args = ["--sftp-path-override", "/mnt/storage"]
    assert seedbox_module._resolve_shell_path("/downloads/music", args) == "/mnt/storage"


def test_resolve_shell_path_equals_form_is_parsed() -> None:
    args = ["-P", "--sftp-path-override=/mnt/storage"]
    assert seedbox_module._resolve_shell_path("/downloads/music", args) == "/mnt/storage"


def test_resolve_shell_path_at_prefix_joins_remote_folder() -> None:
    args = ["--sftp-path-override", "@/mnt/merged"]
    assert seedbox_module._resolve_shell_path("/downloads/music", args) == "/mnt/merged/downloads/music"


# ---------------------------------------------------------------------------
# seedbox._add_to_downloader
# ---------------------------------------------------------------------------


async def test_add_to_downloader_reads_torrent_file_and_calls_client(tmp_path) -> None:
    torrent_path = tmp_path / "release.torrent"
    torrent_path.write_bytes(b"d8:announce3:urle")
    client = FakeInjectClient()

    result = await seedbox_module._add_to_downloader(client, "/shell/path", str(torrent_path), "salmon", True)

    assert result is True
    assert client.added == [("/shell/path", b"d8:announce3:urle", True, "salmon")]


async def test_add_to_downloader_reports_client_error(tmp_path, click_messages) -> None:
    torrent_path = tmp_path / "release.torrent"
    torrent_path.write_bytes(b"data")
    client = FakeInjectClient()
    client.add_error = RuntimeError("client exploded")

    result = await seedbox_module._add_to_downloader(client, "/shell", str(torrent_path), "", False)

    assert result is False
    assert any("Failed to add torrent to client: client exploded" in m for m in click_messages)


async def test_add_to_downloader_reports_failure_when_client_returns_false(tmp_path, click_messages) -> None:
    torrent_path = tmp_path / "release.torrent"
    torrent_path.write_bytes(b"data")
    client = FakeInjectClient()
    client.add_result = False

    result = await seedbox_module._add_to_downloader(client, "/shell", str(torrent_path), "", False)

    assert result is False
    assert any("FAILED to add torrent to client" in m for m in click_messages)
    assert not any("Torrent added to client successfully" in m for m in click_messages)


async def test_add_to_downloader_reports_failure_when_client_swallows_error(monkeypatch, tmp_path) -> None:
    # Real TorrentClient subclasses print their own error and return False;
    # the seedbox layer must report the failure instead of claiming success.
    torrent_path = tmp_path / "release.torrent"
    torrent_path.write_bytes(b"data")
    messages: list[str] = []
    monkeypatch.setattr(seedbox_module.click, "secho", lambda message, **kwargs: messages.append(str(message)))

    def broken(**kwargs):
        raise RuntimeError("rpc down")

    client = make_client(monkeypatch, TransmissionClient, SimpleNamespace(add_torrent=broken))
    result = await seedbox_module._add_to_downloader(client, "/shell", str(torrent_path), "", False)

    assert result is False
    assert any("Failed to add torrent" in m for m in messages)
    assert any("FAILED to add torrent to client" in m for m in messages)
    assert not any("Torrent added to client successfully" in m for m in messages)


# ---------------------------------------------------------------------------
# UploadManager.__init__
# ---------------------------------------------------------------------------


def test_upload_manager_init_without_seedboxes_is_empty(monkeypatch, fake_parse) -> None:
    manager = make_manager(monkeypatch, [])

    assert fake_parse.calls == []
    assert manager._client_cache == {}
    assert len(manager.tasks) == 0


def test_upload_manager_init_logs_in_once_per_unique_client_url(monkeypatch, fake_parse) -> None:
    url_a = "qbittorrent+http://u:p@a:8080"
    url_b = "transmission+http://b:9091"
    boxes = [
        make_seedbox(name="box1", torrent_client=url_a),
        make_seedbox(name="box2", torrent_client=url_b),
        make_seedbox(name="box3", torrent_client=url_a),  # shares box1's client
    ]

    manager = make_manager(monkeypatch, boxes)

    # Constructor side effect: every unique client URL is logged into
    # synchronously, exactly once, before any task exists.
    assert fake_parse.calls == [url_a, url_b]
    assert set(manager._client_cache) == {url_a, url_b}
    assert manager._client(boxes[0]) is manager._client(boxes[2])


def test_upload_manager_init_client_login_error_is_caught_and_client_skipped(monkeypatch, fake_parse) -> None:
    bad_url = "qbittorrent+http://u:p@down:8080"
    good_url = "transmission+http://up:9091"
    fake_parse.fail_urls.add(bad_url)
    boxes = [
        make_seedbox(name="broken", torrent_client=bad_url),
        make_seedbox(name="working", torrent_client=good_url),
    ]

    manager = make_manager(monkeypatch, boxes)  # must not raise

    assert set(manager._client_cache) == {good_url}


def test_upload_manager_init_skips_disabled_seedboxes(monkeypatch, fake_parse) -> None:
    # Disabled seedboxes never get their torrent client logged into.
    box = make_seedbox(name="off", enabled=False)

    manager = make_manager(monkeypatch, [box])

    assert fake_parse.calls == []
    assert manager._client_cache == {}


# ---------------------------------------------------------------------------
# UploadManager.add_upload_task
# ---------------------------------------------------------------------------


def test_add_upload_task_queues_folder_and_seed_for_seedbox(monkeypatch, fake_parse) -> None:
    box = make_seedbox()
    manager = make_manager(monkeypatch, [box])

    manager.add_upload_task("/music/Album", "folder", is_flac=True)
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    assert list(manager.tasks) == [
        (box, "/music/Album", "folder"),
        (box, "/torrents/Album.torrent", "seed"),
    ]


def test_add_upload_task_skips_seedbox_whose_client_failed_to_configure(monkeypatch, fake_parse) -> None:
    url = "qbittorrent+http://u:p@down:8080"
    fake_parse.fail_urls.add(url)
    manager = make_manager(monkeypatch, [make_seedbox(torrent_client=url)])

    manager.add_upload_task("/music/Album", "folder", is_flac=True)

    assert len(manager.tasks) == 0


def test_add_upload_task_skips_disabled_seedbox_even_with_shared_client(monkeypatch, fake_parse) -> None:
    # A disabled seedbox whose client URL is shared with an enabled one still
    # has a cached client; the enabled flag alone must exclude it.
    url = "qbittorrent+http://u:p@a:8080"
    on_box = make_seedbox(name="on", torrent_client=url)
    off_box = make_seedbox(name="off", enabled=False, torrent_client=url)
    manager = make_manager(monkeypatch, [on_box, off_box])

    manager.add_upload_task("/music/Album", "folder", is_flac=True)

    assert [box.name for box, _, _ in manager.tasks] == ["on"]


def test_add_upload_task_flac_only_seedbox_skipped_for_non_flac(monkeypatch, fake_parse) -> None:
    flac_box = make_seedbox(name="flaconly", flac_only=True)
    any_box = make_seedbox(name="anything", flac_only=False, torrent_client="transmission+http://b:9091")
    manager = make_manager(monkeypatch, [flac_box, any_box])

    manager.add_upload_task("/music/Album [MP3]", "seed", is_flac=False)

    assert [box.name for box, _, _ in manager.tasks] == ["anything"]


def test_add_upload_task_flac_only_seedbox_included_for_flac(monkeypatch, fake_parse) -> None:
    manager = make_manager(monkeypatch, [make_seedbox(flac_only=True)])

    manager.add_upload_task("/music/Album [FLAC]", "seed", is_flac=True)

    assert len(manager.tasks) == 1


def test_add_upload_task_deduplicates_identical_tasks(monkeypatch, fake_parse) -> None:
    manager = make_manager(monkeypatch, [make_seedbox()])

    manager.add_upload_task("/music/Album", "folder", is_flac=True)
    manager.add_upload_task("/music/Album", "folder", is_flac=True)

    assert len(manager.tasks) == 1


def test_add_upload_task_folder_tasks_are_prepended_before_seed_tasks(monkeypatch, fake_parse) -> None:
    manager = make_manager(monkeypatch, [make_seedbox()])

    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)
    manager.add_upload_task("/music/Album", "folder", is_flac=True)

    # Folder transfers must run before torrent injection, even if queued later.
    assert [task_type for _, _, task_type in manager.tasks] == ["folder", "seed"]


def test_add_upload_task_unknown_task_type_is_silently_dropped(monkeypatch, fake_parse) -> None:
    manager = make_manager(monkeypatch, [make_seedbox()])

    manager.add_upload_task("/music/Album", "sideload", is_flac=True)

    assert len(manager.tasks) == 0


# ---------------------------------------------------------------------------
# UploadManager.execute_upload
# ---------------------------------------------------------------------------


async def test_execute_upload_with_empty_task_list_is_noop(monkeypatch, fake_parse, recorded_transfers) -> None:
    messages: list[str] = []
    manager = make_manager(monkeypatch, [make_seedbox()])
    monkeypatch.setattr(seedbox_module.click, "secho", lambda message, **kwargs: messages.append(str(message)))

    await manager.execute_upload()

    assert recorded_transfers.rclone == []
    assert recorded_transfers.inject == []
    assert "No upload tasks to execute" in messages


async def test_execute_upload_rclone_folder_transfers_to_seedbox_directory(
    monkeypatch, fake_parse, recorded_transfers
) -> None:
    box = make_seedbox(type="rclone", directory="/remote/music")
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task("/local/Album", "folder", is_flac=True)

    await manager.execute_upload()

    assert recorded_transfers.rclone == [(box, "/remote/music", "/local/Album")]
    assert recorded_transfers.inject == []
    assert len(manager.tasks) == 0  # queue is cleared after a run


async def test_execute_upload_local_seedbox_folder_task_does_nothing(
    monkeypatch, fake_parse, recorded_transfers
) -> None:
    # For type="local" there is no transfer branch: folder tasks are no-ops.
    box = make_seedbox(type="local", directory="/local/music")
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task("/local/Album", "folder", is_flac=True)

    await manager.execute_upload()

    assert recorded_transfers.rclone == []
    assert recorded_transfers.inject == []


async def test_execute_upload_rclone_seed_injects_with_label_and_paused(
    monkeypatch, fake_parse, recorded_transfers
) -> None:
    box = make_seedbox(type="rclone", directory="/remote/music", label="salmon", add_paused=True)
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    await manager.execute_upload()

    client = fake_parse.created[box.torrent_client]
    assert recorded_transfers.inject == [(client, "/remote/music", "/torrents/Album.torrent", "salmon", True)]


async def test_execute_upload_rclone_seed_honors_sftp_path_override(
    monkeypatch, fake_parse, recorded_transfers
) -> None:
    box = make_seedbox(
        type="rclone",
        directory="/remote/music",
        extra_args=["-P", "--sftp-path-override", "@/mnt/tank"],
    )
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    await manager.execute_upload()

    assert recorded_transfers.inject[0][1] == "/mnt/tank/remote/music"


async def test_execute_upload_local_seed_uses_seedbox_directory(monkeypatch, fake_parse, recorded_transfers) -> None:
    box = make_seedbox(type="local", directory="/data/music")
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    await manager.execute_upload()

    assert recorded_transfers.inject[0][1] == "/data/music"


async def test_execute_upload_local_seed_falls_back_to_download_directory(
    monkeypatch, tmp_path, fake_parse, recorded_transfers
) -> None:
    monkeypatch.setattr(cfg.directory, "download_directory", str(tmp_path))
    box = make_seedbox(type="local", directory="")
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    await manager.execute_upload()

    assert recorded_transfers.inject[0][1] == str(tmp_path)


async def test_execute_upload_multiple_seedboxes_each_processed(monkeypatch, fake_parse, recorded_transfers) -> None:
    box1 = make_seedbox(name="box1", directory="/one", torrent_client="qbittorrent+http://u:p@a:8080")
    box2 = make_seedbox(name="box2", directory="/two", torrent_client="transmission+http://b:9091")
    manager = make_manager(monkeypatch, [box1, box2])
    manager.add_upload_task("/local/Album", "folder", is_flac=True)
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    await manager.execute_upload()

    assert [(box.name, folder) for box, folder, _ in recorded_transfers.rclone] == [("box2", "/two"), ("box1", "/one")]
    injected = {(client.url, shell) for client, shell, _, _, _ in recorded_transfers.inject}
    assert injected == {("qbittorrent+http://u:p@a:8080", "/one"), ("transmission+http://b:9091", "/two")}


async def test_execute_upload_one_failing_task_does_not_stop_the_rest(
    monkeypatch, fake_parse, recorded_transfers
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(seedbox_module.click, "secho", lambda message, **kwargs: messages.append(str(message)))
    box1 = make_seedbox(name="box1", torrent_client="qbittorrent+http://u:p@a:8080")
    box2 = make_seedbox(name="box2", torrent_client="transmission+http://b:9091")
    recorded_transfers.rclone_error = ({"box1"}, RuntimeError("rclone binary missing"))
    manager = make_manager(monkeypatch, [box1, box2])
    manager.add_upload_task("/local/Album", "folder", is_flac=True)

    await manager.execute_upload()  # must not raise

    assert [box.name for box, _, _ in recorded_transfers.rclone] == ["box2"]
    assert any("Critical error during task: rclone binary missing" in m for m in messages)
    assert len(manager.tasks) == 0  # queue cleared even after failures


async def test_execute_upload_disabled_seedbox_gets_no_transfers(monkeypatch, fake_parse, recorded_transfers) -> None:
    box = make_seedbox(name="off", enabled=False)
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task("/local/Album", "folder", is_flac=True)
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    await manager.execute_upload()

    assert recorded_transfers.rclone == []
    assert recorded_transfers.inject == []


async def test_execute_upload_seed_failure_is_surfaced_and_does_not_stop_the_rest(
    monkeypatch, fake_parse, recorded_transfers
) -> None:
    messages: list[str] = []
    monkeypatch.setattr(seedbox_module.click, "secho", lambda message, **kwargs: messages.append(str(message)))
    url_a = "qbittorrent+http://u:p@a:8080"
    url_b = "transmission+http://b:9091"
    box1 = make_seedbox(name="box1", torrent_client=url_a)
    box2 = make_seedbox(name="box2", torrent_client=url_b)
    recorded_transfers.inject_fail_urls.add(url_a)
    manager = make_manager(monkeypatch, [box1, box2])
    manager.add_upload_task("/torrents/Album.torrent", "seed", is_flac=True)

    await manager.execute_upload()  # must not raise

    # Both seedboxes were attempted; only box1's failure is reported.
    assert {client.url for client, _, _, _, _ in recorded_transfers.inject} == {url_a, url_b}
    assert "Seed task failed for seedbox: box1" in messages
    assert "Seed task failed for seedbox: box2" not in messages
    assert len(manager.tasks) == 0


async def test_execute_upload_end_to_end_seed_reads_torrent_and_injects(monkeypatch, tmp_path, fake_parse) -> None:
    # Integration through the real _add_to_downloader: the .torrent bytes on
    # disk must reach the client along with directory, label and pause state.
    torrent_path = tmp_path / "Album.torrent"
    torrent_path.write_bytes(b"d4:info0:e")
    box = make_seedbox(type="rclone", directory="/remote/music", label="salmon", add_paused=True)
    manager = make_manager(monkeypatch, [box])
    manager.add_upload_task(str(torrent_path), "seed", is_flac=True)

    await manager.execute_upload()

    client = fake_parse.created[box.torrent_client]
    assert client.added == [("/remote/music", b"d4:info0:e", True, "salmon")]


async def test_execute_upload_end_to_end_folder_runs_rclone_before_seed(monkeypatch, tmp_path, fake_parse) -> None:
    # Real _rclone_upload_folder with a stubbed subprocess: verifies ordering
    # (files land on the remote before injection) and the rclone command line.
    torrent_path = tmp_path / "Album.torrent"
    torrent_path.write_bytes(b"bytes")
    events: list[str] = []

    async def fake_run_process(commands, **kwargs):
        events.append(f"rclone:{' '.join(commands)}")
        return subprocess.CompletedProcess(commands, 0)

    monkeypatch.setattr(seedbox_module.anyio, "run_process", fake_run_process)
    box = make_seedbox(type="rclone", url="mybox", directory="/remote/music", extra_args=["-P"])
    manager = make_manager(monkeypatch, [box])
    client = fake_parse.created[box.torrent_client]
    client.add_to_downloader = lambda *args, **kwargs: events.append("inject") or True

    manager.add_upload_task(str(torrent_path), "seed", is_flac=True)
    manager.add_upload_task("/local/Album", "folder", is_flac=True)
    await manager.execute_upload()

    assert events == [
        "rclone:rclone copy /local/Album mybox:/remote/music/Album -P",
        "inject",
    ]
