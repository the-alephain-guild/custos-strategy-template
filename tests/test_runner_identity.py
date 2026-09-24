from pathlib import Path

import pytest

from tools.runner import identity

VALID = {
    "tenant_id": "local",
    "runner_id": "r",
    "backend_url": "http://standalone.invalid",
    "credential_id": "c",
    "credential_version": 1,
    "credential_valid_until": "2030-01-01T00:00:00Z",
    "machine_key_id": "k",
    "machine_vault_path": "/home/custos/.arx/vault/runner-machine.enc",
    "enrolled_at": "2026-01-01T00:00:00Z",
}


def _toml(fields: dict) -> str:
    lines = []
    for key, value in fields.items():
        lines.append(f"{key} = {value}" if isinstance(value, int) else f'{key} = "{value}"')
    return "\n".join(lines) + "\n"


@pytest.fixture
def arx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".arx"
    (root / "vault").mkdir(parents=True)
    monkeypatch.setattr(identity, "ARX_ROOT", root)
    return root


def _write(path: Path, text: str, mode: int = 0o600) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def test_a_complete_identity_passes(arx: Path) -> None:
    _write(arx / "runner.toml", _toml(VALID))
    _write(arx / "vault" / "runner-machine.enc", "sealed")
    identity.check_identity("local")


def test_another_tenants_identity_is_refused(arx: Path) -> None:
    _write(arx / "runner.toml", _toml(VALID))
    _write(arx / "vault" / "runner-machine.enc", "sealed")
    with pytest.raises(identity.IdentityError, match="belongs to tenant"):
        identity.check_identity("elsewhere")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"enrolled_at": None}, "missing enrolled_at"),
        ({"surprise": "x"}, "unexpected surprise"),
        ({"credential_version": 0}, "positive integer"),
    ],
)
def test_a_document_the_image_would_reject_is_refused(
    arx: Path, change: dict, message: str
) -> None:
    fields = {**VALID, **change}
    fields = {k: v for k, v in fields.items() if v is not None}
    _write(arx / "runner.toml", _toml(fields))
    with pytest.raises(identity.IdentityError, match=message):
        identity.read_runner_toml()


def test_loose_file_modes_are_refused(arx: Path) -> None:
    _write(arx / "runner.toml", _toml(VALID), mode=0o644)
    with pytest.raises(identity.IdentityError, match="mode 0o600"):
        identity.read_runner_toml()


def test_a_machine_credential_recorded_outside_the_container_is_refused(arx: Path) -> None:
    _write(arx / "runner.toml", _toml({**VALID, "machine_vault_path": "/somewhere/else.enc"}))
    with pytest.raises(identity.IdentityError, match="outside the container"):
        identity.check_identity("local")


def test_a_missing_exchange_key_points_at_runner_vault(arx: Path) -> None:
    _write(arx / "runner.toml", _toml(VALID))
    _write(arx / "vault" / "runner-machine.enc", "sealed")
    _write(arx / "age.key", "key")
    with pytest.raises(identity.IdentityError, match="make runner-vault"):
        identity.check_vault("binance-demo")
