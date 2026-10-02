"""What the runner said about a deployment instance, as ARX reports it.

A deployment instance's `lifecycle_state` is the state ARX wants it in, not the
state it is in. Whether the target runner actually started it is the
instance's `runner_observation`, which ARX derives from what that runner
reported for the instance's current generation and command:

- null while the wanted state is not `running`: no runner is asked to run it;
- `awaiting_runner`: the runner has not answered the command yet;
- `running_confirmed`: the runner applied it and saw the engine running;
- `start_rejected`: the runner refused it or gave up (`outcome` says which); a
  refusal outweighs an earlier start, and holds for this generation.

An ARX that predates the field leaves it out of the instance altogether. That
is reported as such: it says nothing about the runner, and is never taken as a
start.

This module uses the standard library only, so `make arx-status` can show the
observations before anything is installed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tools.arx.client import ArxError

FIELD = "runner_observation"
DEPLOYMENTS = ".deployments"
INSTANCES = "/api/v1/deployments"
ENDED = ("stopped", "archived")

# What the observation means for a deployment, as recorded in its receipt.
CONFIRMED = "confirmed"
REJECTED = "rejected"
AWAITING = "awaiting"
NOT_RUNNING = "not running"
UNSUPPORTED = "unsupported"
UNREADABLE = "unreadable"

_CHECKS = {
    "running_confirmed": CONFIRMED,
    "start_rejected": REJECTED,
    "awaiting_runner": AWAITING,
}

# What a runner's refusal means, and what to look at.
OUTCOMES = {
    "conflict": (
        "the runner reports a conflict with the start command, such as another instance "
        "already running on it: a runner process runs one instance at a time"
    ),
    "retry_exhausted": (
        "the runner tried to start the instance and gave up; its log on the runner's "
        "machine says why"
    ),
}


@dataclass(frozen=True)
class Observation:
    """One reading of an instance's runner observation."""

    check: str
    lifecycle_state: str | None
    raw: Mapping | None = None

    @property
    def outcome(self) -> str | None:
        return (self.raw or {}).get("outcome")

    @property
    def observed_at(self) -> str | None:
        return (self.raw or {}).get("observed_at")

    @property
    def event_id(self) -> str | None:
        return (self.raw or {}).get("event_id")

    @property
    def generation(self) -> object:
        return (self.raw or {}).get("generation")


def read(instance: Mapping) -> Observation:
    """What `instance` says about its runner; never a start it does not report."""

    state = instance.get("lifecycle_state")
    state = str(state) if state is not None else None
    if FIELD not in instance:
        return Observation(UNSUPPORTED, state)
    raw = instance[FIELD]
    if raw is None:
        # ARX reports nothing only for an instance it does not want running.
        return Observation(UNREADABLE if state == "running" else NOT_RUNNING, state)
    if not isinstance(raw, Mapping):
        return Observation(UNREADABLE, state)
    check = _CHECKS.get(str(raw.get("status")), UNREADABLE)
    if check in (CONFIRMED, REJECTED) and not raw.get("observed_at"):
        check = UNREADABLE
    return Observation(check, state, dict(raw))


def describe(seen: Observation, runner: str | None = None) -> str:
    """One line on what the runner said, for a table or a message."""

    who = f"runner {runner}" if runner else "the runner"
    if seen.check == CONFIRMED:
        return f"{who} confirmed it running at {seen.observed_at}"
    if seen.check == REJECTED:
        return (
            f"{who} rejected the start at {seen.observed_at} (outcome {seen.outcome}, "
            f"event {seen.event_id})"
        )
    if seen.check == AWAITING:
        return f"{who} has not answered the start command yet"
    if seen.check == NOT_RUNNING:
        return f"ARX does not want it running (state {seen.lifecycle_state}), so no runner is asked"
    if seen.check == UNSUPPORTED:
        return "ARX does not report what the runner said: it is older than this tool expects"
    return f"ARX reported something this tool does not understand: {FIELD}={seen.raw!r}"


UNSUPPORTED_FIX = (
    f"ARX's deployment instance reads carry no {FIELD}, so this tool cannot tell whether the "
    "runner started the instance; ask an ARX admin to upgrade ARX to a version whose "
    f"deployment instance reads include {FIELD}"
)


# -- the deployments made from this directory ----------------------------------------------


def _receipts(root: Path) -> list[dict]:
    found = []
    for path in sorted((root / DEPLOYMENTS).glob("*/*/*/*.json")):
        if path.name.startswith(".") or path.stem.count(".") or "-" not in path.stem:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append(data)
    return found


def deployment_rows(api, root: Path) -> list[tuple[str, str]]:
    """What the runner said about each deployment this directory made through `api`.

    Only deployments to the ARX and organisation `api` acts in, with a first
    instance and not stopped as far as their receipt knows. Each is read from
    ARX once; a read ARX refuses is shown as such rather than stopping the rest.
    """

    rows = []
    for receipt in _receipts(root):
        instance_id = receipt.get("first_instance_id")
        if (
            not instance_id
            or receipt.get("arx_url") != api.url
            or receipt.get("tenant_id") != api.tenant_id
            or receipt.get("state") in ENDED
        ):
            continue
        name = f"{receipt.get('strategy')} {receipt.get('version')} ({receipt.get('mode')})"
        try:
            instance = api.call(
                "GET",
                f"{INSTANCES}/{instance_id}",
                query={"trading_mode": str(receipt.get("mode"))},
                action="reading the deployment instance",
            )
        except ArxError as failure:
            # One deployment ARX will not show must not hide the others.
            rows.append((name, f"instance {instance_id}: not read ({failure})"))
            continue
        if not isinstance(instance, Mapping):
            rows.append((name, f"instance {instance_id}: ARX answered something not an object"))
            continue
        seen = read(instance)
        rows.append(
            (
                name,
                f"instance {instance_id}, {seen.lifecycle_state}: "
                f"{describe(seen, receipt.get('runner_id'))}",
            )
        )
    return rows
