#!/usr/bin/env python3
"""Bring a runner into service through ARX: enroll and name it, authorise its
transport credential, and put a safety policy on it.

Every call is one ARX's *Runner Administration API* guide documents, made with
the session `make arx-login` keeps, and every write asks for a fresh
authenticator code. ARX takes each 30-second code once, so a command asked
right after another waits for the next code, and says so; a code ARX refuses is
asked for again, up to three times, and writes nothing here.

enroll
    Lists the organisation's runners first, which needs no code, and does the
    one step that is due. A runner not enrolled yet gets an enrollment token,
    written to `.runner-enroll/<runner id>.token` (directory 0700, file 0600,
    ignored by git) and nowhere else: not to the screen, a log, an argument or
    an error. The runner enrolls with it on its own machine, as the runner's
    enrollment guide describes. An enrolled runner without a name is given
    NAME; one already named NAME needs nothing.

authorize-transport
    Authorises the intent the runner's transport credential for one mode is
    issued, rotated or revoked against, and prints its id for the runner.

safety-policy submit | approve | activate
    The two-person runner safety policy. Approving a request you asked for
    yourself is refused here, before a code is asked for.

Usage:
    python3 tools/arx/runner_admin.py enroll --runner <uuid> --name "Box 1" [--scope 3] [--live]
    python3 tools/arx/runner_admin.py authorize-transport --runner <uuid> --mode sandbox
    python3 tools/arx/runner_admin.py safety-policy submit --runner <uuid> --mode sandbox --file p.json
    python3 tools/arx/runner_admin.py safety-policy approve --runner <uuid> --mode sandbox \
        --request <uuid> --reason "…"
    python3 tools/arx/runner_admin.py safety-policy activate --runner <uuid> --mode sandbox \
        --request <uuid>
"""  # noqa: E501

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402
from tools.arx import session as arx_session  # noqa: E402
from tools.arx.client import (  # noqa: E402
    MODES,
    ArxClient,
    ArxError,
    Transport,
    base_url,
    error_from,
    urllib_transport,
)

RUNNERS = "/api/v1/runners"
ENROLLMENT_TOKENS = "/api/v1/runner-enrollment-tokens"
TOKEN_DIRECTORY = ROOT / ".runner-enroll"
CODE_ATTEMPTS = 3
OPERATIONS = ("issue", "rotate", "revoke")
SETTLEMENT_CURRENCIES = ("USD", "USDT", "USDC", "BTC", "ETH", "VUSDC", "VBTC", "VETH")
SUBMIT_FIELDS = {
    "settlement_currency",
    "max_order_notional",
    "max_total_notional",
    "effective_at",
    "expires_at",
    "capability",
    "reason",
}
DECIMAL = re.compile(r"^[0-9]+(\.[0-9]+)?$")


def _say(message: str) -> None:
    ui.info(message, tag="arx")


def _uuid(value: object, what: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except ValueError:
        raise ArxError(f"{what} is a UUID, not {value!r}") from None
    if parsed.int == 0:
        raise ArxError(f"{what} must not be the nil UUID")
    return str(parsed)


def _mode(value: str) -> str:
    if value not in MODES:
        raise ArxError(f"a mode is sandbox, testnet or live, not {value!r}")
    return value


@dataclass
class Admin:
    """One signed-in person acting on runners, asking a fresh code for each write."""

    api: ArxClient
    url: str
    store: arx_session.HostStore
    read_secret: Callable[[str], str] = arx_session.hidden_input
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    notify: Callable[[str], None] = _say
    transport: Transport = urllib_transport

    def session(self) -> arx_session.Session:
        return arx_session.current(
            self.url, store=self.store, clock=self.clock, transport=self.transport
        )

    def read(self, path: str, *, action: str, **options) -> object:
        return self.api.call("GET", path, action=action, **options)

    def write(self, method: str, path: str, body: Mapping, *, action: str, **options) -> object:
        """Send one write with a fresh code; ask again if ARX refuses the code."""

        for attempt in range(1, CODE_ATTEMPTS + 1):
            arx_session.wait_for_fresh_code(
                self.session(), clock=self.clock, sleep=self.sleep, notify=self.notify
            )
            code = self.read_secret("Authenticator code")
            self.api.secrets.add(code)
            step = arx_session.code_step(self.clock())
            try:
                response = self.api.request(method, path, {**body, "totp_code": code}, **options)
            except ArxError as failure:
                raise self.api.scrubbed(failure) from None
            refused = response.json()
            wrong_code = (
                response.status == 401
                and isinstance(refused, Mapping)
                and (
                    refused.get("code") == "totp_invalid" or refused.get("error") == "totp_invalid"
                )
            )
            if wrong_code:
                if attempt < CODE_ATTEMPTS:
                    self.notify(
                        "ARX did not accept that code; enter the one your authenticator shows now"
                    )
                    continue
                raise ArxError(
                    f"{action}: ARX did not accept the authenticator code",
                    "check the authenticator's clock, then run the command again",
                )
            # ARX checked the code before anything else, so it counts as used.
            if response.status != 401:
                arx_session.record_code_step(self.url, step, store=self.store)
            if not 200 <= response.status < 300:
                raise self.api.scrubbed(error_from(response, action, self.api.login_fix))
            return refused
        raise AssertionError("unreachable")

    def runners(self) -> list[dict]:
        listed = self.read(RUNNERS, action="listing the runners")
        if not isinstance(listed, list):
            raise ArxError("ARX answered the runner list with something that is not a list")
        return [entry for entry in listed if isinstance(entry, dict)]

    def runner(self, runner_id: str) -> dict | None:
        return next((r for r in self.runners() if str(r.get("runner_id")) == runner_id), None)


# -- enrollment -------------------------------------------------------------------


def _private_directory(path: Path, *, create: bool = True) -> None:
    if not os.path.lexists(path):
        if not create:
            return
        with contextlib.suppress(FileExistsError):
            path.mkdir(mode=0o700)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ArxError(f"{path} is not a real directory", f"move it away: mv {path} {path}.old")
    if info.st_uid != os.getuid():
        raise ArxError(f"{path} belongs to another user", f"move it away: mv {path} {path}.old")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ArxError(
            f"{path} can be opened by other users, and it holds enrollment tokens",
            f"chmod 700 {path}",
        )


def write_token(directory: Path, runner_id: str, token: str) -> Path:
    """The token in a file only you can read; replaced whole, never followed through a link."""

    _private_directory(directory)
    path = directory / f"{runner_id}.token"
    temporary = directory / f".{runner_id}.{uuid.uuid4().hex}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    return path


@dataclass(frozen=True)
class Enrollment:
    """What `enroll` did: issued a token, named the runner, or found nothing to do."""

    done: str
    runner_id: str
    token_path: Path | None = None
    details: dict | None = None


def enroll(
    admin: Admin,
    *,
    runner_id: str,
    name: str,
    scope: int | None,
    paper_only: bool = True,
    token_directory: Path | None = None,
) -> Enrollment:
    runner_id = _uuid(runner_id, "RUNNER, the runner id")
    name = name.strip()
    if not 1 <= len(name) <= 120:
        raise ArxError("NAME, the runner's display name, is 1 to 120 characters")
    found = admin.runner(runner_id)
    if found is not None:
        current = found.get("display_name")
        if current == name:
            return Enrollment("already named", runner_id)
        if current:
            raise ArxError(
                f"runner {runner_id} is already named {current!r}; ARX keeps a runner's first name",
                f'make enroll-runner RUNNER={runner_id} NAME="{current}"',
            )
        answer = admin.write(
            "PUT",
            f"{RUNNERS}/{runner_id}/business-identity",
            {"display_name": name},
            action="naming the runner",
        )
        return Enrollment("named", runner_id, details=answer if isinstance(answer, dict) else None)
    if isinstance(scope, bool) or not isinstance(scope, int) or not 1 <= scope <= 63:
        raise ArxError(
            "issuing an enrollment token needs SCOPE, a whole number from 1 to 63",
            f'make enroll-runner RUNNER={runner_id} NAME="{name}" SCOPE=<1-63>',
        )
    directory = token_directory or TOKEN_DIRECTORY
    # Checked before the token exists: a token ARX issued and this could not keep is lost.
    _private_directory(directory, create=False)
    answer = admin.write(
        "POST",
        ENROLLMENT_TOKENS,
        {"runner_hint": runner_id, "paper_only": paper_only, "scope": scope},
        action="issuing an enrollment token",
        idempotent=False,
    )
    token = answer.get("token") if isinstance(answer, dict) else None
    if not isinstance(token, str) or not token:
        raise ArxError("ARX issued an enrollment token but did not return it")
    admin.api.secrets.add(token)
    path = write_token(directory, runner_id, token)
    details = {key: value for key, value in answer.items() if key != "token"}
    return Enrollment("token issued", runner_id, token_path=path, details=details)


# -- transport ----------------------------------------------------------------------


def authorize_transport(
    admin: Admin,
    *,
    runner_id: str,
    mode: str,
    operation: str = "issue",
    generation: int | None = None,
) -> dict:
    runner_id = _uuid(runner_id, "RUNNER, the runner id")
    _mode(mode)
    if operation not in OPERATIONS:
        raise ArxError(f"OPERATION is issue, rotate or revoke, not {operation!r}")
    if operation == "issue" and generation is not None:
        raise ArxError("issue starts a credential; GENERATION is only for rotate and revoke")
    if operation != "issue" and (
        isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
    ):
        raise ArxError(
            f"{operation} needs GENERATION, the generation active on the runner (1 or more)"
        )
    if admin.runner(runner_id) is None:
        raise ArxError(
            f"runner {runner_id} is not enrolled in this organisation",
            f'make enroll-runner RUNNER={runner_id} NAME="…"',
        )
    answer = admin.write(
        "POST",
        f"{RUNNERS}/{runner_id}/nats-transport-intents",
        {
            "trading_mode": mode,
            "operation_kind": operation,
            "expected_active_generation": generation,
        },
        action="authorising the transport credential",
    )
    if not isinstance(answer, dict) or not answer.get("authorization_intent_id"):
        raise ArxError("ARX authorised the intent but did not return its id")
    return answer


# -- safety policy ---------------------------------------------------------------------


def _timestamp(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise ArxError(f"{what} is an RFC 3339 time such as 2026-01-01T00:00:00Z")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ArxError(f"{what} is an RFC 3339 time, not {value!r}") from None
    if moment.tzinfo is None:
        raise ArxError(f"{what} needs its offset, such as Z or +00:00: {value!r}")
    return moment


def submission(values: Mapping) -> dict:
    """The body of a safety policy request, refused here if a field is wrong."""

    keys = set(values)
    missing = sorted(SUBMIT_FIELDS - keys)
    unknown = sorted(keys - SUBMIT_FIELDS - {"expected_current_revision"})
    if missing or unknown:
        raise ArxError(
            "a safety policy request "
            + "; ".join(
                part
                for part in (
                    f"is missing {', '.join(missing)}" if missing else "",
                    f"has unknown {', '.join(unknown)}" if unknown else "",
                )
                if part
            )
        )
    body = dict(values)
    if body["settlement_currency"] not in SETTLEMENT_CURRENCIES:
        raise ArxError(
            f"settlement_currency is one of {', '.join(SETTLEMENT_CURRENCIES)}, "
            f"not {body['settlement_currency']!r}"
        )
    for name in ("max_order_notional", "max_total_notional"):
        if not isinstance(body[name], str) or not DECIMAL.fullmatch(body[name]):
            raise ArxError(
                f'{name} is a decimal string such as "1000.5", not {body[name]!r}',
                "write it in quotes",
            )
    if _timestamp(body["expires_at"], "expires_at") <= _timestamp(
        body["effective_at"], "effective_at"
    ):
        raise ArxError("expires_at must come after effective_at")
    capability = body["capability"]
    if not isinstance(capability, Mapping) or set(capability) != {
        "capability_version_id",
        "capability_version",
        "manifest_digest",
    }:
        raise ArxError(
            "capability names the runner's published capability version: "
            "capability_version_id, capability_version and manifest_digest"
        )
    _uuid(capability["capability_version_id"], "capability.capability_version_id")
    version = capability["capability_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ArxError("capability.capability_version is a whole number, 1 or more")
    if not isinstance(capability["manifest_digest"], str) or not capability["manifest_digest"]:
        raise ArxError("capability.manifest_digest is the published capability's digest")
    if not isinstance(body["reason"], str) or not body["reason"].strip():
        raise ArxError("a safety policy request needs a reason")
    revision = body.get("expected_current_revision")
    if revision is not None and (
        isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
    ):
        raise ArxError("expected_current_revision is the current policy's revision, 1 or more")
    body["expected_current_revision"] = revision
    return body


def _requests(runner_id: str) -> str:
    return f"{RUNNERS}/{runner_id}/safety-policy-requests"


def submit_safety_policy(admin: Admin, *, runner_id: str, mode: str, values: Mapping) -> dict:
    runner_id = _uuid(runner_id, "RUNNER, the runner id")
    body = submission(values)
    body["trading_mode"] = _mode(mode)
    answer = admin.write("POST", _requests(runner_id), body, action="asking for a safety policy")
    return answer if isinstance(answer, dict) else {}


def _read_request(admin: Admin, runner_id: str, mode: str, request_id: str) -> dict:
    found = admin.read(
        f"{_requests(runner_id)}/{request_id}",
        query={"trading_mode": mode},
        action="reading the safety policy request",
    )
    if not isinstance(found, dict) or not isinstance(found.get("request_version"), int):
        raise ArxError("ARX answered the request read with something that is not a request")
    return found


def approve_safety_policy(
    admin: Admin, *, runner_id: str, mode: str, request_id: str, reason: str
) -> dict:
    runner_id = _uuid(runner_id, "RUNNER, the runner id")
    request_id = _uuid(request_id, "REQUEST, the safety policy request id")
    _mode(mode)
    if not reason.strip():
        raise ArxError("approving needs a reason", 'add REASON="…"')
    found = _read_request(admin, runner_id, mode, request_id)
    me = admin.session().user_id
    if str(found.get("applicant_id") or "").lower() == str(me).lower() and me:
        raise ArxError(
            "you asked for this safety policy, so you cannot approve it; "
            "a second person holding FINANCE approves it",
            "ask a FINANCE holder who is not you to run this approve",
        )
    answer = admin.write(
        "POST",
        f"{_requests(runner_id)}/{request_id}/approve",
        {
            "trading_mode": mode,
            "expected_request_version": found["request_version"],
            "reason": reason,
        },
        action="approving the safety policy",
    )
    return answer if isinstance(answer, dict) else {}


def activate_safety_policy(admin: Admin, *, runner_id: str, mode: str, request_id: str) -> dict:
    runner_id = _uuid(runner_id, "RUNNER, the runner id")
    request_id = _uuid(request_id, "REQUEST, the safety policy request id")
    _mode(mode)
    found = _read_request(admin, runner_id, mode, request_id)
    answer = admin.write(
        "POST",
        f"{_requests(runner_id)}/{request_id}/activate",
        {"trading_mode": mode, "expected_request_version": found["request_version"]},
        action="activating the safety policy",
    )
    return answer if isinstance(answer, dict) else {}


# -- the command ---------------------------------------------------------------------------


def _url(given: str | None, store: arx_session.HostStore) -> str:
    if given:
        return base_url(given)
    urls = arx_session.known_urls(store)
    if len(urls) == 1:
        return urls[0]
    if not urls:
        raise ArxError("not signed in to any ARX on this machine", "make arx-login ARX_URL=…")
    raise ArxError(
        f"signed in to {len(urls)} ARX addresses; name one with ARX_URL=",
        f"add ARX_URL={urls[0]} (or another of {', '.join(urls)})",
    )


def admin_for(url: str | None, *, store: arx_session.HostStore) -> Admin:
    chosen = _url(url, store)
    api = arx_session.client(chosen, store=store)
    return Admin(api=api, url=chosen, store=store)


def _values(args: argparse.Namespace) -> dict:
    values: dict = {}
    if args.file:
        try:
            loaded = json.loads(Path(args.file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as failure:
            raise ArxError(f"cannot read {args.file} as JSON: {failure}") from None
        if not isinstance(loaded, dict):
            raise ArxError(f"{args.file} holds a JSON object of request fields")
        values.update(loaded)
    for option, key in (
        ("currency", "settlement_currency"),
        ("max_order", "max_order_notional"),
        ("max_total", "max_total_notional"),
        ("effective_at", "effective_at"),
        ("expires_at", "expires_at"),
        ("reason", "reason"),
    ):
        if getattr(args, option):
            values[key] = getattr(args, option)
    if args.revision:
        try:
            values["expected_current_revision"] = int(args.revision)
        except ValueError:
            raise ArxError(f"REVISION is a whole number, not {args.revision!r}") from None
    return values


def _enrolled_output(result: Enrollment, name: str) -> None:
    runner = result.runner_id
    if result.done == "already named":
        ui.ok(f"runner {runner} is enrolled and named {name!r}; nothing to do", tag="arx")
        ui.next_steps(
            [(f"make authorize-runner-transport RUNNER={runner} MODE=sandbox", "its transport")]
        )
        return
    if result.done == "named":
        ui.ok(f"runner {runner} is now named {name!r}", tag="arx")
        ui.next_steps(
            [
                (
                    f"make authorize-runner-transport RUNNER={runner} MODE=sandbox",
                    "authorise its transport credential, per mode",
                )
            ]
        )
        return
    details = result.details or {}
    path = result.token_path
    shown = path.relative_to(ROOT) if path and path.is_relative_to(ROOT) else path
    ui.ok(f"enrollment token issued for runner {runner}", tag="arx")
    ui.table(
        "Enrollment token",
        [
            ("runner", runner),
            ("token file", f"{shown} (readable by you only)"),
            ("token id", str(details.get("token_id", ""))),
            ("organisation", str(details.get("tenant_id", ""))),
            ("scope", str(details.get("scope", ""))),
            ("paper only", str(details.get("paper_only", ""))),
            ("expires at", str(details.get("expires_at", ""))),
            ("proof algorithm", str(details.get("proof_algorithm", ""))),
        ],
    )
    ui.next_steps(
        [
            (
                f"copy {shown} to the runner's machine over a channel you trust",
                "the token is shown nowhere else",
            ),
            (
                "on that machine, enroll the runner with the token file",
                "as the runner's enrollment guide describes; the runner makes its own key pair",
            ),
            (
                f'make enroll-runner RUNNER={runner} NAME="{name}"',
                f"once it has enrolled: name it, then delete {shown}",
            ),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url")
    commands = parser.add_subparsers(dest="command", required=True)
    enrolling = commands.add_parser("enroll")
    enrolling.add_argument("--runner", required=True)
    enrolling.add_argument("--name", required=True)
    enrolling.add_argument("--scope")
    enrolling.add_argument("--live", action="store_true", help="a token that is not paper-only")
    transport = commands.add_parser("authorize-transport")
    transport.add_argument("--runner", required=True)
    transport.add_argument("--mode", required=True)
    transport.add_argument("--operation", default="issue")
    transport.add_argument("--generation")
    policy = commands.add_parser("safety-policy")
    policy.add_argument("action", choices=("submit", "approve", "activate"))
    policy.add_argument("--runner", required=True)
    policy.add_argument("--mode", required=True)
    policy.add_argument("--request")
    policy.add_argument("--file")
    for option in ("currency", "max-order", "max-total", "effective-at", "expires-at", "reason"):
        policy.add_argument(f"--{option}")
    policy.add_argument("--revision")
    args = parser.parse_args(argv)
    store = arx_session.HostStore(arx_session.config_directory())
    try:
        admin = admin_for(args.url, store=store)
        if args.command == "enroll":
            scope = None
            if args.scope:
                try:
                    scope = int(args.scope)
                except ValueError:
                    raise ArxError(
                        f"SCOPE is a whole number from 1 to 63, not {args.scope!r}"
                    ) from None
            result = enroll(
                admin,
                runner_id=args.runner,
                name=args.name,
                scope=scope,
                paper_only=not args.live,
            )
            _enrolled_output(result, args.name.strip())
        elif args.command == "authorize-transport":
            generation = None
            if args.generation:
                try:
                    generation = int(args.generation)
                except ValueError:
                    raise ArxError(
                        f"GENERATION is a whole number, not {args.generation!r}"
                    ) from None
            intent = authorize_transport(
                admin,
                runner_id=args.runner,
                mode=args.mode,
                operation=args.operation,
                generation=generation,
            )
            ui.ok(f"transport credential intent authorised for {args.mode}", tag="arx")
            ui.table(
                "Transport intent",
                [
                    ("authorization_intent_id", str(intent["authorization_intent_id"])),
                    ("runner", str(intent.get("runner_id", args.runner))),
                    ("mode", str(intent.get("trading_mode", args.mode))),
                    ("operation", str(intent.get("operation_kind", args.operation))),
                    ("expires at", str(intent.get("expires_at", ""))),
                    ("fingerprint", str(intent.get("intent_fingerprint", ""))),
                ],
            )
            ui.next_steps(
                [
                    (
                        "on the runner's machine, pass the authorization_intent_id above to the "
                        "runner's transport command",
                        f"before {intent.get('expires_at', 'it expires')}, "
                        "as the runner's guide describes",
                    ),
                    (
                        f"make runner-safety-policy ACTION=submit RUNNER={args.runner} "
                        f"MODE={args.mode} FILE=<request.json>",
                        "then cap what the runner may hold",
                    ),
                ]
            )
        else:
            if args.action != "submit" and not args.request:
                raise ArxError(f"{args.action} needs REQUEST, the safety policy request id")
            if args.action == "submit":
                answer = submit_safety_policy(
                    admin, runner_id=args.runner, mode=args.mode, values=_values(args)
                )
                request = str(answer.get("request_id", ""))
                ui.ok(f"safety policy requested: {request}", tag="arx")
                ui.next_steps(
                    [
                        (
                            f"make runner-safety-policy ACTION=approve RUNNER={args.runner} "
                            f'MODE={args.mode} REQUEST={request} REASON="…"',
                            "run by a FINANCE holder who is not you",
                        )
                    ]
                )
            elif args.action == "approve":
                approve_safety_policy(
                    admin,
                    runner_id=args.runner,
                    mode=args.mode,
                    request_id=args.request,
                    reason=args.reason or "",
                )
                ui.ok(f"safety policy request {args.request} approved", tag="arx")
                ui.next_steps(
                    [
                        (
                            f"make runner-safety-policy ACTION=activate RUNNER={args.runner} "
                            f"MODE={args.mode} REQUEST={args.request}",
                            "put it into effect",
                        )
                    ]
                )
            else:
                answer = activate_safety_policy(
                    admin, runner_id=args.runner, mode=args.mode, request_id=args.request
                )
                active = answer.get("policy") if isinstance(answer.get("policy"), dict) else {}
                ui.ok(
                    f"safety policy active: revision {active.get('revision', '?')}, "
                    f"{active.get('max_order_notional', '?')} per order, "
                    f"{active.get('max_total_notional', '?')} in total "
                    f"{active.get('settlement_currency', '')}",
                    tag="arx",
                )
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
