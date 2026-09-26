import argparse
import io
import sys
from pathlib import Path

import pytest

from tools import ui
from tools.runner import spec, vault

CONFIG = """
trading:
  connector:
    value: "{connector}"
  pairs:
    value: ["BTC-USDT"]
  leverage:
    value: 1
"""


@pytest.fixture
def strategy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def make(connector: str = "binance_perpetual") -> Path:
        monkeypatch.setattr(spec, "ROOT", tmp_path)
        directory = tmp_path / "strategies" / "trend" / "demo"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "config.yaml").write_text(CONFIG.format(connector=connector))
        (directory / "run.yaml").write_text("credential_id: binance-demo\n")
        return directory

    return make


@pytest.fixture
def sealed(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []
    monkeypatch.setattr(vault, "seal", lambda *args: calls.append(args))
    monkeypatch.delenv("API_KEY", raising=False)
    return calls


def args(tmp_path: Path, **overrides) -> argparse.Namespace:
    values = dict(
        strategy="trend/demo",
        arx_root=tmp_path / "arx",
        image="img",
        tenant_id="local",
        mode=None,
        replace=False,
        api_secret_env=None,
        api_passphrase_env=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


def test_sandbox_seals_a_placeholder_without_asking(strategy, sealed, tmp_path, monkeypatch):
    strategy()
    stdin(monkeypatch, "")  # nothing to read; a question would get an empty answer
    vault.run(args(tmp_path, mode="sandbox"))
    ((_, _, _, credential_id, credential),) = sealed
    assert credential_id == "binance-demo-sandbox"
    assert credential.api_key == credential.api_secret == vault.PLACEHOLDER


def test_without_a_mode_and_a_terminal_the_key_is_for_sandbox(strategy, sealed, tmp_path):
    strategy()
    vault.run(args(tmp_path))
    assert sealed[0][3] == "binance-demo-sandbox"


def test_testnet_asks_for_the_key_under_its_own_name(strategy, sealed, tmp_path, monkeypatch):
    strategy()
    stdin(monkeypatch, "key-1\nsecret-1\n")
    vault.run(args(tmp_path, mode="testnet"))
    ((_, _, _, credential_id, credential),) = sealed
    assert credential_id == "binance-demo-testnet"
    assert (credential.api_key, credential.api_secret) == ("key-1", "secret-1")


def test_testnet_without_a_secret_seals_nothing(strategy, sealed, tmp_path, monkeypatch):
    strategy()
    stdin(monkeypatch, "key-1\n")
    with pytest.raises(vault.VaultError, match="must both be set"):
        vault.run(args(tmp_path, mode="testnet"))
    assert not sealed


def test_an_okx_testnet_key_needs_its_passphrase(strategy, sealed, tmp_path, monkeypatch):
    strategy("okx_perpetual")
    stdin(monkeypatch, "key-1\nsecret-1\n")
    with pytest.raises(vault.VaultError, match="passphrase"):
        vault.run(args(tmp_path, mode="testnet"))
    assert not sealed


def test_an_okx_sandbox_key_needs_nothing_typed(strategy, sealed, tmp_path, monkeypatch):
    strategy("okx_perpetual")
    stdin(monkeypatch, "")
    vault.run(args(tmp_path, mode="sandbox"))
    assert sealed[0][4].api_passphrase == vault.PLACEHOLDER


def test_live_keys_are_never_sealed(strategy, sealed, tmp_path):
    strategy()
    with pytest.raises(vault.VaultError, match="live keys are not sealed"):
        vault.run(args(tmp_path, mode="live"))
    assert not sealed


def test_a_sealed_key_is_kept_without_replace(strategy, sealed, tmp_path):
    strategy()
    existing = tmp_path / "arx" / "vault" / "binance-demo-testnet.enc"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")
    with pytest.raises(vault.VaultError, match="add REPLACE=1"):
        vault.run(args(tmp_path, mode="testnet"))
    assert existing.read_text() == "old" and not sealed


def test_replace_removes_the_old_key_only_once_the_new_one_is_read(
    strategy, sealed, tmp_path, monkeypatch
):
    strategy()
    existing = tmp_path / "arx" / "vault" / "binance-demo-testnet.enc"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")
    stdin(monkeypatch, "")  # the new key never arrives
    with pytest.raises(vault.VaultError):
        vault.run(args(tmp_path, mode="testnet", replace=True))
    assert existing.exists()

    stdin(monkeypatch, "key-2\nsecret-2\n")
    vault.run(args(tmp_path, mode="testnet", replace=True))
    assert not existing.exists() and sealed[0][4].api_key == "key-2"


def test_the_sandbox_key_is_untouched_by_sealing_testnet(strategy, sealed, tmp_path, monkeypatch):
    strategy()
    sandbox = tmp_path / "arx" / "vault" / "binance-demo-sandbox.enc"
    sandbox.parent.mkdir(parents=True)
    sandbox.write_text("placeholder")
    stdin(monkeypatch, "key-1\nsecret-1\n")
    vault.run(args(tmp_path, mode="testnet", replace=True))
    assert sandbox.read_text() == "placeholder"


def test_a_terminal_is_asked_the_mode_and_before_replacing(strategy, sealed, tmp_path, monkeypatch):
    strategy()
    existing = tmp_path / "arx" / "vault" / "binance-demo-testnet.enc"
    existing.parent.mkdir(parents=True)
    existing.write_text("old")
    asked = []
    monkeypatch.setattr(ui, "interactive", lambda: True)
    monkeypatch.setattr(
        ui, "choose", lambda prompt, choices, default: asked.append(prompt) or "testnet"
    )
    monkeypatch.setattr(ui, "confirm", lambda prompt, default=False: asked.append(prompt) or False)
    with pytest.raises(vault.VaultError, match="kept the testnet key"):
        vault.run(args(tmp_path))
    assert len(asked) == 2 and existing.read_text() == "old" and not sealed


def test_the_scope_digest_depends_on_tenant_and_credential() -> None:
    assert vault.scope_digest("local", "a-sandbox") != vault.scope_digest("local", "a-testnet")
