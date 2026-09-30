#!/usr/bin/env python3
"""Build the DeploymentSpec a release is deployed with, and show what it says.

Creating a DeploymentSpec in ARX starts its first instance: the authenticator
code entered for it is the confirmation of exactly what will trade. So the
request is built here, in full, before anything is sent, and the summary shown
for that confirmation is read from the very object that would be sent. Nothing
shown is taken from anywhere else.

The body follows ARX's *Release & Deployment API* (Deployment specs). It is put
together from three places:

- the release: its trading scope (connector, pairs, leverage), read back from
  the release's own strategy manifest, which is the only source for it;
  `config.yaml` must agree with it, or the spec is refused here;
- `strategies/<category>/<name>/deploy.yaml`, the settings that are yours to
  choose per deployment: risk limits, scheduling, venue source policy, runner
  contract requirements, the strategy_config overrides, and per mode the
  engine binding, the credential scope, the sandbox balances or the shutdown
  policy;
- what the command is given: the mode, the target runner and the product.

The three policy digests are computed as ARX's guide specifies: keys sorted at
every depth, arrays kept in order, compact UTF-8, only integer numbers, then
SHA-256 in lower-case hex. A decimal is always written as a string; a YAML
number with a fraction is refused rather than hashed.

`make deploy-preview` only builds and shows. It reads the release back from its
package (as `make arx-evidence` does) and sends nothing to ARX.

Usage:
    python3 tools/arx/spec.py trend/my_idea --mode sandbox --runner <uuid> \
        --product <uuid> [--version 0.2.0] [--release <uuid>]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import ui  # noqa: E402
from tools.arx.client import ArxError  # noqa: E402

DEPLOY_FILE = "deploy.yaml"
MODES = ("sandbox", "testnet")
CHANNEL_TYPES = {"sandbox": "sandbox_sim_engine", "testnet": "testnet_venue"}
# A policy id is derived from the policy's digest unless deploy.yaml names one,
# so the same limits always carry the same id.
POLICY_ID_NAMESPACE = uuid.UUID("0f6f3c52-8d7e-4a51-9b0c-2e5d1f7a3c64")
DAILY_LOSS_BASE = "start_of_day_cash_flow_adjusted_nav"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DECIMAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")
CURRENCY = re.compile(r"^[A-Z]{3,12}$")
LOCAL_TIME = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]$")

DEPLOY_FIELDS = {
    "reason": False,
    "log_level": True,
    "strategy_config": True,
    "nautilus_config": True,
    "risk_policy": True,
    "scheduling_policy": True,
    "venue_source_policy": True,
    "runner_contract_requirements": True,
    "sandbox": False,
    "testnet": False,
}
MODE_FIELDS = {
    "sandbox": {"engine_binding_id", "credential_scope", "starting_balances", "shutdown_policy"},
    "testnet": {"engine_binding_id", "credential_scope", "shutdown_policy"},
}
REQUIRED_MODE_FIELDS = {
    "sandbox": {"engine_binding_id", "credential_scope", "starting_balances"},
    "testnet": {"engine_binding_id", "credential_scope"},
}
POLICY_FIELDS = {
    "schema_version",
    "trading_day",
    "max_total_exposure",
    "max_total_drawdown_ratio",
    "max_daily_loss",
    "max_notional_leverage",
    "max_single_strategy_allocation_ratio",
    "cooldown_seconds",
}
# Each section of runner_contract_requirements, with the report versions it names.
CONTRACT_SECTIONS = {
    "risk": ({"equity_snapshot", "position_snapshot", "heartbeat"}, set()),
    "settlement": ({"fill", "position_closed", "fee", "period_closed"}, set()),
    "reconciliation": (
        {
            "execution_fill",
            "venue_ledger_snapshot_manifest",
            "venue_ledger_snapshot_chunk",
            "reconciliation_period_closed",
        },
        {"valuation_checkpoint"},
    ),
    "health": ({"heartbeat"}, set()),
    "deployment_lifecycle": ({"deployment_lifecycle"}, set()),
}
LEDGER_SOURCES = ("venue_api", "drop_copy")


class SpecError(ArxError):
    """A deployment spec that cannot be built as asked, and how to put it right."""


# -- canonical JSON and digests ------------------------------------------------


def _check_canonical(value: object, where: str) -> None:
    if isinstance(value, float):
        raise SpecError(
            f"{where} is the number {value!r}; decimals are written as strings",
            f"quote it: '{value}'",
        )
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SpecError(f"{where} has a key that is not a string: {key!r}")
            _check_canonical(item, f"{where}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check_canonical(item, f"{where}[{index}]")
    elif value is not None and not isinstance(value, str | int | bool):
        raise SpecError(f"{where} is a {type(value).__name__}, which JSON cannot hold as is")


def canonical_json(value: object, where: str = "value") -> bytes:
    """ARX's canonical form: sorted keys at every depth, arrays in order, compact UTF-8."""

    _check_canonical(value, where)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def digest(value: object, where: str = "value") -> str:
    return hashlib.sha256(canonical_json(value, where)).hexdigest()


# -- small checks ----------------------------------------------------------------


def _uuid(value: object, what: str, fix: str = "") -> str:
    try:
        parsed = uuid.UUID(str(value))
    except ValueError:
        raise SpecError(f"{what} is a UUID, not {value!r}", fix) from None
    if parsed.int == 0:
        raise SpecError(f"{what} must not be the nil UUID", fix)
    return str(parsed)


def _mapping(value: object, what: str) -> dict:
    if not isinstance(value, Mapping):
        raise SpecError(f"{what} must be a mapping")
    return dict(value)


def _fields(value: Mapping, what: str, required: set[str], optional: set[str] = frozenset()):
    keys = set(value)
    missing, unknown = sorted(required - keys), sorted(keys - required - set(optional))
    if missing or unknown:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if unknown:
            parts.append(f"unknown {', '.join(unknown)}")
        raise SpecError(f"{what}: {'; '.join(parts)}")


def _decimal(value: object, what: str, low=None, high=None, above=None) -> str:
    if isinstance(value, float):
        raise SpecError(
            f"{what} is written as a number; decimals are strings", f"quote it: '{value}'"
        )
    if not isinstance(value, str) or not DECIMAL.fullmatch(value):
        raise SpecError(f"{what} must be a decimal string such as '0.25', not {value!r}")
    number = Decimal(value)
    if low is not None and number < Decimal(low):
        raise SpecError(f"{what} must be at least {low}, not {value}")
    if high is not None and number > Decimal(high):
        raise SpecError(f"{what} must be at most {high}, not {value}")
    if above is not None and number <= Decimal(above):
        raise SpecError(f"{what} must be above {above}, not {value}")
    return value


def _integer(value: object, what: str, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{what} must be a whole number, not {value!r}")
    if value < low or (high is not None and value > high):
        span = f"from {low} to {high}" if high is not None else f"{low} or more"
        raise SpecError(f"{what} must be {span}, not {value}")
    return value


def _money(value: object, what: str) -> dict:
    money = _mapping(value, what)
    _fields(money, what, {"currency", "amount"})
    if not isinstance(money["currency"], str) or not CURRENCY.fullmatch(money["currency"]):
        raise SpecError(f"{what}.currency is 3 to 12 upper-case letters, not {money['currency']!r}")
    _decimal(money["amount"], f"{what}.amount", above=0)
    return money


# -- deploy.yaml -------------------------------------------------------------------


def check_risk_policy(policy: Mapping, where: str = "risk_policy.policy") -> dict:
    policy = _mapping(policy, where)
    _fields(policy, where, POLICY_FIELDS)
    _integer(policy["schema_version"], f"{where}.schema_version", 1, 1)
    day = _mapping(policy["trading_day"], f"{where}.trading_day")
    _fields(day, f"{where}.trading_day", {"time_zone", "boundary_local_time"})
    if not isinstance(day["time_zone"], str) or not day["time_zone"]:
        raise SpecError(f"{where}.trading_day.time_zone is an IANA name such as Etc/UTC")
    if not isinstance(day["boundary_local_time"], str) or not LOCAL_TIME.fullmatch(
        day["boundary_local_time"]
    ):
        raise SpecError(
            f"{where}.trading_day.boundary_local_time is HH:MM:SS in quotes, "
            f"not {day['boundary_local_time']!r}"
        )
    _money(policy["max_total_exposure"], f"{where}.max_total_exposure")
    _decimal(policy["max_total_drawdown_ratio"], f"{where}.max_total_drawdown_ratio", 0, 1)
    loss = _mapping(policy["max_daily_loss"], f"{where}.max_daily_loss")
    if loss.get("kind") == "absolute":
        _fields(loss, f"{where}.max_daily_loss", {"kind", "value"})
        _money(loss["value"], f"{where}.max_daily_loss.value")
    elif loss.get("kind") == "ratio":
        _fields(loss, f"{where}.max_daily_loss", {"kind", "limit", "base"})
        _decimal(loss["limit"], f"{where}.max_daily_loss.limit", 0, 1)
        if loss["base"] != DAILY_LOSS_BASE:
            raise SpecError(f"{where}.max_daily_loss.base is {DAILY_LOSS_BASE}")
    else:
        raise SpecError(f"{where}.max_daily_loss.kind is absolute or ratio")
    _decimal(policy["max_notional_leverage"], f"{where}.max_notional_leverage", above=0)
    if policy["max_single_strategy_allocation_ratio"] is not None:
        _decimal(
            policy["max_single_strategy_allocation_ratio"],
            f"{where}.max_single_strategy_allocation_ratio",
            0,
            1,
        )
    _integer(policy["cooldown_seconds"], f"{where}.cooldown_seconds", 1, 86400)
    canonical_json(policy, where)
    return policy


def check_contract_requirements(value: object, venue_policy: list) -> dict:
    where = "runner_contract_requirements"
    requirements = _mapping(value, where)
    unknown = sorted(set(requirements) - set(CONTRACT_SECTIONS))
    if unknown:
        raise SpecError(f"{where}: unknown sections {', '.join(unknown)}")
    for section, content in requirements.items():
        required, optional = CONTRACT_SECTIONS[section]
        content = _mapping(content, f"{where}.{section}")
        _fields(content, f"{where}.{section}", required | {"schema_version"}, optional)
        _integer(content["schema_version"], f"{where}.{section}.schema_version", 1, 1)
        for report in set(content) - {"schema_version"}:
            if content[report] != "v1":
                raise SpecError(f"{where}.{section}.{report} is 'v1', not {content[report]!r}")
    if bool(venue_policy) != ("reconciliation" in requirements):
        raise SpecError(
            f"{where}.reconciliation must be present exactly when venue_source_policy is not empty"
        )
    return requirements


def check_venue_policy(value: object) -> list:
    if not isinstance(value, list):
        raise SpecError("venue_source_policy must be a list, [] for none")
    seen = set()
    for index, entry in enumerate(value):
        where = f"venue_source_policy[{index}]"
        entry = _mapping(entry, where)
        _fields(entry, where, {"venue", "ledger_source"})
        venue = entry["venue"]
        if not isinstance(venue, str) or not 1 <= len(venue) <= 128:
            raise SpecError(f"{where}.venue is 1 to 128 characters")
        if venue in seen:
            raise SpecError(f"venue_source_policy names {venue} twice")
        seen.add(venue)
        if entry["ledger_source"] not in LEDGER_SOURCES:
            raise SpecError(f"{where}.ledger_source is venue_api or drop_copy")
    return value


def check_scheduling(value: object) -> dict:
    policy = _mapping(value, "scheduling_policy")
    _fields(policy, "scheduling_policy", {"timezone", "schedule"})
    if not isinstance(policy["timezone"], str) or not policy["timezone"]:
        raise SpecError("scheduling_policy.timezone is an IANA name such as Etc/UTC")
    canonical_json(policy, "scheduling_policy")
    return policy


def check_shutdown(value: object, where: str) -> dict:
    policy = _mapping(value, where)
    _fields(policy, where, {"schema_version", "position_policy", "confirmation_timeout_secs"})
    _integer(policy["schema_version"], f"{where}.schema_version", 1, 1)
    if policy["position_policy"] not in ("preserve", "flatten"):
        raise SpecError(f"{where}.position_policy is preserve or flatten")
    _integer(policy["confirmation_timeout_secs"], f"{where}.confirmation_timeout_secs", 1, 120)
    return policy


def load_deploy_file(path: Path) -> dict:
    """deploy.yaml, refused if a field is missing, unknown or of the wrong kind."""

    if not path.is_file():
        raise SpecError(
            f"{path} is missing; it holds the settings a deployment is made with",
            "copy examples/trend/sma_cross/deploy.yaml next to the strategy's config.yaml "
            "and fill it in",
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as failure:
        raise SpecError(f"{path} is not valid YAML: {failure}") from None
    data = _mapping(data or {}, path.name)
    required = {name for name, needed in DEPLOY_FIELDS.items() if needed}
    _fields(data, path.name, required, set(DEPLOY_FIELDS) - required)
    return data


def _mode_settings(settings: Mapping, mode: str) -> dict:
    if mode not in MODES:
        raise SpecError(
            f"a deployment spec is created for sandbox or testnet, not {mode!r}; "
            "live comes only from an approved promotion"
        )
    if settings.get(mode) is None:
        raise SpecError(f"{DEPLOY_FILE} has no {mode}: section", f"add a {mode}: section to it")
    section = _mapping(settings[mode], f"{DEPLOY_FILE} {mode}")
    _fields(section, f"{DEPLOY_FILE} {mode}", REQUIRED_MODE_FIELDS[mode], MODE_FIELDS[mode])
    return section


# -- config.yaml and the release's trading scope -----------------------------------------


def _value(entry: object) -> object:
    return entry.get("value") if isinstance(entry, Mapping) and "value" in entry else entry


def check_trading_scope(scope: object) -> dict:
    """The release manifest's trading_scope, refused unless it is complete and well formed."""

    if not isinstance(scope, Mapping):
        raise SpecError(
            "the release's strategy manifest declares no trading_scope",
            "republish the release with a publisher that records it",
        )
    _fields(scope, "the release's trading_scope", {"connector", "pairs", "leverage"})
    connector, pairs, leverage = scope["connector"], scope["pairs"], scope["leverage"]
    if not isinstance(connector, str) or not connector:
        raise SpecError("the release's trading_scope.connector is empty")
    if not isinstance(pairs, list) or not pairs:
        raise SpecError("the release's trading_scope.pairs is empty")
    if any(not isinstance(p, str) or not 1 <= len(p) <= 128 for p in pairs):
        raise SpecError(
            "the release's trading_scope.pairs holds a name that is not 1–128 characters"
        )
    if len(set(pairs)) != len(pairs):
        raise SpecError("the release's trading_scope.pairs names a pair twice")
    _integer(leverage, "the release's trading_scope.leverage", 1)
    return {"connector": connector, "pairs": list(pairs), "leverage": leverage}


def check_config_agrees(config: Mapping, scope: Mapping, config_name: str = "config.yaml") -> None:
    """config.yaml must trade what the release was validated for; the release decides."""

    trading = config.get("trading") if isinstance(config, Mapping) else None
    trading = trading if isinstance(trading, Mapping) else {}
    written = {key: _value(trading.get(key)) for key in ("connector", "pairs", "leverage")}
    differs = []
    if written["connector"] != scope["connector"]:
        differs.append(f"connector {written['connector']!r} (release: {scope['connector']!r})")
    pairs = written["pairs"]
    if not isinstance(pairs, list) or sorted(map(str, pairs)) != sorted(scope["pairs"]):
        differs.append(f"pairs {pairs!r} (release: {scope['pairs']!r})")
    if written["leverage"] != scope["leverage"] or isinstance(written["leverage"], bool):
        differs.append(f"leverage {written['leverage']!r} (release: {scope['leverage']!r})")
    if differs:
        raise SpecError(
            f"{config_name} trades something other than the release: {'; '.join(differs)}",
            "deploy the release as it was published, or change config.yaml and make release again",
        )


# -- the spec ------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseFacts:
    """What the summary says about the release; none of it is sent except the id."""

    coordinate: str
    version: str
    manifest_digest: str
    producer_repository: str
    producer_commit: str
    trading_scope: dict
    release_id: str | None = None


@dataclass
class DeploymentPlan:
    """The request body, and what is shown about it. Both read `body`; nothing else."""

    body: dict
    release: ReleaseFacts
    warnings: list[str] = field(default_factory=list)

    def request(self, *, totp_code: str, idempotency_key: str) -> dict:
        """The exact body POST /api/v1/deployment-specs is sent: `body` plus the two."""

        sent = copy.deepcopy(self.body)
        sent["idempotency_key"] = _uuid(idempotency_key, "the idempotency key")
        sent["totp_code"] = totp_code
        return sent

    @property
    def request_digest(self) -> str:
        """The digest of what will trade: the body without its idempotency key or code."""

        return request_digest(self.body)

    @property
    def sendable(self) -> bool:
        return self.body["artifact_source"]["snapshot"]["strategy_release_id"] is not None


def request_digest(body: Mapping) -> str:
    content = {k: v for k, v in body.items() if k not in ("idempotency_key", "totp_code")}
    return digest(content, "the request")


def build_plan(
    *,
    mode: str,
    runner_id: str,
    product_id: str,
    settings: Mapping,
    config: Mapping,
    release: ReleaseFacts,
) -> DeploymentPlan:
    """The DeploymentSpec body for one mode, runner and product; SpecError if it cannot be."""

    section = _mode_settings(settings, mode)
    scope = check_trading_scope(release.trading_scope)
    check_config_agrees(config, scope)
    runner = _uuid(runner_id, "RUNNER, the target runner id")
    product = _uuid(product_id, "PRODUCT, the strategy product id")
    release_id = (
        _uuid(release.release_id, "the release id") if release.release_id is not None else None
    )

    risk = _mapping(settings["risk_policy"], "risk_policy")
    _fields(risk, "risk_policy", {"version", "policy"}, {"policy_id"})
    policy = check_risk_policy(risk["policy"])
    policy_digest = digest(policy, "risk_policy.policy")
    policy_id = (
        _uuid(risk["policy_id"], "risk_policy.policy_id")
        if risk.get("policy_id") is not None
        else str(uuid.uuid5(POLICY_ID_NAMESPACE, policy_digest))
    )
    venue_policy = check_venue_policy(settings["venue_source_policy"])
    requirements = check_contract_requirements(
        settings["runner_contract_requirements"], venue_policy
    )
    scheduling = check_scheduling(settings["scheduling_policy"])

    strategy_config = _mapping(settings["strategy_config"] or {}, "strategy_config")
    if "trading" in strategy_config:
        raise SpecError(
            "strategy_config may not carry a trading section; the release's trading scope "
            "is the only source for it"
        )
    nautilus_config = _mapping(settings["nautilus_config"] or {}, "nautilus_config")
    log_level = settings["log_level"]
    if not isinstance(log_level, str) or not log_level:
        raise SpecError("log_level is a level name such as INFO")

    execution: dict = {
        "schema_version": 1,
        "engine": "nautilus",
        "connector": scope["connector"],
        "pairs": scope["pairs"],
        "leverage": scope["leverage"],
        "strategy_config": strategy_config,
        "log_level": log_level,
        "sandbox": None,
        "nautilus_config": nautilus_config,
    }
    warnings = []
    if mode == "sandbox":
        balances = section["starting_balances"]
        if (
            not isinstance(balances, list)
            or not balances
            or not all(isinstance(b, str) and b.strip() for b in balances)
        ):
            raise SpecError(
                "sandbox.starting_balances is a list such as ['10000 USDT'], and not empty"
            )
        execution["sandbox"] = {"starting_balances": balances}
    if section.get("shutdown_policy") is not None:
        execution["shutdown_policy"] = check_shutdown(
            section["shutdown_policy"], f"{mode}.shutdown_policy"
        )
    elif mode == "testnet":
        warnings.append(
            "testnet has no shutdown_policy: a stopped instance keeps its open positions; "
            "add shutdown_policy with position_policy: flatten to close them on stop"
        )

    scope_setting = _mapping(section["credential_scope"], f"{mode}.credential_scope")
    _fields(scope_setting, f"{mode}.credential_scope", {"scope_id", "scope_digest"})
    fill = f"fill in {mode}.credential_scope in {DEPLOY_FILE}"
    scope_id = _uuid(scope_setting["scope_id"], f"{mode}.credential_scope.scope_id", fill)
    if not isinstance(scope_setting["scope_digest"], str) or not HEX64.fullmatch(
        scope_setting["scope_digest"]
    ):
        raise SpecError(f"{mode}.credential_scope.scope_digest is 64 lower-case hex", fill)
    binding = _uuid(
        section["engine_binding_id"],
        f"{mode}.engine_binding_id",
        f"fill in {mode}.engine_binding_id in {DEPLOY_FILE}",
    )

    reason = settings.get("reason") or (f"Deploy {release.coordinate} {release.version} to {mode}")
    if not isinstance(reason, str) or not 10 <= len(reason) <= 500:
        raise SpecError("reason is 10 to 500 characters")

    body = {
        "trading_mode": mode,
        "target_runner_id": runner,
        "artifact_source": {
            "kind": "strategy_release",
            "snapshot": {"strategy_release_id": release_id},
        },
        "strategy_product_id": product,
        "risk_policy": {
            "policy_id": policy_id,
            "version": _integer(risk["version"], "risk_policy.version", 1),
            "policy": policy,
            "policy_digest": policy_digest,
        },
        "runner_contract_requirements": requirements,
        "venue_source_policy": venue_policy,
        "source_policy_digest": digest(venue_policy, "venue_source_policy"),
        "scheduling_policy": scheduling,
        "scheduling_policy_digest": digest(scheduling, "scheduling_policy"),
        "execution_config": execution,
        "execution_channel": {"channel_type": CHANNEL_TYPES[mode], "engine_binding_id": binding},
        "credential_scope": {"scope_id": scope_id, "scope_digest": scope_setting["scope_digest"]},
        "reason": reason,
    }
    canonical_json(body, "the deployment spec")
    return DeploymentPlan(body=body, release=release, warnings=warnings)


# -- what is shown ------------------------------------------------------------------------


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "), sort_keys=True)


def _limits(policy: Mapping) -> list[tuple[str, str]]:
    exposure = policy["max_total_exposure"]
    loss = policy["max_daily_loss"]
    daily = (
        f"{loss['value']['amount']} {loss['value']['currency']}"
        if loss["kind"] == "absolute"
        else f"{loss['limit']} of the day's opening value"
    )
    day = policy["trading_day"]
    return [
        ("max total exposure", f"{exposure['amount']} {exposure['currency']}"),
        ("max total drawdown", str(policy["max_total_drawdown_ratio"])),
        ("max daily loss", daily),
        ("max notional leverage", str(policy["max_notional_leverage"])),
        (
            "max single strategy allocation",
            "none"
            if policy["max_single_strategy_allocation_ratio"] is None
            else str(policy["max_single_strategy_allocation_ratio"]),
        ),
        ("cooldown", f"{policy['cooldown_seconds']} s"),
        ("trading day", f"starts {day['boundary_local_time']} {day['time_zone']}"),
    ]


def summary(plan: DeploymentPlan) -> list[tuple[str, str]]:
    """Every row shown before the code that confirms the spec, read from `plan.body`."""

    body, release = plan.body, plan.release
    execution = body["execution_config"]
    risk = body["risk_policy"]
    release_id = body["artifact_source"]["snapshot"]["strategy_release_id"]
    rows = [
        ("release", f"{release.coordinate} {release.version}"),
        ("release manifest", release.manifest_digest),
        ("release id", release_id or "not chosen yet: drafting the release in ARX chooses it"),
        ("published from", f"{release.producer_repository} @ {release.producer_commit}"),
        ("mode", body["trading_mode"]),
        ("runner", body["target_runner_id"]),
        ("product", body["strategy_product_id"]),
        ("connector", execution["connector"]),
        ("pairs", ", ".join(execution["pairs"])),
        ("leverage", str(execution["leverage"])),
        ("venue source policy", _json(body["venue_source_policy"])),
        ("credential scope", body["credential_scope"]["scope_id"]),
        ("strategy_config", _json(execution["strategy_config"])),
    ]
    if execution.get("sandbox") is not None:
        rows.append(("starting balances", ", ".join(execution["sandbox"]["starting_balances"])))
    shutdown = execution.get("shutdown_policy")
    if body["trading_mode"] == "testnet" or shutdown is not None:
        rows.append(
            (
                "on stop",
                "keep open positions (no shutdown policy)"
                if shutdown is None
                else f"{shutdown['position_policy']} positions, "
                f"{shutdown['confirmation_timeout_secs']} s to confirm",
            )
        )
    rows += _limits(risk["policy"])
    rows += [
        ("risk policy", f"{risk['policy_id']} v{risk['version']}, digest {risk['policy_digest']}"),
        ("venue source digest", body["source_policy_digest"]),
        ("scheduling digest", body["scheduling_policy_digest"]),
        ("reason", body["reason"]),
        ("request digest", plan.request_digest),
    ]
    return rows


# -- the command ----------------------------------------------------------------------


def _config(directory: Path) -> dict:
    path = directory / "config.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as failure:
        raise SpecError(f"cannot read {path}: {failure}") from None
    return _mapping(data or {}, "config.yaml")


def release_facts(evidence, release_id: str | None) -> ReleaseFacts:
    receipt = evidence.receipt
    tag = str(receipt["discovery_tag"])
    return ReleaseFacts(
        coordinate=str(receipt["strategy_coordinate"]),
        version=tag.rsplit("-", 1)[-1],
        manifest_digest=str(receipt["release_manifest_digest"]),
        producer_repository=str(receipt["producer_repository"]),
        producer_commit=str(receipt["producer_commit"]),
        trading_scope=evidence.trading_scope,
        release_id=release_id,
    )


def preview(
    strategy: str,
    *,
    mode: str,
    runner_id: str,
    product_id: str,
    version: str | None = None,
    release_id: str | None = None,
    root: Path = ROOT,
    read_release=None,
) -> DeploymentPlan:
    """Build the plan for a released strategy; reads the release back, sends nothing."""

    directory = root / "strategies" / strategy
    if not (directory / "config.yaml").is_file():
        raise SpecError(f"there is no strategy at strategies/{strategy}", "make next")
    settings = load_deploy_file(directory / DEPLOY_FILE)
    _mode_settings(settings, mode)
    if read_release is None:
        from tools.arx.evidence import read_strategy_release as read_release
    evidence = read_release(strategy, version)
    return build_plan(
        mode=mode,
        runner_id=runner_id,
        product_id=product_id,
        settings=settings,
        config=_config(directory),
        release=release_facts(evidence, release_id),
    )


def show(plan: DeploymentPlan) -> None:
    ui.table("Deployment spec", summary(plan))
    for warning in plan.warnings:
        ui.warn(warning, tag="arx")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("strategy", help="<category>/<name>, the directory under strategies/")
    parser.add_argument("--mode", required=True)
    parser.add_argument("--runner", required=True)
    parser.add_argument("--product", required=True)
    parser.add_argument("--version")
    parser.add_argument("--release")
    args = parser.parse_args(argv)
    try:
        plan = preview(
            args.strategy.strip("/"),
            mode=args.mode,
            runner_id=args.runner,
            product_id=args.product,
            version=args.version or None,
            release_id=args.release or None,
        )
    except ArxError as failure:
        ui.error(str(failure), tag="arx")
        if failure.fix:
            ui.next_steps([(failure.fix, "")])
        return 1
    show(plan)
    ui.info(
        "nothing was sent: this is the spec a deployment would create, and the code "
        "entered for it confirms exactly these values",
        tag="arx",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
