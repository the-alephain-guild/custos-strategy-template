"""Signing in to ARX, keeping the session and ending it.

A fake ARX runs in this process on 127.0.0.1: it knows one account, asks for an
authenticator code, accepts each 30-second code once, rotates the refresh cookie
on every refresh and checks the organisation header. Its clock is the test's, so
tokens expire and codes change when a test moves the clock.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tools.arx import client as arx_client
from tools.arx import session as arx_session

EMAIL = "author@example.com"
PASSWORD = "correct horse battery staple"
TENANT = "tenant_a"
ACCESS_SECONDS = 15 * 60
REFRESH_SECONDS = 7 * 24 * 3600


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
    refresh_delay: float = 0.0
    challenges: dict[str, float] = field(default_factory=dict)
    used_steps: set[int] = field(default_factory=set)
    # token -> expiry, for access and refresh tokens separately
    access: dict[str, float] = field(default_factory=dict)
    refresh: dict[str, tuple[float, str]] = field(default_factory=dict)
    requests: list[dict] = field(default_factory=list)
    refreshes: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def code_now(self) -> str:
        return code_for(int(self.clock() // 30))

    def issue(self) -> tuple[str, str]:
        access, refresh = uuid.uuid4().hex, uuid.uuid4().hex
        self.access[access] = self.clock() + ACCESS_SECONDS
        self.refresh[refresh] = (self.clock() + REFRESH_SECONDS, access)
        return access, refresh

    def valid_access(self, header: str | None) -> bool:
        if not header or not header.startswith("Bearer "):
            return False
        expiry = self.access.get(header.removeprefix("Bearer "))
        return expiry is not None and expiry > self.clock()


def _membership(tenant: str, role: str, *, default: bool) -> dict:
    return {
        "tenant_id": tenant,
        "tenant_display_name": tenant,
        "roles": [role],
        "is_default": default,
        "version": 1,
    }


def _handler(fake: FakeArx):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:  # keep test output quiet
            pass

        def _reply(self, status: int, body=None, cookies=(), text: str | None = None) -> None:
            payload = b""
            if text is not None:
                payload = text.encode()
            elif body is not None:
                payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header(
                "Content-Type", "text/plain" if text is not None else "application/json"
            )
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def _signed_in(self, access: str, refresh: str) -> None:
            self._reply(
                200,
                {"requires_totp": False, "access_token": access, "expires_at": "…"},
                cookies=(
                    f"arx_refresh={refresh}; Path=/api/v1/auth; HttpOnly; SameSite=Strict; "
                    f"Secure; Max-Age={REFRESH_SECONDS}",
                    "arx_console=console-only; Path=/; SameSite=Strict; Secure",
                ),
            )

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def _route(self, method: str) -> None:
            body = self._body() if method == "POST" else {}
            with fake.lock:
                fake.requests.append(
                    {"method": method, "path": self.path, "headers": dict(self.headers)}
                )
            path = self.path.split("?")[0]
            if path == "/api/v1/auth/login":
                if body != {"email": EMAIL, "password": PASSWORD}:
                    return self._reply(
                        401, {"error": "authentication failed", "restart_login": False}
                    )
                token = uuid.uuid4().hex
                fake.challenges[token] = fake.clock() + 300
                return self._reply(
                    200,
                    {"requires_totp": True, "totp_challenge_token": token, "expires_at": "…"},
                )
            if path == "/api/v1/auth/totp-challenge":
                if set(body) != {"challenge_token", "code"}:
                    return self._reply(400, {"error": "invalid_request"})
                expiry = fake.challenges.get(body["challenge_token"])
                if expiry is None or expiry <= fake.clock():
                    return self._reply(401, {"error": "challenge expired", "restart_login": True})
                step = int(fake.clock() // 30)
                if body["code"] != code_for(step) or step in fake.used_steps:
                    return self._reply(
                        401, {"error": "authentication failed", "restart_login": False}
                    )
                fake.used_steps.add(step)
                del fake.challenges[body["challenge_token"]]
                return self._signed_in(*fake.issue())
            if path == "/api/v1/auth/refresh":
                cookie = self.headers.get("Cookie", "")
                token = dict(
                    part.strip().split("=", 1) for part in cookie.split(";") if "=" in part
                ).get("arx_refresh")
                if fake.refresh_delay:
                    threading.Event().wait(fake.refresh_delay)
                with fake.lock:
                    found = fake.refresh.pop(token, None)
                    if found is None or found[0] <= fake.clock():
                        return self._reply(401, {"error": "invalid refresh token"})
                    fake.access.pop(found[1], None)
                    fake.refreshes += 1
                    return self._signed_in(*fake.issue())
            if path == "/api/v1/auth/session":
                if not fake.valid_access(self.headers.get("Authorization")):
                    return self._reply(401, {"error": "invalid session"})
                return self._reply(
                    200,
                    {
                        "id": "user-1",
                        "tenant_id": TENANT,
                        "email": EMAIL,
                        "roles": ["strategist"],
                        "memberships": [
                            _membership(TENANT, "strategist", default=True),
                            _membership("tenant_b", "operator", default=False),
                        ],
                        "totp_enabled": True,
                    },
                )
            if path == "/api/v1/auth/logout":
                header = self.headers.get("Authorization") or ""
                if not fake.valid_access(header):
                    return self._reply(401, {"error": "invalid session"})
                fake.access.pop(header.removeprefix("Bearer "), None)
                for token, (_, access) in list(fake.refresh.items()):
                    if access == header.removeprefix("Bearer "):
                        del fake.refresh[token]
                return self._reply(204)
            # Organisation endpoints.
            if not fake.valid_access(self.headers.get("Authorization")):
                return self._reply(401, text="invalid session")
            if self.headers.get("X-Tenant-Id") not in (TENANT, "tenant_b"):
                return self._reply(401, text="missing tenant context")
            if path == "/api/v1/echo":
                # A careless server that repeats the request's credentials back.
                return self._reply(
                    400, {"error": "invalid_request", "message": self.headers.get("Authorization")}
                )
            if method == "POST":
                key = self.headers.get("Idempotency-Key")
                if not key or uuid.UUID(key).int == 0:
                    return self._reply(
                        400, {"error": "missing_required_header", "header": "Idempotency-Key"}
                    )
            return self._reply(200, {"ok": True, "path": path})

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


@pytest.fixture
def store(tmp_path: Path) -> arx_session.HostStore:
    return arx_session.HostStore(tmp_path / "config" / "custos-strategy" / "arx")


def _answers(arx: FakeArx, codes: list[str] | None = None):
    """Answers the password and code prompts; codes, when given, are used in order."""
    asked: list[str] = []

    def read_secret(prompt: str) -> str:
        asked.append(prompt)
        if "password" in prompt.lower():
            return PASSWORD
        return codes.pop(0) if codes else arx.code_now()

    read_secret.asked = asked  # type: ignore[attr-defined]
    return read_secret


def _login(url: str, arx: FakeArx, store, clock: Clock, **overrides):
    arguments = {
        "email": EMAIL,
        "store": store,
        "read_secret": _answers(arx),
        "clock": clock,
        "sleep": clock.sleep,
        "notify": lambda message: None,
    }
    arguments.update(overrides)
    return arx_session.login(url, **arguments)


# Sign-in


def test_two_step_sign_in_keeps_the_session_for_that_url(fake, store, clock) -> None:
    arx, url = fake

    signed_in = _login(url, arx, store, clock)

    assert signed_in.tenant_id == TENANT
    assert signed_in.roles == ["strategist"]
    assert signed_in.email == EMAIL
    kept = json.loads(store.path.read_text())
    assert list(kept["hosts"]) == [url]
    entry = kept["hosts"][url]
    assert entry["access_token"] in arx.access
    assert entry["refresh_token"] in arx.refresh
    # The console's own cookie is not the refresh cookie and is not kept.
    assert "console-only" not in store.path.read_text()
    assert PASSWORD not in store.path.read_text()
    # Sign-in and session calls name no organisation and need no idempotency key.
    for request in arx.requests:
        assert "X-Tenant-Id" not in request["headers"]
        assert "Idempotency-Key" not in request["headers"]


def test_a_chosen_organisation_must_be_one_of_the_memberships(fake, store, clock) -> None:
    arx, url = fake

    assert _login(url, arx, store, clock, tenant="tenant_b").tenant_id == "tenant_b"
    clock.sleep(30)
    with pytest.raises(arx_client.ArxError, match="tenant_c") as refused:
        _login(url, arx, store, clock, tenant="tenant_c")
    assert "tenant_a" in str(refused.value) and "tenant_b" in str(refused.value)


def test_a_wrong_code_is_asked_again_and_then_refused(fake, store, clock) -> None:
    arx, url = fake
    read_secret = _answers(arx, codes=["000000", "111111", "222222"])

    with pytest.raises(arx_client.ArxError, match="code") as refused:
        _login(url, arx, store, clock, read_secret=read_secret)

    assert len([p for p in read_secret.asked if "code" in p.lower()]) == 3
    assert "make arx-login" in refused.value.fix
    assert not store.path.exists() or json.loads(store.path.read_text())["hosts"] == {}


def test_a_wrong_code_then_the_right_one_signs_in(fake, store, clock) -> None:
    arx, url = fake
    read_secret = _answers(arx, codes=["000000", code_for(int(clock() // 30))])

    assert _login(url, arx, store, clock, read_secret=read_secret).tenant_id == TENANT


def test_a_wrong_password_is_refused_without_asking_for_a_code(fake, store, clock) -> None:
    arx, url = fake
    asked: list[str] = []

    def read_secret(prompt: str) -> str:
        asked.append(prompt)
        return "wrong"

    with pytest.raises(arx_client.ArxError, match="email or password"):
        _login(url, arx, store, clock, read_secret=read_secret)
    assert len(asked) == 1


def test_signing_in_again_in_the_same_step_waits_for_the_next_code(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    said: list[str] = []
    started = clock()

    _login(url, arx, store, clock, notify=said.append)

    assert clock() - started >= 1
    assert int(clock() // 30) == int(started // 30) + 1
    assert any("next code" in line for line in said)


def test_a_code_needed_right_after_sign_in_waits_for_the_next_step(fake, store, clock) -> None:
    arx, url = fake
    signed_in = _login(url, arx, store, clock)
    said: list[str] = []
    step = int(clock() // 30)

    arx_session.wait_for_fresh_code(signed_in, clock=clock, sleep=clock.sleep, notify=said.append)

    assert int(clock() // 30) == step + 1
    assert said and "next code" in said[0]
    said.clear()
    arx_session.wait_for_fresh_code(signed_in, clock=clock, sleep=clock.sleep, notify=said.append)
    assert said == []


# Using and refreshing the session


def test_requests_carry_bearer_tenant_and_idempotency_key(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    api = arx_session.client(url, store=store, clock=clock)
    arx.requests.clear()

    assert api.call("GET", "/api/v1/strategies") == {"ok": True, "path": "/api/v1/strategies"}
    api.call("POST", "/api/v1/strategies", {"name": "x", "reason": "first"})

    read, write = arx.requests
    assert read["headers"]["Authorization"].startswith("Bearer ")
    assert read["headers"]["X-Tenant-Id"] == TENANT
    assert "Idempotency-Key" not in read["headers"]
    key = uuid.UUID(write["headers"]["Idempotency-Key"])
    assert key.int != 0


def test_a_request_without_the_organisation_header_is_refused(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    api = arx_session.client(url, store=store, clock=clock)

    with pytest.raises(arx_client.ArxError, match="missing tenant context"):
        api.call("GET", "/api/v1/strategies", tenant=False)


def test_the_mode_header_follows_the_body_and_a_wrong_mode_is_refused(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    api = arx_session.client(url, store=store, clock=clock)
    arx.requests.clear()

    api.call("POST", "/api/v1/products", {"mode": "sandbox"})
    assert arx.requests[-1]["headers"]["X-Trading-Mode"] == "sandbox"
    api.call("GET", "/api/v1/products", query={"mode": "testnet"})
    assert arx.requests[-1]["headers"]["X-Trading-Mode"] == "testnet"

    with pytest.raises(arx_client.ArxError, match="paper"):
        api.call("POST", "/api/v1/products", {"mode": "paper"})
    with pytest.raises(arx_client.ArxError, match="testnet"):
        api.call("POST", "/api/v1/products", {"mode": "sandbox"}, mode="testnet")


def test_an_expired_access_token_is_refreshed_and_the_old_refresh_stops_working(
    fake, store, clock
) -> None:
    arx, url = fake
    first = _login(url, arx, store, clock)
    clock.sleep(ACCESS_SECONDS + 1)

    renewed = arx_session.current(url, store=store, clock=clock)

    assert renewed.access_token != first.access_token
    assert renewed.refresh_token != first.refresh_token
    assert first.refresh_token not in arx.refresh
    assert json.loads(store.path.read_text())["hosts"][url]["refresh_token"] == (
        renewed.refresh_token
    )
    # A session still in date is used as it is.
    assert arx_session.current(url, store=store, clock=clock) == renewed
    assert arx.refreshes == 1


def test_two_commands_refreshing_at_once_do_not_void_each_other(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    clock.sleep(ACCESS_SECONDS + 1)
    arx.refresh_delay = 0.3
    start = threading.Barrier(2)
    outcomes: list[object] = []

    def refresh_once() -> None:
        start.wait()
        try:
            outcomes.append(arx_session.current(url, store=store, clock=clock).access_token)
        except arx_client.ArxError as failure:
            outcomes.append(failure)

    threads = [threading.Thread(target=refresh_once) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
    assert outcomes[0] == outcomes[1]
    assert arx.refreshes == 1


def test_a_refresh_the_server_refuses_removes_the_session(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    arx.refresh.clear()  # signed out elsewhere
    clock.sleep(ACCESS_SECONDS + 1)

    with pytest.raises(arx_client.ArxError, match="sign in again") as refused:
        arx_session.current(url, store=store, clock=clock)

    assert refused.value.fix == f"make arx-login ARX_URL={url}"
    assert json.loads(store.path.read_text())["hosts"] == {}


def test_a_session_past_its_refresh_lifetime_asks_for_a_new_sign_in(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    clock.sleep(REFRESH_SECONDS + 1)
    arx.requests.clear()

    with pytest.raises(arx_client.ArxError, match="sign in again"):
        arx_session.current(url, store=store, clock=clock)
    assert arx.requests == []
    assert json.loads(store.path.read_text())["hosts"] == {}


def test_signing_out_ends_the_session_on_both_sides(fake, store, clock) -> None:
    arx, url = fake
    signed_in = _login(url, arx, store, clock)

    assert arx_session.logout(url, store=store, clock=clock) is True

    assert signed_in.access_token not in arx.access
    assert signed_in.refresh_token not in arx.refresh
    assert json.loads(store.path.read_text())["hosts"] == {}
    assert arx_session.logout(url, store=store, clock=clock) is False


def test_an_unknown_url_has_no_session(fake, store, clock) -> None:
    _, url = fake
    with pytest.raises(arx_client.ArxError, match="not signed in") as refused:
        arx_session.current(url, store=store, clock=clock)
    assert refused.value.fix == f"make arx-login ARX_URL={url}"


# The file the session is kept in


def test_the_session_file_and_its_directory_are_private(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)

    assert stat.S_IMODE(os.stat(store.directory).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert not list(store.directory.glob("*.tmp*"))


@pytest.mark.parametrize("which", ["file", "directory"])
def test_a_session_file_others_can_read_is_refused(fake, store, clock, which) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    target = store.path if which == "file" else store.directory
    os.chmod(target, 0o644 if which == "file" else 0o755)

    with pytest.raises(arx_client.ArxError, match="other users") as refused:
        arx_session.current(url, store=store, clock=clock)
    assert "chmod" in refused.value.fix


def test_the_config_directory_follows_xdg(tmp_path: Path) -> None:
    assert arx_session.config_directory({"XDG_CONFIG_HOME": str(tmp_path)}) == (
        tmp_path / "custos-strategy" / "arx"
    )
    assert arx_session.config_directory({"HOME": str(tmp_path)}) == (
        tmp_path / ".config" / "custos-strategy" / "arx"
    )


# Addresses and secrets


@pytest.mark.parametrize(
    "url",
    ["https://arx.example.com", "https://arx.example.com:8443/", "http://localhost:8080",
     "http://127.0.0.1:9", "http://[::1]:8080"],
)  # fmt: skip
def test_https_and_loopback_http_are_accepted(url: str) -> None:
    assert arx_client.base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    ["http://arx.example.com", "http://10.0.0.5:8080", "http://127.0.0.2", "ftp://arx.example.com",
     "https://user:pass@arx.example.com", "https://arx.example.com/?x=1", "arx.example.com"],
)  # fmt: skip
def test_other_addresses_are_refused(url: str) -> None:
    with pytest.raises(arx_client.ArxError):
        arx_client.base_url(url)


def test_plain_http_to_another_host_says_to_use_https() -> None:
    with pytest.raises(arx_client.ArxError, match="https") as refused:
        arx_client.base_url("http://arx.example.com")
    assert "https://arx.example.com" in refused.value.fix


def test_tokens_never_appear_in_an_error(fake, store, clock) -> None:
    arx, url = fake
    signed_in = _login(url, arx, store, clock)
    api = arx_session.client(url, store=store, clock=clock)

    with pytest.raises(arx_client.ArxError) as refused:
        api.call("POST", "/api/v1/echo", {})

    text = f"{refused.value} {refused.value.fix} {refused.value!r}"
    assert signed_in.access_token not in text
    assert "[redacted]" in text


def test_a_nil_idempotency_key_is_refused_before_sending(fake, store, clock) -> None:
    arx, url = fake
    _login(url, arx, store, clock)
    api = arx_session.client(url, store=store, clock=clock)

    with pytest.raises(arx_client.ArxError, match="nil"):
        api.call("POST", "/api/v1/strategies", {}, idempotency_key=str(uuid.UUID(int=0)))
