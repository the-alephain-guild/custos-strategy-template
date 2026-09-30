#!/usr/bin/env python3
"""Read a signed release back from its package, checked, as ARX needs it.

`make release` keeps the publication receipt of each release in
`.releases/<category>/<name>/<version>/`. The receipt names the release by
digest: the release manifest and the attestation manifest, each with its size,
and every layer of both with its role, digest and size. ARX drafts a release
from the exact bytes of those layers, so this reads them back from the package
by digest and checks, before anything is sent anywhere:

- that the receipt agrees with itself: its descriptor matrices hash to the
  digests it states, its artifact reference hashes to its digest and names the
  same manifest and layers, and the publisher's own checks are recorded as done;
- that each manifest the registry serves has the receipt's digest and size, and
  lists exactly the receipt's layers, in order, with their roles and names;
- that the attestation manifest's subject is this release manifest;
- that every blob of both manifests has the receipt's digest and size;
- that the attestation reference names this release's statement and bundle.

The package is read with your GitHub login (`gh auth token`), which needs the
`read:packages` scope. The token is exchanged for a GHCR token that may only pull
from this one package; neither is written anywhere or shown.

Usage:
    python3 tools/arx/evidence.py trend/my_idea [--version 0.2.0]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib import parse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402
from tools.arx.client import ArxError, Transport, redact, urllib_transport  # noqa: E402

RECEIPT_FILE = "strategy-release-publication-receipt-v1.json"
RECEIPT_SCHEMA = "alephain.strategy-artifact-oci-publication-receipt.v1"
RECEIPT_FIELDS = frozenset(
    {
        "artifact_ref",
        "artifact_ref_digest",
        "attestation_descriptor_matrix",
        "attestation_descriptor_matrix_digest",
        "attestation_manifest_digest",
        "attestation_manifest_size_bytes",
        "blob_readback_verified",
        "discovery_tag",
        "external_publication_completed",
        "manifest_readback_verified",
        "producer_commit",
        "producer_repository",
        "publisher_profile",
        "referrer_verified",
        "release_descriptor_matrix",
        "release_descriptor_matrix_digest",
        "release_manifest_digest",
        "release_manifest_size_bytes",
        "repository",
        "schema_version",
        "strategy_coordinate",
        "tag_is_authority",
        "workflow",
    }
)
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
RELEASE_ARTIFACT_TYPE = "application/vnd.alephain.strategy-release.v1"
ATTESTATION_ARTIFACT_TYPE = "application/vnd.alephain.strategy-release.attestation.v1"
ROLE_ANNOTATION = "dev.alephain.layer.role"
TITLE_ANNOTATION = "org.opencontainers.image.title"
# Each role in the order the publisher writes it, with its media type.
RELEASE_ROLES = {
    "release_config": "application/vnd.alephain.strategy-release.config.v1+json",
    "strategy_artifact": "application/vnd.alephain.strategy-wheel.v1+zip",
    "strategy_manifest": "application/vnd.alephain.strategy-manifest.v1+json",
    "strategy_artifact_ref": "application/vnd.alephain.strategy-artifact-ref.v1+json",
    "strategy_release_bom": "application/vnd.alephain.strategy-release-bom.v1+json",
    "strategy_sbom": "application/spdx+json",
    "strategy_release_statement": "application/vnd.in-toto+json",
}
ATTESTATION_ROLES = {
    "attestation_config": "application/vnd.alephain.strategy-release.attestation.config.v1+json",
    "attestation_bundle": "application/vnd.dev.sigstore.bundle.v0.3+json",
    "artifact_attestation_ref": "application/vnd.alephain.artifact-attestation-ref.v1+json",
}
MATRIX_FIELDS = frozenset({"digest", "media_type", "name", "role", "size_bytes"})
GHCR_REPOSITORY = re.compile(r"^ghcr\.io/[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:/[a-z0-9._-]+)+$")
OCI_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
READ_PACKAGES_FIX = "gh auth refresh -s read:packages"
READ_ATTEMPTS = 2


class Registry(Protocol):
    """Reads by digest from the one package a release was published to."""

    def manifest(self, digest: str) -> bytes: ...

    def blob(self, digest: str) -> bytes: ...


def _canonical(value: object) -> bytes:
    """The publisher's canonical JSON: sorted keys, no spaces, UTF-8 as written."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _refuse(message: str, fix: str = "") -> ArxError:
    return ArxError(message, fix or "make release again, or report the receipt to the publisher")


def _check_matrix(receipt: Mapping, key: str, roles: Mapping[str, str]) -> list[dict]:
    matrix = receipt[key]
    if not isinstance(matrix, list) or not all(isinstance(item, dict) for item in matrix):
        raise _refuse(f"the receipt's {key} is not a list of descriptors")
    for item in matrix:
        if set(item) != MATRIX_FIELDS:
            raise _refuse(f"a descriptor in the receipt's {key} has fields {sorted(item)}")
        if not OCI_DIGEST.fullmatch(str(item["digest"])):
            raise _refuse(f"{item['role']} in the receipt's {key} has no sha256 digest")
        size = item["size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise _refuse(f"{item['role']} in the receipt's {key} has no positive size")
    if [item["role"] for item in matrix] != list(roles):
        raise _refuse(
            f"the receipt's {key} lists its roles out of order: "
            f"{', '.join(str(item['role']) for item in matrix)}"
        )
    for item in matrix:
        if item["media_type"] != roles[item["role"]]:
            raise _refuse(
                f"{item['role']} in the receipt's {key} has media type {item['media_type']}"
            )
    stated = receipt[f"{key}_digest"]
    if stated != _sha256(_canonical(matrix)):
        raise _refuse(f"the receipt's {key} digest differs from the matrix it lists")
    return matrix


def load_receipt(path: Path, version: str | None = None) -> dict:
    """The receipt at `path`, refused unless it agrees with itself."""

    try:
        receipt = json.loads(path.read_bytes())
    except (OSError, ValueError) as failure:
        raise _refuse(f"cannot read the receipt {path}: {failure}") from None
    if not isinstance(receipt, dict):
        raise _refuse(f"{path} is not a publication receipt")
    keys = set(receipt)
    if keys != RECEIPT_FIELDS:
        missing, unknown = sorted(RECEIPT_FIELDS - keys), sorted(keys - RECEIPT_FIELDS)
        raise _refuse(
            f"{path.name} is not a publication receipt this tool reads "
            f"(missing: {', '.join(missing) or 'none'}; unknown: {', '.join(unknown) or 'none'})"
        )
    if receipt["schema_version"] != RECEIPT_SCHEMA:
        raise _refuse(
            f"the receipt's schema_version is {receipt['schema_version']!r}, not {RECEIPT_SCHEMA}"
        )
    repository = str(receipt["repository"])
    if not GHCR_REPOSITORY.fullmatch(repository):
        raise _refuse(f"the receipt names a package outside ghcr.io: {repository}")
    if receipt["tag_is_authority"] is not False:
        raise _refuse("the receipt treats its discovery tag as authority; only digests are")
    if receipt["publisher_profile"] != "github_oidc":
        raise _refuse(f"the receipt's publisher profile is {receipt['publisher_profile']!r}")
    for flag in ("blob_readback_verified", "manifest_readback_verified", "referrer_verified"):
        if receipt[flag] is not True:
            raise _refuse(f"the publisher did not record {flag} for this release")
    if not isinstance(receipt["workflow"], dict):
        raise _refuse("the receipt's workflow is not a workflow identity")
    for manifest in ("release", "attestation"):
        if not OCI_DIGEST.fullmatch(str(receipt[f"{manifest}_manifest_digest"])):
            raise _refuse(f"the receipt's {manifest}_manifest_digest is not a sha256 digest")
        size = receipt[f"{manifest}_manifest_size_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise _refuse(f"the receipt's {manifest}_manifest_size_bytes is not a positive size")
    if version is not None and not str(receipt["discovery_tag"]).endswith(f"-{version}"):
        raise _refuse(
            f"the receipt is for {receipt['discovery_tag']}, not version {version}",
            "keep each receipt in the directory of its own version",
        )
    release = _check_matrix(receipt, "release_descriptor_matrix", RELEASE_ROLES)
    _check_matrix(receipt, "attestation_descriptor_matrix", ATTESTATION_ROLES)
    artifact_ref = receipt["artifact_ref"]
    if not isinstance(artifact_ref, dict):
        raise _refuse("the receipt's artifact_ref is not an object")
    if receipt["artifact_ref_digest"] != _sha256(_canonical(artifact_ref)):
        raise _refuse("the receipt's artifact_ref digest differs from the artifact_ref it holds")
    if artifact_ref.get("manifest_digest") != receipt["release_manifest_digest"]:
        raise _refuse("the receipt's artifact_ref names another release manifest")
    if f"{artifact_ref.get('registry')}/{artifact_ref.get('repository')}" != repository:
        raise _refuse("the receipt's artifact_ref names another package")
    config = {key: release[0][key] for key in ("digest", "media_type", "size_bytes")}
    if artifact_ref.get("config") != config or artifact_ref.get("layers") != release[1:]:
        raise _refuse("the receipt's artifact_ref layers differ from its release matrix")
    return receipt


def _checked(content: bytes, item: Mapping, what: str) -> bytes:
    if f"sha256:{_sha256(content)}" != item["digest"]:
        raise _refuse(f"{what}: the registry's bytes do not match the receipt's digest")
    if len(content) != item["size_bytes"]:
        raise _refuse(
            f"{what}: the registry's bytes are {len(content)} long, the receipt's size is "
            f"{item['size_bytes']}"
        )
    return content


def _descriptor(item: Mapping, annotated: bool) -> dict:
    value = {"digest": item["digest"], "mediaType": item["media_type"], "size": item["size_bytes"]}
    if annotated:
        value["annotations"] = {ROLE_ANNOTATION: item["role"], TITLE_ANNOTATION: item["name"]}
    return value


def _check_manifest(content: bytes, what: str, artifact_type: str, matrix: list[dict]) -> dict:
    try:
        document = json.loads(content)
    except ValueError:
        raise _refuse(f"the {what} is not JSON") from None
    if not isinstance(document, dict):
        raise _refuse(f"the {what} is not an object")
    if (document.get("mediaType"), document.get("schemaVersion")) != (OCI_MANIFEST, 2):
        raise _refuse(f"the {what} is not an OCI image manifest")
    if document.get("artifactType") != artifact_type:
        raise _refuse(f"the {what} has artifact type {document.get('artifactType')!r}")
    if document.get("config") != _descriptor(matrix[0], annotated=False):
        raise _refuse(f"the {what}'s config differs from the receipt")
    layers = document.get("layers")
    expected = [_descriptor(item, annotated=True) for item in matrix[1:]]
    if not isinstance(layers, list) or len(layers) != len(expected):
        raise _refuse(f"the {what} has another number of layers than the receipt lists")
    for layer, wanted, item in zip(layers, expected, matrix[1:], strict=True):
        if layer != wanted:
            raise _refuse(f"the {what}'s layer for {item['role']} differs from the receipt")
    return document


@dataclass(frozen=True)
class ReleaseEvidence:
    """A release read back and checked: the exact bytes of each layer, by role."""

    receipt_path: Path | None
    receipt: dict
    layers: dict[str, bytes]

    def _text(self, role: str) -> str:
        return self.layers[role].decode("utf-8")

    def _json(self, role: str) -> object:
        return json.loads(self.layers[role])

    def _digest(self, role: str) -> str:
        return _sha256(self.layers[role])

    @property
    def trading_scope(self) -> object:
        manifest = self._json("strategy_manifest")
        return manifest.get("trading_scope") if isinstance(manifest, dict) else None

    @property
    def attestation_bundle_base64(self) -> str:
        return base64.b64encode(self.layers["attestation_bundle"]).decode("ascii")

    def artifact_evidence(self, strategy_release_id: str) -> dict:
        """`artifact_evidence` for drafting the release in ARX, under the id you choose."""

        try:
            chosen = uuid.UUID(strategy_release_id)
        except ValueError:
            raise ArxError(
                f"a strategy release id is a UUID, not {strategy_release_id!r}"
            ) from None
        if chosen.int == 0:
            raise ArxError("a strategy release id must not be the nil UUID")
        return {
            "strategy_release_id": str(chosen),
            "manifest": self._json("strategy_manifest"),
            "manifest_digest": self._digest("strategy_manifest"),
            "manifest_canonical_json": self._text("strategy_manifest"),
            "release_bom_digest": self._digest("strategy_release_bom"),
            "release_bom_canonical_json": self._text("strategy_release_bom"),
            "release_statement_digest": self._digest("strategy_release_statement"),
            "release_statement_canonical_json": self._text("strategy_release_statement"),
            "release_statement": self._json("strategy_release_statement"),
            "detached_attestation_ref_digest": self._digest("artifact_attestation_ref"),
            "detached_attestation_ref_canonical_json": self._text("artifact_attestation_ref"),
            "detached_attestation_ref": self._json("artifact_attestation_ref"),
            "artifact_ref_digest": self._digest("strategy_artifact_ref"),
            "artifact_ref_canonical_json": self._text("strategy_artifact_ref"),
            "artifact_ref": self._json("strategy_artifact_ref"),
        }


def read_release(receipt: dict, registry: Registry, *, receipt_path: Path | None = None):
    """Read every manifest and blob the receipt names and check each; ReleaseEvidence."""

    release_matrix = receipt["release_descriptor_matrix"]
    attestation_matrix = receipt["attestation_descriptor_matrix"]
    manifests = {}
    for what, key in (("release manifest", "release"), ("attestation manifest", "attestation")):
        item = {
            "digest": receipt[f"{key}_manifest_digest"],
            "size_bytes": receipt[f"{key}_manifest_size_bytes"],
        }
        manifests[key] = _checked(registry.manifest(item["digest"]), item, f"the {what}")
    layers = {}
    for item in release_matrix + attestation_matrix:
        layers[item["role"]] = _checked(registry.blob(item["digest"]), item, item["role"])
    _check_manifest(manifests["release"], "release manifest", RELEASE_ARTIFACT_TYPE, release_matrix)
    attestation = _check_manifest(
        manifests["attestation"],
        "attestation manifest",
        ATTESTATION_ARTIFACT_TYPE,
        attestation_matrix,
    )
    subject = {
        "digest": receipt["release_manifest_digest"],
        "mediaType": OCI_MANIFEST,
        "size": receipt["release_manifest_size_bytes"],
    }
    if attestation.get("subject") != subject:
        raise _refuse("the attestation manifest's subject is not this release manifest")
    for role in ("strategy_manifest", "strategy_artifact_ref", "strategy_release_bom",
                 "strategy_release_statement", "artifact_attestation_ref"):  # fmt: skip
        try:
            layers[role].decode("utf-8")
            json.loads(layers[role])
        except ValueError:
            raise _refuse(f"{role} is not UTF-8 JSON") from None
    reference = json.loads(layers["artifact_attestation_ref"])
    if not isinstance(reference, dict):
        raise _refuse("the attestation reference is not an object")
    if reference.get("statement_sha256") != _sha256(layers["strategy_release_statement"]):
        raise _refuse("the attestation reference names another statement than this release's")
    if reference.get("bundle_sha256") != _sha256(layers["attestation_bundle"]):
        raise _refuse("the attestation reference names another bundle than this release's")
    return ReleaseEvidence(receipt_path=receipt_path, receipt=receipt, layers=layers)


class GhcrReader:
    """Reads one GHCR package by digest, with a token that may only pull from it."""

    def __init__(
        self,
        repository: str,
        username: str,
        token: str,
        *,
        transport: Transport | None = urllib_transport,
    ) -> None:
        if not GHCR_REPOSITORY.fullmatch(repository):
            raise _refuse(f"releases are read from ghcr.io only, not {repository}")
        self.name = repository.removeprefix("ghcr.io/")
        self._username = username
        self._token = token
        self._transport = transport or urllib_transport
        self._bearer: str | None = None

    def _fail(self, message: str, fix: str = READ_PACKAGES_FIX) -> ArxError:
        return ArxError(redact(message, [self._token, self._bearer or ""]), fix)

    def _get(self, url: str, headers: Mapping[str, str]):
        # A read by digest can be repeated safely; a connection dropped once is tried again.
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                return self._transport("GET", url, headers, None)
            except ArxError as failure:
                if attempt == READ_ATTEMPTS:
                    raise self._fail(str(failure), failure.fix) from None
        raise AssertionError("unreachable")

    def _bearer_token(self) -> str:
        if self._bearer is None:
            query = parse.urlencode({"scope": f"repository:{self.name}:pull", "service": "ghcr.io"})
            basic = base64.b64encode(f"{self._username}:{self._token}".encode()).decode("ascii")
            response = self._get(
                f"https://ghcr.io/token?{query}", {"Authorization": f"Basic {basic}"}
            )
            if response.status in (401, 403):
                raise self._fail(f"GHCR would not let this GitHub login pull ghcr.io/{self.name}")
            payload = response.json() if response.status == 200 else None
            token = payload.get("token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise self._fail(f"GHCR's token exchange answered {response.status}", "")
            self._bearer = token
        return self._bearer

    def _read(self, kind: str, digest: str, accept: str | None) -> bytes:
        headers = {"Authorization": f"Bearer {self._bearer_token()}"}
        if accept:
            headers["Accept"] = accept
        response = self._get(f"https://ghcr.io/v2/{self.name}/{kind}/{digest}", headers)
        if response.status in (401, 403, 404):
            raise self._fail(
                f"GHCR answered {response.status} for {digest} in ghcr.io/{self.name}; "
                "the package may be private to an account this login cannot read"
            )
        if response.status != 200:
            raise self._fail(f"GHCR answered {response.status} for {digest}", "")
        served = response.header("Docker-Content-Digest")
        if served and served[0] != digest:
            raise _refuse(f"GHCR served {served[0]} when asked for {digest}")
        return response.body

    def manifest(self, digest: str) -> bytes:
        return self._read("manifests", digest, OCI_MANIFEST)

    def blob(self, digest: str) -> bytes:
        return self._read("blobs", digest, None)


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), capture_output=True, text=True, check=False)


def github_credentials(run: Run = _run) -> tuple[str, str]:
    """(login, token) of the GitHub account gh is logged in to; refused without read:packages."""

    try:
        status = run(["gh", "auth", "status", "--hostname", "github.com"])
    except OSError:
        raise ArxError(
            "gh is not installed", "install the GitHub CLI, then gh auth login"
        ) from None
    if status.returncode != 0:
        raise ArxError("gh is not logged in to github.com", "gh auth login")
    listed = re.search(r"Token scopes:\s*(.*)", f"{status.stdout}\n{status.stderr}")
    # Some tokens do not list their scopes; those are tried, and GHCR says if they cannot read.
    if listed is not None:
        scopes = set(re.findall(r"[a-z:_]+", listed.group(1)))
        if not scopes & {"read:packages", "write:packages"}:
            raise ArxError(
                "the gh token cannot read packages: it lacks the read:packages scope",
                READ_PACKAGES_FIX,
            )
    token = run(["gh", "auth", "token", "--hostname", "github.com"]).stdout.strip()
    login = run(["gh", "api", "user", "--jq", ".login"]).stdout.strip()
    if not token or not login:
        raise ArxError("gh did not give a token and login for github.com", "gh auth login")
    return login, token


def find_receipt(root: Path, strategy: str, version: str) -> Path:
    receipts = sorted((root / ".releases" / strategy / version).rglob(RECEIPT_FILE))
    if not receipts:
        raise ArxError(
            f"there is no receipt for {strategy} {version} under .releases/",
            f"make release STRATEGY={strategy}",
        )
    return receipts[-1]


def strategy_version(root: Path, strategy: str) -> str:
    pyproject = root / "strategies" / strategy / "pyproject.toml"
    if not pyproject.is_file():
        raise ArxError(f"there is no strategy at strategies/{strategy}", "make next")
    version = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise ArxError(f"strategies/{strategy}/pyproject.toml has no [project] version")
    return version


def read_strategy_release(
    strategy: str,
    version: str | None = None,
    *,
    root: Path = ROOT,
    run: Run = _run,
    transport: Transport = urllib_transport,
) -> ReleaseEvidence:
    """The released version of a strategy, read back from its package and checked."""

    version = version or strategy_version(root, strategy)
    path = find_receipt(root, strategy, version)
    receipt = load_receipt(path, version=version)
    login, token = github_credentials(run)
    reader = GhcrReader(receipt["repository"], login, token, transport=transport)
    return read_release(receipt, reader, receipt_path=path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("strategy", help="<category>/<name>, the directory under strategies/")
    parser.add_argument("--version", help="defaults to the strategy's [project] version")
    args = parser.parse_args(argv)
    strategy = args.strategy.strip("/")
    try:
        found = read_strategy_release(strategy, args.version or None)
    except ArxError as failure:
        ui.error(str(failure), tag="arx")
        if failure.fix:
            ui.next_steps([(failure.fix, "")])
        return 1
    receipt = found.receipt
    scope = found.trading_scope if isinstance(found.trading_scope, dict) else {}
    ui.ok(f"{receipt['discovery_tag']} reads back as its receipt says", tag="arx")
    ui.table(
        "Release evidence",
        [
            ("package", str(receipt["repository"])),
            ("release manifest", str(receipt["release_manifest_digest"])),
            ("attestation manifest", str(receipt["attestation_manifest_digest"])),
            (
                "layers checked",
                f"{len(found.layers)}, {sum(map(len, found.layers.values()))} bytes",
            ),
            ("connector", str(scope.get("connector", ""))),
            ("pairs", ", ".join(map(str, scope.get("pairs", [])))),
            ("leverage", str(scope.get("leverage", ""))),
            ("receipt", str(found.receipt_path.relative_to(ROOT)) if found.receipt_path else ""),
        ],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
