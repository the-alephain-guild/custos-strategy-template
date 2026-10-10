import argparse
import io
import shutil
import subprocess
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
        toolchain="pinned",
        venue=None,
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


def test_replace_keeps_the_old_key_when_sealing_the_new_one_fails(strategy, tmp_path, monkeypatch):
    """The old key must survive until the new one is sealed: if age, Docker or the
    runner's vault write fails after the credential was read, the vault still holds
    what it held, and nothing is left behind under another name."""
    strategy()

    def failing_seal(*args):
        raise vault.VaultError("the runner could not seal the key: docker is down")

    monkeypatch.setattr(vault, "seal", failing_seal)
    monkeypatch.delenv("API_KEY", raising=False)
    existing = tmp_path / "arx" / "vault" / "binance-demo-testnet.enc"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old-ciphertext")
    stdin(monkeypatch, "key-2\nsecret-2\n")

    with pytest.raises(vault.VaultError, match="docker is down"):
        vault.run(args(tmp_path, mode="testnet", replace=True))

    assert existing.read_bytes() == b"old-ciphertext"
    assert sorted(p.name for p in existing.parent.iterdir()) == ["binance-demo-testnet.enc"]


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


def test_sealing_says_how_to_run_with_the_key(strategy, sealed, tmp_path, monkeypatch, capsys):
    strategy()
    stdin(monkeypatch, "")
    monkeypatch.setattr(ui, "Console", None)

    vault.run(args(tmp_path, mode="sandbox", toolchain="dev"))

    assert sealed
    assert "make start STRATEGY=trend/demo MODE=sandbox TOOLCHAIN=dev" in capsys.readouterr().out


# Venue profiles


def _profiled(strategy, connector: str = "binance_perpetual") -> Path:
    directory = strategy(connector)
    (directory / "venues").mkdir(exist_ok=True)
    (directory / "venues" / "sodex.yaml").write_text(
        'trading:\n  connector:\n    value: "sodex"\n  pairs:\n    value: ["vBTC_vUSDC"]\n'
    )
    return directory


def test_a_profile_seals_its_key_under_its_own_name(strategy, sealed, tmp_path, monkeypatch):
    _profiled(strategy)
    stdin(monkeypatch, "")
    vault.run(args(tmp_path, mode="sandbox", venue="sodex"))
    assert sealed[0][3] == "sodex-demo-sodex-sandbox"


def test_a_profile_asks_for_its_own_exchange_key(strategy, sealed, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ui, "Console", None)
    _profiled(strategy)
    stdin(monkeypatch, "key-1\nsecret-1\n")
    vault.run(args(tmp_path, mode="testnet", venue="sodex"))
    out = capsys.readouterr().out
    assert "SoDEX" in out, "the profile's exchange, not the default's"
    assert sealed[0][3] == "sodex-demo-sodex-testnet"


def test_a_profile_credential_written_in_run_yaml_is_used(strategy, sealed, tmp_path, monkeypatch):
    directory = _profiled(strategy)
    (directory / "run.yaml").write_text(
        "credential_id: binance-demo\nvenues:\n  sodex:\n    credential_id: my-sodex-key\n"
    )
    stdin(monkeypatch, "")
    vault.run(args(tmp_path, mode="sandbox", venue="sodex"))
    assert sealed[0][3] == "my-sodex-key-sandbox"


def test_an_unknown_profile_seals_nothing(strategy, sealed, tmp_path):
    _profiled(strategy)
    with pytest.raises(vault.VaultError, match="no venue profile 'okx'"):
        vault.run(args(tmp_path, mode="sandbox", venue="okx"))
    assert sealed == []


# How the secret reaches the runner's container


class _Docker:
    """Stands in for subprocess.run inside seal(): answers the age key lookup with a
    recipient and records the docker run that seals the key."""

    def __init__(self) -> None:
        self.argv: list[str] = []
        self.kwargs: dict = {}

    def __call__(self, argv, **kwargs):
        if argv[0] == "bash":
            return subprocess.CompletedProcess(argv, 0, stdout="age1recipient\n", stderr="")
        self.argv, self.kwargs = list(argv), kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> _Docker:
    recorder = _Docker()
    monkeypatch.setattr(vault.subprocess, "run", recorder)
    return recorder


def _docker_options(argv: list[str], image: str) -> list[str]:
    return argv[: argv.index(image)]


def _environment_passed(argv: list[str], image: str) -> list[str]:
    options = _docker_options(argv, image)
    return [options[i + 1] for i, flag in enumerate(options) if flag in ("-e", "--env")]


@pytest.mark.parametrize(
    "credential, sent",
    [
        (vault.Credential("key-1", "secret-1"), "secret-1\n"),
        (vault.Credential("key-1", "secret-1", "pass-1"), "secret-1\npass-1\n"),
    ],
)
def test_the_secret_and_passphrase_reach_the_container_on_stdin(docker, tmp_path, credential, sent):
    """docker run -e writes a value into the container's configuration, where
    docker inspect shows it while the container exists; stdin is not kept there."""
    vault.seal(tmp_path, "img", "local", "demo-testnet", credential)

    assert docker.argv[:2] == ["docker", "run"]
    assert "-i" in _docker_options(docker.argv, "img")
    assert docker.kwargs.get("input") == sent
    passed = _environment_passed(docker.argv, "img")
    assert not [name for name in passed if name.startswith("CUSTOS_API_SECRET")]
    assert not [name for name in passed if name.startswith("CUSTOS_API_PASSPHRASE")]
    assert "CUSTOS_API_SECRET" not in " ".join(docker.argv)
    for value in ("secret-1", "pass-1"):
        assert not [part for part in docker.argv if value in part]
        assert value not in (docker.kwargs.get("env") or {}).values()
    assert "--api-secret-stdin" in vault.SEAL_SCRIPT
    assert "--api-secret-env" not in vault.SEAL_SCRIPT


def test_a_secret_spanning_lines_is_refused_before_docker_runs(docker, tmp_path):
    """The runner reads one line of stdin, so a secret with a line break in it would
    be sealed cut short; it is refused instead, and nothing is started."""
    for credential in (
        vault.Credential("key-1", "secret\nmore"),
        vault.Credential("key-1", "secret-1", "pass\nmore"),
    ):
        with pytest.raises(vault.VaultError, match="line break"):
            vault.seal(tmp_path, "img", "local", "demo-testnet", credential)
    assert docker.argv == []


FAKE_RUNNER = """#!/bin/sh
out="$FAKE_RUNNER_OUT"
printf '%s\\n' "$@" > "$out/argv"
/bin/cat > "$out/stdin"
if [ "${CUSTOS_API_PASSPHRASE+set}" = set ]; then
  printf '%s' "$CUSTOS_API_PASSPHRASE" > "$out/passphrase"
fi
"""


def _shell() -> str:
    # The runner image's /bin/sh is dash; use it here when this machine has it.
    return shutil.which("dash") or "/bin/sh"


@pytest.mark.parametrize("passphrase", ["", " pass with 'quotes' and $HOME \\n "])
def test_the_container_script_hands_the_secret_to_the_runner_on_stdin(tmp_path, passphrase):
    """Runs the script the container runs, with a stand-in arx-runner, to show where
    each value ends up: the secret on the runner's stdin, the passphrase only in the
    runner process's environment, neither among any process's arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runner = bin_dir / "arx-runner"
    runner.write_text(FAKE_RUNNER)
    runner.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    secret = "  s3cr3t \\ `x` $(y) 'z\" "
    credential = vault.Credential("key-1", secret, passphrase)
    env = {
        # Only arx-runner is on PATH: the script runs no other program, so no other
        # process is ever handed a value as an argument.
        "PATH": str(bin_dir),
        "FAKE_RUNNER_OUT": str(out),
        "CUSTOS_API_KEY": "key-1",
    }

    result = subprocess.run(
        [_shell(), "-c", vault.SEAL_SCRIPT, "vault-put", "local", "demo", "age1r", "d" * 64],
        input=vault.seal_input(credential),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    argv = (out / "argv").read_text().splitlines()
    assert argv[:2] == ["vault", "put"]
    assert "--api-secret-stdin" in argv
    assert (out / "stdin").read_text() == secret + "\n"
    assert not [part for part in argv if secret.strip() in part]
    if passphrase:
        assert (out / "passphrase").read_text() == passphrase
        assert argv[argv.index("--api-passphrase-env") + 1] == "CUSTOS_API_PASSPHRASE"
        assert not [part for part in argv if passphrase.strip() in part]
    else:
        assert "--api-passphrase-env" not in argv


# What a testnet key is asked for as


def _prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []
    monkeypatch.setattr(ui, "ask", lambda prompt, *a, **k: asked.append(prompt) or "key-1")
    monkeypatch.setattr(ui, "secret", lambda prompt, *a, **k: asked.append(prompt) or "secret-1")
    return asked


@pytest.mark.parametrize("connector", ["sodex", "sodex_perpetual"])
def test_a_sodex_key_is_asked_for_by_its_name_and_its_private_key(
    strategy, sealed, tmp_path, monkeypatch, connector
):
    """SoDEX keeps a key's name where other exchanges keep the key string, and its
    secret is the API key's own private key, which is not the wallet's."""
    strategy(connector)
    asked = _prompts(monkeypatch)
    vault.run(args(tmp_path, mode="testnet"))
    key_prompt, secret_prompt = asked
    assert "API Key Name" in key_prompt
    assert "API key's private key" in secret_prompt and "not the wallet's" in secret_prompt


@pytest.mark.parametrize("connector", ["binance", "binance_perpetual", "okx_perpetual"])
def test_other_exchanges_are_asked_for_the_key_itself(
    strategy, sealed, tmp_path, monkeypatch, connector
):
    strategy(connector)
    asked = _prompts(monkeypatch)
    vault.run(args(tmp_path, mode="testnet"))
    assert "Key Name" not in asked[0] and asked[0].endswith("testnet API key")
    assert asked[1].endswith("testnet API secret")
