#!/usr/bin/env python3
"""Download the pinned strategy toolkit wheels into .toolchain/wheels/.

The toolkit is a public, digest-pinned OCI artifact on GitHub Container
Registry, so no account or token is needed. The manifest is fetched by the
digest in toolchain.lock.toml and rejected unless its bytes hash to that digest;
each wheel is then fetched by its own layer digest and rejected the same way.
Nothing unverified is ever written where pyproject.toml looks for wheels.

This runs before any environment exists, so it uses the standard library only.

Usage:
    python3 tools/toolchain/fetch.py
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "toolchain.lock.toml"
WHEELS = ROOT / ".toolchain" / "wheels"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
WHEEL_TYPE = "application/vnd.pypa.wheel"
TITLE = "org.opencontainers.image.title"


class FetchError(RuntimeError):
    pass


def check_platform(supported: list[dict[str, str]], system: str, machine: str) -> None:
    if {"system": system, "machine": machine} in supported:
        return
    names = ", ".join(f"{p['system']} {p['machine']}" for p in supported)
    raise FetchError(
        f"{system} {machine} is not supported: NautilusTrader wheels exist for {names} only. "
        "See 'Before you start' in README.md."
    )


def sha256_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def verify(content: bytes, expected: str, what: str) -> bytes:
    actual = sha256_digest(content)
    if actual != expected:
        raise FetchError(f"{what} hashes to {actual}, but {expected} was pinned")
    return content


def wheel_layers(manifest: dict, expected_titles: list[str]) -> dict[str, str]:
    layers = {
        layer.get("annotations", {}).get(TITLE, ""): layer["digest"]
        for layer in manifest.get("layers", [])
        if layer.get("mediaType") == WHEEL_TYPE
    }
    for title in layers:
        if "/" in title or "\\" in title or not title.endswith(".whl"):
            raise FetchError(f"the artifact names a wheel {title!r}, which is not a file name")
    missing = sorted(set(expected_titles) - set(layers))
    if missing:
        raise FetchError(f"the pinned artifact has no layer for {', '.join(missing)}")
    return {title: layers[title] for title in expected_titles}


def _get(url: str, token: str | None = None, accept: str | None = None) -> bytes:
    request = urllib.request.Request(url)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if accept:
        request.add_header("Accept", accept)
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def fetch() -> list[Path]:
    lock = tomllib.loads(LOCK.read_text(encoding="utf-8"))
    check_platform(lock["platforms"]["supported"], platform.system(), platform.machine())

    toolkit = lock["toolkit"]
    registry, repository = toolkit["registry"], toolkit["repository"]
    token_url = f"https://{registry}/token?scope=repository:{repository}:pull"
    token = json.loads(_get(token_url))["token"]
    base = f"https://{registry}/v2/{repository}"

    digest = toolkit["manifest_digest"]
    manifest_bytes = _get(f"{base}/manifests/{digest}", token, MANIFEST_TYPE)
    manifest = json.loads(verify(manifest_bytes, digest, "the toolkit manifest"))

    WHEELS.mkdir(parents=True, exist_ok=True)
    written = []
    for title, layer_digest in wheel_layers(manifest, toolkit["wheels"]).items():
        target = WHEELS / title
        if target.is_file() and sha256_digest(target.read_bytes()) == layer_digest:
            written.append(target)
            continue
        content = verify(_get(f"{base}/blobs/{layer_digest}", token), layer_digest, title)
        target.write_bytes(content)
        written.append(target)
    return written


def main() -> int:
    try:
        wheels = fetch()
    except FetchError as error:
        print(f"[toolchain] {error}", file=sys.stderr)
        return 1
    for wheel in wheels:
        print(f"[toolchain] verified {wheel.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
