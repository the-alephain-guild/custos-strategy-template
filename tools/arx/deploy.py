#!/usr/bin/env python3
"""Deploy a release through ARX, and stop a deployment again.

`make deploy` takes a released version of a strategy to a running instance on a
runner, in the order ARX's *Release & Deployment API* guide gives. Every step
first reads what ARX already has, so running the command again after it stopped
part-way, or after it finished, does only what is left:

1. Read the release back from its package and build the DeploymentSpec, exactly
   as `make deploy-preview` does (same function, same object).
2. The strategy definition: found by the release's strategy coordinate, created
   only when ARX has none by that name. No code.
3. The release: its id is derived from the release manifest digest. A release
   already published is used as it is, a draft is only published, and one ARX
   does not have is drafted under the next release number and published. No
   code.
4. The product named with PRODUCT=: it must exist in this mode and belong to this
   strategy. This command never creates a product; it is made in the ARX
   console once the definition exists.
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
   out; running the command again goes on watching.
9. The deployment receipt is written to
   `.deployments/<category>/<name>/<version>/<mode>-<runner>.json`, with what
   is left to do on the runner's machine.

Each step is recorded in `.progress.json` next to the receipt as it completes.

`make deploy-stop` stops the first instance a receipt names, with a fresh code,
waits until ARX lists it as stopped, and records that in the receipt.

Usage:
    python3 tools/arx/deploy.py deploy trend/my_idea --mode sandbox --runner <uuid> \\
        --product <uuid> [--version 0.2.0] [--url https://arx.example.com]
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402
from tools.arx import runner_admin  # noqa: E402
from tools.arx import session as arx_session  # noqa: E402
from tools.arx import spec as arx_spec  # noqa: E402
from tools.arx.client import ArxError, Response  # noqa: E402

DEPLOYMENTS = ".deployments"
PROGRESS_FILE = ".progress.json"
RECEIPT_VERSION = 1
# Every key this tool sends is a version 5 UUID in this namespace: the spec's
# from its request digest, the other writes' from what they write.
IDEMPOTENCY_NAMESPACE = uuid.UUID("3d8a6f12-7c4e-4b9a-a5d0-1e6f2b8c9d47")

STRATEGIES = "/api/v1/strategies"
RELEASES = "/api/v1/strategy-releases"
PRODUCTS = "/api/v1/products"
SPECS = "/api/v1/deployment-specs"
INSTANCES = "/api/v1/deployments"
LIST_LIMIT = 500
SPEC_LIST_LIMIT = 200
POLL_SECONDS = 3.0
TIMEOUT_SECONDS = 180.0
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


def ensure_definition(admin: runner_admin.Admin, name: str, progress: Progress) -> dict:
    """The strategy definition named `name`, created if ARX has none by that name."""

    known = progress.get().get("strategy_definition_id")
    if known:
        found = _read_or_none(admin, f"{STRATEGIES}/{known}", action="reading the strategy")
        if isinstance(found, dict) and found.get("name") == name:
            return found
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
        definition = named[0]
    else:
        if len(listed) >= LIST_LIMIT:
            raise ArxError(
                f"ARX lists {LIST_LIMIT} strategy definitions or more and {name} is not among "
                "the ones listed, so this command cannot tell whether it exists",
                "ask an ARX admin to look it up in the console",
            )
        tenant = admin.session().tenant_id
        definition = _object(
            admin.api.call(
                "POST",
                STRATEGIES,
                {"name": name, "reason": f"Strategy definition for {name}, made by make deploy"},
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


def check_product(admin: runner_admin.Admin, product_id: str, mode: str, definition: dict) -> dict:
    """The product, refused unless it exists in this mode and belongs to this strategy."""

    strategy_id = str(definition["strategy_id"])
    name = definition.get("name", strategy_id)
    make_one = (
        f"create a {mode} product for {name} (strategy {strategy_id}) in the ARX console, "
        "then run make deploy again with PRODUCT=<its id>"
    )
    product = _read_product(admin, product_id, mode)
    if product is None:
        for other in ("sandbox", "testnet"):
            if other != mode and _read_product(admin, product_id, other) is not None:
                raise ArxError(
                    f"product {product_id} is a {other} product, and this deployment is for {mode}",
                    make_one,
                )
        raise ArxError(f"ARX has no product {product_id} in {mode}", make_one)
    if product.get("mode") not in (None, mode):
        raise ArxError(
            f"product {product_id} is a {product.get('mode')} product, and this deployment "
            f"is for {mode}",
            make_one,
        )
    if str(product.get("strategy_id")) != strategy_id:
        raise ArxError(
            f"product {product_id} belongs to another strategy ({product.get('strategy_id')}), "
            f"not {name} ({strategy_id})",
            make_one,
        )
    if product.get("lifecycle") not in (None, "active"):
        ui.warn(
            f"product {product_id} is {product.get('lifecycle')}, not active: until it holds "
            "capital and is activated, ARX cannot value it and risk checks cannot see the "
            "instance",
            tag="arx",
        )
    return product


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
        "bindings and publish its capability again with custos publish-capability, as the "
        "runner's documentation describes.",
        "Then restart the runner: it reads its capability receipt only when it starts. The "
        "restart_required in the publication receipt refers to the runner; until it restarts, "
        "the new instance's commands wait for a binding.",
        "A runner process runs one instance at a time. Before another release is deployed "
        f"to this runner, stop this one: make deploy-stop STRATEGY={receipt['strategy']} "
        f"MODE={receipt['mode']} VERSION={receipt['version']} RUNNER={receipt['runner_id']}",
    ]


# -- deploy --------------------------------------------------------------------------------


@dataclass
class Deployment:
    path: Path
    receipt: dict
    error: str | None = None


def deploy(
    admin: runner_admin.Admin,
    strategy: str,
    *,
    mode: str,
    runner_id: str,
    product_id: str,
    version: str | None = None,
    root: Path = ROOT,
    read_release=None,
    show: Callable[[arx_spec.DeploymentPlan], None] = arx_spec.show,
    timeout: float = TIMEOUT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
) -> Deployment:
    """Take a released version to a running first instance; see the module's docstring."""

    plan, evidence = arx_spec.plan_for(
        strategy,
        mode=mode,
        runner_id=runner_id,
        product_id=product_id,
        version=version,
        root=root,
        read_release=read_release,
    )
    facts, body = plan.release, plan.body
    runner, product = body["target_runner_id"], body["strategy_product_id"]
    target = receipt_path(root, strategy, facts.version, mode, runner)
    progress = Progress(target.parent / PROGRESS_FILE, f"{mode}-{runner}")
    key = idempotency_key_for(plan.request_digest)

    definition = ensure_definition(admin, facts.coordinate, progress)
    release = ensure_release(admin, definition, facts, evidence, progress)
    check_product(admin, product, mode, definition)
    progress.update(product_id=product)

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
                admin.notify("deployed already, as the receipt records; nothing was sent")
                return Deployment(target, existing)
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
    receipt["runner_todo"] = _runner_todo(receipt)
    _write_json(target, receipt)
    progress.update(first_instance_id=receipt["first_instance_id"], state=state)
    error = watched.get("error")
    return Deployment(target, receipt, str(error) if error else None)


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
            f"make deploy STRATEGY={strategy} MODE={mode} RUNNER=<runner id> "
            "PRODUCT=<product id> deploys it",
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
            f"RUNNER={receipt.get('runner_id')} PRODUCT={receipt.get('product_id')} "
            "watches it come up",
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


def _show_receipt(result: Deployment) -> None:
    receipt = result.receipt
    try:
        where = str(result.path.relative_to(ROOT))
    except ValueError:
        where = str(result.path)
    ui.table(
        "Deployment",
        [
            ("release", f"{receipt['strategy']} {receipt['version']}"),
            ("mode", receipt["mode"]),
            ("runner", receipt["runner_id"]),
            ("product", receipt["product_id"]),
            ("spec", receipt["deployment_spec_id"]),
            ("spec digest", receipt["deployment_spec_digest"]),
            ("first instance", receipt.get("first_instance_id") or "not listed yet"),
            ("state", receipt["state"]),
            ("receipt", where),
        ],
    )


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
                    f"RUNNER={receipt['runner_id']} PRODUCT={receipt['product_id']}",
                    "watch it again; no code is asked for",
                )
            ]
        )
        return 0
    ui.ok(
        f"{strategy} {receipt['version']} deployed; its first instance is {receipt['state']}",
        tag="arx",
    )
    ui.next_steps([(step, "") for step in receipt["runner_todo"]])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    deploying = commands.add_parser("deploy")
    stopping = commands.add_parser("stop")
    for command in (deploying, stopping):
        command.add_argument("strategy")
        command.add_argument("--mode", required=True)
        command.add_argument("--version")
        command.add_argument("--url")
    deploying.add_argument("--runner", required=True)
    deploying.add_argument("--product", required=True)
    stopping.add_argument("--runner")
    args = parser.parse_args(argv)
    strategy = args.strategy.strip("/")
    store = arx_session.HostStore(arx_session.config_directory())
    try:
        admin = runner_admin.admin_for(args.url, store=store)
        if args.command == "stop":
            result = stop(
                admin,
                strategy,
                mode=args.mode,
                version=args.version or None,
                runner_id=args.runner or None,
            )
            _show_receipt(result)
            if result.receipt["state"] not in ENDED:
                ui.warn("ARX does not list the instance as stopped yet; run it again", tag="arx")
                return 1
            ui.ok(f"{strategy} {result.receipt['version']} stopped", tag="arx")
            return 0
        result = deploy(
            admin,
            strategy,
            mode=args.mode,
            runner_id=args.runner,
            product_id=args.product,
            version=args.version or None,
        )
    except ArxError as failure:
        ui.error(str(failure), tag="arx")
        if failure.fix:
            ui.next_steps([(failure.fix, "")])
        return 1
    except ui.Cancelled:
        return 130
    return _after_deploy(strategy, args.mode, result)


if __name__ == "__main__":
    sys.exit(main())
