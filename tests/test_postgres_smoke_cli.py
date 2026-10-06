"""Check the manual runner's consent and output boundary without a database."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "smoke_postgres.py"


def runner():
    spec = importlib.util.spec_from_file_location("postgres_smoke", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("args", "env", "message"),
    [
        ([], {}, "confirmation_required"),
        (["--confirm-disposable-database"], {"CI": "true"}, "manual_nonproduction_only"),
        (["--confirm-disposable-database"], {"APP_ENV": "production"}, "manual_nonproduction_only"),
        (["--confirm-disposable-database"], {}, "database_url_required"),
    ],
)
def test_consent_before_any_connection(monkeypatch, capsys, args, env, message):
    module = runner()
    for name in ("CI", "APP_ENV", "POSTGRES_SMOKE_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *args])

    def forbidden(*_args, **_kwargs):
        pytest.fail("subprocess started before consent/config validation")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    assert module.main() == 2
    assert capsys.readouterr().out == f"FAIL {message}\n"


@pytest.mark.parametrize("timeout", [False, True])
def test_failure_output_never_contains_child_diagnostics(monkeypatch, capsys, timeout):
    module = runner()
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("POSTGRES_SMOKE_DATABASE_URL", "postgresql+psycopg://user:secret@db/test")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--confirm-disposable-database"])

    def failed(*args, **kwargs):
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        if timeout:
            raise subprocess.TimeoutExpired(args, 120, output="secret", stderr="secret")
        return subprocess.CompletedProcess(args, 1, stdout="secret", stderr="secret")

    monkeypatch.setattr(module.subprocess, "run", failed)
    assert module.main() == 1
    captured = capsys.readouterr()
    assert captured.out == "FAIL preflight\n"
    assert captured.err == ""
