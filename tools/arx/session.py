#!/usr/bin/env python3
"""Sign in to ARX once and keep the session on this machine.

`make arx-login ARX_URL=<address>` signs in the way ARX's sign-in guide
describes: email and password, then an authenticator code. Both are read
without being shown and neither is kept. What is kept is the session: a
15-minute access token and the refresh cookie, which lasts seven days from the
last sign-in or refresh.

Sessions are kept in `${XDG_CONFIG_HOME:-~/.config}/custos-strategy/arx/hosts.json`,
one entry per ARX address, in a directory only you can open (0700) and a file
only you can read (0600). A file or directory others can read is refused rather
than used. Every refresh rotates the refresh cookie and voids the one before, so
the file is changed only under a lock: two commands that find the access token
expired at the same moment refresh it once, and the second uses the result.

ARX accepts each 30-second authenticator code once per person. A step that asks
for a code right after signing in therefore waits for the next one.

Usage:
    python3 tools/arx/session.py login --url https://arx.example.com [--email you@example.com] [--tenant <id>]
    python3 tools/arx/session.py status [--url https://arx.example.com]
        (also lists the deployments made from this directory, with what their
        runner said about each)
    python3 tools/arx/session.py logout [--url https://arx.example.com]
"""  # noqa: E501

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import getpass
import json
import math
import os
import stat
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from http.cookies import SimpleCookie
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402
from tools.arx import observation  # noqa: E402
from tools.arx.client import (  # noqa: E402
    ArxClient,
    ArxError,
    Response,
    Transport,
    base_url,
    error_from,
    urllib_transport,
)

LOGIN = "/api/v1/auth/login"
CHALLENGE = "/api/v1/auth/totp-challenge"
REFRESH = "/api/v1/auth/refresh"
SESSION = "/api/v1/auth/session"
LOGOUT = "/api/v1/auth/logout"
REFRESH_COOKIE = "arx_refresh"
ACCESS_SECONDS = 15 * 60
REFRESH_SECONDS = 7 * 24 * 3600
# An access token this close to its end is refreshed before it is used.
ACCESS_MARGIN_SECONDS = 60
CODE_STEP_SECONDS = 30
CODE_ATTEMPTS = 3
FILE_VERSION = 1


def config_directory(env: Mapping[str, str] = os.environ) -> Path:
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    return Path(base) / "custos-strategy" / "arx"


def _say(message: str) -> None:
    ui.info(message, tag="arx")


def hidden_input(prompt: str) -> str:
    """A secret typed at the terminal without being shown; a line from stdin otherwise."""

    if ui.interactive():
        return ui.secret(prompt)
    if sys.stdin.isatty():
        return getpass.getpass(f"{prompt}: ").strip()
    return sys.stdin.readline().strip()


def _private(info: os.stat_result, path: Path, directory: bool) -> None:
    kind = "directory" if directory else "file"
    wanted = "700" if directory else "600"
    if stat.S_ISLNK(info.st_mode):
        raise ArxError(
            f"{path} is a symbolic link; sessions are kept in a real {kind} only",
            f"remove the link: rm {path}",
        )
    if (stat.S_ISDIR(info.st_mode)) != directory:
        raise ArxError(f"{path} is not a {kind}", f"move it away: mv {path} {path}.old")
    if info.st_uid != os.getuid():
        raise ArxError(f"{path} belongs to another user", f"move it away: mv {path} {path}.old")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ArxError(
            f"{path} can be opened by other users, and it holds ARX sessions",
            f"chmod {wanted} {path}",
        )


@dataclass
class HostStore:
    """hosts.json and the lock every change to it is made under."""

    directory: Path

    @property
    def path(self) -> Path:
        return self.directory / "hosts.json"

    @property
    def lock_path(self) -> Path:
        return self.directory / "hosts.json.lock"

    def _prepare(self) -> None:
        self.directory.parent.parent.mkdir(parents=True, exist_ok=True)
        for level in (self.directory.parent, self.directory):
            if not os.path.lexists(level):
                with contextlib.suppress(FileExistsError):
                    level.mkdir(mode=0o700)
        _private(os.lstat(self.directory), self.directory, directory=True)

    def _open(self, path: Path, flags: int) -> int:
        fd = os.open(path, flags | os.O_NOFOLLOW, 0o600)
        try:
            _private(os.fstat(fd), path, directory=False)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _read(self) -> dict:
        try:
            fd = self._open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return {"version": FILE_VERSION, "hosts": {}}
        except OSError as failure:
            raise ArxError(
                f"cannot read {self.path}: {failure.strerror}",
                f"move it away and sign in again: mv {self.path} {self.path}.old",
            ) from None
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read()
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("hosts"), dict):
            raise ArxError(
                f"{self.path} is not a session file this tool wrote",
                f"move it away and sign in again: mv {self.path} {self.path}.old",
            )
        return data

    def _write(self, data: dict) -> None:
        temporary = self.directory / f".hosts.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True).encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise
        directory = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[dict]:
        """The file's content, held under an exclusive lock and written back if changed.

        A change made before an error is written too: a session found dead is
        removed even though the command then fails.
        """

        self._prepare()
        lock = self._open(self.lock_path, os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = self._read()
            before = json.dumps(data, sort_keys=True)
            try:
                yield data
            finally:
                if json.dumps(data, sort_keys=True) != before:
                    self._write(data)
        finally:
            os.close(lock)


@dataclass
class Session:
    url: str
    email: str
    user_id: str
    tenant_id: str
    roles: list[str]
    memberships: list[dict]
    access_token: str
    access_expires_at: float
    refresh_token: str
    refresh_expires_at: float
    # The 30-second step of the last authenticator code ARX accepted from this
    # session, at sign-in or for a later write; ARX takes each code once.
    code_step: int | None
    signed_in_at: float

    def to_mapping(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Session:
        names = {item.name for item in dataclasses.fields(cls)}
        if set(value) != names:
            raise ArxError(
                "a session in the session file is not one this tool wrote",
                "sign in again with make arx-login",
            )
        return cls(**value)  # type: ignore[arg-type]


def _step(now: float) -> int:
    return int(now // CODE_STEP_SECONDS)


def _wait_for_next_step(
    used: int | None, clock: Callable[[], float], sleep: Callable[[float], None], notify
) -> None:
    now = clock()
    if used is None or _step(now) != used:
        return
    # One second more, for an authenticator whose clock is a little behind.
    remaining = CODE_STEP_SECONDS - (now % CODE_STEP_SECONDS) + 1
    notify(
        f"ARX takes each authenticator code once, and this one was just used; "
        f"waiting {math.ceil(remaining)} s for the next code"
    )
    sleep(remaining)


def wait_for_fresh_code(
    session: Session,
    *,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[str], None] = _say,
) -> None:
    """Before asking for a code: wait out the step of the last code this session used."""

    _wait_for_next_step(session.code_step, clock, sleep, notify)


def code_step(now: float) -> int:
    """The 30-second step a code entered at `now` belongs to."""

    return _step(now)


def record_code_step(url: str, step: int, *, store: HostStore) -> None:
    """Remember that ARX accepted this step's code, so the next ask waits for a new one."""

    key = base_url(url)
    with store.transaction() as data:
        entry = data["hosts"].get(key)
        if isinstance(entry, dict):
            entry["code_step"] = step


def _access_expiry(payload: Mapping[str, object], now: float) -> float:
    """When to stop using the access token: its documented lifetime, or earlier if ARX says."""

    expiry = now + ACCESS_SECONDS
    stated = payload.get("expires_at")
    if isinstance(stated, str):
        with contextlib.suppress(ValueError):
            expiry = min(expiry, datetime.fromisoformat(stated).timestamp())
    return expiry


def _refresh_cookie(response: Response, now: float) -> tuple[str, float]:
    for header in response.header("Set-Cookie"):
        jar = SimpleCookie()
        with contextlib.suppress(Exception):
            jar.load(header)
        morsel = jar.get(REFRESH_COOKIE)
        if morsel is None or not morsel.value:
            continue
        expiry = now + REFRESH_SECONDS
        if morsel["max-age"]:
            with contextlib.suppress(ValueError):
                expiry = now + int(morsel["max-age"])
        return morsel.value, expiry
    raise ArxError(
        "ARX answered without a refresh cookie", "check that ARX_URL is the ARX API address"
    )


def _ok(response: Response) -> bool:
    return 200 <= response.status < 300


def login(
    url: str,
    *,
    email: str,
    store: HostStore,
    read_secret: Callable[[str], str] = hidden_input,
    tenant: str | None = None,
    clock: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    notify: Callable[[str], None] = _say,
    transport: Transport = urllib_transport,
) -> Session:
    """Sign in with password and authenticator code, and keep the session."""

    api = ArxClient(url, transport=transport)
    try:
        return _login(api, email, store, read_secret, tenant, clock, sleep, notify)
    except ArxError as failure:
        raise api.scrubbed(failure) from None


def _login(api: ArxClient, email, store, read_secret, tenant, clock, sleep, notify) -> Session:
    fix = api.login_fix
    anonymous = {"tenant": False, "authenticated": False, "idempotent": False}
    with store.transaction() as data:
        previous = data["hosts"].get(api.url)
    password = read_secret(f"ARX password for {email}")
    api.secrets.add(password)
    response = api.request("POST", LOGIN, {"email": email, "password": password}, **anonymous)
    if response.status == 401:
        raise ArxError("ARX refused the email or password", f"check them, then {fix} again")
    if not _ok(response):
        raise error_from(response, "sign-in", fix)
    payload = response.json() or {}
    step = None
    if payload.get("requires_totp"):
        challenge = str(payload.get("totp_challenge_token") or "")
        api.secrets.add(challenge)
        if isinstance(previous, Mapping) and previous.get("email") == email:
            _wait_for_next_step(previous.get("code_step"), clock, sleep, notify)
        for attempt in range(1, CODE_ATTEMPTS + 1):
            code = read_secret("Authenticator code (or a backup code)")
            api.secrets.add(code)
            step = _step(clock())
            response = api.request(
                "POST", CHALLENGE, {"challenge_token": challenge, "code": code}, **anonymous
            )
            if _ok(response):
                break
            refused = response.json() or {}
            if response.status == 401 and not refused.get("restart_login"):
                if attempt < CODE_ATTEMPTS:
                    notify(
                        "ARX did not accept that code; enter the one your authenticator shows now"
                    )
                    continue
                raise ArxError("ARX did not accept the authenticator code", fix)
            if response.status == 409:
                if attempt < CODE_ATTEMPTS:
                    notify("ARX cannot read this account's authenticator; enter a backup code")
                    continue
                raise ArxError(
                    "ARX cannot read this account's authenticator; only a backup code will sign in",
                    f"{fix}, and enter a backup code",
                )
            failure = error_from(response, "sign-in", fix)
            if refused.get("restart_login"):
                failure.fix = failure.fix if response.status == 429 else f"start again: {fix}"
            raise failure
        payload = response.json() or {}
    now = clock()
    access = str(payload.get("access_token") or "")
    api.secrets.add(access)
    if not access:
        raise ArxError("ARX signed in but returned no access token", fix)
    refresh, refresh_expiry = _refresh_cookie(response, now)
    api.secrets.add(refresh)
    signed_in = ArxClient(api.url, token=lambda: access, transport=api.transport)
    signed_in.secrets = api.secrets
    who = signed_in.call("GET", SESSION, tenant=False, action="reading the session")
    if not isinstance(who, Mapping):
        raise ArxError("ARX answered the session read with something that is not a session")
    memberships = [m for m in who.get("memberships") or [] if isinstance(m, Mapping)]
    home = str(who.get("tenant_id") or "")
    member_of = [str(m.get("tenant_id")) for m in memberships] or [home]
    if tenant is not None and tenant not in member_of:
        with contextlib.suppress(ArxError):
            signed_in.request(
                "POST",
                LOGOUT,
                tenant=False,
                idempotent=False,
                headers={"Cookie": f"{REFRESH_COOKIE}={refresh}"},
            )
        raise ArxError(
            f"you are not a member of organisation {tenant}; you are a member of "
            f"{', '.join(member_of)}",
            f"{fix} ARX_TENANT=<one of them>",
        )
    default = next((str(m.get("tenant_id")) for m in memberships if m.get("is_default")), home)
    chosen = tenant or default or member_of[0]
    membership = next((m for m in memberships if str(m.get("tenant_id")) == chosen), None)
    roles = membership.get("roles") if membership else who.get("roles")
    session = Session(
        url=api.url,
        email=str(who.get("email") or email),
        user_id=str(who.get("id") or ""),
        tenant_id=chosen,
        roles=[str(role) for role in roles or []],
        memberships=[dict(m) for m in memberships],
        access_token=access,
        access_expires_at=_access_expiry(payload, now),
        refresh_token=refresh,
        refresh_expires_at=refresh_expiry,
        code_step=step,
        signed_in_at=now,
    )
    with store.transaction() as data:
        data["hosts"][api.url] = session.to_mapping()
    return session


def current(
    url: str,
    *,
    store: HostStore,
    clock: Callable[[], float] = time.time,
    transport: Transport = urllib_transport,
) -> Session:
    """The session for `url` with an access token in date, refreshed if it had to be."""

    api = ArxClient(url, transport=transport)
    fix = api.login_fix
    with store.transaction() as data:
        raw = data["hosts"].get(api.url)
        if raw is None:
            raise ArxError(f"not signed in to ARX at {api.url}", fix)
        session = Session.from_mapping(raw)
        now = clock()
        if session.refresh_expires_at <= now:
            del data["hosts"][api.url]
            raise ArxError(f"the ARX session at {api.url} has ended; sign in again", fix)
        if session.access_expires_at - ACCESS_MARGIN_SECONDS > now:
            return session
        api.secrets |= {session.access_token, session.refresh_token}
        try:
            response = api.request(
                "POST",
                REFRESH,
                tenant=False,
                authenticated=False,
                idempotent=False,
                headers={"Cookie": f"{REFRESH_COOKIE}={session.refresh_token}"},
            )
            if response.status == 401:
                del data["hosts"][api.url]
                raise ArxError(
                    f"ARX no longer accepts the session at {api.url}; sign in again", fix
                )
            if not _ok(response):
                raise error_from(response, "refreshing the session", fix)
            payload = response.json() or {}
            access = str(payload.get("access_token") or "")
            refresh, refresh_expiry = _refresh_cookie(response, now)
        except ArxError as failure:
            raise api.scrubbed(failure) from None
        session = dataclasses.replace(
            session,
            access_token=access,
            access_expires_at=_access_expiry(payload, now),
            refresh_token=refresh,
            refresh_expires_at=refresh_expiry,
        )
        data["hosts"][api.url] = session.to_mapping()
        return session


def client(
    url: str,
    *,
    store: HostStore,
    clock: Callable[[], float] = time.time,
    transport: Transport = urllib_transport,
) -> ArxClient:
    """A client acting in the session's organisation, refreshing the session as it goes."""

    session = current(url, store=store, clock=clock, transport=transport)
    return ArxClient(
        session.url,
        token=lambda: current(url, store=store, clock=clock, transport=transport).access_token,
        tenant_id=session.tenant_id,
        transport=transport,
    )


def logout(
    url: str,
    *,
    store: HostStore,
    clock: Callable[[], float] = time.time,
    transport: Transport = urllib_transport,
) -> bool:
    """End the session on ARX and remove it here; False if there was none."""

    key = base_url(url)
    with store.transaction() as data:
        if key not in data["hosts"]:
            return False
    try:
        session = current(key, store=store, clock=clock, transport=transport)
    except ArxError:
        # Already ended on ARX, or unreadable here: only this machine's copy is left.
        with store.transaction() as data:
            data["hosts"].pop(key, None)
        return True
    api = ArxClient(key, token=lambda: session.access_token, transport=transport)
    try:
        with store.transaction() as data:
            data["hosts"].pop(key, None)
            response = api.request(
                "POST",
                LOGOUT,
                tenant=False,
                idempotent=False,
                headers={"Cookie": f"{REFRESH_COOKIE}={session.refresh_token}"},
            )
            if not _ok(response) and response.status != 401:
                raise error_from(response, "signing out", api.login_fix)
    except ArxError as failure:
        api.secrets.add(session.refresh_token)
        raise api.scrubbed(
            ArxError(
                f"the session was removed from this machine, but ARX could not end it: {failure}",
                "sign out in the ARX console to end it there",
            )
        ) from None
    return True


def known_urls(store: HostStore) -> list[str]:
    with store.transaction() as data:
        return sorted(data["hosts"])


def _moment(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def _chosen_url(given: str | None, store: HostStore) -> str | None:
    if given:
        return base_url(given)
    urls = known_urls(store)
    if len(urls) == 1:
        return urls[0]
    if not urls:
        ui.info("not signed in to any ARX on this machine", tag="arx")
        ui.next_steps([("make arx-login ARX_URL=https://arx.example.com", "sign in")])
        return None
    ui.info(f"signed in to {len(urls)} ARX addresses; name one with ARX_URL=", tag="arx")
    ui.next_steps([(f"make arx-status ARX_URL={url}", "") for url in urls])
    return None


def _show(session: Session) -> None:
    ui.table(
        "ARX session",
        [
            ("ARX", session.url),
            ("signed in as", session.email),
            ("organisation", session.tenant_id),
            ("roles", ", ".join(session.roles) or "none"),
            ("access token until", _moment(session.access_expires_at)),
            ("session ends by", _moment(session.refresh_expires_at)),
        ],
    )


def show_deployments(api: ArxClient, root: Path) -> None:
    """What the runner said about each deployment made from `root` through this session."""

    rows = observation.deployment_rows(api, root)
    if rows:
        ui.table("Deployments from this directory, as their runners report them", rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    signing_in = commands.add_parser("login")
    signing_in.add_argument("--url", required=True)
    signing_in.add_argument("--email")
    signing_in.add_argument("--tenant")
    for name in ("status", "logout"):
        commands.add_parser(name).add_argument("--url")
    args = parser.parse_args(argv)
    store = HostStore(config_directory())
    try:
        if args.command == "login":
            url = base_url(args.url)
            email = args.email or ui.ask("ARX email")
            if not email:
                raise ArxError(
                    "sign-in needs an email",
                    f"make arx-login ARX_URL={url} ARX_EMAIL=you@example.com",
                )
            session = login(url, email=email, store=store, tenant=args.tenant or None)
            ui.ok(f"signed in to {session.url} as {session.email}", tag="arx")
            _show(session)
            ui.next_steps([(f"make arx-status ARX_URL={session.url}", "check the session later")])
            return 0
        url = _chosen_url(args.url, store)
        if url is None:
            return 0
        if args.command == "logout":
            ended = logout(url, store=store)
            ui.ok(f"signed out of {url}" if ended else f"there was no session for {url}", tag="arx")
            return 0
        session = current(url, store=store)
        api = client(url, store=store)
        # Ask ARX too: a session removed there is only found out by using it.
        api.call("GET", SESSION, tenant=False, action="reading the session")
        _show(session)
        show_deployments(api, ROOT)
        return 0
    except ArxError as failure:
        ui.error(str(failure), tag="arx")
        if failure.fix:
            ui.next_steps([(failure.fix, "")])
        return 1
    except ui.Cancelled:
        return 130


if __name__ == "__main__":
    sys.exit(main())
