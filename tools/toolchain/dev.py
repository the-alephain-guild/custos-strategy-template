#!/usr/bin/env python3
"""Build and describe the dev toolchain: unreleased Custos and NautilusTrader, locally.

The pinned toolchain (toolchain.lock.toml, pyproject.toml, uv.lock, .venv) is what
CI and `make verify` run, and this never touches it. Instead it builds a second
environment, .venv-dev, from the sources named in toolchain.local.toml, which is
not committed:

    [custos]
    source = "/path/to/custos"          # a Custos repository, read for its history
    revision = "4f3c736"                # the commit to run, local commits included
    runner_image = "custos-runner:dev"  # tag for the runner image built from it

    [nautilus_trader]
    wheel = "/path/to/nautilus_trader-...-macosx_11_0_arm64.whl"

Every entry is optional; whatever is left out stays at its pinned version.

Custos is built from a checkout of `revision` of its own under
.toolchain/dev-sources/, never from the repository's working directory, so work
there -- uncommitted changes, new commits -- neither blocks a build nor makes a
build here out of date. To run a newer Custos, change `revision` and rebuild.

The sources each item came from are recorded in .venv-dev, so `banner` can say
which build is in use and warn when it no longer matches toolchain.local.toml.

Usage:
    python3 tools/toolchain/dev.py build
    python3 tools/toolchain/dev.py banner
    python3 tools/toolchain/dev.py runner-image
    python3 tools/toolchain/dev.py check-image IMAGE
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402

CONFIG = ROOT / "toolchain.local.toml"
LOCK = ROOT / "toolchain.lock.toml"
DEV_ENV = ROOT / ".venv-dev"
RECORD_NAME = "toolchain-sources.json"
DEV_WHEELS = ROOT / ".toolchain" / "dev-wheels"
DEV_SOURCES = ROOT / ".toolchain" / "dev-sources"
TOOLKIT_PACKAGES = ("custos-strategy-toolkit", "custos-strategy-toolkit-nautilus")
NAUTILUS = "nautilus-trader"
PYTHON_ABI = "cp312"
KEYS = {"custos": {"source", "revision", "runner_image"}, "nautilus_trader": {"wheel"}}
# The files and environment the pinned toolchain consists of.
PINNED_FILES = ("pyproject.toml", "uv.lock")


class DevError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sources:
    custos_source: Path | None = None
    custos_revision: str | None = None
    runner_image: str | None = None
    nautilus_wheel: Path | None = None


def load_sources(path: Path = CONFIG) -> Sources:
    if not path.is_file():
        raise DevError(
            f"{path.name} is missing: copy toolchain.local.toml.example to {path.name} "
            "and name the local sources to use"
        )
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    for section, values in data.items():
        if section not in KEYS or not isinstance(values, dict):
            raise DevError(f"{path.name}: unknown section [{section}]")
        unknown = set(values) - KEYS[section]
        if unknown:
            names = ", ".join(sorted(unknown))
            raise DevError(f"{path.name}: unknown keys in [{section}]: {names}")
    custos = data.get("custos", {})
    nautilus = data.get("nautilus_trader", {})

    source = Path(custos["source"]).expanduser() if custos.get("source") else None
    revision = str(custos["revision"]) if custos.get("revision") else None
    if source is not None:
        if not (source / "packages" / "custos-strategy-toolkit").is_dir():
            raise DevError(f"custos.source {source} is not a Custos checkout")
        if revision is None:
            raise DevError(
                "custos.revision is missing: name the commit to run, for example the "
                f"output of `git -C {source} rev-parse --short HEAD`"
            )
        resolve_revision(source, revision)
    elif revision is not None:
        raise DevError("custos.revision needs custos.source, the repository it names a commit in")
    wheel = Path(nautilus["wheel"]).expanduser() if nautilus.get("wheel") else None
    if wheel is not None and not (wheel.is_file() and wheel.suffix == ".whl"):
        raise DevError(f"nautilus_trader.wheel {wheel} is not a wheel file")
    if wheel is not None and f"-{PYTHON_ABI}-{PYTHON_ABI}-" not in wheel.name:
        raise DevError(
            f"nautilus_trader.wheel {wheel.name} is not built for Python 3.12, "
            "the version strategies run on"
        )
    return Sources(source, revision, custos.get("runner_image") or None, wheel)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise DevError(f"git {' '.join(args)} failed in {path}: {result.stderr.strip()}")
    return result.stdout.strip()


def git_state(path: Path) -> dict[str, object]:
    commit = git(path, "rev-parse", "HEAD")
    dirty = bool(git(path, "status", "--porcelain", "--untracked-files=no"))
    return {"commit": commit, "dirty": dirty}


def resolve_revision(source: Path, revision: str) -> str:
    """The full commit a revision names in the source repository."""
    try:
        return git(source, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    except DevError:
        raise DevError(f"custos.revision {revision} is not a commit in {source}") from None


def custos_checkout(source: Path, commit: str) -> Path:
    """A clean checkout of one commit, kept apart from the repository's working directory.

    It is a git worktree of the source repository under .toolchain/dev-sources/, so
    it costs no clone and changes nothing in the working directory people edit.
    A checkout of an earlier revision is removed when the revision changes.
    """
    target = DEV_SOURCES / f"custos-{commit[:12]}"
    if target.is_dir() and git_state(target) == {"commit": commit, "dirty": False}:
        return target
    DEV_SOURCES.mkdir(parents=True, exist_ok=True)
    for earlier in DEV_SOURCES.glob("custos-*"):
        git(source, "worktree", "remove", "--force", str(earlier))
    git(source, "worktree", "prune")
    git(source, "worktree", "add", "--detach", str(target), commit)
    return target


def build_runner_image(checkout: Path, image: str, commit: str) -> None:
    """Build the runner image from the checkout, unless it is already built from it."""
    try:
        if image_revision(image) == commit:
            return
    except DevError:
        pass  # not built yet
    subprocess.run(
        ["make", "-C", str(checkout), "docker-build-local-v030", f"LOCAL_IMAGE={image}"],
        check=True,
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def wheel_version(wheel: Path) -> str:
    # name-version-python-abi-platform.whl; the local part is written with "+" escaped.
    return wheel.name.split("-")[1].replace("%2B", "+")


def wheel_state(wheel: Path) -> dict[str, object]:
    state: dict[str, object] = {
        "path": str(wheel),
        "version": wheel_version(wheel),
        "sha256": sha256(wheel),
    }
    provenance = wheel.with_name(wheel.name + ".provenance.json")
    if provenance.is_file():
        state["provenance"] = json.loads(provenance.read_text(encoding="utf-8"))
    return state


def package_name(requirement: str) -> str:
    """The distribution a line of `uv export` output installs."""
    spec = requirement.split(";")[0].strip()
    if " @ " in spec:
        spec = spec.split(" @ ")[0]
    elif spec.endswith(".whl"):
        return Path(spec).name.split("-")[0].replace("_", "-").lower()
    return re.split(r"[ =<>@!~\[]", spec, maxsplit=1)[0].replace("_", "-").lower()


def requirements(replaced: set[str]) -> list[str]:
    """The pinned environment's requirements, less the packages being replaced."""
    exported = subprocess.run(
        ["uv", "export", "--frozen", "--no-hashes", "--no-emit-project", "--all-groups",
         "--no-header", "--no-annotate"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    kept = []
    for line in exported.splitlines():
        if not line.strip():
            continue
        if package_name(line) in replaced:
            continue
        # uv writes the pinned toolkit wheels relative to the project root.
        kept.append(line.replace("./.toolchain/", f"{ROOT}/.toolchain/", 1))
    return kept


def pinned_fingerprint() -> dict[str, str]:
    fingerprint = {name: sha256(ROOT / name) for name in PINNED_FILES}
    python = ROOT / ".venv" / "bin" / "python"
    if python.exists():
        frozen = subprocess.run(
            ["uv", "pip", "freeze", "--python", str(python)],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        fingerprint[".venv"] = hashlib.sha256(frozen.encode()).hexdigest()
    return fingerprint


def build(sources: Sources) -> dict[str, object]:
    before = pinned_fingerprint()
    record: dict[str, object] = {}
    local_wheels: list[Path] = []
    overrides: list[str] = []

    if sources.custos_source is not None and sources.custos_revision is not None:
        commit = resolve_revision(sources.custos_source, sources.custos_revision)
        checkout = custos_checkout(sources.custos_source, commit)
        # First, while the checkout is still exactly the commit: the image build
        # refuses a checkout with anything in it that is not committed.
        if sources.runner_image:
            build_runner_image(checkout, sources.runner_image, commit)
        out = DEV_WHEELS / commit[:12]
        if out.exists():
            shutil.rmtree(out)
        for package in TOOLKIT_PACKAGES:
            subprocess.run(
                ["uv", "build", "--package", package, "--wheel", "--out-dir", str(out)],
                cwd=checkout, check=True,
            )  # fmt: skip
        local_wheels += sorted(out.glob("*.whl"))
        record["custos"] = {"source": str(sources.custos_source), "revision": commit}

    if sources.nautilus_wheel is not None:
        local_wheels.append(sources.nautilus_wheel)
        # The toolkit pins the released NautilusTrader exactly, and a local build
        # carries a different version, so the pin is overridden for this environment.
        overrides.append(f"{NAUTILUS} @ {sources.nautilus_wheel.resolve().as_uri()}")
        record["nautilus_trader"] = wheel_state(sources.nautilus_wheel)

    if sources.runner_image:
        record["runner_image"] = sources.runner_image

    replaced = set(TOOLKIT_PACKAGES) if sources.custos_source else set()
    if sources.nautilus_wheel is not None:
        replaced.add(NAUTILUS)

    DEV_ENV.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["uv", "venv", "--clear", "--python", "3.12", str(DEV_ENV)], check=True)
    work = DEV_ENV / "build-inputs"
    work.mkdir()
    requirement_file = work / "requirements.txt"
    requirement_file.write_text(
        "\n".join(requirements(replaced) + [str(w) for w in local_wheels]) + "\n",
        encoding="utf-8",
    )
    command = ["uv", "pip", "install", "--python", str(DEV_ENV / "bin" / "python"),
               "-r", str(requirement_file)]  # fmt: skip
    if overrides:
        override_file = work / "overrides.txt"
        override_file.write_text("\n".join(overrides) + "\n", encoding="utf-8")
        command += ["--override", str(override_file)]
    subprocess.run(command, check=True)

    after = pinned_fingerprint()
    if after != before:
        changed = ", ".join(sorted(k for k in before if before[k] != after.get(k)))
        raise DevError(f"building the dev environment changed the pinned toolchain: {changed}")

    (DEV_ENV / RECORD_NAME).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def read_record() -> dict[str, object]:
    path = DEV_ENV / RECORD_NAME
    if not path.is_file():
        raise DevError("the dev environment is not built: run make setup-dev")
    return json.loads(path.read_text(encoding="utf-8"))


def pinned_versions() -> dict[str, str]:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    nautilus = re.search(r"nautilus_trader-([^-]+)-", (ROOT / "pyproject.toml").read_text())
    return {
        "custos": f"toolkit {lock['toolkit']['version']}",
        "nautilus_trader": nautilus.group(1).replace("%2B", "+") if nautilus else "unknown",
        "runner_image": lock["runner"]["image"],
    }


def _short(path: object) -> str:
    """A path as a person would type it: the home directory as ~."""
    text = str(path)
    home = str(Path.home())
    return "~" + text[len(home) :] if text.startswith(home + "/") else text


def describe(
    record: dict[str, object], wanted_custos: str | None = None
) -> tuple[list[tuple[str, str]], list[str]]:
    """Rows saying where each item comes from, and warnings about stale builds.

    `wanted_custos` is the commit toolchain.local.toml names now; the build is
    stale only if it differs from the one built. New commits in the Custos
    repository do not make it stale: the revision is pinned on purpose.
    """
    pinned = pinned_versions()
    lines, warnings = [], []

    custos = record.get("custos")
    if isinstance(custos, dict):
        built = str(custos.get("revision") or custos.get("commit"))
        lines.append(("toolkit", f"local {_short(custos['source'])} @ {built[:12]}"))
        if wanted_custos is not None and wanted_custos != built:
            warnings.append(
                f"toolchain.local.toml names Custos {wanted_custos[:12]}, but the dev "
                f"environment is built from {built[:12]}; run make setup-dev"
            )
    elif wanted_custos is not None:
        lines.append(("toolkit", f"pinned {pinned['custos']}"))
        warnings.append("toolchain.local.toml names a Custos revision; run make setup-dev")
    else:
        lines.append(("toolkit", f"pinned {pinned['custos']}"))

    nautilus = record.get("nautilus_trader")
    if isinstance(nautilus, dict):
        origin = ""
        provenance = nautilus.get("provenance")
        if isinstance(provenance, dict):
            dirty = ", with uncommitted changes" if provenance.get("dirty") else ""
            origin = (
                f" built from {_short(provenance['source'])} @ "
                f"{str(provenance['commit'])[:12]}{dirty}"
            )
        lines.append(("nautilus_trader", f"local {nautilus['version']}{origin}"))
        wheel = Path(str(nautilus["path"]))
        if not wheel.is_file() or sha256(wheel) != nautilus["sha256"]:
            warnings.append(f"{wheel.name} changed after it was installed; run make setup-dev")
        elif isinstance(provenance, dict) and Path(provenance["source"]).is_dir():
            head = git_state(Path(provenance["source"]))["commit"]
            if head != provenance["commit"]:
                warnings.append(
                    f"the NautilusTrader checkout is now at {str(head)[:12]}; this wheel "
                    f"is from {str(provenance['commit'])[:12]}. Rebuild the wheel to use it"
                )
    else:
        lines.append(("nautilus_trader", f"pinned {pinned['nautilus_trader']}"))

    image = record.get("runner_image")
    if image:
        # The image installs NautilusTrader from its own lock, not from this machine.
        lines.append(("runner image", f"local {image} (its NautilusTrader is the released one)"))
    else:
        lines.append(("runner image", f"pinned {pinned['runner_image']}"))
    return lines, warnings


def current_toolchain() -> dict[str, object]:
    """What the running interpreter is, for a backtest summary."""
    from importlib.metadata import PackageNotFoundError, version

    def installed(name: str) -> str | None:
        try:
            return version(name)
        except PackageNotFoundError:
            return None

    dev = Path(sys.prefix).resolve() == DEV_ENV.resolve()
    summary: dict[str, object] = {
        "mode": "dev" if dev else "pinned",
        "nautilus_trader": installed(NAUTILUS),
        "custos_strategy_toolkit": installed("custos-strategy-toolkit"),
    }
    if dev and (DEV_ENV / RECORD_NAME).is_file():
        summary["sources"] = read_record()
    return summary


def image_revision(image: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", image, "--format",
         '{{index .Config.Labels "org.opencontainers.image.revision"}}'],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise DevError(f"runner image {image} is not available locally")
    return result.stdout.strip()


def wanted_custos(sources: Sources) -> str | None:
    if sources.custos_source is None or sources.custos_revision is None:
        return None
    return resolve_revision(sources.custos_source, sources.custos_revision)


def check_image(image: str, sources: Sources) -> str:
    revision = image_revision(image)
    wanted = wanted_custos(sources)
    if wanted is None:
        return f"runner image {image} is at revision {revision or 'unknown'}"
    if revision != wanted:
        raise DevError(
            f"runner image {image} was built from {revision[:12] or 'an unknown revision'}, "
            f"but toolchain.local.toml names Custos {wanted[:12]}; run make setup-dev, "
            "which builds it"
        )
    return f"runner image {image} is built from Custos {wanted[:12]}"


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else ""
    try:
        if command == "build":
            rows, _ = describe(build(load_sources()))
            ui.table("Dev environment built in .venv-dev", rows)
        elif command == "banner":
            rows, warnings = describe(read_record(), wanted_custos(load_sources()))
            ui.table("Dev toolchain", rows)
            for warning in warnings:
                ui.warn(warning, tag="toolchain")
        elif command == "runner-image":
            # Read by the Makefile; plain on purpose.
            print(load_sources().runner_image or pinned_versions()["runner_image"])
        elif command == "check-image" and len(argv) == 3:
            ui.ok(check_image(argv[2], load_sources()), tag="toolchain")
        else:
            print(__doc__)
            return 2
    except (DevError, subprocess.CalledProcessError) as error:
        ui.error(str(error), tag="toolchain")
        return 1
    return 0


if __name__ == "__main__":
    # `make TOOLCHAIN=dev` points uv at .venv-dev and turns syncing off; the uv
    # commands here must act on their own targets, never on those settings.
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_NO_SYNC"):
        os.environ.pop(name, None)
    sys.exit(main(sys.argv))
