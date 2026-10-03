#!/usr/bin/env python3
"""Deploy a release through ARX, and stop a deployment again.

`make deploy` takes a released version of a strategy to a running instance on a
runner, in the order ARX's *Release & Deployment API* guide gives. Every step
first reads what ARX already has, so running the command again after it stopped
part-way, or after it finished, does only what is left:

1. Read the release back from its package and build the DeploymentSpec, exactly
   as `make deploy-preview` does (same function, same object).
2. The strategy definition: named by the release's strategy coordinate without
   its version, so every release of the strategy goes under one definition and
   one product per mode; found by that name, created only when ARX has none by
   it. No code. A definition named with a version, as this command named them
   before, is left as it is and pointed out.
3. The release: its id is derived from the organisation and the release
   manifest digest. A release
   already published is used as it is, a draft is only published, and one ARX
   does not have is drafted under the next release number and published. No
   code.
4. The product: a strategy has one product per mode, found in ARX's product
   list by the strategy definition. If there is none, the product to create is
   shown (its name and currency from deploy.yaml) and created with one fresh
   code, under an id and key derived from the organisation, the mode and the
   definition, then the command stops: capital goes into it and it is
   activated in the ARX console, by other people. A product that is not active
   yet stops it the same way, with no code; a retired one is refused; an active
   one is deployed to. PRODUCT= names it explicitly and must be the one found.
5. The trading account: until money can move between accounts, a new release
   of a strategy keeps the account the last deployment of it in this mode used,
   as recorded in this machine's deployment receipts. ARX checks the same; this
   only saves a code.
6. Whether this spec was created already: the spec's idempotency key is derived
   from its request digest, so the same parameters always carry the same key.
   A receipt for it, or a spec ARX lists for this release and runner, means no
   code is asked for. Other parameters for a release already deployed on this
   runner are refused here rather than started as a second instance.
7. The effect point: the full summary is shown, then one fresh authenticator
   code is asked for and the spec is created, which starts its first instance.
8. The first instance is watched until ARX lists it, or until the wait runs
   out; running the command again goes on watching. Once it is listed, the
   product is read again and what it says runs must be this release. Then the
   instance is read until its runner observation says whether the runner
   started it: confirmed is a deployment, a rejected start or no answer within
   the wait ends with an error, and so does an ARX that does not report it. An
   instance listed for the first time by this run is read once and not waited
   for: its runner cannot be bound to it yet, so the command ends with exit
   status 3 and the runner-side steps, and the next run waits.
9. The deployment receipt is written to
   `.deployments/<category>/<name>/<version>/<mode>-<runner>.json`, with what
   is left to do on the runner's machine. The command ends by saying that the
   product's approved contribution is to be allocated to the new instance in
   the ARX console: until it is, ARX holds the contribution as blocked.

Each step is recorded in `.progress.json` next to the receipt as it completes.

`make deploy-preview` builds the same spec and looks the definition and the
product up without writing anything: it shows the product found, or the one
that would be created.

`make deploy-stop` stops the first instance a receipt names, with a fresh code,
waits until ARX lists it as stopped, records that in the receipt, and gives
the `make deploy` for the next release.

Usage:
    python3 tools/arx/deploy.py deploy trend/my_idea --mode sandbox --runner <uuid> \\
        [--product <uuid>] [--version 0.2.0] [--url https://arx.example.com]
    python3 tools/arx/deploy.py preview trend/my_idea --mode sandbox --runner <uuid> \\
        [--product <uuid>] [--version 0.2.0] [--url https://arx.example.com]
    python3 tools/arx/deploy.py stop trend/my_idea --mode sandbox [--version 0.2.0] \\
        [--runner <uuid>] [--url https://arx.example.com]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402
from tools.arx import observation, runner_admin  # noqa: E402
from tools.arx import session as arx_session  # noqa: E402
from tools.arx import spec as arx_spec  # noqa: E402
from tools.arx.client import ArxError, Response  # noqa: E402

DEPLOYMENTS = ".deployments"
PROGRESS_FILE = ".progress.json"
RECEIPT_VERSION = 1
# Every key this tool sends is a version 5 UUID in this namespace: the spec's
# from its request digest, the other writes' from what they write.
IDEMPOTENCY_NAMESPACE = uuid.UUID("3d8a6f12-7c4e-4b9a-a5d0-1e6f2b8c9d47")
# A product's id is chosen by the caller: a version 5 UUID in this namespace of
# the organisation, the mode and the strategy definition, one per strategy and mode.
PRODUCT_ID_NAMESPACE = uuid.UUID("8f3b2a61-5c7d-4e9f-a1b2-6d4c8e0f7a35")

STRATEGIES = "/api/v1/strategies"
RELEASES = "/api/v1/strategy-releases"
PRODUCTS = "/api/v1/products"
SPECS = "/api/v1/deployment-specs"
INSTANCES = "/api/v1/deployments"
LIST_LIMIT = 500
SPEC_LIST_LIMIT = 200
POLL_SECONDS = 3.0
# How long each wait lasts: for the first instance to be listed, then for the
# runner to say whether it started it. TIMEOUT= sets it for make deploy.
TIMEOUT_SECONDS = 180.0
# make deploy's exit status when the instance is created but its runner cannot
# have been bound to it yet: the runner-side steps come next, then make deploy
# again. 1 is a deployment that failed, 2 a command used wrongly, 130 cancelled.
EXIT_BIND_RUNNER = 3
ENDED = ("stopped", "archived")
REFUSED_PROJECTIONS = ("terminal_conflict", "rejected_policy")
MONEY_MOVEMENT_RULE = (
    "until money can move between trading accounts, a new release of a strategy must "
    "trade from the same trading account as the deployment before it"
)


def idempotency_key_for(request_digest: str) -> str:
    """The spec's idempotency key: the same request always carries the same key."""

    return str(uuid.uuid5(IDEMPOTENCY_NAMESPACE, request_digest))


def _key(*parts: str) -> str:
    return str(uuid.uuid5(IDEMPOTENCY_NAMESPACE, ":".join(parts)))


def product_id_for(tenant: str, mode: str, strategy_definition_id: str) -> str:
    """The id the strategy's product in this mode is created under."""

    return str(uuid.uuid5(PRODUCT_ID_NAMESPACE, f"{tenant}:{mode}:{strategy_definition_id}"))


def product_key_for(tenant: str, mode: str, strategy_definition_id: str) -> str:
    """The Idempotency-Key the product's creation is sent with."""

    return _key("product", tenant, mode, strategy_definition_id)


def _wrong_code(response: Response, refused: object) -> bool:
    """ARX refuses a missing or wrong code on deployment writes with 403 GSOD_VIOLATION."""

    return (
        response.status == 403
        and isinstance(refused, Mapping)
        and "GSOD_VIOLATION" in (refused.get("code"), refused.get("error"))
    )


def _now(clock: Callable[[], float]) -> str:
    return datetime.fromtimestamp(clock(), UTC).isoformat(timespec="seconds")


# -- files on this machine ---------------------------------------------------------------


def receipt_path(root: Path, strategy: str, version: str, mode: str, runner: str) -> Path:
    return root / DEPLOYMENTS / strategy / version / f"{mode}-{runner}.json"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as failure:
        raise ArxError(
            f"cannot read {path}: {failure}",
            f"move it away and run the command again: mv {path} {path}.old",
        ) from None
    if not isinstance(data, dict):
        raise ArxError(
            f"{path} is not a file this tool wrote", f"move it away: mv {path} {path}.old"
        )
    return data


def _write_json(path: Path, data: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def receipts(root: Path, strategy: str, mode: str) -> list[tuple[Path, dict]]:
    """Every deployment receipt of a strategy in one mode, of any version and runner."""

    base = root / DEPLOYMENTS / strategy
    if not base.is_dir():
        return []
    found = []
    for path in sorted(base.glob(f"*/{mode}-*.json")):
        data = _read_json(path)
        if data is not None:
            found.append((path, data))
    return found


@dataclass
class Progress:
    """`.progress.json`: what each deployment from this directory has done so far."""

    path: Path
    key: str

    def get(self) -> dict:
        entry = (_read_json(self.path) or {}).get(self.key)
        return dict(entry) if isinstance(entry, dict) else {}

    def update(self, **fields: object) -> None:
        data = _read_json(self.path) or {}
        entry = data.get(self.key) if isinstance(data.get(self.key), dict) else {}
        for name, value in fields.items():
            if value is None:
                entry.pop(name, None)
            else:
                entry[name] = value
        data[self.key] = entry
        _write_json(self.path, data)


# -- reading and writing ARX ---------------------------------------------------------------


def _list(answer: object, what: str) -> list[dict]:
    if not isinstance(answer, list):
        raise ArxError(f"ARX answered {what} with something that is not a list")
    return [entry for entry in answer if isinstance(entry, dict)]


def _object(answer: object, what: str) -> dict:
    if not isinstance(answer, dict):
        raise ArxError(f"ARX answered {what} with something that is not an object")
    return answer


def _read_or_none(admin, path: str, *, action: str, missing=(404,), **options):
    try:
        return admin.read(path, action=action, **options)
    except ArxError as failure:
        if failure.status in missing:
            return None
        raise


def find_definition(admin: runner_admin.Admin, name: str) -> dict | None:
    """The strategy definition named `name`, or None if ARX has none by that name."""

    listed = _list(
        admin.read(STRATEGIES, query={"limit": str(LIST_LIMIT)}, action="listing strategies"),
        "the strategy list",
    )
    named = [entry for entry in listed if entry.get("name") == name]
    if len(named) > 1:
        ids = ", ".join(str(entry.get("strategy_id")) for entry in named)
        raise ArxError(
            f"ARX has {len(named)} strategy definitions named {name} ({ids}); "
            "this command cannot tell which one to deploy under",
            "keep one of them in the ARX console, then run the command again",
        )
    if named:
        return named[0]
    if len(listed) >= LIST_LIMIT:
        raise ArxError(
            f"ARX lists {LIST_LIMIT} strategy definitions or more and {name} is not among "
            "the ones listed, so this command cannot tell whether it exists",
            "ask an ARX admin to look it up in the console",
        )
    return None


def legacy_definitions(admin: runner_admin.Admin, name: str) -> list[dict]:
    """Definitions named `name@<version>`: one per release, as this command once made them."""

    listed = _list(
        admin.read(STRATEGIES, query={"limit": str(LIST_LIMIT)}, action="listing strategies"),
        "the strategy list",
    )
    return [
        entry
        for entry in listed
        if arx_spec.definition_name_for(str(entry.get("name", ""))) == name
        and entry.get("name") != name
    ]


def legacy_note(name: str, legacy: list[dict]) -> str | None:
    """What to say about definitions named with a version; None if there are none."""

    if not legacy:
        return None
    listed = ", ".join(f"{entry.get('name')} ({entry.get('strategy_id')})" for entry in legacy)
    return (
        f"ARX also has strategy definitions named with a version: {listed}. Every release "
        f"now goes under {name}, with its own product per mode; those are left as they are. "
        "Once nothing runs under them, retire their releases and products in the ARX console"
    )


def ensure_definition(admin: runner_admin.Admin, name: str, progress: Progress) -> dict:
    """The strategy definition named `name`, created if ARX has none by that name."""

    known = progress.get().get("strategy_definition_id")
    if known:
        found = _read_or_none(admin, f"{STRATEGIES}/{known}", action="reading the strategy")
        if isinstance(found, dict) and found.get("name") == name:
            return found
    definition = find_definition(admin, name)
    if definition is None:
        tenant = admin.session().tenant_id
        definition = _object(
            admin.api.call(
                "POST",
                STRATEGIES,
                {
                    "name": name,
                    # ARX stores a definition's config as given and refuses one that is
                    # not an object; a release carries everything the strategy runs with.
                    "config": {},
                    "reason": f"Strategy definition for {name}, made by make deploy",
                },
                idempotency_key=_key("strategy-definition", tenant, name),
                action="creating the strategy definition",
            ),
            "the new strategy definition",
        )
        admin.notify(f"strategy definition {name} created: {definition.get('strategy_id')}")
    progress.update(strategy_definition_id=str(definition["strategy_id"]))
    return definition


def _release(admin, release_id: str) -> dict | None:
    found = _read_or_none(admin, f"{RELEASES}/{release_id}", action="reading the release")
    return _object(found, "the release") if found is not None else None


def ensure_release(
    admin: runner_admin.Admin,
    definition: dict,
    facts: arx_spec.ReleaseFacts,
    evidence,
    progress: Progress,
) -> dict:
    """The release, published: used as it is, published if a draft, drafted if absent."""

    release_id = facts.release_id
    strategy_id = str(definition["strategy_id"])
    expected = evidence.artifact_evidence(release_id)["manifest_digest"]
    release = _release(admin, release_id)
    if release is not None:
        if str(release.get("strategy_id")) != strategy_id:
            _refuse_legacy_release(admin, release, facts)
            raise ArxError(
                f"release {release_id} belongs to another strategy "
                f"({release.get('strategy_id')}), not {facts.coordinate} ({strategy_id})",
                "a release is deployed under the strategy it was published from",
            )
        recorded = (release.get("artifact_evidence") or {}).get("manifest_digest")
        if recorded not in (None, expected):
            raise ArxError(
                f"release {release_id} in ARX holds another strategy manifest than "
                f"{facts.coordinate} {facts.version}"
            )
        if release.get("lifecycle") == "retired":
            raise ArxError(
                f"release {release_id} ({facts.coordinate} {facts.version}) is retired in ARX "
                "and cannot be deployed",
                "publish a new version with make release",
            )
    else:
        release = _draft(admin, definition, facts, evidence)
        admin.notify(f"release {facts.coordinate} {facts.version} drafted: {release_id}")
    if release.get("lifecycle") == "draft":
        release = _publish(admin, definition, release, facts, evidence)
        admin.notify(f"release {facts.coordinate} {facts.version} published")
    progress.update(
        release={
            "strategy_release_id": release_id,
            "release_number": release.get("release_number"),
            "lifecycle": release.get("lifecycle"),
        }
    )
    return release


def _refuse_legacy_release(admin, release: Mapping, facts: arx_spec.ReleaseFacts) -> None:
    """Explain a release ARX holds under a definition named with this strategy's version."""

    holder = _read_or_none(
        admin, f"{STRATEGIES}/{release.get('strategy_id')}", action="reading the strategy"
    )
    held_by = str((holder or {}).get("name", ""))
    if held_by == facts.definition_name or arx_spec.definition_name_for(held_by) != (
        facts.definition_name
    ):
        return
    raise ArxError(
        f"release {facts.release_id} ({facts.version}) is held in ARX by the strategy definition "
        f"{held_by} ({release.get('strategy_id')}), named with a version as this command once "
        f"named them; every release now goes under {facts.definition_name}, and a release stays "
        "under the definition it was drafted under",
        f"publish a new version with make release and deploy that: it goes under "
        f"{facts.definition_name}. Once nothing runs under {held_by}, retire its releases and "
        "products in the ARX console",
    )


def _draft(admin, definition: dict, facts: arx_spec.ReleaseFacts, evidence) -> dict:
    strategy_id = str(definition["strategy_id"])
    for attempt in (1, 2):
        listed = _list(
            admin.read(
                f"{STRATEGIES}/{strategy_id}/releases",
                query={"limit": str(LIST_LIMIT)},
                action="listing the strategy's releases",
            ),
            "the release list",
        )
        number = max((int(entry.get("release_number") or 0) for entry in listed), default=0) + 1
        body = {
            "expected_definition_version": definition["version"],
            "release_number": number,
            "artifact_evidence": evidence.artifact_evidence(facts.release_id),
            "reason": f"Release {facts.coordinate} {facts.version}, drafted by make deploy",
        }
        try:
            return _object(
                admin.api.call(
                    "POST",
                    f"{STRATEGIES}/{strategy_id}/releases",
                    body,
                    idempotency_key=_key("release-draft", facts.release_id, str(number)),
                    action="drafting the release",
                ),
                "the drafted release",
            )
        except ArxError as failure:
            if failure.status != 409 or attempt == 2:
                raise
            # Someone drafted it, took the number or changed the definition meanwhile.
            found = _release(admin, facts.release_id)
            if found is not None:
                return found
            definition = _object(
                admin.read(f"{STRATEGIES}/{strategy_id}", action="reading the strategy"),
                "the strategy definition",
            )
    raise AssertionError("unreachable")


def _publish(admin, definition: dict, release: dict, facts, evidence) -> dict:
    body = {
        "expected_definition_version": definition["version"],
        "expected_release_version": release["version"],
        "attestation_bundle_base64": evidence.attestation_bundle_base64,
        "reason": f"Release {facts.coordinate} {facts.version}, published by make deploy",
    }
    try:
        return _object(
            admin.api.call(
                "POST",
                f"{RELEASES}/{facts.release_id}/release",
                body,
                idempotency_key=_key("release-publish", facts.release_id, str(release["version"])),
                action="publishing the release",
            ),
            "the published release",
        )
    except ArxError as failure:
        if failure.code == "attestation_rejected":
            failure.fix = (
                "ARX's trust policy for release signers did not accept this release's "
                "signature; ask an ARX admin which publisher identities it trusts"
            )
        raise


def _read_product(admin, product_id: str, mode: str) -> dict | None:
    # ARX reports an unknown product as 502 at present, as its guide says.
    found = _read_or_none(
        admin,
        f"{PRODUCTS}/{product_id}",
        action="reading the product",
        missing=(404, 502),
        query={"mode": mode},
    )
    return _object(found, "the product") if found is not None else None


def list_products(admin: runner_admin.Admin, mode: str) -> list[dict]:
    """ARX's products in one mode."""

    answer = admin.read(PRODUCTS, query={"mode": mode}, action="listing products")
    if isinstance(answer, Mapping) and isinstance(answer.get("products"), list):
        answer = answer["products"]
    return _list(answer, "the product list")


def find_product(
    admin: runner_admin.Admin, mode: str, definition: Mapping, given: str | None
) -> dict | None:
    """The strategy's product in this mode, or None if ARX has none yet.

    A strategy has at most one product per mode. PRODUCT= (`given`) only names
    it explicitly: anything other than the product found is refused, before any
    code is asked for.
    """

    strategy_id = str(definition["strategy_id"])
    name = definition.get("name", strategy_id)
    listed = list_products(admin, mode)
    mine = [entry for entry in listed if str(entry.get("strategy_id")) == strategy_id]
    if len(mine) > 1:
        ids = ", ".join(str(entry.get("product_id")) for entry in mine)
        raise ArxError(
            f"ARX lists {len(mine)} {mode} products for {name} ({ids}), and a strategy has one "
            "product per mode; this command cannot tell which one to deploy to",
            "ask an ARX admin to look at the strategy's products in the console",
        )
    product = mine[0] if mine else None
    if given is None or (product is not None and str(product.get("product_id")) == given):
        return product
    other = next((entry for entry in listed if str(entry.get("product_id")) == given), None)
    if other is not None:
        problem = (
            f"PRODUCT={given} belongs to another strategy ({other.get('strategy_id')}), "
            f"not {name} ({strategy_id})"
        )
    else:
        problem = f"ARX has no {mode} product {given}"
    if product is not None:
        raise ArxError(
            f"{problem}; {name}'s {mode} product is {product.get('product_id')}",
            f"run make deploy without PRODUCT=, or with PRODUCT={product.get('product_id')}",
        )
    raise ArxError(
        f"{problem}, and {name} has no {mode} product yet",
        "run make deploy without PRODUCT=: it creates the strategy's product",
    )


def create_product(
    admin: runner_admin.Admin, plan: arx_spec.DeploymentPlan, definition: Mapping
) -> dict:
    """Create the strategy's product in this mode with one fresh code, as shown first."""

    mode = plan.body["trading_mode"]
    strategy_id = str(definition["strategy_id"])
    name = str(definition.get("name", strategy_id))
    settings = arx_spec.check_product_settings(plan.product_settings)
    tenant = admin.session().tenant_id
    body = {
        "mode": mode,
        "product_id": product_id_for(tenant, mode, strategy_id),
        "display_name": settings["display_name"],
        "strategy_definition_id": strategy_id,
        "currency": settings["currency"],
    }
    ui.table(
        "Product to create",
        [
            ("strategy", f"{name} ({strategy_id})"),
            ("mode", mode),
            ("product id", body["product_id"]),
            ("display name", body["display_name"]),
            ("currency", body["currency"]),
        ],
    )
    admin.notify(
        f"{name} has no {mode} product yet; the code you enter now creates the one shown "
        "above. It is a draft until capital is put into it and it is activated"
    )
    try:
        answer = admin.write(
            "POST",
            PRODUCTS,
            body,
            action="creating the product",
            wrong_code=_wrong_product_code,
            idempotency_key=product_key_for(tenant, mode, strategy_id),
        )
    except ArxError as failure:
        if failure.code == runner_admin.CODE_REFUSED:
            failure.fix = (
                "check the authenticator's clock and run make deploy again; ARX answers the same "
                "way to a role that may not create products (ADMIN, OPERATOR or STRATEGIST may)"
            )
        elif failure.code == "strategy_definition_not_found":
            raise ArxError(
                f"ARX has no strategy definition {strategy_id} ({name}) in this organisation, so "
                "no product was created",
                "run make deploy again: it looks the definition up by name, and makes it if "
                "it is gone",
            ) from None
        elif failure.code == "source_unavailable":
            raise ArxError(
                f"ARX has nothing of {name} that can run in {mode} yet (no published release), "
                "so no product was created",
                "check in the ARX console that the release is published and not retired, then "
                "run make deploy again",
            ) from None
        raise
    product = _object(answer, "the created product")
    admin.notify(f"{mode} product {product.get('product_id')} created for {name}")
    return product


def _wrong_product_code(response: Response, refused: object) -> bool:
    """ARX refuses a missing or wrong code on product writes with 403 forbidden."""

    return (
        response.status == 403
        and isinstance(refused, Mapping)
        and refused.get("error") == "forbidden"
    )


def check_running_release(product: Mapping | None, release_id: str) -> tuple[str, str | None]:
    """Whether the product says this release runs: matches, differs or not yet."""

    running = (product or {}).get("running_release")
    if not isinstance(running, Mapping):
        return "not yet", None
    found = running.get("strategy_release_id")
    return ("matches" if str(found) == release_id else "differs"), (str(found) if found else None)


# -- the trading account and the spec ----------------------------------------------------------


def _account(body: Mapping) -> dict:
    return {
        "credential_scope": dict(body["credential_scope"]),
        "execution_channel": dict(body["execution_channel"]),
    }


def check_account(
    root: Path, strategy: str, mode: str, release_id: str, body: Mapping, target: Path
) -> None:
    """Refuse another trading account than the last deployment of another release used."""

    earlier = [
        data
        for path, data in receipts(root, strategy, mode)
        if path != target and data.get("strategy_release_id") != release_id
    ]
    if not earlier:
        return
    last = max(earlier, key=lambda data: str(data.get("created_at", "")))
    wanted = _account(body)
    differs = [name for name in wanted if last.get(name) != wanted[name]]
    if not differs:
        return
    before = (last.get("credential_scope") or {}).get("scope_id")
    raise ArxError(
        f"{MONEY_MOVEMENT_RULE}, so it must keep the same trading account: "
        f"{strategy} {last.get('version')} was deployed in {mode} with credential scope "
        f"{before}, and this deployment names {wanted['credential_scope']['scope_id']} "
        f"({', '.join(differs)} differ)",
        f"set {mode}.credential_scope and {mode}.engine_binding_id in deploy.yaml back to "
        f"the ones in the receipt of {last.get('version')}",
    )


def _list_specs(admin, mode: str) -> list[dict]:
    return _list(
        admin.read(
            SPECS,
            query={"trading_mode": mode, "limit": str(SPEC_LIST_LIMIT)},
            action="listing deployment specs",
        ),
        "the deployment spec list",
    )


def _instance(admin, instance_id: str, mode: str) -> dict:
    return _object(
        admin.read(
            f"{INSTANCES}/{instance_id}",
            query={"trading_mode": mode},
            action="reading the deployment instance",
        ),
        "the deployment instance",
    )


def _spec_release(spec: Mapping) -> str | None:
    snapshot = (spec.get("artifact_source") or {}).get("snapshot") or {}
    return snapshot.get("release_id") or snapshot.get("strategy_release_id")


def find_existing_spec(
    admin, *, mode: str, release_id: str, runner: str, key: str, progress: Progress, known: set
) -> dict | None:
    """A spec ARX already has for this release and runner, or None.

    The one this machine sent and never heard back about is taken as created.
    Any other that still runs is refused, so the runner is not handed a second
    instance with other parameters.
    """

    same = [
        spec
        for spec in _list_specs(admin, mode)
        if _spec_release(spec) == release_id
        and str(spec.get("target_runner_id")) == runner
        and str(spec.get("deployment_spec_id")) not in known
    ]
    sent = progress.get().get("spec_request") or {}
    if sent.get("idempotency_key") == key and len(same) == 1:
        admin.notify("ARX has the spec this machine sent before; it is not sent again")
        return same[0]
    for spec in same:
        if spec.get("projection_status") in REFUSED_PROJECTIONS:
            continue
        instance_id = spec.get("projected_deployment_instance_id")
        if instance_id and _instance(admin, str(instance_id), mode).get("lifecycle_state") in ENDED:
            continue
        raise ArxError(
            f"ARX already has a deployment of this release on runner {runner} "
            f"(spec {spec.get('deployment_spec_id')}) that this machine has no receipt for, "
            "made elsewhere or with other parameters; it is left as it is. Other parameters "
            "make a new idempotency key and a new spec, and a runner runs one instance at a "
            "time",
            "stop that instance in the ARX console first, then run make deploy again",
        )
    return None


def _create_spec(admin, plan: arx_spec.DeploymentPlan, key: str, progress: Progress, fix_stop):
    """POST the spec with one fresh code; the body is the plan's, the code added last."""

    progress.update(
        spec_request={
            "idempotency_key": key,
            "request_digest": plan.request_digest,
            "sent_at": _now(admin.clock),
        }
    )
    try:
        answer = admin.write(
            "POST",
            SPECS,
            lambda code: plan.request(totp_code=code, idempotency_key=key),
            action="creating the deployment spec",
            wrong_code=_wrong_code,
            idempotent=False,
        )
    except ArxError as failure:
        # Unanswered (unreachable, or ARX unavailable): the spec may exist, so the
        # request stays recorded and a second run looks for it before asking a code.
        unanswered = (failure.status is None and failure.code != runner_admin.CODE_REFUSED) or (
            failure.status or 0
        ) >= 500
        if not unanswered:
            progress.update(spec_request=None)
        if failure.code == runner_admin.CODE_REFUSED:
            failure.fix = "wait for your authenticator's next code, then run make deploy again"
        elif failure.code == "running_release_conflict":
            raise ArxError(
                "ARX refused the deployment: another release of this strategy is running in "
                f"{plan.body['trading_mode']}, and a strategy runs one release per mode at a "
                "time. Nothing was created",
                fix_stop(),
            ) from None
        elif failure.code == "account_scope_conflict":
            raise ArxError(
                f"ARX refused the deployment: {MONEY_MOVEMENT_RULE}. Nothing was created",
                "deploy with the credential scope and engine binding the release before used",
            ) from None
        raise
    return _object(_object(answer, "the spec creation").get("spec"), "the created spec")


# -- watching the first instance -------------------------------------------------------------


def watch_runner(
    admin, instance_id: str, mode: str, *, timeout: float, poll_seconds: float
) -> observation.Observation:
    """Read the instance until its runner has answered, or time runs out.

    Only an answer that is still awaited is read again: a confirmed or rejected
    start, an instance ARX does not want running, and an ARX that reports no
    observation at all are returned at once.
    """

    deadline = admin.clock() + timeout
    while True:
        seen = observation.read(_instance(admin, instance_id, mode))
        if seen.check != observation.AWAITING or admin.clock() >= deadline:
            return seen
        admin.sleep(poll_seconds)


def observe(admin, spec_id: str, mode: str, *, timeout: float, poll_seconds: float) -> dict:
    """Watch the spec until its first instance is listed, ARX refuses it, or time runs out."""

    deadline = admin.clock() + timeout
    while True:
        spec = next(
            (s for s in _list_specs(admin, mode) if str(s.get("deployment_spec_id")) == spec_id),
            None,
        )
        if spec is None:
            raise ArxError(f"ARX does not list deployment spec {spec_id} in {mode}")
        status = str(spec.get("projection_status"))
        if status in REFUSED_PROJECTIONS:
            return {"status": status, "instance": None, "error": spec.get("last_projection_error")}
        instance_id = spec.get("projected_deployment_instance_id")
        if instance_id:
            return {"status": status, "instance": _instance(admin, str(instance_id), mode)}
        if admin.clock() >= deadline:
            return {"status": status, "instance": None}
        admin.sleep(poll_seconds)


# What follows once an instance trades for the product: ARX books each approved
# contribution to the instances it is allocated to, and holds it until then.
UNALLOCATED = (
    "a FINANCE or ADMIN holder does it with a fresh authenticator code (docs/deploying.md, "
    "The product); until it is done the contribution stays blocked, and ARX stops updating "
    "the risk figures of this product's deployments in the mode (other products are not "
    "affected)"
)


def _allocation_step(product_id: str, instance: str) -> str:
    return (
        f"in the ARX console, allocate the approved contribution to product {product_id} "
        f"to {instance}"
    )


def _runner_todo(receipt: Mapping) -> list[str]:
    instance = receipt.get("first_instance_id")
    which = (
        f"deployment instance {instance}"
        if instance
        else "the first instance, once make deploy shows its id,"
    )
    return [
        f"On the runner's machine, add {which} (spec {receipt['deployment_spec_id']}, digest "
        f"{receipt['deployment_spec_digest']}) to runner {receipt['runner_id']}'s capability "
        "bindings and publish its capability again with arx-runner publish-capability, as the "
        "runner's documentation describes.",
        "Then restart the runner: it reads its capability receipt only when it starts. The "
        "restart_required in the publication receipt refers to the runner; until it restarts, "
        "the new instance's commands wait for a binding.",
        "A runner process runs one instance at a time, and a strategy runs one release per "
        "mode, for its one product. Before another release of it is deployed in this mode, "
        f"on this runner or another, stop this one: make deploy-stop "
        f"STRATEGY={receipt['strategy']} MODE={receipt['mode']} VERSION={receipt['version']} "
        f"RUNNER={receipt['runner_id']}",
    ]


# -- deploy --------------------------------------------------------------------------------


@dataclass
class Deployment:
    path: Path
    receipt: dict
    error: str | None = None


@dataclass(frozen=True)
class AwaitingProduct:
    """The strategy's product is not active yet: nothing was deployed, no receipt written."""

    strategy_definition_id: str
    definition_name: str
    release_id: str
    version: str
    mode: str
    runner_id: str
    product_id: str
    lifecycle: str | None
    created: bool


def deploy(
    admin: runner_admin.Admin,
    strategy: str,
    *,
    mode: str,
    runner_id: str,
    product_id: str | None,
    version: str | None = None,
    root: Path = ROOT,
    read_release=None,
    show: Callable[[arx_spec.DeploymentPlan], None] = arx_spec.show,
    timeout: float = TIMEOUT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> Deployment | AwaitingProduct:
    """Take a released version to a running first instance; see the module's docstring.

    While the strategy's product is not active it stops once the definition,
    the release and the product exist, and writes no deployment receipt; the
    product's creation is the only code asked for then.
    """

    plan, evidence = arx_spec.plan_for(
        strategy,
        mode=mode,
        runner_id=runner_id,
        product_id=product_id,
        tenant=admin.session().tenant_id,
        version=version,
        root=root,
        read_release=read_release,
    )
    facts, body = plan.release, plan.body
    runner, product = body["target_runner_id"], body["strategy_product_id"]
    target = receipt_path(root, strategy, facts.version, mode, runner)
    progress = Progress(target.parent / PROGRESS_FILE, f"{mode}-{runner}")

    definition = ensure_definition(admin, facts.definition_name, progress)
    note = legacy_note(facts.definition_name, legacy_definitions(admin, facts.definition_name))
    if note:
        admin.notify(note)
    release = ensure_release(admin, definition, facts, evidence, progress)
    found = find_product(admin, mode, definition, product)
    created = found is None
    if created:
        found = create_product(admin, plan, definition)
    product = str(found["product_id"])
    progress.update(product_id=product)
    lifecycle = found.get("lifecycle")
    if lifecycle == "retired":
        raise ArxError(
            f"{definition.get('name')}'s {mode} product {product} is retired, and a strategy "
            "has one product per mode, so it cannot be deployed in this mode again",
            "ask an ARX admin; a release under another strategy name gets a new product",
        )
    if created or lifecycle != "active":
        return AwaitingProduct(
            strategy_definition_id=str(definition["strategy_id"]),
            definition_name=str(definition.get("name", facts.definition_name)),
            release_id=facts.release_id,
            version=facts.version,
            mode=mode,
            runner_id=runner,
            product_id=product,
            lifecycle=lifecycle,
            created=created,
        )
    plan = plan.with_product(product)
    body = plan.body
    key = idempotency_key_for(plan.request_digest)

    existing = _read_json(target)
    if existing is not None:
        if existing.get("request_digest") == plan.request_digest:
            if existing.get("state") in ENDED:
                raise ArxError(
                    f"this exact deployment was made before and its instance is "
                    f"{existing.get('state')}; ARX answers the same spec again for the same "
                    "parameters, and that does not start it again",
                    "start a further instance of it in the ARX console, or change deploy.yaml "
                    "(its reason, for one) to create a new spec",
                )
            if existing.get("first_instance_id"):
                admin.notify(
                    "deployed already, as the receipt records; nothing is sent, and what the "
                    "runner says about it is read again"
                )
            return _watch(admin, existing, target, progress, timeout, poll_seconds)
        instance_id = existing.get("first_instance_id")
        state = (
            _instance(admin, str(instance_id), mode).get("lifecycle_state")
            if instance_id
            else existing.get("state")
        )
        if state not in ENDED:
            raise ArxError(
                f"{strategy} {facts.version} is deployed on runner {runner} in {mode} with "
                f"other parameters already (spec {existing.get('deployment_spec_id')}); it is "
                "left as it is. Other parameters make a new idempotency key and a new spec, "
                "and a runner runs one instance at a time",
                f"make deploy-stop STRATEGY={strategy} MODE={mode} VERSION={facts.version} "
                f"RUNNER={runner}, then run make deploy again",
            )
        # Kept beside the new one under its spec id, so its history is not lost.
        target.rename(target.with_name(f"{target.stem}.{existing.get('deployment_spec_id')}.json"))

    check_account(root, strategy, mode, facts.release_id, body, target)
    known = {str(data.get("deployment_spec_id")) for _, data in receipts(root, strategy, mode)}
    spec = find_existing_spec(
        admin,
        mode=mode,
        release_id=facts.release_id,
        runner=runner,
        key=key,
        progress=progress,
        known=known,
    )
    if spec is None:
        spec = _effect_point(admin, plan, key, progress, show, root, strategy)
    session = admin.session()
    receipt = {
        "receipt_version": RECEIPT_VERSION,
        "arx_url": admin.url,
        "tenant_id": session.tenant_id,
        "operator": {"email": session.email, "user_id": session.user_id},
        "strategy": strategy,
        "version": facts.version,
        "mode": mode,
        "runner_id": runner,
        "strategy_definition_id": str(definition["strategy_id"]),
        "strategy_release_id": facts.release_id,
        "release_number": release.get("release_number"),
        "release_manifest_digest": facts.manifest_digest,
        "product_id": product,
        "product_origin_artifact_source": found.get("origin_artifact_source"),
        "running_release": None,
        "running_release_check": "not yet",
        "deployment_spec_id": str(spec["deployment_spec_id"]),
        "deployment_spec_digest": str(spec.get("spec_digest")),
        "idempotency_key": key,
        "request_digest": plan.request_digest,
        **_account(body),
        "first_instance_id": None,
        "projection_status": spec.get("projection_status"),
        "state": "pending",
        "created_at": _now(admin.clock),
        "updated_at": _now(admin.clock),
        "stopped_at": None,
    }
    receipt["runner_todo"] = _runner_todo(receipt)
    _write_json(target, receipt)
    progress.update(
        spec={
            "deployment_spec_id": receipt["deployment_spec_id"],
            "deployment_spec_digest": receipt["deployment_spec_digest"],
            "idempotency_key": key,
        },
        spec_request=None,
    )
    return _watch(admin, receipt, target, progress, timeout, poll_seconds)


def _effect_point(admin, plan, key, progress, show, root, strategy) -> dict:
    """Show the spec, ask one code, create it: the step that starts trading."""

    mode = plan.body["trading_mode"]
    running = [
        data
        for _, data in receipts(root, strategy, mode)
        if data.get("strategy_release_id") != plan.release.release_id
        and data.get("state") not in ENDED
    ]
    for data in running:
        ui.warn(
            f"the receipt of {strategy} {data.get('version')} says it may still run in {mode}; "
            "ARX refuses a second release of a strategy in one mode",
            tag="arx",
        )

    def fix_stop() -> str:
        if running:
            last = running[-1]
            return (
                f"make deploy-stop STRATEGY={strategy} MODE={mode} VERSION={last.get('version')} "
                f"RUNNER={last.get('runner_id')}, then run make deploy again"
            )
        return (
            f"make deploy-stop STRATEGY={strategy} MODE={mode} VERSION=<the version running> "
            "on the machine that deployed it, or stop it in the ARX console; then run make "
            "deploy again"
        )

    show(plan)
    admin.notify(
        f"idempotency key {key}: the same parameters always send this key, so running the "
        "command again never creates a second spec"
    )
    admin.notify(
        "creating this spec starts its first instance on the runner; the code you enter now "
        "confirms exactly what is shown above"
    )
    return _create_spec(admin, plan, key, progress, fix_stop)


def _watch(admin, receipt: dict, target: Path, progress, timeout, poll_seconds) -> Deployment:
    # ARX offers no read of a runner's capability bindings, so whether the runner
    # can be bound to the instance is judged by this run: an instance it lists
    # for the first time is one whose id the runner's operator has not had.
    listed_before = bool(receipt.get("first_instance_id"))
    watched = observe(
        admin,
        receipt["deployment_spec_id"],
        receipt["mode"],
        timeout=timeout,
        poll_seconds=poll_seconds,
    )
    instance = watched["instance"]
    if watched["status"] in REFUSED_PROJECTIONS:
        state = "refused"
    elif instance is not None:
        state = str(instance.get("lifecycle_state"))
    else:
        state = "pending"
    receipt.update(
        projection_status=watched["status"],
        first_instance_id=str(instance["deployment_instance_id"]) if instance else None,
        state=state,
        updated_at=_now(admin.clock),
    )
    error = watched.get("error")
    if instance is not None:
        # What the product says runs now must be the release just deployed.
        product = _read_product(admin, str(receipt["product_id"]), str(receipt["mode"]))
        check, found = check_running_release(product, str(receipt["strategy_release_id"]))
        receipt.update(
            running_release=(product or {}).get("running_release"), running_release_check=check
        )
        if check == "differs":
            error = (
                f"product {receipt['product_id']} says release {found} runs, not "
                f"{receipt['strategy_release_id']}, the one just deployed"
            )
        seen = watch_runner(
            admin,
            str(receipt["first_instance_id"]),
            str(receipt["mode"]),
            timeout=timeout if listed_before else 0,
            poll_seconds=poll_seconds,
        )
        receipt.update(
            state=seen.lifecycle_state or state,
            runner_observation=dict(seen.raw) if seen.raw is not None else None,
            runner_check=seen.check,
            runner_checked_at=_now(admin.clock),
            runner_waited=listed_before,
            runner_check_basis=(
                f"instance listed before this run: read for up to {timeout:g} seconds"
                if listed_before
                else "instance first listed by this run, so its runner cannot have been bound "
                "to it yet: read once, not waited for"
            ),
        )
        if error is None and seen.check != observation.CONFIRMED:
            error = observation.describe(seen, str(receipt["runner_id"]))
    receipt["runner_todo"] = _runner_todo(receipt)
    _write_json(target, receipt)
    progress.update(first_instance_id=receipt["first_instance_id"], state=receipt["state"])
    return Deployment(target, receipt, str(error) if error else None)


# -- preview -------------------------------------------------------------------------------


def preview(
    admin: runner_admin.Admin,
    strategy: str,
    *,
    mode: str,
    runner_id: str,
    product_id: str | None,
    tenant: str,
    version: str | None = None,
    root: Path = ROOT,
    read_release=None,
) -> arx_spec.DeploymentPlan:
    """The spec `make deploy` would send, with the product it would deploy to.

    The definition and the product are looked up, never created: only reads are
    sent, and no code is asked for.
    """

    plan = arx_spec.preview(
        strategy,
        mode=mode,
        runner_id=runner_id,
        product_id=product_id,
        tenant=tenant,
        version=version,
        root=root,
        read_release=read_release,
    )
    name = plan.release.definition_name
    note = legacy_note(name, legacy_definitions(admin, name))
    if note:
        plan = replace(plan, warnings=[*plan.warnings, note])
    definition = find_definition(admin, name)
    if definition is None:
        if product_id is not None:
            raise ArxError(
                f"ARX has no strategy definition {name} yet, so PRODUCT={product_id} cannot be "
                "its product",
                "preview without PRODUCT=: make deploy creates the definition and its product",
            )
        arx_spec.check_product_settings(plan.product_settings)
        return replace(
            plan,
            product_note=(
                f"will be created: make deploy first creates the strategy definition {name}, "
                f"then its {mode} product; this spec cannot be sent until then"
            ),
        )
    found = find_product(admin, mode, definition, product_id)
    strategy_id = str(definition["strategy_id"])
    if found is None:
        settings = arx_spec.check_product_settings(plan.product_settings)
        return plan.with_product(
            product_id_for(tenant, mode, strategy_id),
            f"will be created by make deploy as {settings['display_name']!r} in "
            f"{settings['currency']}, then needs capital and activating",
        )
    lifecycle = found.get("lifecycle")
    note = {
        "active": "found: active",
        "retired": "found: retired, so make deploy refuses this mode",
    }.get(lifecycle, f"found: {lifecycle or 'state not given'}, needs capital and activating")
    return plan.with_product(str(found["product_id"]), note)


def _after_preview(plan: arx_spec.DeploymentPlan) -> int:
    arx_spec.show(plan)
    ui.info(
        "nothing was written to ARX and no code was asked for: this is the spec make deploy "
        "would create, and the code entered for it confirms exactly these values",
        tag="arx",
    )
    return 0


# -- stop ------------------------------------------------------------------------------------


def _chosen_receipt(root, strategy, mode, version, runner_id) -> tuple[Path, dict]:
    found = [
        (path, data)
        for path, data in receipts(root, strategy, mode)
        if path.stem == f"{mode}-{data.get('runner_id')}"
        and (version is None or data.get("version") == version)
        and (runner_id is None or data.get("runner_id") == runner_id)
    ]
    if not found:
        named = f" {version}" if version else ""
        raise ArxError(
            f"there is no deployment receipt for {strategy}{named} in {mode} on this machine",
            f"make deploy STRATEGY={strategy} MODE={mode} RUNNER=<runner id> deploys it",
        )
    if len(found) > 1:
        found = [item for item in found if item[1].get("state") not in ENDED] or found
    if len(found) > 1:
        names = ", ".join(f"{d.get('version')} on {d.get('runner_id')}" for _, d in found)
        raise ArxError(
            f"{strategy} has several deployments in {mode}: {names}",
            "name one with VERSION= and RUNNER=",
        )
    return found[0]


def stop(
    admin: runner_admin.Admin,
    strategy: str,
    *,
    mode: str,
    version: str | None = None,
    runner_id: str | None = None,
    root: Path = ROOT,
    timeout: float = TIMEOUT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> Deployment:
    """Stop the first instance a deployment receipt names, and record it there."""

    path, receipt = _chosen_receipt(root, strategy, mode, version, runner_id)
    instance_id = receipt.get("first_instance_id")
    if not instance_id:
        raise ArxError(
            f"the receipt of {strategy} {receipt.get('version')} names no instance yet",
            f"make deploy STRATEGY={strategy} MODE={mode} VERSION={receipt.get('version')} "
            f"RUNNER={receipt.get('runner_id')} watches it come up",
        )
    instance = _instance(admin, str(instance_id), mode)
    if instance.get("lifecycle_state") not in ENDED:
        admin.notify(
            f"stopping instance {instance_id} of {strategy} {receipt.get('version')} in {mode}"
        )
        admin.write(
            "POST",
            f"{INSTANCES}/{instance_id}/stop",
            {
                "trading_mode": mode,
                "expected_version": instance["version"],
                "reason": f"Stop {strategy} {receipt.get('version')} with make deploy-stop",
            },
            action="stopping the instance",
            wrong_code=_wrong_code,
            idempotency_key=_key("stop", str(instance_id), str(instance["version"])),
        )
        deadline = admin.clock() + timeout
        while True:
            instance = _instance(admin, str(instance_id), mode)
            if instance.get("lifecycle_state") in ENDED or admin.clock() >= deadline:
                break
            admin.sleep(poll_seconds)
    state = str(instance.get("lifecycle_state"))
    receipt.update(state=state if state in ENDED else "stopping", updated_at=_now(admin.clock))
    if state in ENDED and not receipt.get("stopped_at"):
        receipt["stopped_at"] = _now(admin.clock)
    _write_json(path, receipt)
    return Deployment(path, receipt)


# -- the command -------------------------------------------------------------------------------


def _seen(receipt: Mapping) -> observation.Observation:
    """The runner observation the receipt recorded when it was last read."""

    return observation.Observation(
        str(receipt.get("runner_check")), receipt.get("state"), receipt.get("runner_observation")
    )


def _show_receipt(result: Deployment) -> None:
    receipt = result.receipt
    try:
        where = str(result.path.relative_to(ROOT))
    except ValueError:
        where = str(result.path)
    rows = [
        ("release", f"{receipt['strategy']} {receipt['version']}"),
        ("mode", receipt["mode"]),
        ("runner", receipt["runner_id"]),
        ("product", receipt["product_id"]),
        ("spec", receipt["deployment_spec_id"]),
        ("spec digest", receipt["deployment_spec_digest"]),
        ("first instance", receipt.get("first_instance_id") or "not listed yet"),
        ("state", receipt["state"]),
    ]
    if receipt.get("runner_check"):
        rows.append(
            (
                "runner said",
                f"{observation.describe(_seen(receipt))} (read {receipt.get('runner_checked_at')})",
            )
        )
    ui.table("Deployment", [*rows, ("receipt", where)])


def _after_deploy(strategy: str, mode: str, result: Deployment) -> int:
    receipt = result.receipt
    _show_receipt(result)
    if receipt["state"] == "refused":
        ui.error(
            f"ARX recorded the spec but did not start it: {result.error or 'no reason given'}",
            tag="arx",
        )
        ui.next_steps(
            [
                (
                    "stop the release running in this mode, then change deploy.yaml (its "
                    "reason, for one) and run make deploy again",
                    "the same parameters would return this same refused spec",
                )
            ]
        )
        return 1
    if receipt["state"] == "pending":
        ui.warn("ARX does not list the first instance yet", tag="arx")
        ui.next_steps(
            [
                (
                    f"make deploy STRATEGY={strategy} MODE={mode} VERSION={receipt['version']} "
                    f"RUNNER={receipt['runner_id']}",
                    "watch it again; no code is asked for",
                )
            ]
        )
        return 0
    if receipt.get("running_release_check") == "differs":
        ui.error(f"the deployment does not show as running: {result.error}", tag="arx")
        ui.next_steps(
            [
                (
                    f"look at product {receipt['product_id']} in the ARX console",
                    "it should list this release as the one running in this mode",
                )
            ]
        )
        return 1
    if receipt.get("runner_check") != observation.CONFIRMED:
        return _runner_not_confirmed(strategy, mode, receipt)
    ui.ok(
        f"{strategy} {receipt['version']} deployed; "
        f"{observation.describe(_seen(receipt), receipt['runner_id'])}",
        tag="arx",
    )
    allocate = _allocation_step(
        str(receipt["product_id"]), f"instance {receipt['first_instance_id']}"
    )
    ui.next_steps(
        [(step, "") for step in receipt["runner_todo"]]
        + [(allocate, f"unless it is allocated already: {UNALLOCATED}")]
    )
    return 0


def _runner_not_confirmed(strategy: str, mode: str, receipt: Mapping) -> int:
    """Say why the runner is not known to run the instance, and what to do.

    EXIT_BIND_RUNNER when the instance is new and its runner has not answered:
    the runner-side steps are what comes next. 1 for every other case.
    """

    seen = _seen(receipt)
    instance = receipt["first_instance_id"]
    runner = receipt["runner_id"]
    again = (
        f"make deploy STRATEGY={strategy} MODE={mode} VERSION={receipt['version']} RUNNER={runner}"
    )
    stop = (
        f"make deploy-stop STRATEGY={strategy} MODE={mode} VERSION={receipt['version']} "
        f"RUNNER={runner}"
    )
    if seen.check == observation.AWAITING and receipt.get("runner_waited") is False:
        ui.warn(
            f"ARX created instance {instance}; runner {runner} answers its start only once it "
            "is bound to it, which is done on the runner's machine now",
            tag="arx",
        )
        ui.next_steps(
            [(step, "") for step in receipt["runner_todo"][:2]]
            + [
                (
                    again,
                    f"then confirm that the runner started it; no code is asked for (exit "
                    f"status {EXIT_BIND_RUNNER} said these steps are due)",
                )
            ]
        )
        return EXIT_BIND_RUNNER
    if seen.check == observation.REJECTED:
        ui.error(
            f"ARX created instance {instance}, but runner {runner} rejected its start at "
            f"{seen.observed_at}: outcome {seen.outcome}, event {seen.event_id}",
            tag="arx",
        )
        steps = [
            (
                f"on the runner's machine, look at the runner's log around {seen.observed_at} "
                f"for event {seen.event_id}",
                observation.OUTCOMES.get(str(seen.outcome), "the runner did not start it"),
            ),
            (
                stop,
                "a rejected start holds for this instance; stop it, put right what the runner "
                "refused, then deploy again with a new spec (change deploy.yaml's reason, for one)",
            ),
        ]
    elif seen.check == observation.AWAITING:
        ui.error(
            f"runner {runner} has not said whether it started instance {instance} within "
            "the wait; it is not known to run",
            tag="arx",
        )
        steps = [(step, "") for step in receipt["runner_todo"][:2]] + [
            (
                "check that the runner is running and connected to ARX",
                "it answers the start command only once it is bound to the instance",
            ),
            (again, "then read what the runner says again; no code is asked for"),
        ]
    elif seen.check == observation.NOT_RUNNING:
        ui.error(
            f"ARX lists instance {instance} as {seen.lifecycle_state}, so no runner is asked to "
            "run it",
            tag="arx",
        )
        steps = [(f"look at instance {instance} in the ARX console", "")]
    elif seen.check == observation.UNSUPPORTED:
        ui.error(
            f"ARX created instance {instance}, but does not report whether runner {runner} "
            "started it",
            tag="arx",
        )
        steps = [
            (observation.UNSUPPORTED_FIX, ""),
            (again, "once it does, this reads what the runner says; no code is asked for"),
        ]
    else:
        ui.error(f"instance {instance}: {observation.describe(seen, runner)}", tag="arx")
        steps = [
            (f"look at instance {instance} in the ARX console", ""),
            (
                "update this repository's tools (docs/upgrading.md)",
                "ARX may be newer than they are",
            ),
        ]
    ui.next_steps(steps)
    return 1


def _awaiting_product(strategy: str, mode: str, result: AwaitingProduct) -> int:
    state = "created, as a draft" if result.created else f"is {result.lifecycle or 'not active'}"
    ui.ok(
        f"{strategy} {result.version}: ARX has the strategy definition "
        f"{result.definition_name} ({result.strategy_definition_id}) and the release "
        f"{result.release_id}; its {mode} product {result.product_id} {state}. Nothing was "
        "deployed and no deployment receipt was written",
        tag="arx",
    )
    ui.next_steps(
        [
            (
                f"in the ARX console, ask to put capital into product {result.product_id}, "
                "have another person approve it, then activate the product",
                "each with a fresh authenticator code (docs/deploying.md, The product)",
            ),
            (
                f"make deploy STRATEGY={strategy} MODE={mode} VERSION={result.version} "
                f"RUNNER={result.runner_id}",
                "then deploy it to that product, with one authenticator code",
            ),
            (
                _allocation_step(result.product_id, "the first instance make deploy lists"),
                f"once that instance is listed: {UNALLOCATED}",
            ),
        ]
    )
    return 0


def _after_stop(strategy: str, mode: str, result: Deployment) -> int:
    receipt = result.receipt
    _show_receipt(result)
    if receipt["state"] not in ENDED:
        ui.warn("ARX does not list the instance as stopped yet; run it again", tag="arx")
        ui.next_steps(
            [
                (
                    f"make deploy-stop STRATEGY={strategy} MODE={mode} "
                    f"VERSION={receipt['version']} RUNNER={receipt['runner_id']}",
                    "see whether it has stopped; no code is asked for again",
                )
            ]
        )
        return 1
    ui.ok(f"{strategy} {receipt['version']} stopped", tag="arx")
    ui.next_steps(
        [
            (
                f"make deploy STRATEGY={strategy} MODE={mode} RUNNER={receipt['runner_id']}",
                "deploy the next release of it; it goes to the same product",
            ),
            (f"make next STRATEGY={strategy} MODE={mode}", "or see where it stands"),
        ]
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    deploying = commands.add_parser("deploy")
    previewing = commands.add_parser("preview")
    stopping = commands.add_parser("stop")
    for command in (deploying, previewing, stopping):
        command.add_argument("strategy")
        command.add_argument("--mode", required=True)
        command.add_argument("--version")
        command.add_argument("--url")
    for command in (deploying, previewing):
        command.add_argument("--runner", required=True)
        command.add_argument("--product")
    deploying.add_argument("--timeout", type=float, default=TIMEOUT_SECONDS)
    stopping.add_argument("--runner")
    args = parser.parse_args(argv)
    strategy = args.strategy.strip("/")
    store = arx_session.HostStore(arx_session.config_directory())
    try:
        if args.command == "preview":
            tenant = arx_spec.session_tenant(store, args.url or None)
            admin = runner_admin.admin_for(args.url, store=store)
            return _after_preview(
                preview(
                    admin,
                    strategy,
                    mode=args.mode,
                    runner_id=args.runner,
                    product_id=args.product or None,
                    tenant=tenant,
                    version=args.version or None,
                )
            )
        admin = runner_admin.admin_for(args.url, store=store)
        if args.command == "stop":
            result = stop(
                admin,
                strategy,
                mode=args.mode,
                version=args.version or None,
                runner_id=args.runner or None,
            )
            return _after_stop(strategy, args.mode, result)
        result = deploy(
            admin,
            strategy,
            mode=args.mode,
            runner_id=args.runner,
            product_id=args.product or None,
            version=args.version or None,
            timeout=args.timeout,
        )
    except ArxError as failure:
        ui.error(str(failure), tag="arx")
        if failure.fix:
            ui.next_steps([(failure.fix, "")])
        return 1
    except ui.Cancelled:
        return 130
    if isinstance(result, AwaitingProduct):
        return _awaiting_product(strategy, args.mode, result)
    return _after_deploy(strategy, args.mode, result)


if __name__ == "__main__":
    sys.exit(main())
