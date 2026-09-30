"""An HTTP client for ARX's public API.

Every request follows ARX's API conventions:

- `Authorization: Bearer <access token>` once there is a session;
- `X-Tenant-Id` on every organisation endpoint, which is every endpoint apart
  from sign-in, refresh, session and sign-out;
- `Idempotency-Key`, a fresh non-nil UUID, on every write;
- `X-Trading-Mode` whenever the body or the query names a mode, equal to it.

Only https is spoken, apart from plain http to this machine (localhost,
127.0.0.1 or ::1), where a local ARX runs without a certificate.

Tokens are secrets: they are sent in headers only, never in a URL, and an error
never repeats one, even when the server echoes it back.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from urllib import error as urllib_error
from urllib import parse, request

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MODES = ("sandbox", "testnet", "live")
MODE_FIELDS = ("mode", "trading_mode", "source_trading_mode")
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TIMEOUT_SECONDS = 30
_TENANT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REDACTED = "[redacted]"


class ArxError(RuntimeError):
    """Why a call to ARX could not be made or was refused, and how to fix it."""

    def __init__(
        self, message: str, fix: str = "", *, status: int | None = None, code: str = ""
    ) -> None:
        super().__init__(message)
        self.fix = fix
        self.status = status
        self.code = code


def redact(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def base_url(value: str) -> str:
    """The ARX address as the session file keys it: scheme, host, port and path."""

    parts = parse.urlsplit(value.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("https", "http") or not host:
        raise ArxError(
            f"not an ARX address: {value!r}",
            "give the full address, such as https://arx.example.com",
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ArxError(
            f"an ARX address has no user, query or fragment: {value!r}",
            "give only the scheme, host, port and path, such as https://arx.example.com",
        )
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        secure = parse.urlunsplit(("https", parts.netloc, parts.path, "", "")).rstrip("/")
        raise ArxError(
            f"ARX is reached over https; plain http is accepted only on this machine: {value}",
            f"use {secure}",
        )
    try:
        port = parts.port
    except ValueError:
        raise ArxError(f"not a port: {value!r}", "give a port from 1 to 65535") from None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return parse.urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


def is_loopback(url: str) -> bool:
    return (parse.urlsplit(url).hostname or "").lower() in LOOPBACK_HOSTS


@dataclass(frozen=True)
class Response:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key.lower() == name.lower()]

    def json(self) -> object:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except ValueError:
            return None


Transport = Callable[[str, str, Mapping[str, str], "bytes | None"], Response]


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: bytes | None):
    """Send one request. Credentials are never carried across a redirect."""

    handlers: list[request.BaseHandler] = []
    if is_loopback(url):
        # A proxy in the environment must not see a request meant for this machine.
        handlers.append(request.ProxyHandler({}))
    opener = request.build_opener(*handlers)
    outgoing = request.Request(url, data=body, method=method)
    for name, value in headers.items():
        if name in ("Authorization", "Cookie"):
            outgoing.add_unredirected_header(name, value)
        else:
            outgoing.add_header(name, value)
    try:
        with opener.open(outgoing, timeout=TIMEOUT_SECONDS) as answer:
            return Response(answer.status, list(answer.headers.items()), answer.read())
    except urllib_error.HTTPError as refused:
        return Response(refused.code, list(refused.headers.items()), refused.read())
    except (urllib_error.URLError, TimeoutError, OSError) as failure:
        reason = getattr(failure, "reason", failure)
        raise ArxError(
            f"cannot reach {parse.urlsplit(url).netloc}: {reason}",
            "check the address and this machine's network",
        ) from None


def _mode_named(body: object, query: Mapping[str, str] | None) -> set[str]:
    named = set()
    if isinstance(body, Mapping):
        named |= {str(body[key]) for key in MODE_FIELDS if key in body}
    if query:
        named |= {str(query[key]) for key in MODE_FIELDS if key in query}
    return named


def _idempotency_key(value: str | None) -> str:
    if value is None:
        return str(uuid.uuid4())
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ArxError(f"an idempotency key is a UUID, not {value!r}") from None
    if parsed.int == 0:
        raise ArxError("an idempotency key must not be the nil UUID")
    return str(parsed)


def error_from(response: Response, action: str, login_fix: str) -> ArxError:
    """The ArxError for a refused request, from either of ARX's two error shapes."""

    payload = response.json()
    code, message = "", ""
    if isinstance(payload, Mapping):
        code = str(payload.get("error") or payload.get("code") or "")
        message = str(payload.get("message") or "")
        if payload.get("required"):
            message = f"{message} (requires one of: {', '.join(map(str, payload['required']))})"
    elif response.body:
        message = response.body[:200].decode("utf-8", errors="replace").strip()
    detail = ": ".join(part for part in (code, message) if part) or "no detail"
    fix = ""
    if response.status == 401:
        fix = login_fix
    elif response.status == 403:
        fix = "ask an ARX admin for the role or strategy access this needs"
    elif response.status == 429:
        retry = response.header("Retry-After")
        fix = f"wait {retry[0]} seconds and try again" if retry else "wait a while and try again"
    elif response.status == 503:
        fix = "ARX is temporarily unavailable; try again later"
    return ArxError(
        f"{action} was refused ({response.status}): {detail}",
        fix,
        status=response.status,
        code=code,
    )


@dataclass
class ArxClient:
    """Requests to one ARX, as one signed-in person in one organisation.

    `token` is called before every request, so a session can refresh itself
    between calls; None means no session yet (sign-in).
    """

    url: str
    token: Callable[[], str] | None = None
    tenant_id: str | None = None
    transport: Transport = urllib_transport
    secrets: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.url = base_url(self.url)

    @property
    def login_fix(self) -> str:
        return f"make arx-login ARX_URL={self.url}"

    def request(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        query: Mapping[str, str] | None = None,
        tenant: bool = True,
        authenticated: bool = True,
        mode: str | None = None,
        idempotency_key: str | None = None,
        idempotent: bool | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Send one request and return the answer, whatever its status."""

        method = method.upper()
        outgoing: dict[str, str] = {"Accept": "application/json"}
        if authenticated:
            if self.token is None:
                raise ArxError("not signed in to ARX", self.login_fix)
            access = self.token()
            self.secrets.add(access)
            outgoing["Authorization"] = f"Bearer {access}"
        if tenant:
            if not self.tenant_id:
                raise ArxError("no organisation is chosen for this session", self.login_fix)
            if not _TENANT.fullmatch(self.tenant_id):
                raise ArxError(f"not an organisation id: {self.tenant_id!r}", self.login_fix)
            outgoing["X-Tenant-Id"] = self.tenant_id
        if idempotent if idempotent is not None else method in WRITE_METHODS:
            outgoing["Idempotency-Key"] = _idempotency_key(idempotency_key)
        named = _mode_named(body, query)
        if mode is not None:
            named.add(mode)
        if len(named) > 1:
            raise ArxError(f"a request names one mode, not {', '.join(sorted(named))}")
        if named:
            (only,) = named
            if only not in MODES:
                raise ArxError(f"a mode is sandbox, testnet or live, not {only!r}")
            outgoing["X-Trading-Mode"] = only
        payload = None
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            outgoing["Content-Type"] = "application/json"
        if headers:
            outgoing.update(headers)
        target = self.url + path
        if query:
            target = f"{target}?{parse.urlencode(query)}"
        return self.transport(method, target, outgoing, payload)

    def call(self, method: str, path: str, body: object = None, **options) -> object:
        """Send one request; its JSON answer, or ArxError if ARX refused it."""

        action = options.pop("action", f"{method.upper()} {path}")
        try:
            response = self.request(method, path, body, **options)
            if not 200 <= response.status < 300:
                raise error_from(response, action, self.login_fix)
        except ArxError as failure:
            raise self.scrubbed(failure) from None
        return response.json()

    def scrubbed(self, failure: ArxError) -> ArxError:
        """The same error with every secret this client has held taken out."""

        return ArxError(
            redact(str(failure), self.secrets),
            redact(failure.fix, self.secrets),
            status=failure.status,
            code=redact(failure.code, self.secrets),
        )
