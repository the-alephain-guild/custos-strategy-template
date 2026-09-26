import json
import subprocess
from pathlib import Path

import pytest

from tools.toolchain import dev

ROOT = Path(__file__).resolve().parents[1]


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = {"cwd": path, "check": True, "capture_output": True}
    subprocess.run(["git", "init", "-q"], **run)
    (path / "file.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], **run)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one"], **run
    )
    return path


def commit(path: Path, text: str) -> str:
    (path / "file.txt").write_text(text, encoding="utf-8")
    run = {"cwd": path, "check": True, "capture_output": True}
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", text], **run
    )
    return dev.git(path, "rev-parse", "HEAD")


@pytest.fixture
def custos(tmp_path: Path) -> Path:
    repo = git_repo(tmp_path / "custos")
    (repo / "packages" / "custos-strategy-toolkit").mkdir(parents=True)
    return repo


def described(*args, **kwargs) -> tuple[list[str], list[str]]:
    rows, warnings = dev.describe(*args, **kwargs)
    return [f"{label} {value}" for label, value in rows], warnings


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "toolchain.local.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_missing_config_points_at_the_example(tmp_path: Path) -> None:
    with pytest.raises(dev.DevError, match="copy toolchain.local.toml.example"):
        dev.load_sources(tmp_path / "toolchain.local.toml")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("[elsewhere]\nx = 1\n", r"unknown section \[elsewhere\]"),
        ('[custos]\nsorce = "x"\n', r"unknown keys in \[custos\]: sorce"),
        ('[custos]\nsource = "/nowhere"\n', "is not a Custos checkout"),
        ('[custos]\nrevision = "abc"\n', "custos.revision needs custos.source"),
        ('[nautilus_trader]\nwheel = "/nowhere.whl"\n', "is not a wheel file"),
    ],
)
def test_a_wrong_config_says_which_entry(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(dev.DevError, match=message):
        dev.load_sources(write_config(tmp_path, body))


def test_a_wheel_for_another_python_is_refused(tmp_path: Path) -> None:
    wheel = tmp_path / "nautilus_trader-2.0.0rc5+sodex.2-cp313-cp313-macosx_11_0_arm64.whl"
    wheel.write_bytes(b"wheel")
    with pytest.raises(dev.DevError, match="not built for Python 3.12"):
        dev.load_sources(write_config(tmp_path, f'[nautilus_trader]\nwheel = "{wheel}"\n'))
    right = tmp_path / wheel.name.replace("cp313", "cp312")
    right.write_bytes(b"wheel")
    assert dev.load_sources(write_config(tmp_path, f'[nautilus_trader]\nwheel = "{right}"\n'))


def test_every_entry_is_optional(tmp_path: Path) -> None:
    assert dev.load_sources(write_config(tmp_path, "")) == dev.Sources()


def test_the_example_config_names_only_known_keys() -> None:
    import tomllib

    example = tomllib.loads((ROOT / "toolchain.local.toml.example").read_text(encoding="utf-8"))
    for section, values in example.items():
        assert set(values) <= dev.KEYS[section]


def test_export_lines_are_matched_to_their_package() -> None:
    assert dev.package_name("nautilus-trader @ https://x/y.whl ; sys_platform == 'darwin'") == (
        "nautilus-trader"
    )
    assert dev.package_name(
        "./.toolchain/wheels/custos_strategy_toolkit_nautilus-0.1.0rc7-py3-none-any.whl ; x"
    ) == ("custos-strategy-toolkit-nautilus")
    assert dev.package_name("pyyaml==6.0.3 ; x") == "pyyaml"


def test_replaced_packages_leave_the_pinned_requirements() -> None:
    kept = dev.requirements({"nautilus-trader", *dev.TOOLKIT_PACKAGES})
    names = {dev.package_name(line) for line in kept}
    assert "nautilus-trader" not in names
    assert not names & set(dev.TOOLKIT_PACKAGES)
    assert {"pyyaml", "pytest", "ruff"} <= names
    # With nothing replaced, the pinned toolkit wheels are named by absolute path.
    pinned = [line for line in dev.requirements(set()) if ".toolchain/" in line]
    assert pinned and all(line.startswith(f"{dev.ROOT}/.toolchain/") for line in pinned)


def test_custos_needs_a_revision_and_it_must_be_a_commit(custos: Path, tmp_path: Path) -> None:
    with pytest.raises(dev.DevError, match="custos.revision is missing"):
        dev.load_sources(write_config(tmp_path, f'[custos]\nsource = "{custos}"\n'))
    with pytest.raises(dev.DevError, match="is not a commit"):
        dev.load_sources(
            write_config(tmp_path, f'[custos]\nsource = "{custos}"\nrevision = "nope"\n')
        )
    head = dev.git(custos, "rev-parse", "HEAD")
    body = f'[custos]\nsource = "{custos}"\nrevision = "{head[:7]}"\n'
    assert dev.wanted_custos(dev.load_sources(write_config(tmp_path, body))) == head


def test_work_in_the_custos_repository_does_not_make_the_build_stale(custos: Path) -> None:
    built = dev.git(custos, "rev-parse", "HEAD")
    record = {"custos": {"source": str(custos), "revision": built}}
    commit(custos, "two")
    (custos / "file.txt").write_text("edited, not committed\n", encoding="utf-8")
    lines, warnings = described(record, wanted_custos=built)
    assert built[:12] in lines[0] and not warnings


def test_naming_another_revision_asks_for_a_rebuild(custos: Path) -> None:
    built = dev.git(custos, "rev-parse", "HEAD")
    newer = commit(custos, "two")
    record = {"custos": {"source": str(custos), "revision": built}}
    _, warnings = dev.describe(record, wanted_custos=newer)
    assert warnings and "run make toolkit-dev" in warnings[0]


def test_custos_is_checked_out_apart_from_its_working_directory(
    custos: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dev, "DEV_SOURCES", tmp_path / "dev-sources")
    first = dev.git(custos, "rev-parse", "HEAD")
    second = commit(custos, "two")
    # Work in progress in the repository must not reach the checkout.
    (custos / "file.txt").write_text("half done\n", encoding="utf-8")

    checkout = dev.custos_checkout(custos, first)
    assert dev.git_state(checkout) == {"commit": first, "dirty": False}
    assert (checkout / "file.txt").read_text(encoding="utf-8") == "one\n"
    assert dev.custos_checkout(custos, first) == checkout

    newer = dev.custos_checkout(custos, second)
    assert dev.git_state(newer)["commit"] == second
    assert not checkout.exists()
    assert (custos / "file.txt").read_text(encoding="utf-8") == "half done\n"


def test_a_wheel_is_traced_to_its_checkout(tmp_path: Path) -> None:
    fork = git_repo(tmp_path / "fork")
    wheel = tmp_path / "nautilus_trader-2.0.0rc5+sodex.2-cp312-cp312-macosx_11_0_arm64.whl"
    wheel.write_bytes(b"wheel")
    provenance = {"source": str(fork), **dev.git_state(fork)}
    wheel.with_name(wheel.name + ".provenance.json").write_text(json.dumps(provenance))
    record = {"nautilus_trader": dev.wheel_state(wheel)}

    lines, warnings = described(record)
    assert "local 2.0.0rc5+sodex.2 built from" in lines[1] and not warnings

    commit(fork, "two")
    _, warnings = dev.describe(record)
    assert "Rebuild the wheel" in warnings[0]

    wheel.write_bytes(b"rebuilt")
    _, warnings = dev.describe(record)
    assert "changed after it was installed" in warnings[0]


def test_items_left_out_are_described_as_pinned() -> None:
    lines, warnings = described({})
    assert all("pinned" in line for line in lines)
    assert not warnings


def test_the_runner_image_says_its_nautilus_trader_is_released() -> None:
    lines, _ = described({"runner_image": "custos-runner:dev"})
    assert "its NautilusTrader is the released one" in lines[2]


def test_a_dev_image_from_another_revision_is_refused(
    custos: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = dev.git(custos, "rev-parse", "HEAD")
    sources = dev.Sources(
        custos_source=custos, custos_revision=pinned[:7], runner_image="custos-runner:dev"
    )
    commit(custos, "two")  # the repository moves on; the pinned revision does not
    monkeypatch.setattr(dev, "image_revision", lambda _image: pinned)
    assert pinned[:12] in dev.check_image("custos-runner:dev", sources)
    monkeypatch.setattr(dev, "image_revision", lambda _image: "0" * 40)
    with pytest.raises(dev.DevError, match="run make toolkit-dev"):
        dev.check_image("custos-runner:dev", sources)


def test_a_banner_before_any_build_says_how_to_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dev, "DEV_ENV", tmp_path / ".venv-dev")
    with pytest.raises(dev.DevError, match="run make toolkit-dev"):
        dev.read_record()


def test_the_running_environment_reports_which_toolchain_it_is() -> None:
    import sys

    summary = dev.current_toolchain()
    in_dev = Path(sys.prefix).resolve() == dev.DEV_ENV.resolve()
    assert summary["mode"] == ("dev" if in_dev else "pinned")
    assert ("sources" in summary) == in_dev
    assert summary["nautilus_trader"]


def test_verify_refuses_the_dev_toolchain() -> None:
    result = subprocess.run(
        ["make", "-C", str(ROOT), "verify-pinned", "TOOLCHAIN=dev"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode != 0
    assert "pinned toolchain only" in result.stdout + result.stderr


def test_an_unknown_toolchain_is_refused() -> None:
    result = subprocess.run(
        ["make", "-C", str(ROOT), "-n", "test", "TOOLCHAIN=nightly"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert result.returncode != 0
    assert "TOOLCHAIN is pinned or dev" in result.stderr
