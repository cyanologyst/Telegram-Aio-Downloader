from pathlib import Path

from app.infrastructure.aria2_rpc import Aria2DaemonConfig, Aria2RpcClient


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
