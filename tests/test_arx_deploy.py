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
# What a release receipt names the strategy by: its coordinate, which ends in the
# version. ARX's definition is named by the coordinate without the version.
COORDINATE = "strategy://github.com/example/strategies/trend/sma_cross"
DEFINITION = COORDINATE
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
    # what a product read says runs, instead of what the instances say
    running_release_override: dict | None = None
    # (status, body) a product creation is refused with, after its code is taken
    refuse_product: tuple[int, dict] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def posts(self, prefix: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == "POST" and r["path"].startswith(prefix)]

    def seed_strategy(self, name: str = DEFINITION) -> str:
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

    def seed_product(
        self, strategy_id: str, mode: str = "sandbox", product_id=PRODUCT, lifecycle="active"
    ) -> None:
        self.products[product_id] = {
            "tenant": TENANT,
            "mode": mode,
            "product_id": product_id,
            "strategy_id": strategy_id,
            "display_name": "Example product",
            "currency": "USDT",
            "origin_artifact_source": None,
            "lifecycle": lifecycle,
            "version": 3,
        }

    def fund_and_activate(self, product_id: str) -> None:
        """What the ARX console does: capital in, approved, then activated."""

        self.products[product_id].update(lifecycle="active", total_shares="1000")

    def running_release(self, product: dict) -> dict | None:
        running = [
            i
            for i in self.instances.values()
            if i["strategy_id"] == product["strategy_id"]
            and i["trading_mode"] == product["mode"]
            and i["lifecycle_state"] in ("running", "paused")
        ]
        if not running or self.running_release_override is not None:
            return self.running_release_override or None
        return {
            "artifact_source_kind": "strategy_release",
            "artifact_source_digest": "sha256:" + "ee" * 32,
            "strategy_release_id": running[0]["release_id"],
            "running_instance_count": len(running),
            "updated_at": "2027-01-15T08:00:00Z",
        }

    def shown_product(self, product: dict) -> dict:
        return {**product, "running_release": self.running_release(product)}


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
                # ARX stores the definition's config and refuses one that is not an object.
                if not isinstance(body.get("config"), dict):
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
            if parts == ["products"]:
                if method == "GET":
                    listed = [p for p in fake.products.values() if p["mode"] == query.get("mode")]
                    return 200, {
                        "tenant": TENANT,
                        "mode": query.get("mode"),
                        "products": [fake.shown_product(p) for p in listed],
                    }
                return self._create_product(body)
            if parts[0] == "products":
                product = fake.products.get(parts[1])
                if product is None or product["mode"] != query.get("mode"):
                    return 502, None
                return 200, fake.shown_product(product)
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

        def _create_product(self, body: dict):
            if not self._code_ok(body):
                return 403, {"error": "forbidden", "message": "step-up code required"}
            if fake.refuse_product is not None:
                return fake.refuse_product
            fields = {"mode", "product_id", "display_name", "strategy_definition_id", "currency"}
            if set(body) != fields | {"totp_code"}:
                return 400, {"error": "invalid_request", "message": "unknown or missing field"}
            replay = self._header_key({k: v for k, v in body.items() if k != "totp_code"})
            if replay:
                return replay
            strategy_id = body["strategy_definition_id"]
            if strategy_id not in fake.strategies:
                return 404, {
                    "code": "strategy_definition_not_found",
                    "message": "no such strategy definition",
                    "correlation_id": "c-2",
                    "retryable": False,
                }
            released = [
                r
                for r in fake.releases.values()
                if r["strategy_id"] == strategy_id and r["lifecycle"] == "released"
            ]
            if not released:
                return 409, {
                    "code": "source_unavailable",
                    "message": "nothing can run in this mode",
                    "correlation_id": "c-3",
                    "retryable": False,
                }
            for other in fake.products.values():
                if other["strategy_id"] == strategy_id and other["mode"] == body["mode"]:
                    return 409, {"code": "conflict", "message": "one product per strategy"}
            fake.products[body["product_id"]] = {
                "tenant": TENANT,
                "mode": body["mode"],
                "product_id": body["product_id"],
                "strategy_id": strategy_id,
                "display_name": body["display_name"],
                "currency": body["currency"],
                "origin_artifact_source": {
                    "kind": "strategy_release",
                    "snapshot": {"release_id": released[-1]["release_id"]},
                },
                "lifecycle": "draft",
                "version": 1,
                "total_shares": "0",
            }
            answer = fake.shown_product(fake.products[body["product_id"]])
            return self._remember({k: v for k, v in body.items() if k != "totp_code"}, 201, answer)

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
            "strategy_coordinate": f"{COORDINATE}@{version}",
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
        "venue_source_policy": [{"venue": "BINANCE", "ledger_source": "venue_api"}],
        "runner_contract_requirements": copy.deepcopy(arx_spec.DEFAULT_CONTRACTS),
        "product": {"display_name": "Trend SMA cross", "currency": "USDT"},
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
    notified: list[str] = field(default_factory=list)


def _operator(arx, url, tmp_path, clock, *, codes=None, root=None) -> Operator:
    directory = tmp_path / "config"
    store = (
        arx_session.HostStore(directory)
        if (directory / "hosts.json").exists()
        else _signed_in(arx, url, directory, clock)
    )
    asked: list[str] = []
    notified: list[str] = []

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
        notify=notified.append,
    )
    return Operator(admin, asked, [], root or _repo(tmp_path), notified)


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


def test_the_release_id_is_derived_from_the_organisation_and_the_manifest_digest() -> None:
    digest = "sha256:" + "cd" * 32

    derived = arx_spec.release_id_for(TENANT, digest)

    assert derived == str(uuid.uuid5(arx_spec.RELEASE_ID_NAMESPACE, f"{TENANT}:{digest}"))
    assert derived == arx_spec.release_id_for(TENANT, digest)
    assert derived != arx_spec.release_id_for("tenant_b", digest)
    assert derived != arx_spec.release_id_for(TENANT, "sha256:" + "ce" * 32)


@pytest.mark.parametrize("tenant", ["", "a:b", None])
def test_a_release_id_needs_a_plain_organisation_id(tenant) -> None:
    with pytest.raises(arx_spec.SpecError, match="organisation"):
        arx_spec.release_id_for(tenant, "sha256:" + "cd" * 32)


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
        tenant=TENANT,
        root=root,
        read_release=lambda strategy, version: _evidence(),
    )

    expected = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
    assert plan.body["artifact_source"]["snapshot"]["strategy_release_id"] == expected
    assert dict(arx_spec.summary(plan))["release id"] == expected


# -- a first deployment -------------------------------------------------------------


def _derived_product(arx: FakeArx) -> str:
    (strategy_id,) = arx.strategies
    return arx_deploy.product_id_for(TENANT, "sandbox", strategy_id)


def test_a_first_deployment_creates_the_product_then_deploys_once_it_is_active(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)
    # The first run makes the definition, the release and the product, then stops:
    # capital goes into the product and it is activated in the ARX console.
    first = _deploy(op, product=None)
    assert isinstance(first, arx_deploy.AwaitingProduct)
    assert first.created and first.lifecycle == "draft"
    assert len(op.asked) == 1
    assert not _receipt_path(op.root).exists()
    assert not arx.posts("/api/v1/deployment-specs")
    (strategy_id,) = arx.strategies
    assert first.strategy_definition_id == strategy_id
    assert first.product_id == _derived_product(arx)
    assert arx.releases[first.release_id]["lifecycle"] == "released"
    arx.fund_and_activate(first.product_id)

    result = _deploy(op, product=None)

    assert len(op.asked) == 2
    assert len(arx.posts("/api/v1/products")) == 1
    assert len(arx.posts("/api/v1/strategies")) == 2  # the definition, then the draft
    assert len(arx.posts("/api/v1/strategy-releases/")) == 1
    assert len(arx.posts("/api/v1/deployment-specs")) == 1
    assert arx.strategies[strategy_id]["name"] == DEFINITION
    release_id = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
    assert arx.releases[release_id]["lifecycle"] == "released"
    assert arx.releases[release_id]["release_number"] == 1

    path = _receipt_path(op.root)
    assert result.path == path
    receipt = json.loads(path.read_text())
    (spec_id,) = arx.specs
    (instance_id,) = arx.instances
    sent = arx.posts("/api/v1/deployment-specs")[0]["body"]
    assert sent["strategy_product_id"] == first.product_id
    assert receipt["arx_url"] == arx_session.base_url(url)
    assert receipt["tenant_id"] == TENANT
    assert receipt["operator"] == {"email": "alice@example.com", "user_id": ALICE}
    assert receipt["strategy_definition_id"] == strategy_id
    assert receipt["strategy_release_id"] == release_id
    assert receipt["release_manifest_digest"] == "sha256:" + "cd" * 32
    assert receipt["product_id"] == first.product_id
    assert receipt["product_origin_artifact_source"] == {
        "kind": "strategy_release",
        "snapshot": {"release_id": release_id},
    }
    assert receipt["running_release"]["strategy_release_id"] == release_id
    assert receipt["running_release_check"] == "matches"
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
    assert instance_id in todo and "arx-runner publish-capability" in todo
    assert "custos publish-capability" not in todo
    assert "restart the runner" in todo and "restart_required" in todo
    assert "one instance at a time" in todo and "make deploy-stop" in todo
    assert "one release per mode" in todo and "on this runner or another" in todo
    assert (path.parent / ".progress.json").is_file()


def test_the_definition_found_by_name_is_used_and_not_made_again(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = _ready(arx)
    arx.seed_strategy("trend/another")
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    assert not [r for r in arx.posts("/api/v1/strategies") if r["path"] == "/api/v1/strategies"]
    release_id = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
    assert arx.releases[release_id]["strategy_id"] == strategy_id


@pytest.mark.parametrize(
    ("coordinate", "name"),
    [
        (f"{COORDINATE}@0.1.1", COORDINATE),
        (f"{COORDINATE}@1.0.0-rc.1", COORDINATE),
        (COORDINATE, COORDINATE),
        ("trend/sma_cross", "trend/sma_cross"),
    ],
)
def test_the_definition_is_named_by_the_coordinate_without_its_version(coordinate, name) -> None:
    assert arx_spec.definition_name_for(coordinate) == name


def test_every_release_of_a_strategy_goes_under_one_definition_and_one_product(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)

    first = _deploy(op, version="0.2.0", manifest="cd", product=None)
    second = _deploy(op, version="0.3.0", manifest="ce", product=None)

    assert isinstance(first, arx_deploy.AwaitingProduct)
    assert isinstance(second, arx_deploy.AwaitingProduct)
    assert first.definition_name == second.definition_name == DEFINITION
    assert first.strategy_definition_id == second.strategy_definition_id
    assert first.product_id == second.product_id
    assert first.release_id != second.release_id
    assert not second.created
    (strategy_id,) = arx.strategies
    assert arx.strategies[strategy_id]["name"] == DEFINITION
    assert len(arx.products) == 1
    assert {r["strategy_id"] for r in arx.releases.values()} == {strategy_id}
    assert sorted(r["release_number"] for r in arx.releases.values()) == [1, 2]
    created = [r for r in arx.posts("/api/v1/strategies") if r["path"] == "/api/v1/strategies"]
    assert len(created) == 1 and created[0]["body"]["name"] == DEFINITION


def test_a_definition_named_with_a_version_is_left_alone_and_pointed_out(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    old = arx.seed_strategy(f"{DEFINITION}@0.1.1")
    arx.seed_product(old, lifecycle="active")
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, product=None)

    assert isinstance(result, arx_deploy.AwaitingProduct)
    assert result.strategy_definition_id != old
    assert arx.strategies[result.strategy_definition_id]["name"] == DEFINITION
    assert result.product_id != PRODUCT
    assert arx.strategies[old]["name"] == f"{DEFINITION}@0.1.1"
    assert arx.products[PRODUCT]["lifecycle"] == "active"
    said = " ".join(op.notified)
    assert f"{DEFINITION}@0.1.1" in said and old in said
    assert "retire" in said and "ARX console" in said


def test_a_release_held_by_a_definition_named_with_a_version_says_what_to_do(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    old = arx.seed_strategy(f"{DEFINITION}@0.2.0")
    release_id = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
    arx.seed_release(release_id, old, "released")
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError) as refused:
        _deploy(op, product=None)

    assert f"{DEFINITION}@0.2.0" in str(refused.value)
    assert "make release" in refused.value.fix and "retire" in refused.value.fix
    assert op.asked == []


def test_the_preview_points_out_a_definition_named_with_a_version(fake, tmp_path, clock) -> None:
    arx, url = fake
    old = arx.seed_strategy(f"{DEFINITION}@0.1.1")
    op = _operator(arx, url, tmp_path, clock)

    plan = _preview(op)

    assert any(f"{DEFINITION}@0.1.1" in warning and old in warning for warning in plan.warnings)
    assert not [r for r in arx.requests if r["method"] == "POST"]


def test_the_release_number_follows_the_highest_one(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = _ready(arx)
    arx.seed_release(str(uuid.uuid4()), strategy_id, "released", number=4)
    arx.seed_release(str(uuid.uuid4()), strategy_id, "retired", number=7)
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    release_id = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
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
    release_id = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
    arx.seed_release(release_id, strategy_id, "released")
    op = _operator(arx, url, tmp_path, clock)

    _deploy(op)

    assert not arx.posts(f"/api/v1/strategies/{strategy_id}/releases")
    assert not arx.posts(f"/api/v1/strategy-releases/{release_id}/release")
    assert len(op.asked) == 1


def test_a_draft_release_is_only_published(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = _ready(arx)
    release_id = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
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
    release_id = arx_spec.release_id_for(TENANT, "sha256:" + "cd" * 32)
    arx.seed_release(release_id, other, "released")
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match="belongs to another strategy"):
        _deploy(op)
    assert op.asked == []


# -- the product -------------------------------------------------------------------


def test_the_product_is_created_once_with_one_code_and_not_again(fake, tmp_path, clock) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)

    first = _deploy(op, product=None)
    again = _deploy(_operator(arx, url, tmp_path, clock, root=op.root), product=None)

    assert len(op.asked) == 1
    (created,) = arx.posts("/api/v1/products")
    (strategy_id,) = arx.strategies
    assert {k: v for k, v in created["body"].items() if k != "totp_code"} == {
        "mode": "sandbox",
        "product_id": arx_deploy.product_id_for(TENANT, "sandbox", strategy_id),
        "display_name": "Trend SMA cross",
        "strategy_definition_id": strategy_id,
        "currency": "USDT",
    }
    assert created["headers"]["Idempotency-Key"] == arx_deploy.product_key_for(
        TENANT, "sandbox", strategy_id
    )
    assert created["headers"]["X-Trading-Mode"] == "sandbox"
    assert isinstance(again, arx_deploy.AwaitingProduct)
    assert not again.created and again.lifecycle == "draft"
    assert again.product_id == first.product_id
    assert len(arx.products) == 1
    assert not arx.posts("/api/v1/deployment-specs")
    assert not _receipt_path(op.root).exists()


def test_a_product_whose_creation_was_not_answered_is_found_without_another_code(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    arx.fail_after[("POST", "/api/v1/products")] = [503]
    op = _operator(arx, url, tmp_path, clock)
    with pytest.raises(ArxError):
        _deploy(op, product=None)
    assert len(op.asked) == 1 and len(arx.products) == 1

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, product=None)

    assert again.asked == []
    assert isinstance(result, arx_deploy.AwaitingProduct) and not result.created
    assert len(arx.posts("/api/v1/products")) == 1


def test_a_draft_product_stops_before_any_code(fake, tmp_path, clock, monkeypatch) -> None:
    arx, url = fake
    arx.seed_product(arx.seed_strategy(), lifecycle="draft")
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, product=None)
    said = _said(monkeypatch)

    assert isinstance(result, arx_deploy.AwaitingProduct)
    assert result.product_id == PRODUCT and result.lifecycle == "draft" and not result.created
    assert op.asked == []
    assert not arx.posts("/api/v1/products") and not arx.posts("/api/v1/deployment-specs")
    assert not _receipt_path(op.root).exists()
    assert arx_deploy._awaiting_product(STRATEGY, "sandbox", result) == 0
    assert any("capital" in step and "approve" in step and "activate" in step for step in said)
    assert said[1] == f"make deploy STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"


def _steps(monkeypatch) -> list[tuple[str, str]]:
    steps: list[tuple[str, str]] = []
    monkeypatch.setattr(arx_deploy.ui, "next_steps", steps.extend)
    for name in ("ok", "info", "warn", "error", "table"):
        monkeypatch.setattr(arx_deploy.ui, name, lambda *a, **k: None)
    return steps


def test_a_product_awaiting_capital_says_its_capital_is_then_allocated(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)
    result = _deploy(op, product=None)
    steps = _steps(monkeypatch)

    assert arx_deploy._awaiting_product(STRATEGY, "sandbox", result) == 0

    commands = [command for command, _ in steps]
    deploy = f"make deploy STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"
    (allocate,) = [step for step in steps if "allocat" in step[0]]
    assert commands.index(deploy) < steps.index(allocate)
    said = " ".join(allocate)
    assert "FINANCE" in said and "ARX console" in said and result.product_id in said
    assert "first instance" in said and "contribution" in said
    assert "blocked" in said and "risk" in said


def test_a_new_deployment_says_to_allocate_the_products_capital_to_its_instance(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    result = _deploy(op)
    steps = _steps(monkeypatch)

    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0

    (allocate,) = [step for step in steps if "allocat" in step[0]]
    said = " ".join(allocate)
    assert result.receipt["first_instance_id"] in said and PRODUCT in said
    assert "FINANCE" in said and "blocked" in said


def test_a_product_with_no_state_given_is_not_deployed_to(fake, tmp_path, clock) -> None:
    arx, url = fake
    arx.seed_product(arx.seed_strategy())
    del arx.products[PRODUCT]["lifecycle"]
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, product=None)

    assert isinstance(result, arx_deploy.AwaitingProduct) and result.lifecycle is None
    assert op.asked == [] and not arx.posts("/api/v1/deployment-specs")


def test_a_retired_product_is_refused(fake, tmp_path, clock) -> None:
    arx, url = fake
    arx.seed_product(arx.seed_strategy(), lifecycle="retired")
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match="retired"):
        _deploy(op, product=None)

    assert op.asked == []
    assert not arx.posts("/api/v1/products") and not arx.posts("/api/v1/deployment-specs")
    assert not _receipt_path(op.root).exists()


def test_an_active_product_is_found_without_product_given(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, product=None)

    assert isinstance(result, arx_deploy.Deployment)
    assert len(op.asked) == 1
    assert not arx.posts("/api/v1/products")
    assert arx.posts("/api/v1/deployment-specs")[0]["body"]["strategy_product_id"] == PRODUCT
    assert result.receipt["product_id"] == PRODUCT


@pytest.mark.parametrize("problem", ["another id", "another strategy", "none yet", "other mode"])
def test_a_product_given_that_is_not_the_strategys_is_refused_before_any_code(
    fake, tmp_path, clock, problem
) -> None:
    arx, url = fake
    strategy_id = arx.seed_strategy()
    given = PRODUCT
    if problem == "another id":
        arx.seed_product(strategy_id)
        given = str(uuid.uuid4())
    elif problem == "another strategy":
        arx.seed_product(strategy_id, product_id=str(uuid.uuid4()))
        arx.seed_product(arx.seed_strategy("trend/another"))
    elif problem == "other mode":
        arx.seed_product(strategy_id, mode="testnet")
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError) as refused:
        _deploy(op, product=given)

    message = str(refused.value)
    expected = {
        "another id": f"no sandbox product {given}",
        "another strategy": "belongs to another strategy",
        "none yet": "has no sandbox product yet",
        "other mode": "has no sandbox product yet",
    }[problem]
    assert expected in message and given in message
    assert "without PRODUCT=" in refused.value.fix
    assert op.asked == []
    assert not arx.posts("/api/v1/products") and not arx.posts("/api/v1/deployment-specs")
    assert not _receipt_path(op.root).exists()


@pytest.mark.parametrize(
    ("status", "code", "said"),
    [
        (404, "strategy_definition_not_found", "has no strategy definition"),
        (409, "source_unavailable", f"nothing of {DEFINITION} that can run in sandbox"),
    ],
)
def test_a_refused_product_creation_is_explained(fake, tmp_path, clock, status, code, said) -> None:
    arx, url = fake
    arx.refuse_product = (status, {"code": code, "message": "refused", "retryable": False})
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError) as refused:
        _deploy(op, product=None)

    assert said in str(refused.value) and "no product was created" in str(refused.value)
    assert "run make deploy again" in refused.value.fix
    assert code not in str(refused.value)
    assert len(op.asked) == 1 and not arx.products
    assert not _receipt_path(op.root).exists()


def test_a_product_without_its_name_in_deploy_yaml_asks_for_it_before_any_code(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    settings = _settings()
    settings["product"]["display_name"] = None
    op = _operator(arx, url, tmp_path, clock, root=_repo(tmp_path, settings))

    with pytest.raises(arx_spec.SpecError) as refused:
        _deploy(op, product=None)

    assert "product.display_name" in str(refused.value)
    assert "fill in product.display_name" in refused.value.fix
    assert op.asked == [] and not arx.posts("/api/v1/products")


def test_creating_the_product_then_the_spec_right_away_waits_for_a_new_code(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op, product=None)
    step = int(clock() // 30)
    # The product is given capital and activated within the same 30 seconds.
    arx.fund_and_activate(first.product_id)

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, product=None)

    assert len(again.asked) == 1
    assert int(clock() // 30) > step
    assert isinstance(result, arx_deploy.Deployment) and result.receipt["state"] == "running"


def test_a_running_release_other_than_the_one_deployed_fails_the_deployment(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    other = str(uuid.uuid4())
    arx.running_release_override = {
        "artifact_source_kind": "strategy_release",
        "artifact_source_digest": "sha256:" + "ee" * 32,
        "strategy_release_id": other,
        "running_instance_count": 1,
        "updated_at": "2027-01-15T08:00:00Z",
    }
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op)
    monkeypatch.setattr(arx_deploy, "_show_receipt", lambda result: None)
    _said(monkeypatch)

    assert result.receipt["running_release_check"] == "differs"
    assert result.receipt["running_release"]["strategy_release_id"] == other
    assert other in result.error
    assert json.loads(result.path.read_text())["running_release_check"] == "differs"
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1


def test_a_product_that_lists_nothing_running_yet_is_recorded_as_not_yet(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    arx.running_release_override = {}
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op)

    assert result.receipt["running_release_check"] == "not yet"
    assert result.receipt["running_release"] is None
    assert result.error is None


# -- the preview ----------------------------------------------------------------------


def _preview(op: Operator, product=None):
    return arx_deploy.preview(
        op.admin,
        STRATEGY,
        mode="sandbox",
        runner_id=RUNNER,
        product_id=product,
        tenant=TENANT,
        version="0.2.0",
        root=op.root,
        read_release=lambda strategy, wanted: _evidence(),
    )


def test_the_preview_shows_the_product_found(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)

    plan = _preview(op)

    shown = dict(arx_spec.summary(plan))
    assert shown["product"] == f"{PRODUCT} (found: active)"
    assert plan.sendable and shown["request digest"] == plan.request_digest
    assert op.asked == [] and not [r for r in arx.requests if r["method"] == "POST"]


def test_the_preview_shows_the_product_it_would_create(fake, tmp_path, clock) -> None:
    arx, url = fake
    strategy_id = arx.seed_strategy()
    op = _operator(arx, url, tmp_path, clock)

    plan = _preview(op)

    shown = dict(arx_spec.summary(plan))
    derived = arx_deploy.product_id_for(TENANT, "sandbox", strategy_id)
    assert shown["product"].startswith(f"{derived} (will be created")
    assert "Trend SMA cross" in shown["product"]
    assert op.asked == [] and not [r for r in arx.requests if r["method"] == "POST"]


def test_the_preview_of_a_strategy_ARX_does_not_have_yet(fake, tmp_path, clock) -> None:
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)

    plan = _preview(op)

    shown = dict(arx_spec.summary(plan))
    assert not plan.sendable
    assert shown["product"].startswith("will be created")
    assert not [r for r in arx.requests if r["method"] == "POST"]
    with pytest.raises(ArxError, match="cannot be its product"):
        _preview(op, product=PRODUCT)


def test_the_preview_of_a_deploy_file_arx_would_refuse_says_what_to_add(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    # A deploy.yaml as make new-strategy wrote it before: one contract, no venue.
    settings = _settings()
    settings["venue_source_policy"] = []
    settings["runner_contract_requirements"] = {"health": {"schema_version": 1, "heartbeat": "v1"}}
    op = _operator(arx, url, tmp_path, clock, root=_repo(tmp_path, settings))

    with pytest.raises(ArxError) as venue:
        _preview(op)
    assert "venue_source_policy is empty" in str(venue.value)
    assert "- venue: BINANCE" in venue.value.fix

    settings["venue_source_policy"] = [{"venue": "BINANCE", "ledger_source": "venue_api"}]
    op = _operator(arx, url, tmp_path, clock, root=_repo(tmp_path, settings))
    with pytest.raises(ArxError) as contracts:
        _preview(op)
    for section in ("risk", "settlement", "reconciliation", "deployment_lifecycle"):
        assert section in str(contracts.value) and f"  {section}:" in contracts.value.fix
    assert not [r for r in arx.requests if r["method"] == "POST"] and op.asked == []


def test_the_preview_refuses_a_product_that_is_not_the_strategys(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match=f"product is {PRODUCT}"):
        _preview(op, product=str(uuid.uuid4()))


# -- resuming --------------------------------------------------------------------------


def test_a_run_cut_short_resumes_where_it_stopped(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    arx.fail_before[("GET", "/api/v1/products")] = [503]
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


# -- what the commands say next ---------------------------------------------------------


def _said(monkeypatch) -> list[str]:
    said: list[str] = []
    monkeypatch.setattr(arx_deploy.ui, "next_steps", lambda steps: said.extend(c for c, _ in steps))
    for name in ("ok", "info", "warn", "error", "table"):
        monkeypatch.setattr(arx_deploy.ui, name, lambda *a, **k: None)
    return said


def test_a_deployment_names_the_runners_own_command(fake, tmp_path, clock, monkeypatch) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    result = _deploy(op)
    said = _said(monkeypatch)

    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0

    assert any("arx-runner publish-capability" in step for step in said)
    assert not any("custos publish-capability" in step for step in said)


def test_a_preview_without_a_product_shows_the_rest_and_cannot_be_sent(tmp_path) -> None:
    plan = arx_spec.preview(
        STRATEGY,
        mode="sandbox",
        runner_id=RUNNER,
        product_id=None,
        version="0.2.0",
        tenant=TENANT,
        root=_repo(tmp_path),
        read_release=lambda strategy, version: _evidence(),
    )

    shown = dict(arx_spec.summary(plan))
    assert not plan.sendable
    assert "not known yet" in shown["product"]
    assert "once the product is known" in shown["request digest"]
    assert shown["credential scope"] == SCOPE_ID
    with pytest.raises(arx_spec.SpecError, match="names no product"):
        plan.request(totp_code="123456", idempotency_key=str(uuid.uuid4()))
    with pytest.raises(arx_spec.SpecError, match="names no product"):
        plan.request_digest  # noqa: B018


def test_a_stop_points_at_the_next_deployment(fake, tmp_path, clock, monkeypatch) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)
    stopper = _operator(arx, url, tmp_path, clock, root=op.root)
    stopped = arx_deploy.stop(stopper.admin, STRATEGY, mode="sandbox", root=op.root)
    said = _said(monkeypatch)

    assert arx_deploy._after_stop(STRATEGY, "sandbox", stopped) == 0

    assert said == [
        f"make deploy STRATEGY={STRATEGY} MODE=sandbox RUNNER={RUNNER}",
        f"make next STRATEGY={STRATEGY} MODE=sandbox",
    ]


def test_a_stop_not_yet_seen_points_at_running_it_again(monkeypatch) -> None:
    said = _said(monkeypatch)
    monkeypatch.setattr(arx_deploy, "_show_receipt", lambda result: None)
    pending = arx_deploy.Deployment(
        Path("receipt.json"),
        {"state": "stopping", "version": "0.2.0", "runner_id": RUNNER, "product_id": PRODUCT},
    )

    assert arx_deploy._after_stop(STRATEGY, "sandbox", pending) == 1

    assert said == [
        f"make deploy-stop STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"
    ]


@pytest.mark.parametrize("target", ["deploy", "deploy-preview"])
def test_make_passes_product_only_when_given(target) -> None:
    import subprocess

    root = Path(__file__).resolve().parents[1]

    def dry(*extra: str) -> str:
        return subprocess.run(
            ["make", "-n", target, f"STRATEGY={STRATEGY}", f"RUNNER={RUNNER}", *extra],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip

    assert "--product" not in dry()
    assert f'--product "{PRODUCT}"' in dry(f"PRODUCT={PRODUCT}")
    command = "preview" if target == "deploy-preview" else "deploy"
    assert f"tools/arx/deploy.py {command} {STRATEGY}" in dry()
