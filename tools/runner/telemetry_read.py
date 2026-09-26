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

Usage (inside the runner container):
    python tools/runner/telemetry_read.py --tenant-id local \\
        --runner-label local-my_idea --spec-id my_idea-sandbox
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import sys
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

RECENT_FILLS = 20
# Seconds to wait for the first message; a run with nothing published answers empty.
FIRST_MESSAGE_TIMEOUT = 1.0


def subject(tenant_id: str, runner_label: str, spec_id: str) -> str:
    return f"arx.{tenant_id}.telemetry.{runner_label}.{spec_id}.>"


def runner_publishes() -> bool:
    """Whether the Custos installed here publishes these messages at all."""
    return importlib.util.find_spec("custos.offline.telemetry") is not None


def summarize(envelopes: Iterable[dict[str, Any]], recent: int = RECENT_FILLS) -> dict[str, Any]:
    snapshots = 0
    first = latest = None
    fills: list[dict[str, Any]] = []
    fill_count = closed = unreadable = 0
    fees: dict[str, Decimal] = {}
    realized: dict[str, Decimal] = {}

    def add(totals: dict[str, Decimal], text: Any) -> None:
        nonlocal unreadable
        amount, _, currency = str(text or "").strip().partition(" ")
        try:
            value = Decimal(amount)
        except InvalidOperation:
            unreadable += 1
            return
        totals[currency] = totals.get(currency, Decimal(0)) + value

    for envelope in envelopes:
        payload = envelope.get("payload") or {}
        kind = payload.get("kind")
        if kind == "snapshot":
            snapshots += 1
            entry = {"occurred_at": envelope.get("occurred_at"), **payload}
            # The baseline is the first account the runner could value: until the
            # exchange answers, a snapshot reports an equity of zero.
            if first is None and (payload.get("status") or {}).get("reliable"):
                first = entry
            latest = entry
        elif kind == "fill":
            fill_count += 1
            fills = [*fills, payload][-recent:]
            add(fees, payload.get("commission"))
        elif kind == "position_closed":
            closed += 1
            add(realized, payload.get("realized_pnl"))

    return {
        "snapshots": snapshots,
        "first_snapshot": first,
        "latest_snapshot": latest,
        "fills": fills,
        "fill_count": fill_count,
        "closed_positions": closed,
        "realized_pnl": {currency: str(value) for currency, value in realized.items()},
        "fees": {currency: str(value) for currency, value in fees.items()},
        "unreadable_amounts": unreadable,
    }


async def read(nats_url: str, filter_subject: str) -> list[dict[str, Any]]:
    """Every message the run's stream still holds for this run, oldest first."""
    import nats
    from nats.js.api import DeliverPolicy

    connection = await nats.connect(nats_url)
    envelopes = []
    try:
        subscription = await connection.jetstream().subscribe(
            filter_subject, ordered_consumer=True, deliver_policy=DeliverPolicy.ALL
        )
        timeout = FIRST_MESSAGE_TIMEOUT
        while True:
            try:
                message = await subscription.next_msg(timeout=timeout)
            except TimeoutError:
                break
            envelopes.append(json.loads(message.data.decode("utf-8")))
            if message.metadata.num_pending == 0:
                break
    finally:
        await connection.drain()
    return envelopes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nats-url", default="nats://nats:4222")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--runner-label", required=True)
    parser.add_argument("--spec-id", required=True)
    parser.add_argument("--recent", type=int, default=RECENT_FILLS)
    args = parser.parse_args(argv[1:])
    envelopes = asyncio.run(
        read(args.nats_url, subject(args.tenant_id, args.runner_label, args.spec_id))
    )
    summary = {"runner_publishes": runner_publishes(), **summarize(envelopes, args.recent)}
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
