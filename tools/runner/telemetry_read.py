#!/usr/bin/env python3
"""Summarize what the runner has reported about one run, as JSON.

This runs inside the Custos image, next to the NATS server the runner publishes
to, which is not reachable from the host; tools/runner/report.py lays the result
out for a terminal. The runner publishes a snapshot of a running strategy's
account, positions and open orders every 10 seconds, and a message for each fill
and each closed position. The messages last as long as the run's NATS server,
so this can only look at a run that is still up.

Amounts arrive as strings such as "0.46253570 USDT" and are added up as decimals,
per currency. Nothing here is a float.

With --follow SECONDS it keeps its subscription open and prints a line of JSON
every SECONDS, adding only what arrived since, until it is stopped or the run's
NATS server goes away.

Usage (inside the runner container):
    python tools/runner/telemetry_read.py --tenant-id local \\
        --runner-label local-my_idea --spec-id my_idea-sandbox [--follow 10]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

RECENT_FILLS = 20
# Seconds to wait for the first message; a run with nothing published answers empty.
FIRST_MESSAGE_TIMEOUT = 1.0
# While following, messages that arrived during the interval are already waiting.
ROUND_MESSAGE_TIMEOUT = 0.2


def subject(tenant_id: str, runner_label: str, spec_id: str) -> str:
    return f"arx.{tenant_id}.telemetry.{runner_label}.{spec_id}.>"


def runner_publishes() -> bool:
    """Whether the Custos installed here publishes these messages at all."""
    return importlib.util.find_spec("custos.offline.telemetry") is not None


class Summary:
    """What the runner has reported about a run, added up one message at a time."""

    def __init__(self, recent: int = RECENT_FILLS) -> None:
        self._recent = recent
        self.snapshots = 0
        self.first: dict[str, Any] | None = None
        self.latest: dict[str, Any] | None = None
        self.fills: list[dict[str, Any]] = []
        self.fill_count = 0
        self.closed = 0
        self.unreadable = 0
        self.fees: dict[str, Decimal] = {}
        self.realized: dict[str, Decimal] = {}

    def add(self, envelope: dict[str, Any]) -> None:
        payload = envelope.get("payload") or {}
        kind = payload.get("kind")
        if kind == "snapshot":
            self.snapshots += 1
            entry = {"occurred_at": envelope.get("occurred_at"), **payload}
            # The baseline is the first account the runner could value: until the
            # exchange answers, a snapshot reports an equity of zero.
            if self.first is None and (payload.get("status") or {}).get("reliable"):
                self.first = entry
            self.latest = entry
        elif kind == "fill":
            self.fill_count += 1
            self.fills = [*self.fills, payload][-self._recent :]
            self._total(self.fees, payload.get("commission"))
        elif kind == "position_closed":
            self.closed += 1
            self._total(self.realized, payload.get("realized_pnl"))

    def _total(self, totals: dict[str, Decimal], text: Any) -> None:
        amount, _, currency = str(text or "").strip().partition(" ")
        try:
            value = Decimal(amount)
        except InvalidOperation:
            self.unreadable += 1
            return
        totals[currency] = totals.get(currency, Decimal(0)) + value

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshots": self.snapshots,
            "first_snapshot": self.first,
            "latest_snapshot": self.latest,
            "fills": self.fills,
            "fill_count": self.fill_count,
            "closed_positions": self.closed,
            "realized_pnl": {currency: str(value) for currency, value in self.realized.items()},
            "fees": {currency: str(value) for currency, value in self.fees.items()},
            "unreadable_amounts": self.unreadable,
        }


def summarize(envelopes: Iterable[dict[str, Any]], recent: int = RECENT_FILLS) -> dict[str, Any]:
    summary = Summary(recent)
    for envelope in envelopes:
        summary.add(envelope)
    return summary.to_dict()


# A source of messages: the next envelope and how many are still waiting after it,
# or TimeoutError when none arrives in time.
NextMessage = Callable[[float], Awaitable[tuple[dict[str, Any], int]]]


async def _drain(next_message: NextMessage, summary: Summary, timeout: float) -> None:
    """Add every message waiting now, stopping once caught up or when none comes."""
    while True:
        try:
            envelope, pending = await next_message(timeout)
        except TimeoutError:
            return
        summary.add(envelope)
        if pending == 0:
            return


async def follow(
    next_message: NextMessage,
    *,
    interval: float,
    emit: Callable[[dict[str, Any]], None],
    recent: int = RECENT_FILLS,
    wait: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rounds: int | None = None,
) -> None:
    """Emit the run so far, then again every `interval` with what arrived meanwhile.

    The history is read once; after it only new messages are added, so a run that
    has been up for a day costs no more to watch than one that just started.
    """
    summary = Summary(recent)
    await _drain(next_message, summary, FIRST_MESSAGE_TIMEOUT)
    emit(summary.to_dict())
    done = 0
    while rounds is None or done < rounds:
        await wait(interval)
        await _drain(next_message, summary, ROUND_MESSAGE_TIMEOUT)
        emit(summary.to_dict())
        done += 1


def watch_stdin(stream: Any, on_close: Callable[[], None]) -> None:
    """Wait for `stream` to close, then call `on_close`.

    A client that leaves `docker exec` does not end the process it started, which
    would go on holding its subscription for good; what does reach it is its stdin
    closing. The watcher keeps that stdin open for as long as it is watching.
    """
    while stream.read(4096):
        pass
    on_close()


async def _subscribe(nats_url: str, filter_subject: str):
    import nats
    from nats.js.api import DeliverPolicy

    connection = await nats.connect(nats_url)
    subscription = await connection.jetstream().subscribe(
        filter_subject, ordered_consumer=True, deliver_policy=DeliverPolicy.ALL
    )

    async def next_message(timeout: float) -> tuple[dict[str, Any], int]:
        message = await subscription.next_msg(timeout=timeout)
        return json.loads(message.data.decode("utf-8")), message.metadata.num_pending

    return connection, next_message


async def read(nats_url: str, filter_subject: str, recent: int = RECENT_FILLS) -> dict[str, Any]:
    """The run so far, from every message the stream still holds for it."""
    connection, next_message = await _subscribe(nats_url, filter_subject)
    try:
        summary = Summary(recent)
        await _drain(next_message, summary, FIRST_MESSAGE_TIMEOUT)
        return summary.to_dict()
    finally:
        await connection.drain()


async def _follow(nats_url: str, filter_subject: str, interval: float, recent: int) -> None:
    connection, next_message = await _subscribe(nats_url, filter_subject)
    publishes = runner_publishes()

    def emit(summary: dict[str, Any]) -> None:
        print(json.dumps({"runner_publishes": publishes, **summary}), flush=True)

    try:
        await follow(next_message, interval=interval, emit=emit, recent=recent)
    finally:
        await connection.drain()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nats-url", default="nats://nats:4222")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--runner-label", required=True)
    parser.add_argument("--spec-id", required=True)
    parser.add_argument("--recent", type=int, default=RECENT_FILLS)
    parser.add_argument("--follow", type=float, metavar="SECONDS")
    parser.add_argument("--exit-on-stdin-eof", action="store_true")
    args = parser.parse_args(argv[1:])
    filter_subject = subject(args.tenant_id, args.runner_label, args.spec_id)
    if args.exit_on_stdin_eof:
        threading.Thread(
            target=watch_stdin, args=(sys.stdin, lambda: os._exit(0)), daemon=True
        ).start()
    if args.follow:
        try:
            asyncio.run(_follow(args.nats_url, filter_subject, args.follow, args.recent))
        except KeyboardInterrupt:
            pass
        return 0
    summary = asyncio.run(read(args.nats_url, filter_subject, args.recent))
    print(json.dumps({"runner_publishes": runner_publishes(), **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
