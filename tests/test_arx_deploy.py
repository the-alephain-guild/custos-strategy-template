"""Deploying a release through ARX, and stopping it again.

A fake ARX runs in this process on 127.0.0.1. It keeps strategy definitions,
releases, products, deployment specs and their instances the way ARX's Release &
Deployment API guide describes them: releases are drafted and published without
a code, a spec is created with one fresh code and starts its first instance,
each 30-second code is taken once, a spec's idempotency key travels in its body,
the same key with the same body returns the same spec, and a second release of
a strategy in a mode that already runs one is refused with a conflict. Sessions
are written into the session file directly: signing in is tested elsewhere.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.arx import deploy as arx_deploy
from tools.arx import runner_admin
from tools.arx import session as arx_session
from tools.arx import spec as arx_spec
from tools.arx.client import ArxError

TENANT = "tenant_a"
ALICE = "11111111-1111-4111-8111-111111111111"
RUNNER = "5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b"
PRODUCT = "4d1f6a0e-2b7c-4c1e-9a53-0e8f2d6b7c10"
BINDING = "7c8d9e0f-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
SCOPE_ID = "9e0f1a2b-3c4d-4e5f-8a6b-7c8d9e0f1a2b"
OTHER_SCOPE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
SCOPE_DIGEST = "ab" * 32
STRATEGY = "trend/sma_cross"
SCOPE = {"connector": "binance_perpetual", "pairs": ["BTC-USDT", "ETH-USDT"], "leverage": 2}
POLICY = {
    "schema_version": 1,
    "trading_day": {"time_zone": "Etc/UTC", "boundary_local_time": "00:00:00"},
    "max_total_exposure": {"currency": "USDT", "amount": "10000"},
    "max_total_drawdown_ratio": "0.2",
    "max_daily_loss": {
        "kind": "ratio",
        "limit": "0.05",
        "base": "start_of_day_cash_flow_adjusted_nav",
    },
    "max_notional_leverage": "2",
    "max_single_strategy_allocation_ratio": None,
    "cooldown_seconds": 300,
}


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def code_for(step: int) -> str:
    return f"{(step * 7919 + 12345) % 1_000_000:06d}"


@dataclass
class FakeArx:
    clock: Clock
    tokens: dict[str, str] = field(default_factory=dict)
    strategies: dict[str, dict] = field(default_factory=dict)
    releases: dict[str, dict] = field(default_factory=dict)
    products: dict[str, dict] = field(default_factory=dict)
    specs: dict[str, dict] = field(default_factory=dict)
    instances: dict[str, dict] = field(default_factory=dict)
    spec_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    header_keys: dict[str, tuple[str, int, dict]] = field(default_factory=dict)
    used_steps: set[int] = field(default_factory=set)
    requests: list[dict] = field(default_factory=list)
    # (method, path prefix) -> statuses to answer with once each, before acting
    fail_before: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    # (method, path prefix) -> statuses to answer with once each, after acting
    fail_after: dict[tuple[str, str], list[int]] = field(default_factory=dict)
    # how many reads of the spec list before a new spec's first instance exists
    project_after: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)

    def posts(self, prefix: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST" and r["path"].startswith(prefix)]

    def seed_strategy(self, name: str = STRATEGY) -> str:
        strategy_id = str(uuid.uuid4())
        self.strategies[strategy_id] = {
            "strategy_id": strategy_id,
            "tenant_id": TENANT,
            "version": 1,
            "name": name,
            "description": None,
            "config": None,
        }
        return strategy_id

    def seed_release(self, release_id: str, strategy_id: str, lifecycle: str, number=1) -> None:
        self.releases[release_id] = {
            "release_id": release_id,
            "tenant_id": TENANT,
            "strategy_id": strategy_id,
            "release_number": number,
            "version": 1 if lifecycle == "draft" else 2,
            "lifecycle": lifecycle,
            "artifact_evidence": _evidence().artifact_evidence(release_id),
        }

    def seed_product(self, strategy_id: str, mode: str = "sandbox", product_id=PRODUCT) -> None:
        self.products[product_id] = {
            "tenant": TENANT,
            "mode": mode,
            "product_id": product_id,
            "strategy_id": strategy_id,
            "display_name": "Example product",
            "lifecycle": "active",
            "version": 3,
        }


def _matches(rules: dict, method: str, path: str) -> int | None:
    for (wanted, prefix), statuses in rules.items():
        if wanted == method and path.startswith(prefix) and statuses:
            return statuses.pop(0)
    return None


def _handler(fake: FakeArx):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _reply(self, status: int, body=None, *, text: str | None = None) -> None:
            if text is not None:
                payload, kind = text.encode(), "text/plain"
            else:
                payload = json.dumps(body).encode() if body is not None else b""
                kind = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            with fake.lock:
                self._route("GET")

        def do_POST(self) -> None:
            with fake.lock:
                self._route("POST")

        def _code_ok(self, body: dict) -> bool:
            step = int(fake.clock() // 30)
            if body.get("totp_code") != code_for(step) or step in fake.used_steps:
                return False
            fake.used_steps.add(step)
            return True

        def _route(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else {}
            path, _, query_text = self.path.partition("?")
            query = dict(part.split("=", 1) for part in query_text.split("&") if "=" in part)
            fake.requests.append(
                {
                    "method": method,
                    "path": path,
                    "query": query,
                    "headers": dict(self.headers),
                    "body": body,
                }  # fmt: skip
            )
            user = fake.tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "))
            if user is None:
                return self._reply(401, {"error": "invalid session"})
            if self.headers.get("X-Tenant-Id") != TENANT:
                return self._reply(403, {"code": "CROSS_TENANT_DENIED"})
            mode = body.get("trading_mode") or query.get("trading_mode") or query.get("mode")
            if mode is not None and self.headers.get("X-Trading-Mode") != mode:
                return self._reply(400, {"code": "trading_mode_mismatch"})
            failing = _matches(fake.fail_before, method, path)
            if failing is not None:
                return self._reply(failing, {"code": "dependency_unavailable"})
            status, answer = self._act(method, path, query, body)
            failing = _matches(fake.fail_after, method, path)
            if failing is not None:
                return self._reply(failing, {"code": "dependency_unavailable"})
            if status == 502:
                return self._reply(502, text="the product could not be read")
            return self._reply(status, answer)

        def _header_key(self, body: dict) -> tuple[int, dict] | None:
            key = self.headers.get("Idempotency-Key")
            if not key or uuid.UUID(key).int == 0:
                return 400, {"code": "BAD_REQUEST"}
            seen = fake.header_keys.get(key)
            if seen is not None:
                if seen[0] != json.dumps(body, sort_keys=True):
                    return 409, {"code": "idempotency_conflict"}
                return seen[1], seen[2]
            return None

        def _remember(self, body: dict, status: int, answer: dict) -> tuple[int, dict]:
            key = self.headers["Idempotency-Key"]
            fake.header_keys[key] = (json.dumps(body, sort_keys=True), status, answer)
            return status, answer

        def _act(self, method: str, path: str, query: dict, body: dict):
            parts = path.strip("/").split("/")[2:]
            if parts == ["strategies"]:
                if method == "GET":
                    return 200, list(fake.strategies.values())
                replay = self._header_key(body)
                if replay:
                    return replay
                if set(body) - {"name", "description", "config", "reason"} or not body["reason"]:
                    return 400, {"code": "invalid_request"}
                strategy_id = fake.seed_strategy(body["name"])
                return self._remember(body, 201, fake.strategies[strategy_id])
            if parts[0] == "strategies" and len(parts) == 2:
                found = fake.strategies.get(parts[1])
                return (200, found) if found else (404, {"code": "not_found"})
            if parts[0] == "strategies" and parts[2:] == ["releases"]:
                strategy = fake.strategies.get(parts[1])
                if strategy is None:
                    return 404, {"code": "not_found"}
                if method == "GET":
                    return 200, [r for r in fake.releases.values() if r["strategy_id"] == parts[1]]
                replay = self._header_key(body)
                if replay:
                    return replay
                if body.get("expected_definition_version") != strategy["version"]:
                    return 409, {"code": "version_conflict"}
                evidence = body["artifact_evidence"]
                release_id = evidence["strategy_release_id"]
                numbers = {
                    r["release_number"]
                    for r in fake.releases.values()
                    if r["strategy_id"] == parts[1]
                }
                if release_id in fake.releases or body["release_number"] in numbers:
                    return 409, {"code": "already_exists"}
                if body["release_number"] < 1 or len(evidence) != 15:
                    return 400, {"code": "invalid_request"}
                fake.releases[release_id] = {
                    "release_id": release_id,
                    "tenant_id": TENANT,
                    "strategy_id": parts[1],
                    "release_number": body["release_number"],
                    "version": 1,
                    "lifecycle": "draft",
                    "artifact_evidence": evidence,
                }
                return self._remember(body, 201, fake.releases[release_id])
            if parts[0] == "strategy-releases":
                release = fake.releases.get(parts[1])
                if release is None:
                    return 404, {"code": "not_found"}
                if method == "GET":
                    return 200, release
                replay = self._header_key(body)
                if replay:
                    return replay
                strategy = fake.strategies[release["strategy_id"]]
                if (
                    body.get("expected_definition_version") != strategy["version"]
                    or body.get("expected_release_version") != release["version"]
                ):
                    return 409, {"code": "version_conflict"}
                if release["lifecycle"] != "draft":
                    return 409, {"code": "invalid_lifecycle"}
                if not body.get("attestation_bundle_base64"):
                    return 422, {"code": "attestation_rejected"}
                release.update(lifecycle="released", version=release["version"] + 1)
                return self._remember(body, 200, release)
            if parts[0] == "products":
                product = fake.products.get(parts[1])
                if product is None or product["mode"] != query.get("mode"):
                    return 502, None
                return 200, product
            if parts == ["deployment-specs"]:
                if method == "GET":
                    listed = [s for s in fake.specs.values() if s["trading_mode"] == mode_of(query)]
                    for spec in listed:
                        self._project(spec)
                    return 200, listed
                return self._create_spec(body)
            if parts[0] == "deployments":
                instance = fake.instances.get(parts[1])
                if instance is None or instance["trading_mode"] != mode_of(query, body):
                    return 404, {"code": "not_found"}
                if method == "GET":
                    return 200, instance
                if not self._code_ok(body):
                    return 403, {"code": "GSOD_VIOLATION", "message": "totp verification failed"}
                replay = self._header_key({k: v for k, v in body.items() if k != "totp_code"})
                if replay:
                    return replay
                if body.get("expected_version") != instance["version"]:
                    return 409, {"code": "version_conflict"}
                before = instance["lifecycle_state"]
                instance.update(lifecycle_state="stopped", version=instance["version"] + 1)
                answer = {
                    "deployment_instance_id": instance["deployment_instance_id"],
                    "from_state": before,
                    "to_state": "stopped",
                    "version": instance["version"],
                }
                return self._remember(
                    {k: v for k, v in body.items() if k != "totp_code"}, 200, answer
                )
            return 404, {"code": "not_found"}

        def _create_spec(self, body: dict):
            if not self._code_ok(body):
                return 403, {"code": "GSOD_VIOLATION", "message": "totp verification failed"}
            if "Idempotency-Key" in self.headers:
                return 400, {"code": "BAD_REQUEST"}
            key = body["idempotency_key"]
            content = json.dumps(
                {k: v for k, v in body.items() if k != "totp_code"}, sort_keys=True
            )
            if key in fake.spec_keys:
                if fake.spec_keys[key][0] != content:
                    return 409, {"code": "version_conflict"}
                return 200, {"created": False, "spec": fake.specs[fake.spec_keys[key][1]]}
            release_id = body["artifact_source"]["snapshot"]["strategy_release_id"]
            release = fake.releases.get(release_id)
            if release is None or release["lifecycle"] != "released":
                return 409, {"code": "release_unavailable"}
            for other in fake.instances.values():
                if (
                    other["trading_mode"] == body["trading_mode"]
                    and other["strategy_id"] == release["strategy_id"]
                    and other["release_id"] != release_id
                    and other["lifecycle_state"] in ("running", "paused")
                ):
                    return 409, {
                        "code": "running_release_conflict",
                        "message": "another release of this strategy is running in this mode",
                        "correlation_id": "c-1",
                        "retryable": False,
                    }
            spec_id = str(uuid.uuid4())
            fake.specs[spec_id] = {
                "deployment_spec_id": spec_id,
                "tenant_id": TENANT,
                "trading_mode": body["trading_mode"],
                "spec_digest": hashlib.sha256(content.encode()).hexdigest(),
                "strategy_id": release["strategy_id"],
                "artifact_source": {
                    "kind": "strategy_release",
                    "snapshot": {"release_id": release_id, "release_version": 2},
                },
                "target_runner_id": body["target_runner_id"],
                "projection_status": "pending_projection",
                "projected_deployment_instance_id": None,
                "last_projection_error": None,
                "reads": 0,
            }
            fake.spec_keys[key] = (content, spec_id)
            return 201, {"created": True, "spec": fake.specs[spec_id]}

        def _project(self, spec: dict) -> None:
            spec["reads"] += 1
            if spec["projection_status"] != "pending_projection":
                return
            if spec["reads"] < fake.project_after:
                return
            instance_id = str(uuid.uuid4())
            fake.instances[instance_id] = {
                "deployment_instance_id": instance_id,
                "deployment_spec_id": spec["deployment_spec_id"],
                "deployment_spec_digest": spec["spec_digest"],
                "tenant_id": TENANT,
                "strategy_id": spec["strategy_id"],
                "release_id": spec["artifact_source"]["snapshot"]["release_id"],
                "trading_mode": spec["trading_mode"],
                "target_runner_id": spec["target_runner_id"],
                "lifecycle_state": "running",
                "version": 1,
            }
            spec.update(projection_status="projected", projected_deployment_instance_id=instance_id)

    return Handler


def mode_of(query: dict, body: dict | None = None) -> str | None:
    return query.get("trading_mode") or (body or {}).get("trading_mode")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(clock: Clock) -> Iterator[tuple[FakeArx, str]]:
    arx = FakeArx(clock=clock)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(arx))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield arx, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _evidence(version: str = "0.2.0", manifest: str = "cd"):
    release_manifest = "sha256:" + manifest * 32
    strategy_manifest_digest = hashlib.sha256(release_manifest.encode()).hexdigest()

    def artifact_evidence(release_id: str) -> dict:
        evidence = {f"field_{n}": n for n in range(13)}
        evidence.update(strategy_release_id=release_id, manifest_digest=strategy_manifest_digest)
        return evidence

    return SimpleNamespace(
        receipt={
            "strategy_coordinate": STRATEGY,
            "discovery_tag": f"trend-sma_cross-{version}",
            "release_manifest_digest": release_manifest,
            "producer_repository": "example/strategies",
            "producer_commit": "0123456789abcdef0123456789abcdef01234567",
        },
        trading_scope=copy.deepcopy(SCOPE),
        attestation_bundle_base64="YnVuZGxl",
        artifact_evidence=artifact_evidence,
    )


def _settings(scope_id: str = SCOPE_ID, reason: str | None = None) -> dict:
    settings = {
        "log_level": "INFO",
        "strategy_config": {},
        "nautilus_config": {},
        "risk_policy": {"version": 1, "policy": copy.deepcopy(POLICY)},
        "scheduling_policy": {"timezone": "Etc/UTC", "schedule": {}},
        "venue_source_policy": [],
        "runner_contract_requirements": {"health": {"schema_version": 1, "heartbeat": "v1"}},
        "sandbox": {
            "engine_binding_id": BINDING,
            "credential_scope": {"scope_id": scope_id, "scope_digest": SCOPE_DIGEST},
            "starting_balances": ["10000 USDT"],
        },
    }
    if reason is not None:
        settings["reason"] = reason
    return settings


def _repo(tmp_path: Path, settings: dict | None = None) -> Path:
    directory = tmp_path / "repo" / "strategies" / "trend" / "sma_cross"
    directory.mkdir(parents=True, exist_ok=True)
    config = {
        "trading": {
            "connector": {"value": SCOPE["connector"]},
            "pairs": {"value": SCOPE["pairs"]},
            "leverage": {"value": SCOPE["leverage"]},
        }
    }
    (directory / "config.yaml").write_text(yaml.safe_dump(config))
    (directory / "deploy.yaml").write_text(yaml.safe_dump(settings or _settings()))
    return tmp_path / "repo"


def _signed_in(arx: FakeArx, url: str, directory: Path, clock: Clock) -> arx_session.HostStore:
    store = arx_session.HostStore(directory)
    access = uuid.uuid4().hex
    arx.tokens[access] = ALICE
    now = clock()
    session = arx_session.Session(
        url=arx_session.base_url(url),
        email="alice@example.com",
        user_id=ALICE,
        tenant_id=TENANT,
        roles=["admin"],
        memberships=[],
        access_token=access,
        access_expires_at=now + 10_000_000,
        refresh_token=uuid.uuid4().hex,
        refresh_expires_at=now + 20_000_000,
        code_step=None,
        signed_in_at=now,
    )
    with store.transaction() as data:
        data["hosts"][session.url] = session.to_mapping()
    return store


@dataclass
class Operator:
    admin: runner_admin.Admin
    asked: list[str]
    shown: list[dict]
    root: Path


def _operator(arx, url, tmp_path, clock, *, codes=None, root=None) -> Operator:
    directory = tmp_path / "config"
    store = (
        arx_session.HostStore(directory)
        if (directory / "hosts.json").exists()
        else _signed_in(arx, url, directory, clock)
    )
    asked: list[str] = []

    def read_secret(prompt: str) -> str:
        asked.append(prompt)
        return codes.pop(0) if codes else code_for(int(clock() // 30))

    admin = runner_admin.Admin(
        api=arx_session.client(url, store=store, clock=clock),
        url=url,
        store=store,
        read_secret=read_secret,
        clock=clock,
        sleep=clock.sleep,
        notify=lambda message: None,
    )
    return Operator(admin, asked, [], root or _repo(tmp_path))


def _deploy(op: Operator, *, version="0.2.0", manifest="cd", product=PRODUCT, **options):
    return arx_deploy.deploy(
        op.admin,
        STRATEGY,
        mode="sandbox",
        runner_id=RUNNER,
        product_id=product,
        version=version,
        root=op.root,
        read_release=lambda strategy, wanted: _evidence(version, manifest),
        show=lambda plan: op.shown.append(dict(arx_spec.summary(plan))),
        **options,
    )


def _receipt_path(root: Path, version: str = "0.2.0") -> Path:
    return root / ".deployments" / "trend" / "sma_cross" / version / f"sandbox-{RUNNER}.json"


def _ready(arx: FakeArx) -> str:
    """A definition and a product for it, as an ARX console would have made them."""

    strategy_id = arx.seed_strategy()
    arx.seed_product(strategy_id)
    return strategy_id


# -- identities --------------------------------------------------------------------


def test_the_release_id_is_derived_from_the_release_manifest_digest() -> None:
    digest = "sha256:" + "cd" * 32

    derived = arx_spec.release_id_for(digest)

    assert derived == str(uuid.uuid5(arx_spec.RELEASE_ID_NAMESPACE, digest))
    assert derived == arx_spec.release_id_for(digest)
    assert derived != arx_spec.release_id_for("sha256:" + "ce" * 32)


def test_the_idempotency_key_is_derived_from_the_request_digest() -> None:
    assert arx_deploy.idempotency_key_for("ab" * 32) == str(
        uuid.uuid5(arx_deploy.IDEMPOTENCY_NAMESPACE, "ab" * 32)
    )
    assert arx_deploy.idempotency_key_for("ab" * 32) != arx_deploy.idempotency_key_for("cd" * 32)


def test_the_preview_uses_the_derived_release_id(tmp_path) -> None:
    root = _repo(tmp_path)

    plan = arx_spec.preview(
        STRATEGY,
        mode="sandbox",
        runner_id=RUNNER,
        product_id=PRODUCT,
        version="0.2.0",
        root=root,
        read_release=lambda strategy, version: _evidence(),
    )

    expected = arx_spec.release_id_for("sha256:" + "cd" * 32)
    assert plan.body["artifact_source"]["snapshot"]["strategy_release_id"] == expected
    assert dict(arx_spec.summary(plan))["release id"] == expected


# -- a first deployment -------------------------------------------------------------


def test_a_first_deployment_asks_one_code_and_writes_a_receipt(fake, tmp_path, clock) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)
    # The first run makes the definition and the release, then stops: the product
    # is made for that definition in the ARX console.
    with pytest.raises(ArxError, match="no product") as refused:
        _deploy(op)
    assert "ARX console" in refused.value.fix
    assert op.asked == []
    (strategy_id,) = arx.strategies
    arx.seed_product(strategy_id)

    result = _deploy(op)

    assert len(op.asked) == 1
    assert len(arx.posts("/api/v1/strategies")) == 2  # the definition, then the draft
    assert len(arx.posts("/api/v1/strategy-releases/")) == 1
    assert len(arx.posts("/api/v1/deployment-specs")) == 1
    (strategy_id,) = arx.strategies
    assert arx.strategies[strategy_id]["name"] == STRATEGY
    release_id = arx_spec.release_id_for("sha256:" + "cd" * 32)
    assert arx.releases[release_id]["lifecycle"] == "released"
    assert arx.releases[release_id]["release_number"] == 1

    path = _receipt_path(op.root)
    assert result.path == path
    receipt = json.loads(path.read_text())
    (spec_id,) = arx.specs
    (instance_id,) = arx.instances
    sent = arx.posts("/api/v1/deployment-specs")[0]["body"]
    assert receipt["arx_url"] == arx_session.base_url(url)
    assert receipt["tenant_id"] == TENANT
    assert receipt["operator"] == {"email": "alice@example.com", "user_id": ALICE}
    assert receipt["strategy_definition_id"] == strategy_id
    assert receipt["strategy_release_id"] == release_id
    assert receipt["release_manifest_digest"] == "sha256:" + "cd" * 32
    assert receipt["product_id"] == PRODUCT
    assert receipt["deployment_spec_id"] == spec_id
    assert receipt["deployment_spec_digest"] == arx.specs[spec_id]["spec_digest"]
    assert receipt["idempotency_key"] == sent["idempotency_key"]
    assert receipt["request_digest"] == arx_spec.request_digest(sent)
    assert receipt["idempotency_key"] == arx_deploy.idempotency_key_for(receipt["request_digest"])
    assert receipt["credential_scope"] == {"scope_id": SCOPE_ID, "scope_digest": SCOPE_DIGEST}
    assert receipt["first_instance_id"] == instance_id
    assert receipt["state"] == "running"
    assert receipt["created_at"]
    todo = " ".join(receipt["runner_todo"])
    assert instance_id in todo and "publish-capability" in todo
    assert "restart the runner" in todo and "restart_required" in todo
    assert "one instance at a time" in todo and "make deploy-stop" in todo
    assert (path.parent / ".progress.json").is_file()


def test_the_definition_found_by_name_is_used_and_not_made_again(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = _ready(arx)
    arx.seed_strategy("trend/another")
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    assert not [r for r in arx.posts("/api/v1/strategies") if r["path"] == "/api/v1/strategies"]
    release_id = arx_spec.release_id_for("sha256:" + "cd" * 32)
    assert arx.releases[release_id]["strategy_id"] == strategy_id


def test_the_release_number_follows_the_highest_one(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = _ready(arx)
    arx.seed_release(str(uuid.uuid4()), strategy_id, "released", number=4)
    arx.seed_release(str(uuid.uuid4()), strategy_id, "retired", number=7)
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    release_id = arx_spec.release_id_for("sha256:" + "cd" * 32)
    assert arx.releases[release_id]["release_number"] == 8


def test_running_it_again_asks_no_code_and_creates_nothing(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)
    writes = len([r for r in arx.requests if r["method"] == "POST"])
    before = _receipt_path(op.root).read_text()

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again)

    assert again.asked == []
    assert len([r for r in arx.requests if r["method"] == "POST"]) == writes
    assert len(arx.specs) == 1 and len(arx.instances) == 1
    assert result.receipt["deployment_spec_id"] == json.loads(before)["deployment_spec_id"]


def test_a_released_release_is_not_drafted_or_published_again(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = _ready(arx)
    release_id = arx_spec.release_id_for("sha256:" + "cd" * 32)
    arx.seed_release(release_id, strategy_id, "released")
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    assert not arx.posts(f"/api/v1/strategies/{strategy_id}/releases")
    assert not arx.posts(f"/api/v1/strategy-releases/{release_id}/release")
    assert len(op.asked) == 1


def test_a_draft_release_is_only_published(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = _ready(arx)
    release_id = arx_spec.release_id_for("sha256:" + "cd" * 32)
    arx.seed_release(release_id, strategy_id, "draft")
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    assert not arx.posts(f"/api/v1/strategies/{strategy_id}/releases")
    (published,) = arx.posts(f"/api/v1/strategy-releases/{release_id}/release")
    assert published["body"]["expected_release_version"] == 1
    assert published["body"]["attestation_bundle_base64"] == "YnVuZGxl"
    assert "totp_code" not in published["body"]
    assert arx.releases[release_id]["lifecycle"] == "released"


def test_a_release_id_held_by_another_strategy_is_refused(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    other = arx.seed_strategy("trend/another")
    release_id = arx_spec.release_id_for("sha256:" + "cd" * 32)
    arx.seed_release(release_id, other, "released")
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match="belongs to another strategy"):
        _deploy(op)
    assert op.asked == []


# -- the product -------------------------------------------------------------------


@pytest.mark.parametrize("problem", ["missing", "other mode", "other strategy"])
def test_a_product_that_does_not_fit_is_refused_before_any_code(
    fake, tmp_path, clock, problem
) -> None:
    arx, url = fake
    strategy_id = arx.seed_strategy()
    if problem == "other mode":
        arx.seed_product(strategy_id, mode="testnet")
    elif problem == "other strategy":
        arx.seed_product(arx.seed_strategy("trend/another"))
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError) as refused:
        _deploy(op)

    message = str(refused.value)
    expected = {
        "missing": "no product",
        "other mode": "a testnet product",
        "other strategy": "belongs to another strategy",
    }[problem]
    assert expected in message
    assert PRODUCT in message
    assert op.asked == []
    assert not arx.posts("/api/v1/deployment-specs")
    assert not _receipt_path(op.root).exists()


# -- the trading account --------------------------------------------------------------


def _earlier_receipt(root: Path, scope_id: str, *, state="stopped") -> None:
    path = _receipt_path(root, "0.1.0")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "strategy": STRATEGY,
                "version": "0.1.0",
                "mode": "sandbox",
                "runner_id": RUNNER,
                "strategy_release_id": arx_spec.release_id_for("sha256:" + "aa" * 32),
                "credential_scope": {"scope_id": scope_id, "scope_digest": SCOPE_DIGEST},
                "execution_channel": {
                    "channel_type": "sandbox_sim_engine",
                    "engine_binding_id": BINDING,
                },
                "state": state,
                "created_at": "2026-09-01T00:00:00+00:00",
            }
        )
    )


def test_another_account_than_the_last_deployment_is_refused(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _earlier_receipt(op.root, OTHER_SCOPE_ID)

    with pytest.raises(ArxError, match="same trading account") as refused:
        _deploy(op)

    assert OTHER_SCOPE_ID in str(refused.value) and SCOPE_ID in str(refused.value)
    assert op.asked == []
    assert not arx.posts("/api/v1/deployment-specs")


def test_the_same_account_as_the_last_deployment_is_accepted(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _earlier_receipt(op.root, SCOPE_ID)

    _deploy(op)

    assert _receipt_path(op.root).is_file()


# -- creating the spec ---------------------------------------------------------------


def test_the_summary_shown_is_the_body_sent(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    (shown,) = op.shown
    sent = arx.posts("/api/v1/deployment-specs")[0]["body"]
    assert shown["request digest"] == arx_spec.request_digest(sent)
    assert shown["release id"] == sent["artifact_source"]["snapshot"]["strategy_release_id"]
    assert shown["runner"] == sent["target_runner_id"]
    assert shown["product"] == sent["strategy_product_id"]
    assert shown["credential scope"] == sent["credential_scope"]["scope_id"]
    assert shown["reason"] == sent["reason"]
    assert sent["idempotency_key"] == arx_deploy.idempotency_key_for(shown["request digest"])


def test_a_conflict_with_a_running_release_points_at_deploy_stop(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op, version="0.1.0", manifest="aa")

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    with pytest.raises(ArxError) as refused:
        _deploy(later)

    assert "another release" in str(refused.value)
    assert f"make deploy-stop STRATEGY={STRATEGY} MODE=sandbox VERSION=0.1.0" in refused.value.fix
    assert len(later.asked) == 1
    assert not _receipt_path(op.root).exists()
    assert len(arx.specs) == 1


def test_a_wrong_code_writes_no_receipt(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock, codes=["000000", "000000", "000000"])

    with pytest.raises(ArxError, match="did not accept the authenticator code"):
        _deploy(op)

    assert len(op.asked) == 3
    assert not arx.specs
    assert not _receipt_path(op.root).exists()


def test_a_wrong_code_then_the_next_one_deploys(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock, codes=["000000"])

    _deploy(op)

    assert len(op.asked) == 2
    assert _receipt_path(op.root).is_file()


def test_other_parameters_for_a_deployed_release_are_not_sent(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)
    receipt = _receipt_path(op.root).read_text()
    _repo(tmp_path, _settings(reason="Deploy it again with another reason"))

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    with pytest.raises(ArxError, match="new idempotency key"):
        _deploy(again)

    assert again.asked == []
    assert len(arx.specs) == 1
    assert _receipt_path(op.root).read_text() == receipt


def test_a_spec_made_elsewhere_for_this_release_and_runner_is_not_doubled(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)
    _receipt_path(op.root).unlink()
    (_receipt_path(op.root).parent / ".progress.json").unlink()
    _repo(tmp_path, _settings(reason="Deploy it again with another reason"))

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    with pytest.raises(ArxError, match="already has a deployment of this release"):
        _deploy(again)

    assert again.asked == []
    assert len(arx.specs) == 1


# -- resuming --------------------------------------------------------------------------


def test_a_run_cut_short_resumes_where_it_stopped(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    arx.fail_before[("GET", "/api/v1/products/")] = [503]
    op = _operator(arx, url, tmp_path, clock)
    with pytest.raises(ArxError):
        _deploy(op)
    assert op.asked == []

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    _deploy(again)

    assert len(again.asked) == 1
    assert len(arx.strategies) == 1
    drafts = [r for r in arx.posts("/api/v1/strategies/") if r["path"].endswith("/releases")]
    assert len(drafts) == 1
    assert len(arx.posts("/api/v1/strategy-releases/")) == 1
    assert len(arx.releases) == 1
    progress = json.loads((_receipt_path(op.root).parent / ".progress.json").read_text())
    assert progress[f"sandbox-{RUNNER}"]["spec"]["deployment_spec_id"] in arx.specs


def test_a_lost_answer_to_the_spec_is_recovered_without_another_code(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    arx.fail_after[("POST", "/api/v1/deployment-specs")] = [503]
    op = _operator(arx, url, tmp_path, clock)
    with pytest.raises(ArxError):
        _deploy(op)
    assert len(op.asked) == 1 and len(arx.specs) == 1
    assert not _receipt_path(op.root).exists()

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again)

    assert again.asked == []
    assert len(arx.specs) == 1
    assert result.receipt["deployment_spec_id"] in arx.specs
    assert result.receipt["first_instance_id"] in arx.instances


def test_an_instance_not_up_in_time_is_watched_again_without_a_code(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    arx.project_after = 1000
    op = _operator(arx, url, tmp_path, clock)

    first = _deploy(op, timeout=30, poll_seconds=5)

    assert first.receipt["state"] == "pending"
    assert first.receipt["first_instance_id"] is None
    arx.project_after = 0
    again = _operator(arx, url, tmp_path, clock, root=op.root)
    second = _deploy(again)
    assert again.asked == []
    assert second.receipt["state"] == "running"
    assert second.receipt["first_instance_id"] in arx.instances


# -- stopping and changing release -------------------------------------------------------


def test_stop_then_a_new_release_deploys(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op, version="0.1.0", manifest="aa")
    old = json.loads(_receipt_path(op.root, "0.1.0").read_text())

    stopper = _operator(arx, url, tmp_path, clock, root=op.root)
    stopped = arx_deploy.stop(stopper.admin, STRATEGY, mode="sandbox", root=op.root)

    assert len(stopper.asked) == 1
    (request,) = arx.posts(f"/api/v1/deployments/{old['first_instance_id']}/stop")
    assert request["body"]["expected_version"] == 1
    assert request["body"]["trading_mode"] == "sandbox"
    assert request["headers"].get("Idempotency-Key")
    assert arx.instances[old["first_instance_id"]]["lifecycle_state"] == "stopped"
    receipt = json.loads(_receipt_path(op.root, "0.1.0").read_text())
    assert receipt["state"] == "stopped" and receipt["stopped_at"]
    assert stopped.receipt == receipt

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(later)
    assert len(later.asked) == 1
    assert result.receipt["state"] == "running"
    assert result.receipt["strategy_release_id"] != old["strategy_release_id"]


def test_stopping_an_instance_already_stopped_asks_no_code(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)
    instance = json.loads(_receipt_path(op.root).read_text())["first_instance_id"]
    arx.instances[instance]["lifecycle_state"] = "stopped"

    stopper = _operator(arx, url, tmp_path, clock, root=op.root)
    arx_deploy.stop(stopper.admin, STRATEGY, mode="sandbox", version="0.2.0", root=op.root)

    assert stopper.asked == []
    assert not arx.posts(f"/api/v1/deployments/{instance}/stop")
    assert json.loads(_receipt_path(op.root).read_text())["state"] == "stopped"


def test_stop_without_a_receipt_says_so(fake, tmp_path, clock) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match="no deployment receipt"):
        arx_deploy.stop(op.admin, STRATEGY, mode="sandbox", root=op.root)
    assert op.asked == []


def test_deployment_receipts_are_ignored_by_git() -> None:
    root = Path(__file__).resolve().parents[1]
    assert ".deployments/" in (root / ".gitignore").read_text().splitlines()
