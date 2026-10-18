import asyncio
import json
import sys
import types
from enum import Enum

import pytest

from tools.runner import wait_status


class _Subscription:
    """Status messages as JetStream hands them over, then silence until the timeout."""

    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = list(payloads)

    async def next_msg(self, timeout: float) -> types.SimpleNamespace:
        if self._payloads:
            data = json.dumps({"payload": self._payloads.pop(0)}).encode("utf-8")
            return types.SimpleNamespace(data=data)
        await asyncio.sleep(timeout)
        raise TimeoutError


class _Connection:
    def __init__(self, subscription: _Subscription) -> None:
        self._subscription = subscription
        self.drained = False

    def jetstream(self) -> "_Connection":
        return self

    async def subscribe(self, subject: str, **_: object) -> _Subscription:
        return self._subscription

    async def drain(self) -> None:
        self.drained = True


def _status(health: str, generation: int = 3, phase: str = "running") -> dict:
    return {"observed_generation": generation, "phase": phase, "health": health}


@pytest.fixture
def stream(monkeypatch: pytest.MonkeyPatch):
    """Stands in for the NATS client the image ships, fed with the given statuses."""

    def feed(payloads: list[dict]) -> _Connection:
        connection = _Connection(_Subscription(payloads))

        async def connect(url: str) -> _Connection:
            return connection

        api = types.ModuleType("nats.js.api")
        api.DeliverPolicy = Enum("DeliverPolicy", "LAST")
        js = types.ModuleType("nats.js")
        js.api = api
        nats = types.ModuleType("nats")
        nats.connect = connect
        nats.js = js
        monkeypatch.setitem(sys.modules, "nats", nats)
        monkeypatch.setitem(sys.modules, "nats.js", js)
        monkeypatch.setitem(sys.modules, "nats.js.api", api)
        return connection

    return feed


def _wait(timeout: float = 0.2) -> None:
    asyncio.run(
        wait_status.wait_for_status(
            nats_url="nats://nats:4222",
            subject="arx.tenant.deployment_status.runner.spec",
            generation=3,
            phase="running",
            timeout=timeout,
        )
    )


def test_an_unhealthy_status_that_turns_healthy_is_a_start(stream, capsys) -> None:
    """A venue call that fails once and succeeds on the retry does not fail the start."""
    connection = stream([_status("unhealthy"), _status("healthy")])

    _wait()

    assert "generation 3 is running and healthy" in capsys.readouterr().out
    assert connection.drained


def test_a_status_that_stays_unhealthy_fails_at_the_timeout_and_says_how_to_look(stream) -> None:
    connection = stream([_status("unhealthy"), _status("unhealthy")])

    with pytest.raises(TimeoutError) as failure:
        _wait()

    message = str(failure.value)
    assert "generation 3 reported running but was still unhealthy" in message
    assert "the container may still be running" in message
    assert "make status" in message and "make stop" in message
    assert connection.drained


def test_a_generation_that_never_reports_says_how_to_look_too(stream) -> None:
    stream([_status("healthy", generation=2)])

    with pytest.raises(TimeoutError, match="generation 3 never reported running") as failure:
        _wait()

    assert "the container may still be running" in str(failure.value)


def test_a_replaced_generation_still_fails_at_once(stream) -> None:
    stream([_status("unhealthy"), _status("healthy", generation=4)])

    with pytest.raises(AssertionError, match="generation 3 was replaced by 4"):
        _wait(timeout=30)
