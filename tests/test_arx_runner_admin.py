"""Bringing a runner into service: enrollment, naming, transport and safety policy.

A fake ARX runs in this process on 127.0.0.1. It knows two people, keeps the
organisation's runners, checks each write's authenticator code and accepts each
30-second code once, checks idempotency keys and the mode header where ARX's
Runner Administration API guide asks for them, and refuses an approval by the
person who asked. The sessions are written into the session file directly: how
one is signed in to is tested elsewhere.
"""

from __future__ import annotations

import io
import json
import os
import stat
import threading
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.arx import runner_admin
from tools.arx import session as arx_session
from tools.arx.client import ArxError

TENANT = "tenant_a"
ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
RUNNER = "5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b"
TOKEN = "enrl_SECRET_7f3a9c1e5b2d4f60"
CAPABILITY = {
    "capability_version_id": "7c8d9e0f-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
    "capability_version": 1,
    "manifest_digest": "sha256:" + "ab" * 32,
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
    tokens: dict[str, str] = field(default_factory=dict)  # access token -> user id
    runners: dict[str, str | None] = field(default_factory=dict)  # id -> display name
    used_steps: dict[str, set[int]] = field(default_factory=dict)
    requests: list[dict] = field(default_factory=list)
    policy_requests: dict[str, dict] = field(default_factory=dict)
    issued: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def writes(self) -> list[dict]:
        return [r for r in self.requests if r["method"] != "GET"]


def _handler(fake: FakeArx):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _reply(self, status: int, body=None) -> None:
            payload = json.dumps(body).encode() if body is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PUT(self) -> None:
            self._route("PUT")

        def _route(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            with fake.lock:
                fake.requests.append(
                    {
                        "method": method,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }  # fmt: skip
                )
            auth = self.headers.get("Authorization", "")
            user = fake.tokens.get(auth.removeprefix("Bearer "))
            if user is None:
                return self._reply(401, {"error": "invalid session"})
            if self.headers.get("X-Tenant-Id") != TENANT:
                return self._reply(403, {"code": "CROSS_TENANT_DENIED"})
            path, _, query = self.path.partition("?")
            mode = body.get("trading_mode") or dict(
                part.split("=", 1) for part in query.split("&") if "=" in part
            ).get("trading_mode")
            if mode is not None and self.headers.get("X-Trading-Mode") != mode:
                return self._reply(400, {"code": "trading_mode_mismatch"})
            if method != "GET":
                if path != "/api/v1/runner-enrollment-tokens":
                    key = self.headers.get("Idempotency-Key")
                    if not key or uuid.UUID(key).int == 0:
                        return self._reply(400, {"code": "idempotency_key_invalid"})
                elif "Idempotency-Key" in self.headers:
                    return self._reply(400, {"code": "unexpected idempotency key"})
                step = int(fake.clock() // 30)
                used = fake.used_steps.setdefault(user, set())
                if body.get("totp_code") != code_for(step) or step in used:
                    return self._reply(401, {"code": "totp_invalid", "message": "TOTP failed"})
                used.add(step)
            parts = path.strip("/").split("/")
            if path == "/api/v1/runners" and method == "GET":
                return self._reply(
                    200,
                    [
                        {"runner_id": rid, "display_name": name, "agent_version": "0.5.2"}
                        for rid, name in fake.runners.items()
                    ],
                )
            if path == "/api/v1/runner-enrollment-tokens":
                if set(body) != {"totp_code", "runner_hint", "paper_only", "scope"}:
                    return self._reply(400, {"code": "invalid_request"})
                if not 1 <= body["scope"] <= 63:
                    return self._reply(400, {"code": "runner_scope_invalid"})
                fake.issued += 1
                return self._reply(
                    201,
                    {
                        "token": TOKEN,
                        "token_id": "tok-1",
                        "tenant_id": TENANT,
                        "scope": body["scope"],
                        "paper_only": body["paper_only"],
                        "expires_at": "2027-01-16T08:00:00Z",
                        "proof_algorithm": "ed25519",
                    },
                )
            if parts[:3] == ["api", "v1", "runners"] and len(parts) >= 5:
                runner = parts[3]
                if parts[4] == "business-identity":
                    if set(body) != {"display_name", "totp_code"}:
                        return self._reply(400, {"code": "invalid_request"})
                    if runner not in fake.runners:
                        return self._reply(404, {"code": "runner_not_found"})
                    if fake.runners[runner] not in (None, body["display_name"]):
                        return self._reply(409, {"code": "runner_business_identity_conflict"})
                    fake.runners[runner] = body["display_name"]
                    return self._reply(
                        201,
                        {
                            "runner_id": runner,
                            "display_name": body["display_name"],
                            "status": "registered",
                        },  # fmt: skip
                    )
                if parts[4] == "nats-transport-intents":
                    expected = {"totp_code", "trading_mode", "operation_kind",
                                "expected_active_generation"}  # fmt: skip
                    if set(body) != expected:
                        return self._reply(400, {"code": "invalid_request"})
                    return self._reply(
                        201,
                        {
                            "authorization_intent_id": "intent-42",
                            "runner_id": runner,
                            "trading_mode": body["trading_mode"],
                            "operation_kind": body["operation_kind"],
                            "expected_active_generation": body["expected_active_generation"],
                            "expires_at": "2027-01-15T08:15:00Z",
                            "intent_fingerprint": "fp",
                            "replayed": False,
                        },
                    )
                if parts[4] == "safety-policy-requests" and len(parts) == 5:
                    request_id = str(uuid.uuid4())
                    stored = {k: v for k, v in body.items() if k != "totp_code"}
                    stored.update(
                        request_id=request_id, request_version=1, status="pending",
                        applicant_id=user,
                    )  # fmt: skip
                    fake.policy_requests[request_id] = stored
                    return self._reply(201, stored)
                if parts[4] == "safety-policy-requests" and len(parts) >= 6:
                    found = fake.policy_requests.get(parts[5])
                    if found is None:
                        return self._reply(404, {"code": "not_found"})
                    if method == "GET":
                        return self._reply(200, found)
                    if body.get("expected_request_version") != found["request_version"]:
                        return self._reply(409, {"code": "conflict"})
                    if parts[6] == "approve":
                        if user == found["applicant_id"]:
                            return self._reply(403, {"code": "forbidden"})
                        found.update(status="approved", approved_by=user, request_version=2)
                        return self._reply(200, found)
                    if parts[6] == "activate":
                        return self._reply(
                            201,
                            {"created": True, "policy": {"revision": 1, "status": "active",
                             "max_order_notional": found["max_order_notional"],
                             "max_total_notional": found["max_total_notional"],
                             "settlement_currency": found["settlement_currency"]}},
                        )  # fmt: skip
            return self._reply(404, {"code": "not_found"})

    return Handler


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


def _signed_in(arx: FakeArx, url: str, directory: Path, user: str, clock) -> arx_session.HostStore:
    store = arx_session.HostStore(directory)
    access = uuid.uuid4().hex
    arx.tokens[access] = user
    now = clock()
    session = arx_session.Session(
        url=arx_session.base_url(url),
        email=f"{user[:4]}@example.com",
        user_id=user,
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


def _admin(
    arx, url, tmp_path, clock, *, user=ALICE, codes=None, name="alice"
) -> runner_admin.Admin:
    store = _signed_in(arx, url, tmp_path / name, user, clock)
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
    admin.asked = asked  # type: ignore[attr-defined]
    return admin


# -- enrollment ---------------------------------------------------------------------


def test_a_new_runner_gets_a_token_in_a_private_file_only(fake, tmp_path, clock) -> None:
    arx, url = fake
    admin = _admin(arx, url, tmp_path, clock)

    result = runner_admin.enroll(
        admin, runner_id=RUNNER, name="Box 1", scope=3, token_directory=tmp_path / "enroll"
    )

    assert result.done == "token issued"
    path = tmp_path / "enroll" / f"{RUNNER}.token"
    assert result.token_path == path
    assert path.read_text() == TOKEN + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "enroll").stat().st_mode) == 0o700
    assert TOKEN not in json.dumps(result.details)
    sent = arx.writes()[-1]
    assert sent["body"]["runner_hint"] == RUNNER
    assert sent["body"]["paper_only"] is True and sent["body"]["scope"] == 3
    assert "Idempotency-Key" not in sent["headers"]


def test_the_token_is_never_on_screen_in_argv_or_in_an_error(
    fake, tmp_path, clock, monkeypatch, capsys
) -> None:
    arx, url = fake
    _signed_in(arx, url, tmp_path / "config", ALICE, time.time)
    monkeypatch.setattr(arx_session, "config_directory", lambda: tmp_path / "config")
    monkeypatch.setattr(runner_admin, "TOKEN_DIRECTORY", tmp_path / "enroll")
    monkeypatch.setattr(runner_admin, "ROOT", tmp_path)
    arx.clock = time.time  # type: ignore[assignment]
    monkeypatch.setattr("sys.stdin", io.StringIO(code_for(int(time.time() // 30)) + "\n"))
    monkeypatch.setenv("COLUMNS", "400")
    argv = ["enroll", "--runner", RUNNER, "--name", "Box 1", "--scope", "3"]

    assert runner_admin.main(argv) == 0

    out, err = capsys.readouterr()
    assert TOKEN not in out and TOKEN not in err and TOKEN not in " ".join(argv)
    assert (tmp_path / "enroll" / f"{RUNNER}.token").read_text().strip() == TOKEN
    # What the runner's operator needs next is all said.
    assert f"enroll/{RUNNER}.token" in out
    assert "enroll the runner with the token file" in out
    assert "runner's enrollment guide" in out
    assert f'make enroll-runner RUNNER={RUNNER} NAME="Box 1"' in out
    assert "2027-01-16T08:00:00Z" in out


def test_a_token_directory_others_can_open_is_refused_before_a_token(fake, tmp_path, clock) -> None:
    arx, url = fake
    admin = _admin(arx, url, tmp_path, clock)
    (tmp_path / "enroll").mkdir(mode=0o755)

    with pytest.raises(ArxError) as refused:
        runner_admin.enroll(
            admin, runner_id=RUNNER, name="Box 1", scope=3, token_directory=tmp_path / "enroll"
        )

    assert "chmod 700" in refused.value.fix
    assert admin.asked == [] and arx.issued == 0
    assert not list((tmp_path / "enroll").iterdir())


def test_a_wrong_code_writes_no_file_and_is_asked_again_three_times(fake, tmp_path, clock) -> None:
    arx, url = fake
    admin = _admin(arx, url, tmp_path, clock, codes=["000000", "000001", "000002"])

    with pytest.raises(ArxError, match="did not accept the authenticator code"):
        runner_admin.enroll(
            admin, runner_id=RUNNER, name="Box 1", scope=3, token_directory=tmp_path / "enroll"
        )

    assert len(admin.asked) == 3
    assert arx.issued == 0
    assert not (tmp_path / "enroll").exists()
    with admin.store.transaction() as data:
        assert next(iter(data["hosts"].values()))["code_step"] is None


def test_scope_is_checked_before_a_code_is_asked_for(fake, tmp_path, clock) -> None:
    arx, url = fake
    admin = _admin(arx, url, tmp_path, clock)

    for scope in (None, 0, 64):
        with pytest.raises(ArxError, match="SCOPE"):
            runner_admin.enroll(
                admin, runner_id=RUNNER, name="Box 1", scope=scope, token_directory=tmp_path / "e"
            )
    assert admin.asked == [] and arx.writes() == []


def test_an_enrolled_runner_is_named_not_given_a_second_token(fake, tmp_path, clock) -> None:
    arx, url = fake
    arx.runners[RUNNER] = None
    admin = _admin(arx, url, tmp_path, clock)

    result = runner_admin.enroll(
        admin, runner_id=RUNNER, name="Box 1", scope=None, token_directory=tmp_path / "enroll"
    )

    assert result.done == "named"
    assert arx.runners[RUNNER] == "Box 1" and arx.issued == 0
    sent = arx.writes()[-1]
    assert sent["method"] == "PUT" and sent["path"].endswith(f"/{RUNNER}/business-identity")
    assert set(sent["body"]) == {"display_name", "totp_code"}
    assert uuid.UUID(sent["headers"]["Idempotency-Key"]).int != 0
    assert not (tmp_path / "enroll").exists()


def test_a_runner_already_named_so_needs_no_code(fake, tmp_path, clock) -> None:
    arx, url = fake
    arx.runners[RUNNER] = "Box 1"
    admin = _admin(arx, url, tmp_path, clock)

    assert runner_admin.enroll(admin, runner_id=RUNNER, name="Box 1", scope=None).done == (
        "already named"
    )
    with pytest.raises(ArxError, match="already named 'Box 1'"):
        runner_admin.enroll(admin, runner_id=RUNNER, name="Box 2", scope=None)
    assert admin.asked == [] and arx.writes() == []


def test_two_writes_in_one_code_step_wait_for_the_next_code(fake, tmp_path, clock) -> None:
    arx, url = fake
    admin = _admin(arx, url, tmp_path, clock)
    waited: list[float] = []
    admin.sleep = lambda seconds: (waited.append(seconds), clock.sleep(seconds))  # type: ignore

    runner_admin.enroll(admin, runner_id=RUNNER, name="Box 1", scope=3, token_directory=tmp_path)
    arx.runners[RUNNER] = None
    runner_admin.enroll(admin, runner_id=RUNNER, name="Box 1", scope=None)

    assert len(waited) == 1 and 0 < waited[0] <= 31
    assert arx.runners[RUNNER] == "Box 1"


# -- transport ---------------------------------------------------------------------------


def test_transport_intent_is_authorised_with_its_mode_and_key(fake, tmp_path, clock) -> None:
    arx, url = fake
    arx.runners[RUNNER] = "Box 1"
    admin = _admin(arx, url, tmp_path, clock)

    intent = runner_admin.authorize_transport(admin, runner_id=RUNNER, mode="testnet")

    assert intent["authorization_intent_id"] == "intent-42"
    sent = arx.writes()[-1]
    assert sent["headers"]["X-Trading-Mode"] == "testnet"
    assert sent["body"]["operation_kind"] == "issue"
    assert sent["body"]["expected_active_generation"] is None


def test_transport_next_steps_name_the_intent_and_its_expiry(
    fake, tmp_path, monkeypatch, capsys
) -> None:
    arx, url = fake
    arx.runners[RUNNER] = "Box 1"
    arx.clock = time.time  # type: ignore[assignment]
    _signed_in(arx, url, tmp_path / "config", ALICE, time.time)
    monkeypatch.setattr(arx_session, "config_directory", lambda: tmp_path / "config")
    monkeypatch.setattr("sys.stdin", io.StringIO(code_for(int(time.time() // 30)) + "\n"))
    monkeypatch.setenv("COLUMNS", "400")

    assert runner_admin.main(["authorize-transport", "--runner", RUNNER, "--mode", "sandbox"]) == 0

    out = capsys.readouterr().out
    assert "intent-42" in out and "2027-01-15T08:15:00Z" in out
    assert "runner's transport command" in out
    assert f"make runner-safety-policy ACTION=submit RUNNER={RUNNER} MODE=sandbox" in out


@pytest.mark.parametrize(
    ("operation", "generation", "message"),
    [("issue", 2, "only for rotate"), ("rotate", None, "needs GENERATION"), ("renew", None, "")],
)
def test_transport_arguments_are_checked_before_a_code(
    fake, tmp_path, clock, operation, generation, message
) -> None:
    arx, url = fake
    arx.runners[RUNNER] = "Box 1"
    admin = _admin(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match=message or "OPERATION"):
        runner_admin.authorize_transport(
            admin, runner_id=RUNNER, mode="sandbox", operation=operation, generation=generation
        )
    assert admin.asked == []


def test_transport_for_a_runner_not_enrolled_is_refused_before_a_code(
    fake, tmp_path, clock
) -> None:
    arx, url = fake
    admin = _admin(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match="not enrolled"):
        runner_admin.authorize_transport(admin, runner_id=RUNNER, mode="sandbox")
    assert admin.asked == [] and arx.writes() == []


# -- safety policy ----------------------------------------------------------------------------


def _policy(**changes) -> dict:
    values = {
        "settlement_currency": "USDT",
        "max_order_notional": "250.5",
        "max_total_notional": "1000",
        "effective_at": "2027-01-01T00:00:00Z",
        "expires_at": "2027-04-01T00:00:00Z",
        "capability": dict(CAPABILITY),
        "reason": "Cap the sandbox runner",
    }
    values.update(changes)
    return values


def test_a_policy_is_asked_approved_by_another_and_activated(fake, tmp_path, clock) -> None:
    arx, url = fake
    alice = _admin(arx, url, tmp_path, clock)
    bob = _admin(arx, url, tmp_path, clock, user=BOB, name="bob")

    asked = runner_admin.submit_safety_policy(alice, runner_id=RUNNER, mode="sandbox",
                                              values=_policy())  # fmt: skip
    request = asked["request_id"]
    sent = arx.writes()[-1]["body"]
    assert sent["max_order_notional"] == "250.5" and sent["expected_current_revision"] is None
    assert sent["trading_mode"] == "sandbox"

    runner_admin.approve_safety_policy(
        bob, runner_id=RUNNER, mode="sandbox", request_id=request, reason="Checked the caps"
    )
    active = runner_admin.activate_safety_policy(
        bob, runner_id=RUNNER, mode="sandbox", request_id=request
    )

    assert active["policy"]["status"] == "active"
    assert arx.writes()[-1]["body"]["expected_request_version"] == 2


def test_approving_your_own_request_is_refused_before_a_code(fake, tmp_path, clock) -> None:
    arx, url = fake
    alice = _admin(arx, url, tmp_path, clock)
    request = runner_admin.submit_safety_policy(
        alice, runner_id=RUNNER, mode="sandbox", values=_policy()
    )["request_id"]
    asked_before, writes_before = len(alice.asked), len(arx.writes())

    with pytest.raises(ArxError, match="you asked for this safety policy"):
        runner_admin.approve_safety_policy(
            alice, runner_id=RUNNER, mode="sandbox", request_id=request, reason="Looks right"
        )

    assert len(alice.asked) == asked_before and len(arx.writes()) == writes_before
    assert arx.policy_requests[request]["status"] == "pending"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"max_order_notional": 250.5}, "decimal string"),
        ({"max_total_notional": 1000}, "decimal string"),
        ({"settlement_currency": "EUR"}, "settlement_currency"),
        ({"expires_at": "2026-12-01T00:00:00Z"}, "after effective_at"),
        ({"effective_at": "2027-01-01T00:00:00"}, "offset"),
        ({"capability": {"capability_version": 1}}, "capability"),
        ({"leverage": "2"}, "unknown leverage"),
        ({"reason": " "}, "reason"),
    ],
)
def test_a_request_with_a_wrong_field_is_refused_before_a_code(
    fake, tmp_path, clock, changes, message
) -> None:
    arx, url = fake
    alice = _admin(arx, url, tmp_path, clock)

    with pytest.raises(ArxError, match=message):
        runner_admin.submit_safety_policy(
            alice, runner_id=RUNNER, mode="sandbox", values=_policy(**changes)
        )
    assert alice.asked == [] and arx.writes() == []


def test_request_fields_come_from_the_file_and_the_command_line(tmp_path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy(max_total_notional="900")))
    args = runner_admin.argparse.Namespace(
        file=str(path), currency=None, max_order="100", max_total=None, effective_at=None,
        expires_at=None, reason=None, revision="4",
    )  # fmt: skip

    values = runner_admin._values(args)

    assert values["max_order_notional"] == "100" and values["max_total_notional"] == "900"
    assert values["expected_current_revision"] == 4


def test_error_text_never_carries_the_code_or_session(fake, tmp_path, clock) -> None:
    arx, url = fake
    admin = _admin(arx, url, tmp_path, clock, codes=["123456", "123457", "123458"])

    with pytest.raises(ArxError) as refused:
        runner_admin.submit_safety_policy(admin, runner_id=RUNNER, mode="sandbox",
                                          values=_policy())  # fmt: skip

    text = f"{refused.value} {refused.value.fix}"
    for secret in ("123456", "123457", "123458", *arx.tokens):
        assert secret not in text


def test_the_token_directory_is_ignored_by_git() -> None:
    ignored = (Path(__file__).resolve().parents[1] / ".gitignore").read_text().splitlines()
    assert ".runner-enroll/" in ignored


def test_a_token_file_is_not_written_through_a_link(tmp_path) -> None:
    directory = tmp_path / "enroll"
    directory.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    os.symlink(target, directory / f"{RUNNER}.token")

    path = runner_admin.write_token(directory, RUNNER, TOKEN)

    assert not path.is_symlink() and not target.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
