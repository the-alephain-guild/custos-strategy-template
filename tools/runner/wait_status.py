#!/usr/bin/env python3
"""Wait until the runner reports a generation in a given lifecycle phase.

This runs inside the Custos image, which ships the NATS client it needs. The
runner publishes its status to a JetStream subject that keeps the latest message,
so the last status is read first and the wait ends as soon as the target
generation reports the target phase and is healthy. A newer generation than the
one awaited means this one was replaced before it landed, and is reported as such.
"""

from __future__ import annotations

import argparse
import asyncio
import json


async def wait_for_status(
    *, nats_url: str, subject: str, generation: int, phase: str, timeout: float
) -> None:
    import nats
    from nats.js.api import DeliverPolicy

    connection = await nats.connect(nats_url)
    try:
        subscription = await connection.jetstream().subscribe(
            subject, ordered_consumer=True, deliver_policy=DeliverPolicy.LAST
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"generation {generation} never reported {phase}")
            try:
                message = await subscription.next_msg(timeout=remaining)
            except TimeoutError as error:
                raise TimeoutError(f"generation {generation} never reported {phase}") from error
            payload = json.loads(message.data.decode("utf-8")).get("payload", {})
            observed = payload.get("observed_generation")
            if not isinstance(observed, int):
                continue
            if observed > generation:
                raise AssertionError(f"generation {generation} was replaced by {observed}")
            if observed == generation:
                if payload.get("phase") != phase:
                    raise AssertionError(f"expected phase {phase}, got {payload.get('phase')}")
                if payload.get("health") != "healthy":
                    raise AssertionError(f"expected healthy, got {payload.get('health')}")
                print(f"generation {generation} is {phase} and healthy")
                return
    finally:
        await connection.drain()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    asyncio.run(
        wait_for_status(
            nats_url=args.nats_url,
            subject=args.subject,
            generation=args.generation,
            phase=args.phase,
            timeout=args.timeout,
        )
    )


if __name__ == "__main__":
    main()
