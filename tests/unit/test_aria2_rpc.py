from pathlib import Path

from app.infrastructure.aria2_rpc import (
    DEFAULT_BT_TRACKERS,
    Aria2ConnectionError,
    Aria2DaemonConfig,
    Aria2RpcClient,
    Aria2RpcError,
)


def test_aria2_daemon_config_builds_rpc_url(tmp_path):
    config = Aria2DaemonConfig(
        aria2_bin="aria2c",
        download_dir=tmp_path,
        rpc_host="127.0.0.2",
        rpc_port=6801,
    )

    assert config.rpc_url == "http://127.0.0.2:6801/jsonrpc"


def test_aria2_rpc_client_uses_configured_secret(tmp_path):
    config = Aria2DaemonConfig(
        aria2_bin="aria2c",
        download_dir=Path(tmp_path),
        rpc_secret="test-secret",
    )

    client = Aria2RpcClient(config)

    assert client._secret == "test-secret"


def test_aria2_rpc_client_reuses_generated_secret(tmp_path):
    config = Aria2DaemonConfig(aria2_bin="aria2c", download_dir=tmp_path)

    first = Aria2RpcClient(config)
    second = Aria2RpcClient(config)

    assert first._secret == second._secret
    assert (tmp_path / ".aria2.rpc-secret").exists()


async def test_aria2_daemon_launches_on_fixed_ports_with_trackers(tmp_path, monkeypatch):
    import asyncio
    import shutil

    from app.infrastructure import aria2_rpc

    client = Aria2RpcClient(Aria2DaemonConfig(aria2_bin="aria2c", download_dir=tmp_path))
    launched = []
    ready = iter([False, True])

    async def fake_is_ready():
        return next(ready)

    class FakeProcess:
        pid = 1
        returncode = None
        stderr = None

    async def fake_exec(*args, **kwargs):
        launched.extend(args)
        return FakeProcess()

    monkeypatch.setattr(client, "is_ready", fake_is_ready)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/aria2c")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await client.ensure_started()

    assert "--listen-port=6881" in launched
    assert "--dht-listen-port=6881" in launched
    assert "--enable-dht6=true" in launched
    assert "--dht-listen-addr6=::" in launched
    assert DEFAULT_BT_TRACKERS[:2] == ("udp://open.stealth.si:80/announce", "udp://tracker.dler.org:80/announce")
    assert f"--bt-tracker={','.join(aria2_rpc.DEFAULT_BT_TRACKERS)}" in launched


async def test_unreachable_aria2_raises_connection_error(tmp_path):
    config = Aria2DaemonConfig(
        aria2_bin="aria2c", download_dir=tmp_path, rpc_port=1, rpc_secret="secret"
    )
    client = Aria2RpcClient(config)

    try:
        await client.call("aria2.getVersion")
    except Aria2ConnectionError:
        pass
    else:
        raise AssertionError("expected Aria2ConnectionError")
    assert issubclass(Aria2ConnectionError, Aria2RpcError)
