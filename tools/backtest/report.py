"""Chart data contracts: event time is separate from any display coordinate."""

from __future__ import annotations

import json
import math
import re
import shutil
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from tools.backtest.language import render_template, validate_language


class ChartError(ValueError):
    """The backtest ran, but its chart data cannot be reported faithfully."""


def finite(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ChartError("expected a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ChartError("expected a finite number")
    return result


def timestamp(value: object) -> str:
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or value <= 0:
        raise ChartError("expected a positive integer nanosecond timestamp")
    return str(value)


def split_fill(before: Decimal, side: str, quantity: Decimal) -> tuple[list[dict], Decimal]:
    if side not in ("BUY", "SELL") or not quantity.is_finite() or quantity <= 0:
        raise ChartError("invalid fill side or quantity")
    change = quantity if side == "BUY" else -quantity
    closing = min(abs(before), quantity) if before * change < 0 else Decimal(0)
    legs = []
    if closing:
        legs.append(
            {"action": "exit_long" if before > 0 else "exit_short", "quantity": str(closing)}
        )
    if quantity > closing:
        legs.append(
            {
                "action": "enter_long" if change > 0 else "enter_short",
                "quantity": str(quantity - closing),
            }
        )
    return legs, before + change


def indicator_snapshot(series: dict, snapshot: object, ctx: object, bar: object) -> None:
    if not isinstance(snapshot, dict):
        raise ChartError("get_chart_snapshot must return a mapping")
    for key, raw in snapshot.items():
        if not isinstance(key, str) or not key or not isinstance(raw, dict):
            raise ChartError("indicator series needs a string key and a descriptor")
        fields = {
            "value",
            "label",
            "label_i18n",
            "pane",
            "style",
            "color",
            "segment",
            "levels",
            "source_time_ns",
            "segment_color",
            "render",
            "projection_from",
            "line_style",
            "default_visible",
        }
        if set(raw) - fields or "value" not in raw:
            raise ChartError("unknown indicator fields or missing value")
        pane, style = raw.get("pane", "price"), raw.get("style", "line")
        if not isinstance(pane, str) or not pane or style not in ("line", "histogram", "area"):
            raise ChartError("invalid indicator pane or style")
        color = raw.get("color", "#60a5fa")
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ChartError("indicator color must be #RRGGBB")
        label = raw.get("label", key)
        if not isinstance(label, str):
            raise ChartError("indicator label must be text")
        descriptor = {
            "key": key,
            "label": label,
            "pane": pane,
            "style": style,
            "color": color,
            "levels": [finite(n) for n in raw.get("levels", [])],
            "instrument_id": str(ctx.instrument_id),
            "bar_type": str(bar.bar_type),
            "provenance": "bar_snapshot",
        }
        for field in ("render", "projection_from", "line_style", "default_visible"):
            if field in raw:
                descriptor[field] = raw[field]
        validate_drawing(descriptor)
        if "label_i18n" in raw:
            translations = raw["label_i18n"]
            if (
                not isinstance(translations, dict)
                or set(translations) != {"zh", "en"}
                or any(not isinstance(v, str) or not v.strip() for v in translations.values())
            ):
                raise ChartError("label_i18n must provide nonempty zh and en labels")
            descriptor["label_i18n"] = dict(translations)
        identity = f"{ctx.instrument_id}|{bar.bar_type}|{key}"
        existing = series.setdefault(identity, {**descriptor, "points": []})
        if {k: v for k, v in existing.items() if k != "points"} != descriptor:
            raise ChartError(f"indicator descriptor changed: {key}")
        point = {
            "time_ns": timestamp(bar.ts_event),
            "value": finite(raw["value"]) if raw["value"] is not None else None,
            "segment": raw.get("segment"),
        }
        if point["segment"] is not None and type(point["segment"]) not in (str, int):
            raise ChartError("segment must be a string or integer")
        if "segment_color" in raw:
            if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(raw["segment_color"])):
                raise ChartError("segment_color must be #RRGGBB")
            point["color"] = raw["segment_color"]
        if "source_time_ns" in raw:
            point["source_time_ns"] = timestamp(raw["source_time_ns"])
            if int(point["source_time_ns"]) > bar.ts_event:
                raise ChartError("indicator source time is in the future")
        if raw.get("render") == "path" and point["value"] is not None:
            if "source_time_ns" not in point:
                raise ChartError("path points require a source time")
            previous = next(
                (p for p in reversed(existing["points"]) if p["value"] is not None), None
            )
            if previous and "projection_from" not in raw:
                if int(previous["source_time_ns"]) >= int(point["source_time_ns"]):
                    raise ChartError("confirmed path source times must increase")
        if existing["points"] and int(existing["points"][-1]["time_ns"]) >= bar.ts_event:
            raise ChartError("indicator snapshot times must increase")
        existing["points"].append(point)


def validate_drawing(series: dict) -> None:
    if series.get("render", "observations") not in ("observations", "path"):
        raise ChartError("invalid indicator render mode")
    if series.get("line_style", "solid") not in ("solid", "dashed"):
        raise ChartError("invalid indicator line style")
    if type(series.get("default_visible", True)) is not bool:
        raise ChartError("default_visible must be a boolean")
    if series.get("render") == "path":
        if series["style"] != "line" or series["provenance"] != "bar_snapshot":
            raise ChartError("paths require observed line snapshots")
    if "projection_from" in series:
        source = series["projection_from"]
        if series.get("render") != "path" or not isinstance(source, str) or not source:
            raise ChartError("projection_from requires a path series key")
        if source == series["key"]:
            raise ChartError("a projection cannot reference itself")


def legacy_indicators(history: object) -> list[dict]:
    if not isinstance(history, dict):
        raise ChartError("get_indicator_history must return a mapping")
    result = []
    for name, raw in history.items():
        if not isinstance(raw, dict) or not isinstance(raw.get("points", []), list):
            raise ChartError("invalid historical indicator descriptor")
        points = []
        for point in raw.get("points", []):
            # The legacy interface uses UTC seconds. It cannot prove when the
            # value became available; never label it as a causal snapshot.
            seconds = finite(point["time"])
            points.append(
                {
                    "time_ns": timestamp(int(Decimal(str(seconds)) * 1_000_000_000)),
                    "value": finite(point["value"]) if point["value"] is not None else None,
                    "segment": point.get("direction"),
                }
            )
        if any(
            int(a["time_ns"]) >= int(b["time_ns"]) for a, b in zip(points, points[1:], strict=False)
        ):
            raise ChartError("historical indicator times must increase")
        config = raw.get("config", {})
        result.append(
            {
                "key": name,
                "label": name,
                "pane": config.get("pane", "price"),
                "style": config.get("style", "line"),
                "color": config.get("color", "#94a3b8"),
                "levels": [],
                "instrument_id": raw.get("instrument_id"),
                "bar_type": raw.get("bar_type"),
                "points": points,
                "provenance": "historical_unverified",
            }
        )
    return result


ACTIONS = {"enter_long", "exit_long", "enter_short", "exit_short"}


def validate_trigger_details(raw: object, observed_at: int) -> None:
    if raw is None:
        return
    fields = {"version", "observed_time_ns", "reason_code", "reason_i18n", "fields"}
    if (
        not isinstance(raw, dict)
        or set(raw) != fields
        or type(raw["version"]) is not int
        or raw["version"] != 1
    ):
        raise ChartError("invalid signal trigger details")
    observed = int(timestamp(raw["observed_time_ns"]))
    if observed > observed_at:
        raise ChartError("trigger observation is after its signal")
    if not isinstance(raw["reason_code"], str) or not raw["reason_code"]:
        raise ChartError("trigger reason needs a code")

    def translated(value):
        if (
            not isinstance(value, dict)
            or set(value) != {"zh", "en"}
            or any(not isinstance(v, str) or not v.strip() for v in value.values())
        ):
            raise ChartError("trigger labels require nonempty zh and en text")

    translated(raw["reason_i18n"])
    if not isinstance(raw["fields"], list) or not raw["fields"]:
        raise ChartError("trigger fields must be a nonempty list")
    keys = set()
    for field in raw["fields"]:
        required = {"key", "label_i18n", "format", "value"}
        if (
            not isinstance(field, dict)
            or not required <= set(field)
            or set(field) - required - {"value_i18n"}
        ):
            raise ChartError("invalid trigger field descriptor")
        key, kind, value = field["key"], field["format"], field["value"]
        if not isinstance(key, str) or not key or key in keys:
            raise ChartError("trigger field keys must be unique nonempty strings")
        keys.add(key)
        translated(field["label_i18n"])
        if kind not in ("number", "percent", "time", "text"):
            raise ChartError("invalid trigger field format")
        if "value_i18n" in field:
            translated(field["value_i18n"])
            if kind != "text" or not isinstance(value, str):
                raise ChartError("translated trigger values must be text")
        if value is None:
            continue
        if kind in ("number", "percent"):
            if isinstance(value, str):
                if not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
                    raise ChartError("trigger decimal strings must use plain decimal notation")
                value = Decimal(value)
            finite(value)
        elif kind == "time":
            if int(timestamp(value)) > observed:
                raise ChartError("trigger field time is after its observation")
        elif not isinstance(value, str):
            raise ChartError("trigger text must be a string")


def validate_report(report: dict) -> None:
    if report["schema_version"] != 1:
        raise ChartError("unsupported chart schema")
    candles: set[tuple] = set()
    for bar in report["candles"]:
        opened, closed = int(timestamp(bar["open_time_ns"])), int(timestamp(bar["close_time_ns"]))
        if opened >= closed:
            raise ChartError("candle open must precede its close")
        for field in ("open", "high", "low", "close", "volume"):
            finite(bar[field])
        if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(
            bar["open"], bar["close"]
        ):
            raise ChartError("invalid candle range")
        identity = bar["instrument_id"], bar["bar_type"], closed
        if identity in candles:
            raise ChartError("duplicate candle")
        candles.add(identity)
    for signal in report["signals"]:
        event_time = int(timestamp(signal["event_time_ns"]))
        validate_trigger_details(signal["metadata"].get("trigger_details"), event_time)
        if signal["action"] not in ACTIONS or not Decimal(signal["price"]).is_finite():
            raise ChartError("invalid signal action or price")
        confirmed = signal["metadata"].get("confirmed_at_ns")
        if confirmed is not None and event_time < int(timestamp(confirmed)):
            raise ChartError("signal precedes its confirmation")
    ids = {s["id"] for s in report["signals"]}
    signal_times = {s["id"]: int(s["event_time_ns"]) for s in report["signals"]}
    for fill in report["fills"]:
        timestamp(fill["event_time_ns"])
        if not Decimal(fill["price"]).is_finite():
            raise ChartError("invalid fill price")
        if fill["signal_id"] is not None and fill["signal_id"] not in ids:
            raise ChartError("fill refers to an absent signal")
        if (
            fill["signal_id"] is not None
            and int(fill["event_time_ns"]) < signal_times[fill["signal_id"]]
        ):
            raise ChartError("fill precedes its signal")
    streams = {(bar["instrument_id"], bar["bar_type"]) for bar in report["candles"]}
    for series in report["indicators"]:
        validate_drawing(series)
        stream = series["instrument_id"], series["bar_type"]
        if series["provenance"] == "historical_unverified" and None in stream:
            if len(streams) != 1:
                raise ChartError(
                    "legacy indicator must specify instrument_id and bar_type for multiple feeds"
                )
            series["instrument_id"], series["bar_type"] = next(iter(streams))
        elif stream not in streams:
            raise ChartError("indicator refers to an absent candle feed")
        if series["style"] not in ("line", "histogram", "area"):
            raise ChartError("invalid indicator style")
        previous_time, previous_source = 0, 0
        for point in series["points"]:
            timestamp(point["time_ns"])
            observed = int(point["time_ns"])
            if observed <= previous_time:
                raise ChartError("indicator snapshot times must increase")
            previous_time = observed
            if "source_time_ns" in point and int(timestamp(point["source_time_ns"])) > observed:
                raise ChartError("indicator source time is in the future")
            if point["value"] is not None:
                finite(point["value"])
                if series.get("render") == "path":
                    if "source_time_ns" not in point:
                        raise ChartError("path points require a source time")
                    source = int(point["source_time_ns"])
                    if "projection_from" not in series and source <= previous_source:
                        raise ChartError("confirmed path source times must increase")
                    previous_source = source
        if "projection_from" in series:
            parents = [
                s
                for s in report["indicators"]
                if s["key"] == series["projection_from"]
                and (s["instrument_id"], s["bar_type"]) == stream
            ]
            if (
                len(parents) != 1
                or parents[0].get("render") != "path"
                or "projection_from" in parents[0]
                or parents[0]["pane"] != series["pane"]
            ):
                raise ChartError("projection needs a confirmed path in the same feed and pane")


def json_safe(value: object) -> object:
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if type(value) is int and abs(value) > 9_007_199_254_740_991:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_report(strategy_dir: Path, summary: dict, report: dict, *, language: str = "zh") -> Path:
    validate_language(language)
    validate_report(report)
    assets = Path(__file__).with_name("assets")
    folder = (
        strategy_dir
        / "backtests/output"
        / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex[:12])
    )
    folder.mkdir(parents=True, exist_ok=False)
    payload = json.dumps(
        json_safe({**report, "summary": summary}), allow_nan=False, ensure_ascii=True
    )
    (folder / "chart.json").write_text(payload + "\n", encoding="utf-8")
    (folder / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    shutil.copytree(assets, folder / "assets")
    # JSON is data, not source. Escape HTML delimiters before embedding in a
    # script element, including when strategy metadata contains closing tags.
    escaped = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    template = render_template((assets / "report.html").read_text(encoding="utf-8"), language)
    path = folder / "report.html"
    path.write_text(template.replace("__BACKTEST_DATA__", escaped), encoding="utf-8")
    return path
