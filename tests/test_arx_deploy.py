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
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
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
EVENT = "2b3c4d5e-6f70-4a81-9b2c-3d4e5f607182"
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
    # the state ARX wants a new instance in
    new_instance_state: str = "running"
    # what the runner answers a start with: running_confirmed, start_rejected or
    # awaiting_runner (never answers); None is an ARX that does not report it
    runner_says: str | None = "running_confirmed"
    rejected_outcome: str = "retry_exhausted"
    # the runner's reason for a rejected start, when ARX reports one
    rejected_reason_code: str | None = None
    # idempotency key -> (what was asked, the answer) for further instances of a spec
    materialized: dict[str, tuple[tuple, dict]] = field(default_factory=dict)
    # (status, body) a further instance of a spec is refused with, after its code is taken
    refuse_materialize: tuple[int, dict] | None = None
    # how many reads of an instance before its runner's answer shows
    answer_after: int = 0
    instance_reads: dict[str, int] = field(default_factory=dict)
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

    def shown_instance(self, instance: dict) -> dict:
        """An instance read: the wanted state, and what its runner said about it."""

        instance_id = instance["deployment_instance_id"]
        reads = self.instance_reads[instance_id] = self.instance_reads.get(instance_id, 0) + 1
        if self.runner_says is None:
            return dict(instance)
        said = self.runner_says if reads > self.answer_after else "awaiting_runner"
        if instance["lifecycle_state"] != "running":
            seen = None
        elif said == "awaiting_runner":
            seen = {
                "status": said,
                "generation": 1,
                "outcome": None,
                "observed_at": None,
                "event_id": None,
            }
        else:
            seen = {
                "status": said,
                "generation": 1,
                "outcome": "applied" if said == "running_confirmed" else self.rejected_outcome,
                "observed_at": "2027-01-15T08:01:00Z",
                "event_id": EVENT,
            }
            if said == "start_rejected" and self.rejected_reason_code is not None:
                seen["reason_code"] = self.rejected_reason_code
        return {**instance, "runner_observation": seen}


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
            if parts[0] == "deployment-specs" and parts[2:] == ["instances"]:
                return self._materialize(parts[1], body)
            if parts[0] == "deployments":
                instance = fake.instances.get(parts[1])
                if instance is None or instance["trading_mode"] != mode_of(query, body):
                    return 404, {"code": "not_found"}
                if method == "GET":
                    return 200, fake.shown_instance(instance)
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

        def _materialize(self, spec_id: str, body: dict):
            """A further instance of a spec: one fresh code, an Idempotency-Key header."""

            if not self._code_ok(body):
                return 403, {"code": "GSOD_VIOLATION", "message": "totp verification failed"}
            if fake.refuse_materialize is not None:
                return fake.refuse_materialize
            fields = {
                "trading_mode",
                "deployment_spec_digest",
                "target_runner_id",
                "reason",
                "totp_code",
            }
            if set(body) != fields:
                return 422, {"code": "unprocessable_entity"}
            key = self.headers.get("Idempotency-Key")
            if not key:
                return 400, {"code": "BAD_REQUEST"}
            spec = fake.specs.get(spec_id)
            asked = (
                spec_id,
                body["trading_mode"],
                body["deployment_spec_digest"],
                body["target_runner_id"],
            )
            if (
                spec is None
                or spec["trading_mode"] != body["trading_mode"]
                or spec["spec_digest"] != body["deployment_spec_digest"]
                or spec["target_runner_id"] != body["target_runner_id"]
            ):
                return 404, {"code": "NOT_FOUND", "message": "deployment spec not found"}
            if key in fake.materialized:
                before, answer = fake.materialized[key]
                if before != asked:
                    return 409, {
                        "code": "version_conflict",
                        "message": "materialization conflict",
                        "correlation_id": str(uuid.uuid4()),
                        "retryable": False,
                    }
                return 200, {**answer, "disposition": "exact_replay"}
            first = fake.instances[spec["projected_deployment_instance_id"]]
            instance_id = str(uuid.uuid4())
            fake.instances[instance_id] = {
                **first,
                "deployment_instance_id": instance_id,
                "lifecycle_state": "running",
                "version": 1,
            }
            answer = {
                "disposition": "accepted",
                "tenant_id": TENANT,
                "trading_mode": spec["trading_mode"],
                "deployment_instance_id": instance_id,
                "deployment_spec_id": spec_id,
                "deployment_spec_digest": spec["spec_digest"],
                "target_runner_id": spec["target_runner_id"],
                "idempotency_key": key,
                "request_fingerprint": "f" * 64,
                "generation": 1,
                "lifecycle_state": "running",
            }
            fake.materialized[key] = (asked, answer)
            return 201, answer

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
                "lifecycle_state": fake.new_instance_state,
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
    # The runner confirmed the start, so binding it and restarting it are done.
    todo = " ".join(receipt["runner_todo"])
    assert "publish-capability" not in todo and "restart" not in todo
    assert "one instance at a time" in todo and "make deploy-stop" in todo
    assert "one release per mode" in todo and "on this runner or another" in todo
    assert (path.parent / ".progress.json").is_file()


def test_a_receipt_lists_the_runner_steps_until_the_runner_confirms_the_start(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)

    waiting = _deploy(op, timeout=30, poll_seconds=5)

    instance_id = waiting.receipt["first_instance_id"]
    todo = " ".join(json.loads(waiting.path.read_text())["runner_todo"])
    assert instance_id in todo and "arx-runner publish-capability" in todo
    assert "custos publish-capability" not in todo
    assert "restart the runner" in todo and "restart_required" in todo
    assert "one instance at a time" in todo and "make deploy-stop" in todo

    # Once the runner confirms the start, the binding and the restart are done.
    arx.runner_says = "running_confirmed"
    confirmed = _deploy(_operator(arx, url, tmp_path, clock, root=op.root))

    assert confirmed.receipt["runner_check"] == "confirmed"
    todo = " ".join(json.loads(confirmed.path.read_text())["runner_todo"])
    assert "publish-capability" not in todo and "capability bindings" not in todo
    assert "restart" not in todo
    assert "one instance at a time" in todo and "make deploy-stop" in todo


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
    # A blocked contribution holds back its own product only.
    assert "other products are not affected" in said and "organisation" not in said


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
    # The runner confirmed the start, so its own steps are done.
    assert "publish-capability" not in " ".join(" ".join(step) for step in steps)
    assert f"make arx-status ARX_URL={url}" in [command for command, _ in steps]


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
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)
    result = _deploy(op)
    said = _said(monkeypatch)

    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == arx_deploy.EXIT_BIND_RUNNER

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


# -- whether the runner started it --------------------------------------------------------


@dataclass
class Told:
    oks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    steps: list[tuple[str, str]] = field(default_factory=list)

    @property
    def everything(self) -> str:
        return " ".join([*self.oks, *self.errors, *(" ".join(step) for step in self.steps)])


def _told(monkeypatch) -> Told:
    told = Told()
    monkeypatch.setattr(arx_deploy.ui, "ok", lambda message, **k: told.oks.append(message))
    monkeypatch.setattr(arx_deploy.ui, "error", lambda message, **k: told.errors.append(message))
    monkeypatch.setattr(arx_deploy.ui, "next_steps", told.steps.extend)
    for name in ("info", "warn", "table"):
        monkeypatch.setattr(arx_deploy.ui, name, lambda *a, **k: None)
    return told


def _instance_reads(arx: FakeArx, result) -> int:
    return arx.instance_reads.get(result.receipt["first_instance_id"], 0)


def test_a_start_the_runner_confirms_is_a_deployment(fake, tmp_path, clock, monkeypatch) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.error is None
    assert result.receipt["runner_check"] == "confirmed"
    assert result.receipt["runner_observation"]["status"] == "running_confirmed"
    assert result.receipt["runner_observation"]["event_id"] == EVENT
    assert json.loads(result.path.read_text())["runner_check"] == "confirmed"
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0
    assert not told.errors
    (said,) = told.oks
    assert "confirmed it running at 2027-01-15T08:01:00Z" in said and RUNNER in said


@pytest.mark.parametrize("outcome", ["retry_exhausted", "conflict"])
def test_a_start_the_runner_rejects_fails_and_says_when_and_why(
    fake, tmp_path, clock, monkeypatch, outcome
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "start_rejected"
    arx.rejected_outcome = outcome
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.receipt["runner_check"] == "rejected"
    assert result.receipt["runner_observation"]["outcome"] == outcome
    assert _instance_reads(arx, result) == 2  # as it is listed, then once: not waited on
    assert outcome in result.error
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    assert not told.oks
    (error,) = told.errors
    assert outcome in error and "2027-01-15T08:01:00Z" in error and EVENT in error
    commands = [command for command, _ in told.steps]
    assert any(EVENT in command and "log" in command for command in commands)
    stop = f"make deploy-stop STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"
    assert stop in commands
    assert arx_deploy.observation.OUTCOMES[outcome] in told.everything


def _bound_steps_due(told: Told) -> None:
    said = told.everything
    assert "arx-runner publish-capability" in said and "restart the runner" in said
    again = f"make deploy STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"
    assert again in [command for command, _ in told.steps]


def _bind_notices(op: Operator) -> list[str]:
    """What the run said, while it waited, about binding the instance to the runner."""

    return [said for said in op.notified if "publish-capability" in said]


def test_a_new_instance_is_waited_for_until_the_runner_confirms_it(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    # The runner is bound to the new instance while the command waits, then confirms it.
    arx.answer_after = 3
    op = _operator(arx, url, tmp_path, clock)
    started = clock()

    result = _deploy(op, timeout=180, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.error is None
    assert clock() - started == 10  # as listed, awaited twice, then confirmed
    receipt = json.loads(result.path.read_text())
    assert receipt["runner_check"] == "confirmed"
    assert receipt["runner_waited"] is True
    assert receipt.get("first_listed_by_this_run") is True
    assert "not confirmed yet" in receipt["runner_check_basis"]
    assert "180 seconds" in receipt["runner_check_basis"]
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0
    assert not told.errors and told.oks
    # The same run that lists the instance names the allocation to it.
    (allocate,) = [step for step in told.steps if "allocat" in step[0]]
    assert result.receipt["first_instance_id"] in " ".join(allocate)
    (notice,) = _bind_notices(op)
    assert result.receipt["first_instance_id"] in notice and "restart" in notice


def test_a_new_instance_the_runner_is_not_bound_to_in_time_ends_with_its_steps_due(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)
    started = clock()

    result = _deploy(op, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    # Waited for, as any instance is, and still not answered at the end of the wait.
    assert clock() - started >= 60
    assert _instance_reads(arx, result) == 1 + 1 + 12  # listed, then every 5 s for 60
    receipt = json.loads(result.path.read_text())
    assert receipt["runner_check"] == "awaiting"
    assert receipt["runner_waited"] is True
    assert receipt.get("first_listed_by_this_run") is True
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == arx_deploy.EXIT_BIND_RUNNER
    assert arx_deploy.EXIT_BIND_RUNNER == 3
    assert not told.oks and not told.errors
    _bound_steps_due(told)
    # The capital is allocated to this instance once the runner confirms it, which the
    # next run is the one to say: so it is named here, after that run.
    again = f"make deploy STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"
    commands = [command for command, _ in told.steps]
    (allocate,) = [step for step in told.steps if "allocat" in step[0]]
    assert commands.index(again) < told.steps.index(allocate)
    said = " ".join(allocate)
    assert result.receipt["first_instance_id"] in said and PRODUCT in said
    assert "FINANCE" in said and "blocked" in said


def test_a_new_instance_whose_start_the_runner_refuses_while_waited_for_fails_at_once(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "start_rejected"
    arx.answer_after = 2
    op = _operator(arx, url, tmp_path, clock)
    started = clock()

    result = _deploy(op, timeout=180, poll_seconds=5)
    told = _told(monkeypatch)

    # The refusal ends the wait: it is not waited out to the timeout.
    assert clock() - started == 5
    assert result.receipt["runner_check"] == "rejected"
    assert "retry_exhausted" in result.error
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    assert not told.oks
    (error,) = told.errors
    assert "refused its start" in error and EVENT in error


def test_the_runner_steps_are_shown_once_while_a_new_instance_is_waited_for(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, timeout=180, poll_seconds=3)

    assert _instance_reads(arx, result) > 60  # read again and again while waited for
    (notice,) = _bind_notices(op)
    assert result.receipt["first_instance_id"] in notice
    assert RUNNER in notice and "180" in notice


def test_a_new_instance_confirmed_at_once_shows_no_runner_steps(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, timeout=180, poll_seconds=5)

    assert result.receipt["runner_check"] == "confirmed"
    assert _bind_notices(op) == []


def test_an_instance_first_listed_on_a_later_run_is_new_to_that_run(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.project_after = 1000
    op = _operator(arx, url, tmp_path, clock)
    assert _deploy(op, timeout=30, poll_seconds=5).receipt["first_instance_id"] is None

    # The receipt named no instance: the run that lists it is the first to, and
    # names the allocation to it.
    arx.project_after = 0
    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.receipt.get("first_listed_by_this_run") is True
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0
    (allocate,) = [step for step in told.steps if "allocat" in step[0]]
    assert result.receipt["first_instance_id"] in " ".join(allocate)


def test_a_runner_that_never_confirmed_the_instance_ends_with_its_steps_due_on_every_run(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op, timeout=30, poll_seconds=5)
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", first) == arx_deploy.EXIT_BIND_RUNNER

    # Run again, but the runner still does not answer: it has never confirmed this
    # instance, so binding it is still what comes next, whichever run listed it.
    again = _operator(arx, url, tmp_path, clock, root=op.root)
    started = clock()
    reads = arx.instance_reads[next(iter(arx.instances))]
    result = _deploy(again, timeout=30, poll_seconds=5)
    told = _told(monkeypatch)

    assert again.asked == []
    assert result.receipt["runner_check"] == "awaiting"
    assert result.receipt["runner_waited"] is True
    assert "not confirmed" in result.receipt["runner_check_basis"]
    assert not result.receipt.get("first_listed_by_this_run")
    assert result.receipt.get("runner_confirmed_instance_id") is None
    (notice,) = _bind_notices(again)
    assert result.receipt["first_instance_id"] in notice
    assert result.receipt["state"] == "running"
    # as listed, then every 5 seconds for 30
    assert _instance_reads(arx, result) - reads == 1 + 7
    assert clock() - started >= 30
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == arx_deploy.EXIT_BIND_RUNNER
    assert not told.oks and not told.errors
    _bound_steps_due(told)


def test_a_wait_cut_short_names_the_runner_steps_again_on_the_next_run(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)

    def interrupted(message: str) -> None:
        # Ctrl-C as the wait for the runner begins: the receipt names the instance by then.
        op.notified.append(message)
        if "publish-capability" in message:
            raise KeyboardInterrupt

    cut = replace(op, admin=replace(op.admin, notify=interrupted))
    with pytest.raises(KeyboardInterrupt):
        _deploy(cut, timeout=180, poll_seconds=5)
    assert len(_bind_notices(op)) == 1
    written = json.loads(_receipt_path(op.root).read_text())
    assert written["instance_id"] and not written.get("runner_waited")

    # The runner is bound while the next run waits, and confirms the start.
    (instance,) = arx.instances
    arx.runner_says = "running_confirmed"
    arx.answer_after = arx.instance_reads[instance] + 2
    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, timeout=180, poll_seconds=5)
    told = _told(monkeypatch)

    assert again.asked == []
    (notice,) = _bind_notices(again)
    assert instance in notice and "restart" in notice
    assert result.receipt["runner_check"] == "confirmed"
    assert result.receipt["runner_confirmed_instance_id"] == instance
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0
    # The run cut short never got to name the allocation; this one does.
    (allocate,) = [step for step in told.steps if "allocat" in step[0]]
    assert instance in " ".join(allocate)


def test_a_wait_cut_short_then_timed_out_ends_with_the_runner_steps(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)

    def interrupted(message: str) -> None:
        if "publish-capability" in message:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _deploy(replace(op, admin=replace(op.admin, notify=interrupted)), timeout=180)

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, timeout=30, poll_seconds=5)
    told = _told(monkeypatch)

    assert len(_bind_notices(again)) == 1
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == arx_deploy.EXIT_BIND_RUNNER
    _bound_steps_due(told)


def _confirmed_then_silent(arx: FakeArx, url: str, tmp_path: Path, clock: Clock) -> Operator:
    """A deployment whose runner confirmed its instance, and then stopped answering."""

    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op, timeout=60, poll_seconds=5)
    assert first.receipt["runner_check"] == "confirmed"
    arx.runner_says = "awaiting_runner"
    return op


def test_a_runner_that_confirmed_the_instance_and_stops_answering_fails_without_its_steps(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    op = _confirmed_then_silent(arx, url, tmp_path, clock)
    instance = json.loads(_receipt_path(op.root).read_text())["instance_id"]
    assert (
        json.loads(_receipt_path(op.root).read_text())["runner_confirmed_instance_id"] == instance
    )

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, timeout=30, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.receipt["runner_check"] == "awaiting"
    # What the runner confirmed once stays recorded: it is not bound to it again.
    assert result.receipt["runner_confirmed_instance_id"] == instance
    assert _bind_notices(again) == []
    assert "confirmed before" in result.receipt["runner_check_basis"]
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    (error,) = told.errors
    assert "has not said whether it started" in error and RUNNER in error
    assert "connected to ARX" in told.everything
    for bound in ("publish-capability", "capability bindings", "restart the runner"):
        assert bound not in told.everything
    for bound in ("publish-capability", "capability bindings"):
        assert bound not in " ".join(result.receipt["runner_todo"])


def test_a_receipt_written_before_the_confirmed_instance_was_recorded_reads_as_confirmed(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    op = _confirmed_then_silent(arx, url, tmp_path, clock)
    path = _receipt_path(op.root)
    older = json.loads(path.read_text())
    older.pop("runner_confirmed_instance_id", None)
    path.write_text(json.dumps(older))

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, timeout=30, poll_seconds=5)
    told = _told(monkeypatch)

    assert _bind_notices(again) == []
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    assert "publish-capability" not in told.everything


def test_a_runner_that_answers_while_watched_again_is_a_deployment(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)

    # The runner is bound and restarted, and confirms the start a little later.
    (instance,) = arx.instances
    arx.runner_says = "running_confirmed"
    arx.answer_after = arx.instance_reads[instance] + 3
    again = _operator(arx, url, tmp_path, clock, root=op.root)
    started = clock()
    result = _deploy(again, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    assert again.asked == []
    assert result.receipt["runner_check"] == "confirmed"
    assert result.receipt["runner_waited"] is True
    assert clock() - started == 10  # awaited twice while watched, then confirmed
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0
    assert not told.errors and told.oks


def test_a_confirmed_deployment_lists_only_what_can_be_done_next(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "awaiting_runner"
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)

    # The runner-side steps and the allocation were named by that first run and are
    # done by now: the run that sees the runner confirm the start does not list them.
    arx.runner_says = "running_confirmed"
    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.receipt["runner_waited"] is True
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0
    assert [command for command, _ in told.steps] == [
        f"make arx-status ARX_URL={url}",
        f"make deploy-stop STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}",
    ]
    listed = " ".join(" ".join(step) for step in told.steps)
    for done in ("publish-capability", "capability bindings", "restart", "allocat"):
        assert done not in listed
    assert "one instance at a time" in listed


def test_the_timeout_reaches_the_command(monkeypatch) -> None:
    seen = {}
    monkeypatch.setattr(arx_deploy.runner_admin, "admin_for", lambda *a, **k: None)
    monkeypatch.setattr(arx_deploy, "deploy", lambda *a, **k: seen.update(k) or None)
    monkeypatch.setattr(arx_deploy, "_after_deploy", lambda *a: 0)
    common = ["deploy", STRATEGY, "--mode", "sandbox", "--runner", RUNNER]

    arx_deploy.main(common)
    assert seen["timeout"] == arx_deploy.TIMEOUT_SECONDS
    arx_deploy.main([*common, "--timeout", "600"])
    assert seen["timeout"] == 600


def test_make_passes_the_timeout_only_when_given() -> None:
    import subprocess

    root = Path(__file__).resolve().parents[1]

    def dry(*extra: str) -> str:
        return subprocess.run(
            ["make", "-n", "deploy", f"STRATEGY={STRATEGY}", f"RUNNER={RUNNER}", *extra],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip

    assert "--timeout" not in dry()
    assert '--timeout "600"' in dry("TIMEOUT=600")


def test_an_arx_that_does_not_report_the_runner_is_not_taken_as_a_start(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = None
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.receipt["runner_check"] == "unsupported"
    assert result.receipt["runner_observation"] is None
    assert _instance_reads(arx, result) == 2  # as it is listed, then once: nothing to wait for
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    assert not told.oks
    said = told.everything
    assert "older than this tool" in result.error
    assert "runner_observation" in said and "upgrade ARX" in said


def test_an_instance_arx_does_not_want_running_is_not_a_deployment(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.new_instance_state = "paused"
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    assert result.receipt["runner_check"] == "not running"
    assert result.receipt["runner_observation"] is None
    assert result.receipt["state"] == "paused"
    assert _instance_reads(arx, result) == 2
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    assert not told.oks
    (error,) = told.errors
    assert "paused" in error and "no runner is asked" in error


AWAITING = {"status": "awaiting_runner", "generation": 1, "outcome": None,
            "observed_at": None, "event_id": None}  # fmt: skip
CONFIRMED = {"status": "running_confirmed", "generation": 1, "outcome": "applied",
             "observed_at": "2027-01-15T08:01:00Z", "event_id": EVENT}  # fmt: skip
REJECTED = {**CONFIRMED, "status": "start_rejected", "outcome": "conflict"}


@pytest.mark.parametrize(
    ("instance", "check"),
    [
        ({"lifecycle_state": "running", "runner_observation": CONFIRMED}, "confirmed"),
        ({"lifecycle_state": "running", "runner_observation": REJECTED}, "rejected"),
        ({"lifecycle_state": "running", "runner_observation": AWAITING}, "awaiting"),
        ({"lifecycle_state": "paused", "runner_observation": None}, "not running"),
        ({"lifecycle_state": "stopped", "runner_observation": None}, "not running"),
        ({"lifecycle_state": "running"}, "unsupported"),
        # What ARX says it never sends is not read as a start either.
        ({"lifecycle_state": "running", "runner_observation": None}, "unreadable"),
        ({"lifecycle_state": "running", "runner_observation": {"status": "started"}}, "unreadable"),
        (
            {
                "lifecycle_state": "running",
                "runner_observation": {**CONFIRMED, "observed_at": None},
            },
            "unreadable",
        ),
        ({"lifecycle_state": "running", "runner_observation": "running"}, "unreadable"),
    ],
)
def test_only_a_confirmed_observation_reads_as_a_start(instance, check) -> None:
    assert arx_deploy.observation.read(instance).check == check


@pytest.mark.parametrize(
    ("instance", "line"),
    [
        (
            {"lifecycle_state": "running", "runner_observation": CONFIRMED},
            f"ARX wants it running; runner {RUNNER} confirmed it running at 2027-01-15T08:01:00Z",
        ),
        (
            {"lifecycle_state": "running", "runner_observation": REJECTED},
            f"ARX wants it running; runner {RUNNER} refused the start at 2027-01-15T08:01:00Z "
            f"(outcome conflict, event {EVENT})",
        ),
        (
            {"lifecycle_state": "running", "runner_observation": AWAITING},
            f"ARX wants it running; runner {RUNNER} has not answered the start command yet",
        ),
        (
            {"lifecycle_state": "paused", "runner_observation": None},
            "ARX wants it paused; no runner is asked to run it",
        ),
        (
            {"lifecycle_state": "running"},
            "ARX wants it running; ARX does not report what the runner said: it is older "
            "than this tool expects",
        ),
        (
            {"lifecycle_state": "running", "runner_observation": None},
            "ARX wants it running; ARX reported something this tool does not understand: "
            "runner_observation=None",
        ),
        (
            {"runner_observation": AWAITING},
            f"ARX gives no state for it; runner {RUNNER} has not answered the start command yet",
        ),
    ],
)
def test_a_status_line_keeps_what_arx_wants_apart_from_what_the_runner_said(instance, line) -> None:
    seen = arx_deploy.observation.read(instance)
    assert arx_deploy.observation.line(seen, RUNNER) == line


def test_arx_status_lists_what_each_runner_said(fake, tmp_path, clock, monkeypatch) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    result = _deploy(op, version="0.1.0", manifest="aa")
    instance = result.receipt["first_instance_id"]

    rows = arx_deploy.observation.deployment_rows(op.admin.api, op.root)
    assert rows == [
        (
            f"{STRATEGY} 0.1.0 (sandbox)",
            f"instance {instance}: ARX wants it running; runner {RUNNER} confirmed it "
            "running at 2027-01-15T08:01:00Z",
        )
    ]

    # The runner gives up on it later: the status says so, read from ARX again, and
    # does not read as an instance that is running.
    arx.runner_says = "start_rejected"
    ((_, said),) = arx_deploy.observation.deployment_rows(op.admin.api, op.root)
    assert said.startswith(f"instance {instance}: ARX wants it running; runner {RUNNER} refused")
    assert "refused the start" in said and "retry_exhausted" in said and EVENT in said
    assert ", running:" not in said

    shown = []
    monkeypatch.setattr(arx_session.ui, "table", lambda title, rows: shown.append(rows))
    arx_session.show_deployments(op.admin.api, op.root)
    assert shown and "refused the start" in shown[0][0][1]

    # A deployment stopped, as its receipt records, is not read.
    stopper = _operator(arx, url, tmp_path, clock, root=op.root)
    arx_deploy.stop(stopper.admin, STRATEGY, mode="sandbox", root=op.root)
    reads = arx.instance_reads[instance]
    assert arx_deploy.observation.deployment_rows(op.admin.api, op.root) == []
    assert arx.instance_reads[instance] == reads


def test_arx_status_shows_an_old_arx_and_a_refused_read_without_stopping(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)
    arx.runner_says = None

    ((_, said),) = arx_deploy.observation.deployment_rows(op.admin.api, op.root)
    assert "older than this tool" in said

    arx.fail_before[("GET", "/api/v1/deployments/")] = [503]
    ((_, said),) = arx_deploy.observation.deployment_rows(op.admin.api, op.root)
    assert "not read" in said


# -- the receipt follows ARX --------------------------------------------------------------


def _archived(root: Path, spec_id: str) -> dict:
    path = _receipt_path(root)
    return json.loads(path.with_name(f"{path.stem}.{spec_id}.json").read_text())


def _console_stop(arx: FakeArx, instance_id: str) -> None:
    """What stopping an instance in the ARX console does: this machine is not told."""

    instance = arx.instances[instance_id]
    instance.update(lifecycle_state="stopped", version=instance["version"] + 1)


def test_an_instance_stopped_in_the_console_is_recorded_before_other_parameters_deploy(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op)
    _console_stop(arx, first.receipt["first_instance_id"])
    _repo(tmp_path, _settings(reason="Run it again with another reason recorded"))

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(later)

    assert len(later.asked) == 1
    assert result.receipt["first_instance_id"] != first.receipt["first_instance_id"]
    kept = _archived(op.root, first.receipt["deployment_spec_id"])
    assert kept["state"] == "stopped"
    assert kept["stopped_at"]


def test_a_receipt_still_stopping_follows_arx_once_it_has_stopped(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op)
    # make deploy-stop gave up waiting: its receipt says stopping, and ARX stopped it later.
    path = _receipt_path(op.root)
    path.write_text(json.dumps({**first.receipt, "state": "stopping", "stopped_at": None}))
    _console_stop(arx, first.receipt["first_instance_id"])
    _repo(tmp_path, _settings(reason="Run it again with another reason recorded"))

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    _deploy(later)

    kept = _archived(op.root, first.receipt["deployment_spec_id"])
    assert kept["state"] == "stopped"
    assert kept["stopped_at"]


def test_the_same_parameters_after_a_console_stop_start_the_spec_as_a_new_instance(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op)
    old = first.receipt["first_instance_id"]
    _console_stop(arx, old)
    arx.runner_says = "awaiting_runner"

    again = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(again)

    assert len(again.asked) == 1
    spec_id = first.receipt["deployment_spec_id"]
    assert len(arx.posts(f"/api/v1/deployment-specs/{spec_id}/instances")) == 1
    assert len(arx.specs) == 1
    receipt = json.loads(_receipt_path(op.root).read_text())
    assert receipt["deployment_spec_id"] == spec_id
    assert receipt["instance_id"] in arx.instances and receipt["instance_id"] != old
    assert receipt["earlier_instance_ids"] == [old]
    assert receipt["state"] == "running" and receipt["stopped_at"] is None
    # The new instance's runner is waited for, as a first instance's is.
    assert receipt["runner_waited"] is True and receipt["first_listed_by_this_run"] is True
    assert receipt["runner_check"] == "awaiting"
    _told(monkeypatch)
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == arx_deploy.EXIT_BIND_RUNNER


def test_a_receipt_that_names_no_instance_follows_the_spec_arx_lists(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    arx.project_after = 1000
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op, timeout=30, poll_seconds=5)
    assert first.receipt["first_instance_id"] is None
    # ARX lists the instance after this machine stopped watching, and it is stopped.
    arx.project_after = 0
    op.admin.read(arx_deploy.SPECS, query={"trading_mode": "sandbox"}, action="listing")
    (instance,) = arx.instances
    _console_stop(arx, instance)
    _repo(tmp_path, _settings(reason="Run it again with another reason recorded"))

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(later)

    assert len(later.asked) == 1
    assert result.receipt["first_instance_id"] not in (None, instance)
    kept = _archived(op.root, first.receipt["deployment_spec_id"])
    assert kept["first_instance_id"] == instance
    assert kept["state"] == "stopped" and kept["stopped_at"]


def test_a_spec_arx_refused_does_not_hold_back_other_parameters(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    arx.project_after = 1000
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op, timeout=30, poll_seconds=5)
    arx.specs[first.receipt["deployment_spec_id"]].update(
        projection_status="terminal_conflict", last_projection_error="a release runs already"
    )
    arx.project_after = 0
    _repo(tmp_path, _settings(reason="Run it again with another reason recorded"))

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(later)

    assert len(later.asked) == 1
    assert result.receipt["state"] == "running"
    assert _archived(op.root, first.receipt["deployment_spec_id"])["state"] == "refused"


def test_an_instance_arx_still_wants_running_holds_back_other_parameters_and_says_so(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "start_rejected"
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op)
    _repo(tmp_path, _settings(reason="Run it again with another reason recorded"))

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    with pytest.raises(ArxError) as refused:
        _deploy(later)

    assert later.asked == []
    said = str(refused.value)
    assert f"ARX wants instance {first.receipt['first_instance_id']} running" in said
    assert refused.value.fix.startswith(
        f"make deploy-stop STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"
    )


# -- starting the same spec again ------------------------------------------------------------

DEPLOY = f"make deploy STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"
STOP = f"make deploy-stop STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}"


def _rejected_then_stopped(arx: FakeArx, url: str, tmp_path: Path, clock: Clock) -> Operator:
    """A start the runner refused, then make deploy-stop: the spec is kept, unchanged."""

    _ready(arx)
    arx.runner_says = "start_rejected"
    op = _operator(arx, url, tmp_path, clock)
    _deploy(op)
    stopper = _operator(arx, url, tmp_path, clock, root=op.root)
    arx_deploy.stop(stopper.admin, STRATEGY, mode="sandbox", root=op.root)
    return op


def _materialized(arx: FakeArx, receipt: Mapping) -> list[dict]:
    return arx.posts(f"/api/v1/deployment-specs/{receipt['deployment_spec_id']}/instances")


def test_a_rejected_start_points_at_stopping_then_deploying_the_same_spec_again(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "start_rejected"
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op)
    told = _told(monkeypatch)

    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    commands = [command for command, _ in told.steps]
    assert commands.index(STOP) < commands.index(DEPLOY)
    assert "deploy.yaml" not in told.everything.replace("deploy.yaml stays as it is", "")
    assert "new spec" not in told.everything


def test_a_rejected_start_shows_the_runners_reason_code_when_arx_reports_it(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "start_rejected"
    arx.rejected_reason_code = "runtime_capacity_rejected:runner_engine_occupied"
    op = _operator(arx, url, tmp_path, clock)

    result = _deploy(op)
    told = _told(monkeypatch)

    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    (error,) = told.errors
    assert "runtime_capacity_rejected:runner_engine_occupied" in error
    assert result.receipt["runner_observation"]["reason_code"] == (
        "runtime_capacity_rejected:runner_engine_occupied"
    )


def test_deploying_a_rejected_start_once_stopped_makes_a_new_instance_with_one_code(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    op = _rejected_then_stopped(arx, url, tmp_path, clock)
    old = json.loads(_receipt_path(op.root).read_text())
    arx.runner_says = "awaiting_runner"

    starter = _operator(arx, url, tmp_path, clock, root=op.root, codes=["000000"])
    result = _deploy(starter)
    told = _told(monkeypatch)

    assert len(starter.asked) == 2  # a wrong code is asked again, as every write does
    (request,) = [r for r in _materialized(arx, old) if r["body"]["totp_code"] != "000000"]
    body = request["body"]
    assert body["trading_mode"] == "sandbox"
    assert body["deployment_spec_digest"] == old["deployment_spec_digest"]
    assert body["target_runner_id"] == RUNNER
    assert old["first_instance_id"] in body["reason"]
    assert "make deploy" in body["reason"] and "deploy-again" not in body["reason"]
    assert request["headers"].get("Idempotency-Key")
    assert len(arx.specs) == 1
    receipt = json.loads(_receipt_path(op.root).read_text())
    assert receipt["deployment_spec_id"] == old["deployment_spec_id"]
    assert receipt["first_instance_id"] == old["first_instance_id"]
    assert receipt["instance_id"] in arx.instances
    assert receipt["instance_id"] != old["first_instance_id"]
    assert receipt["earlier_instance_ids"] == [old["first_instance_id"]]
    assert receipt["state"] == "running" and receipt["stopped_at"] is None
    assert receipt["runner_check"] == "awaiting" and receipt["runner_waited"] is True
    assert receipt.get("first_listed_by_this_run") is True
    # The runner was not bound to the new instance within the wait: its steps are due,
    # as for a first one.
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == arx_deploy.EXIT_BIND_RUNNER
    assert receipt["instance_id"] in told.everything
    assert old["first_instance_id"] not in " ".join(receipt["runner_todo"][:2])
    (notice,) = _bind_notices(starter)
    assert receipt["instance_id"] in notice and old["first_instance_id"] not in notice


def test_a_new_instance_is_announced_with_the_ended_one_and_why_before_the_code(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    arx.rejected_reason_code = "runtime_capacity_rejected:runner_engine_occupied"
    op = _rejected_then_stopped(arx, url, tmp_path, clock)
    old = json.loads(_receipt_path(op.root).read_text())
    arx.runner_says = "running_confirmed"

    starter = _operator(arx, url, tmp_path, clock, root=op.root)
    events: list[tuple[str, str]] = []
    said, asked = starter.admin.notify, starter.admin.read_secret
    starter.admin.notify = lambda message: (events.append(("said", message)), said(message))[1]
    starter.admin.read_secret = lambda prompt: (events.append(("code", prompt)), asked(prompt))[1]
    _deploy(starter)

    first_code = next(i for i, (kind, _) in enumerate(events) if kind == "code")
    before_code = " ".join(message for kind, message in events[:first_code] if kind == "said")
    (announced,) = [
        message
        for kind, message in events[:first_code]
        if kind == "said" and old["first_instance_id"] in message
    ]
    assert "ended" in announced
    # Why it ended: ARX lists it as stopped, after its runner refused the start.
    assert "stopped" in announced
    assert "refused" in announced and "runtime_capacity_rejected" in announced
    assert "new instance" in announced and old["deployment_spec_id"] in announced
    assert "deploy-again" not in before_code


def test_deploying_while_arx_still_wants_the_instance_running_starts_nothing_and_asks_no_code(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    _ready(arx)
    arx.runner_says = "start_rejected"
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op)

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(later)
    told = _told(monkeypatch)

    assert later.asked == []
    assert _materialized(arx, first.receipt) == []
    assert len(arx.instances) == 1
    assert result.receipt["instance_id"] == first.receipt["first_instance_id"]
    assert result.receipt["state"] == "running"
    # It follows what ARX and the runner say, and says to stop it before deploying again.
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1
    commands = [command for command, _ in told.steps]
    assert commands.index(STOP) < commands.index(DEPLOY)


def test_deploying_a_running_confirmed_instance_again_starts_nothing(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op)

    later = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(later)

    assert later.asked == []
    assert _materialized(arx, first.receipt) == []
    assert result.error is None and result.receipt["runner_check"] == "confirmed"
    assert result.receipt["instance_id"] == first.receipt["first_instance_id"]


def test_deploying_a_rejected_start_again_waits_until_the_runner_confirms_the_new_instance(
    fake, tmp_path, clock, monkeypatch
) -> None:
    arx, url = fake
    op = _rejected_then_stopped(arx, url, tmp_path, clock)
    arx.runner_says = "running_confirmed"
    arx.answer_after = 2

    starter = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(starter, timeout=60, poll_seconds=5)
    told = _told(monkeypatch)

    # read as started, awaited once, then confirmed
    assert arx.instance_reads[result.receipt["instance_id"]] == 3
    assert result.error is None
    assert result.receipt["runner_check"] == "confirmed"
    assert result.receipt["runner_waited"] is True
    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 0
    assert told.oks and not told.errors
    (allocate,) = [step for step in told.steps if "allocat" in step[0]]
    assert result.receipt["instance_id"] in " ".join(allocate)


def test_a_lost_answer_to_starting_a_new_instance_finds_the_same_one(fake, tmp_path, clock) -> None:
    arx, url = fake
    op = _rejected_then_stopped(arx, url, tmp_path, clock)
    old = json.loads(_receipt_path(op.root).read_text())
    arx.fail_after[("POST", "/api/v1/deployment-specs/")] = [503]

    with pytest.raises(ArxError) as lost:
        _deploy(_operator(arx, url, tmp_path, clock, root=op.root))
    assert len(arx.instances) == 2
    assert json.loads(_receipt_path(op.root).read_text()) == old
    assert lost.value.fix.startswith("run make deploy again")

    result = _deploy(_operator(arx, url, tmp_path, clock, root=op.root))

    assert len(arx.instances) == 2
    keys = {r["headers"].get("Idempotency-Key") for r in _materialized(arx, old)}
    assert len(keys) == 1
    assert result.receipt["instance_id"] in arx.instances
    assert result.receipt["instance_id"] != old["first_instance_id"]


def test_codes_refused_while_starting_a_new_instance_point_at_make_deploy(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    op = _rejected_then_stopped(arx, url, tmp_path, clock)
    old = json.loads(_receipt_path(op.root).read_text())
    codes = ["000000"] * arx_deploy.runner_admin.CODE_ATTEMPTS

    with pytest.raises(ArxError) as refused:
        _deploy(_operator(arx, url, tmp_path, clock, root=op.root, codes=codes))

    assert refused.value.fix == (
        f"wait for your authenticator's next code, then run {DEPLOY} again"
    )
    assert len(arx.instances) == 1
    assert json.loads(_receipt_path(op.root).read_text()) == old


@pytest.mark.parametrize(
    ("status", "body", "said"),
    [
        (409, {"code": "version_conflict", "message": "materialization conflict",
               "correlation_id": str(uuid.uuid4()), "retryable": False}, "version_conflict"),
        (409, {"code": "idempotency_conflict", "message": "key reused",
               "correlation_id": str(uuid.uuid4()), "retryable": False}, "idempotency_conflict"),
        (422, {"code": "unprocessable_entity"}, "docs/upgrading.md"),
        (404, {"code": "NOT_FOUND", "message": "deployment spec not found"}, "NOT_FOUND"),
        (403, {"code": "FORBIDDEN", "message": "user roles do not satisfy required set"},
         "OPERATOR"),
    ],
)  # fmt: skip
def test_a_refused_new_instance_is_explained_and_changes_nothing(
    fake, tmp_path, clock, status, body, said
) -> None:
    arx, url = fake
    op = _rejected_then_stopped(arx, url, tmp_path, clock)
    old = json.loads(_receipt_path(op.root).read_text())
    arx.refuse_materialize = (status, body)

    with pytest.raises(ArxError) as refused:
        _deploy(_operator(arx, url, tmp_path, clock, root=op.root))

    assert said in f"{refused.value} {refused.value.fix}"
    assert refused.value.fix
    assert "deploy.yaml" not in refused.value.fix
    assert len(arx.instances) == 1
    assert json.loads(_receipt_path(op.root).read_text()) == old


def test_deploy_after_a_new_instance_watches_the_new_instance(fake, tmp_path, clock) -> None:
    arx, url = fake
    op = _rejected_then_stopped(arx, url, tmp_path, clock)
    arx.runner_says = "awaiting_runner"
    started = _deploy(_operator(arx, url, tmp_path, clock, root=op.root))
    new = started.receipt["instance_id"]
    arx.runner_says = "running_confirmed"

    watcher = _operator(arx, url, tmp_path, clock, root=op.root)
    result = _deploy(watcher, timeout=30, poll_seconds=5)

    assert watcher.asked == []
    assert result.error is None
    assert result.receipt["instance_id"] == new
    assert result.receipt["runner_check"] == "confirmed"
    assert result.receipt["state"] == "running"
    assert result.receipt["first_instance_id"] != new


def test_there_is_no_deploy_again_any_more() -> None:
    import subprocess

    root = Path(__file__).resolve().parents[1]
    dry = subprocess.run(
        ["make", "-n", "deploy-again", f"STRATEGY={STRATEGY}"],
        cwd=root, capture_output=True, text=True,
    )  # fmt: skip

    assert dry.returncode != 0
    assert "No rule to make target" in dry.stderr and "deploy-again" in dry.stderr
    assert not hasattr(arx_deploy, "again")
    with pytest.raises(SystemExit) as wrong:
        arx_deploy.main(["again", STRATEGY, "--mode", "sandbox"])
    assert wrong.value.code == 2


def test_make_next_reads_a_deployments_state_from_arx(fake, tmp_path, clock) -> None:
    from tools import next as next_step

    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op)
    _console_stop(arx, first.receipt["first_instance_id"])
    before = _receipt_path(op.root).read_text()

    follow = next_step.arx_follower(lambda wanted: op.admin)

    assert next_step.deployments(op.root, STRATEGY, "sandbox", follow=follow) == [
        ("0.2.0", "stopped")
    ]
    # make next only reads: the receipt is brought up to date by make deploy, not here.
    assert _receipt_path(op.root).read_text() == before


# -- a deploy inputs file ------------------------------------------------------------------


def test_a_deployment_made_with_a_deploy_inputs_file_records_it(fake, tmp_path, clock) -> None:
    arx, url = fake
    _ready(arx)
    settings = _settings()
    settings["sandbox"]["engine_binding_id"] = None
    settings["sandbox"]["credential_scope"] = {"scope_id": None, "scope_digest": None}
    op = _operator(arx, url, tmp_path, clock)
    _repo(tmp_path, settings)
    inputs = tmp_path / "deploy-inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "modes": {
                    "sandbox": {
                        "engine_binding_id": BINDING,
                        "credential_scope": {"scope_id": SCOPE_ID, "scope_digest": SCOPE_DIGEST},
                    }
                },
            }
        )
    )

    result = _deploy(op, deploy_inputs=inputs)

    assert result.error is None
    (sent,) = arx.posts("/api/v1/deployment-specs")
    assert sent["body"]["execution_channel"]["engine_binding_id"] == BINDING
    assert sent["body"]["credential_scope"]["scope_id"] == SCOPE_ID
    recorded = json.loads(result.path.read_text())["deploy_inputs"]
    assert recorded == {
        "path": str(inputs.resolve()),
        "sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("target", ["deploy", "deploy-preview"])
def test_make_passes_deploy_inputs_only_when_given(target) -> None:
    import subprocess

    root = Path(__file__).resolve().parents[1]

    def dry(*extra: str) -> str:
        return subprocess.run(
            ["make", "-n", target, f"STRATEGY={STRATEGY}", f"RUNNER={RUNNER}", *extra],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip

    assert "--deploy-inputs" not in dry()
    assert '--deploy-inputs "/tmp/inputs.json"' in dry("DEPLOY_INPUTS=/tmp/inputs.json")


def test_the_deploy_inputs_reach_the_command(monkeypatch, tmp_path) -> None:
    seen: list[tuple[str, Path | None]] = []
    monkeypatch.setattr(arx_deploy.runner_admin, "admin_for", lambda *a, **k: None)
    monkeypatch.setattr(arx_deploy.arx_spec, "session_tenant", lambda *a, **k: TENANT)

    def preview(admin, strategy, **options):
        seen.append(("preview", options["deploy_inputs"]))
        raise ArxError("stop here")

    def deploy(admin, strategy, **options):
        seen.append(("deploy", options["deploy_inputs"]))
        raise ArxError("stop here")

    monkeypatch.setattr(arx_deploy, "preview", preview)
    monkeypatch.setattr(arx_deploy, "deploy", deploy)
    monkeypatch.setattr(arx_deploy.ui, "error", lambda *a, **k: None)
    common = [STRATEGY, "--mode", "sandbox", "--runner", RUNNER]

    arx_deploy.main(["preview", *common, "--deploy-inputs", "/tmp/a.json"])
    arx_deploy.main(["deploy", *common, "--deploy-inputs", "/tmp/b.json"])
    arx_deploy.main(["deploy", *common])

    assert seen == [
        ("preview", Path("/tmp/a.json")),
        ("deploy", Path("/tmp/b.json")),
        ("deploy", None),
    ]


def _inputs_repo(tmp_path) -> Path:
    """A deploy.yaml whose binding is left empty, and a deploy inputs file that fills it."""
    settings = _settings()
    settings["sandbox"]["engine_binding_id"] = None
    settings["sandbox"]["credential_scope"] = {"scope_id": None, "scope_digest": None}
    _repo(tmp_path, settings)
    inputs = tmp_path / "deploy-inputs.json"
    inputs.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "modes": {
                    "sandbox": {
                        "engine_binding_id": BINDING,
                        "credential_scope": {"scope_id": SCOPE_ID, "scope_digest": SCOPE_DIGEST},
                    }
                },
            }
        )
    )
    return inputs


def test_a_product_awaiting_capital_names_the_deploy_inputs_in_the_next_deploy(
    fake, tmp_path, clock, monkeypatch
) -> None:
    # Without the file the next make deploy stops at "fill in" before anything
    # is sent: deploy.yaml leaves the binding to it.
    arx, url = fake
    op = _operator(arx, url, tmp_path, clock)
    inputs = _inputs_repo(tmp_path)
    result = _deploy(op, product=None, deploy_inputs=inputs)
    steps = _steps(monkeypatch)

    assert isinstance(result, arx_deploy.AwaitingProduct)
    assert arx_deploy._awaiting_product(STRATEGY, "sandbox", result) == 0
    commands = [command for command, _ in steps]
    assert (
        f"make deploy STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER} "
        f"DEPLOY_INPUTS={inputs.resolve()}"
    ) in commands


def test_a_deployment_made_with_deploy_inputs_names_them_when_it_is_run_again(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    _ready(arx)
    op = _operator(arx, url, tmp_path, clock)
    inputs = _inputs_repo(tmp_path)
    result = _deploy(op, deploy_inputs=inputs)

    command = arx_deploy._deploy_command(STRATEGY, "sandbox", result.receipt)

    assert command.endswith(f" DEPLOY_INPUTS={inputs.resolve()}")
    assert arx_deploy._deploy_command(
        STRATEGY, "sandbox", {**result.receipt, "deploy_inputs": None}
    ) == (f"make deploy STRATEGY={STRATEGY} MODE=sandbox VERSION=0.2.0 RUNNER={RUNNER}")


def _refused_on_a_later_run(fake, tmp_path, clock):
    """A spec ARX refused after the run that created it had stopped watching."""
    arx, url = fake
    _ready(arx)
    arx.project_after = 1000
    op = _operator(arx, url, tmp_path, clock)
    first = _deploy(op, timeout=30, poll_seconds=5)
    arx.specs[first.receipt["deployment_spec_id"]].update(
        projection_status="rejected_policy",
        last_projection_error="another release is running for this strategy and mode",
    )
    arx.project_after = 0
    later = _operator(arx, url, tmp_path, clock, root=op.root)
    return _deploy(later)


def test_a_spec_found_refused_on_a_later_run_keeps_arxs_reason(fake, tmp_path, clock) -> None:
    result = _refused_on_a_later_run(fake, tmp_path, clock)

    assert result.receipt["state"] == "refused"
    assert (
        result.receipt["projection_error"]
        == "another release is running for this strategy and mode"
    )


def test_following_a_refused_spec_records_arxs_reason(fake, tmp_path, clock) -> None:
    # make next and make deploy-stop follow a receipt without watching it.
    result = _refused_on_a_later_run(fake, tmp_path, clock)
    arx, url = fake
    reader = _operator(arx, url, tmp_path, clock, root=result.path.parents[4])
    receipt = {k: v for k, v in result.receipt.items() if k != "projection_error"}

    state, _ = arx_deploy.follow_arx(reader.admin, receipt, "sandbox")

    assert state == "refused"
    assert receipt["projection_error"] == "another release is running for this strategy and mode"


def test_a_refused_spec_says_arx_made_no_instance_and_what_to_change(
    fake, tmp_path, clock, monkeypatch
) -> None:
    # Measured 2026-10-05: on the normal paths ARX refuses a conflict when the
    # spec is created, and a projection refused later leaves the spec with no
    # instance at all, so there is nothing to start again: the way on is to put
    # the refusal right and make a new spec.
    result = _refused_on_a_later_run(fake, tmp_path, clock)
    errors: list[str] = []
    steps: list[tuple[str, str]] = []
    monkeypatch.setattr(arx_deploy.ui, "next_steps", steps.extend)
    monkeypatch.setattr(arx_deploy.ui, "error", lambda message, **_: errors.append(message))
    for name in ("ok", "info", "warn", "table"):
        monkeypatch.setattr(arx_deploy.ui, name, lambda *a, **k: None)

    assert arx_deploy._after_deploy(STRATEGY, "sandbox", result) == 1

    said = " ".join(errors + [f"{command} {why}" for command, why in steps])
    assert "another release is running for this strategy and mode" in said
    assert "made no instance" in said
    assert "nothing to start again" in said
    assert "materiali" not in said
