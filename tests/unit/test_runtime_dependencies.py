import os

from app.services.runtime_dependencies import configure_deno_runtime, get_deno_version


def test_configure_deno_runtime_uses_configured_executable(tmp_path, monkeypatch):
    executable = tmp_path / "deno"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "")

    discovered = configure_deno_runtime(str(executable))

    assert discovered == executable.resolve()
    assert str(tmp_path) in os.environ["PATH"]


def test_get_deno_version_returns_none_for_missing_runtime(tmp_path):
    assert get_deno_version(tmp_path / "missing-deno") is None
