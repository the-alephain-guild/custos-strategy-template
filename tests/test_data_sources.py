from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from tools.data import common, sources
from tools.data.common import DataError

ROOT = Path(__file__).resolve().parents[1]
HOUR = 3_600_000


def offered_connectors() -> set[str]:
    questions = yaml.safe_load((ROOT / "copier.yml").read_text(encoding="utf-8"))
    return set(questions["connector"]["choices"].values())


def test_every_connector_a_strategy_can_choose_has_a_data_source() -> None:
    assert offered_connectors() == set(sources.SOURCES)


def test_every_connector_is_a_venue_the_toolkit_knows() -> None:
    from custos_toolkit_nautilus.adapter import VENUE_MAP

    assert offered_connectors() <= set(VENUE_MAP)


def test_an_unknown_connector_is_refused() -> None:
    with pytest.raises(DataError, match="no public data source"):
        sources.source_for("elsewhere")


def test_bar_types_map_to_intervals() -> None:
    assert common.interval_for("1-HOUR") == "1h"
    assert common.interval_for("15-minute") == "15m"
    with pytest.raises(DataError, match="not supported"):
        common.interval_for("7-MINUTE")


class FakeSource:
    NAME = "Fake"
    CONNECTORS = {"fake": "perpetual"}
    INTERVALS = frozenset({"1h"})

    def __init__(self, rows):
        self.rows, self.calls = rows, 0

    def symbol_for(self, connector, pair):
        return pair

    def fetch_rules(self, connector, pair):
        return {"symbol": pair}

    def fetch_klines(self, connector, pair, interval, start_ms, end_ms):
        self.calls += 1
        return self.rows


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(common, "DATA_DIR", tmp_path)

    def install(rows):
        source = FakeSource(rows)
        monkeypatch.setitem(sources.SOURCES, "fake", source)
        return source

    return install


def test_bars_are_cached_per_connector_and_reused(fake, tmp_path: Path) -> None:
    source = fake([(t * HOUR, "1", "2", "0.5", "1.5", "3") for t in range(4)])
    start, end = datetime.fromtimestamp(0, UTC), datetime.fromtimestamp(4 * 3600, UTC)
    csv_path, rules_path = sources.ensure_data("fake", "X-Y", "1-HOUR", start, end)
    assert csv_path == tmp_path / "fake" / "X-Y_1h.csv"
    assert rules_path.read_text(encoding="utf-8").strip().startswith("{")
    assert csv_path.read_text(encoding="utf-8").splitlines()[1] == "0,1,2,0.5,1.5,3"
    sources.ensure_data("fake", "X-Y", "1-HOUR", start, end)
    assert source.calls == 1


def test_a_bar_that_has_not_closed_is_not_cached(fake, monkeypatch) -> None:
    fake([(0, "1", "1", "1", "1", "1"), (HOUR, "1", "1", "1", "1", "1")])
    monkeypatch.setattr(sources.time, "time", lambda: 1.5 * 3600)
    start, end = datetime.fromtimestamp(0, UTC), datetime.fromtimestamp(2 * 3600, UTC)
    csv_path, _ = sources.ensure_data("fake", "X-Y", "1-HOUR", start, end)
    assert len(csv_path.read_text(encoding="utf-8").splitlines()) == 2


def test_an_interval_the_exchange_lacks_is_refused_with_the_ones_it_has(fake) -> None:
    fake([])
    with pytest.raises(DataError, match="Fake has no 4h bars for fake; use 1h"):
        sources.ensure_data("fake", "X-Y", "4-HOUR", datetime.now(UTC), datetime.now(UTC))


def test_sodex_perpetual_intervals_are_narrower_than_spot() -> None:
    assert "3m" in sources.intervals_for("sodex")
    assert "3m" not in sources.intervals_for("sodex_perpetual")
