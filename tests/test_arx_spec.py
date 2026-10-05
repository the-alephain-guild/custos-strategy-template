"""Building the DeploymentSpec a release is deployed with, and what is shown about it.

The digest vectors are fixed: the canonical bytes are written out by hand from
the rules in ARX's guide (keys sorted at every depth, arrays in order, compact
UTF-8, only integer numbers), and each SHA-256 was computed from those bytes
alone, outside this code. The risk policy is the one in the guide's example.
"""

from __future__ import annotations

import ast
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.arx import session as arx_session
from tools.arx import spec as arx_spec

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "5e3c1b7a-9d2f-4a6e-b180-3c5d7e9f1a2b"
PRODUCT = "4d1f6a0e-2b7c-4c1e-9a53-0e8f2d6b7c10"
RELEASE = "8b2e4c1a-5d3f-4e7a-9c06-1f2a3b4c5d6e"
BINDING = "7c8d9e0f-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
SCOPE_ID = "9e0f1a2b-3c4d-4e5f-8a6b-7c8d9e0f1a2b"
SCOPE_DIGEST = "ab" * 32

GUIDE_POLICY = {
    "schema_version": 1,
    "trading_day": {"time_zone": "Etc/UTC", "boundary_local_time": "00:00:00"},
    "max_total_exposure": {"currency": "USDT", "amount": "10000"},
    "max_total_drawdown_ratio": "0.2",
    "max_daily_loss": {
        "kind": "ratio",
        "limit": "0.05",
        "base": "start_of_day_cash_flow_adjusted_nav",
    },
    "max_notional_leverage": "2",
    "max_single_strategy_allocation_ratio": None,
    "cooldown_seconds": 300,
}

# (value, canonical bytes written by hand, sha256 of those bytes)
VECTORS = [
    ([], b"[]", "4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"),
    (
        {"timezone": "Etc/UTC", "schedule": {}},
        b'{"schedule":{},"timezone":"Etc/UTC"}',
        "fb2eb8e36ba302bc00dfff6a83feb0729a2265ae532dbc1d0ffc0992d28d2a08",
    ),
    (
        {"z": {"b": 2, "a": 1}, "a": [2, 1]},
        b'{"a":[2,1],"z":{"a":1,"b":2}}',
        "3f17d4713da9d7e54dbd2e163000d9880c451a1271888862bbb80bce597be563",
    ),
    (
        [{"venue": "BINANCE", "ledger_source": "venue_api"}],
        b'[{"ledger_source":"venue_api","venue":"BINANCE"}]',
        "20b77f1ec428e8eac5dc1a5d6038ea530d48aadde3347ee2bde1012cfd62c250",
    ),
    (
        {"é": "ü"},
        '{"é":"ü"}'.encode(),
        "7ab44b65251d851ae53c921c1aec83b09b135bcf2c26bcfa8cdb3568229d52b8",
    ),
    (
        GUIDE_POLICY,
        b'{"cooldown_seconds":300,"max_daily_loss":{"base":"start_of_day_cash_flow_adjusted_nav",'
        b'"kind":"ratio","limit":"0.05"},"max_notional_leverage":"2",'
        b'"max_single_strategy_allocation_ratio":null,"max_total_drawdown_ratio":"0.2",'
        b'"max_total_exposure":{"amount":"10000","currency":"USDT"},"schema_version":1,'
        b'"trading_day":{"boundary_local_time":"00:00:00","time_zone":"Etc/UTC"}}',
        "82e5de02fc9ee3a7799a09cb16bb05c24e98c17df0623df5127e4932018243bb",
    ),
]

SCOPE = {"connector": "binance_perpetual", "pairs": ["BTC-USDT", "ETH-USDT"], "leverage": 2}

# Every runner contract ARX requires of a deployment spec, each report at v1.
CONTRACTS = {
    "risk": {
        "schema_version": 1,
        "equity_snapshot": "v1",
        "position_snapshot": "v1",
        "heartbeat": "v1",
    },
    "settlement": {
        "schema_version": 1,
        "fill": "v1",
        "position_closed": "v1",
        "fee": "v1",
        "period_closed": "v1",
    },
    "reconciliation": {
        "schema_version": 1,
        "execution_fill": "v1",
        "venue_ledger_snapshot_manifest": "v1",
        "venue_ledger_snapshot_chunk": "v1",
        "reconciliation_period_closed": "v1",
        "valuation_checkpoint": "v1",
    },
    "health": {"schema_version": 1, "heartbeat": "v1"},
    "deployment_lifecycle": {"schema_version": 1, "deployment_lifecycle": "v1"},
}
VENUES = [{"venue": "BINANCE", "ledger_source": "venue_api"}]


def arx_refuses(body: dict) -> str | None:
    """What ARX refuses a spec's contracts and venue sources for, or None if it takes them.

    Written from ARX's rule as it stands, not from this tool: all five contracts
    present, each report exactly v1 (valuation_checkpoint may be left out), and
    at least one venue source, each named, at most 128 characters, read from
    venue_api or drop_copy, and named once.
    """

    contracts = body["runner_contract_requirements"]
    for section in ("risk", "settlement", "reconciliation", "health", "deployment_lifecycle"):
        if section not in contracts:
            return f"{section} runner contract is required"
    exact = {
        section: {k: v for k, v in CONTRACTS[section].items() if k != "valuation_checkpoint"}
        for section in CONTRACTS
    }
    for section, wanted in exact.items():
        given = contracts[section]
        if any(given.get(k) != v for k, v in wanted.items()):
            return "runner contracts must be exact v1"
        if given.get("valuation_checkpoint", "v1") != "v1":
            return "runner contracts must be exact v1"
    sources = body["venue_source_policy"]
    names = [source["venue"] for source in sources]
    if (
        not sources
        or any(not name.strip() or len(name) > 128 for name in names)
        or len(set(names)) != len(names)
        or any(s["ledger_source"] not in ("venue_api", "drop_copy") for s in sources)
    ):
        return "ordered independent venue sources are required"
    return None


def _settings(**changes) -> dict:
    settings = {
        "log_level": "INFO",
        "strategy_config": {"parameters": {"fast_period": {"value": 12}}},
        "nautilus_config": {},
        "risk_policy": {"version": 1, "policy": copy.deepcopy(GUIDE_POLICY)},
        "scheduling_policy": {"timezone": "Etc/UTC", "schedule": {}},
        "venue_source_policy": copy.deepcopy(VENUES),
        "runner_contract_requirements": copy.deepcopy(CONTRACTS),
        "sandbox": {
            "engine_binding_id": BINDING,
            "credential_scope": {"scope_id": SCOPE_ID, "scope_digest": SCOPE_DIGEST},
            "starting_balances": ["10000 USDT"],
        },
        "testnet": {
            "engine_binding_id": BINDING,
            "credential_scope": {"scope_id": SCOPE_ID, "scope_digest": SCOPE_DIGEST},
            "shutdown_policy": {
                "schema_version": 1,
                "position_policy": "flatten",
                "confirmation_timeout_secs": 30,
            },
        },
    }
    settings.update(changes)
    return settings


def _config(connector="binance_perpetual", pairs=("ETH-USDT", "BTC-USDT"), leverage=2) -> dict:
    return {
        "trading": {
            "connector": {"value": connector},
            "pairs": {"value": list(pairs)},
            "leverage": {"value": leverage},
        }
    }


def _release(scope=None, release_id: str | None = RELEASE) -> arx_spec.ReleaseFacts:
    return arx_spec.ReleaseFacts(
        coordinate="trend/sma_cross",
        version="0.2.0",
        manifest_digest="sha256:" + "cd" * 32,
        producer_repository="example/strategies",
        producer_commit="0123456789abcdef0123456789abcdef01234567",
        trading_scope=copy.deepcopy(SCOPE if scope is None else scope),
        release_id=release_id,
    )


def _plan(mode="sandbox", settings=None, config=None, release=None) -> arx_spec.DeploymentPlan:
    return arx_spec.build_plan(
        mode=mode,
        runner_id=RUNNER,
        product_id=PRODUCT,
        settings=_settings() if settings is None else settings,
        config=_config() if config is None else config,
        release=_release() if release is None else release,
    )


def _rows(plan) -> dict[str, str]:
    return dict(arx_spec.summary(plan))


# -- digests ------------------------------------------------------------------


@pytest.mark.parametrize(("value", "canonical", "expected"), VECTORS)
def test_canonical_bytes_and_digests_match_the_fixed_vectors(value, canonical, expected) -> None:
    assert arx_spec.canonical_json(value) == canonical
    assert arx_spec.digest(value) == expected


def test_the_three_policy_digests_are_of_the_three_policies() -> None:
    body = _plan().body

    assert body["venue_source_policy"] == VECTORS[3][0]
    assert body["risk_policy"]["policy_digest"] == VECTORS[5][2]
    assert body["source_policy_digest"] == VECTORS[3][2]
    assert body["scheduling_policy_digest"] == VECTORS[1][2]


def test_a_float_anywhere_is_refused_before_hashing() -> None:
    with pytest.raises(arx_spec.SpecError, match="decimals are written as strings"):
        arx_spec.canonical_json({"a": [1, {"b": 0.25}]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_total_drawdown_ratio", 0.2),
        ("max_notional_leverage", 2.5),
        ("max_notional_leverage", 2),
        ("max_single_strategy_allocation_ratio", 0.5),
    ],
)
def test_a_limit_not_written_as_a_decimal_string_is_refused(field, value) -> None:
    settings = _settings()
    settings["risk_policy"]["policy"][field] = value

    with pytest.raises(arx_spec.SpecError, match="decimal"):
        _plan(settings=settings)


def test_an_amount_written_as_a_number_is_refused() -> None:
    settings = _settings()
    settings["risk_policy"]["policy"]["max_total_exposure"]["amount"] = 10000

    with pytest.raises(arx_spec.SpecError, match="max_total_exposure.amount"):
        _plan(settings=settings)


def test_a_float_in_the_schedule_or_strategy_config_is_refused() -> None:
    with pytest.raises(arx_spec.SpecError, match="decimals are written as strings"):
        _plan(settings=_settings(scheduling_policy={"timezone": "Etc/UTC", "schedule": {"x": 1.5}}))
    with pytest.raises(arx_spec.SpecError, match="decimals are written as strings"):
        _plan(settings=_settings(strategy_config={"threshold": 0.5}))


def test_every_decimal_in_the_body_is_a_string() -> None:
    def numbers(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from numbers(item)
        elif isinstance(value, list):
            for item in value:
                yield from numbers(item)
        elif isinstance(value, float):
            yield value

    body = _plan().body
    assert list(numbers(body)) == []
    policy = body["risk_policy"]["policy"]
    assert policy["max_total_drawdown_ratio"] == "0.2"
    assert policy["max_total_exposure"]["amount"] == "10000"


# -- trading scope ------------------------------------------------------------


def test_connector_pairs_and_leverage_come_from_the_release() -> None:
    execution = _plan().body["execution_config"]

    assert (execution["connector"], execution["pairs"], execution["leverage"]) == (
        "binance_perpetual",
        ["BTC-USDT", "ETH-USDT"],
        2,
    )


@pytest.mark.parametrize(
    ("config", "field"),
    [
        (_config(connector="okx_perpetual"), "connector"),
        (_config(pairs=("BTC-USDT",)), "pairs"),
        (_config(pairs=("BTC-USDT", "ETH-USDT", "SOL-USDT")), "pairs"),
        (_config(leverage=3), "leverage"),
        ({"trading": {}}, "connector"),
    ],
)
def test_a_config_that_trades_something_else_is_refused_here(config, field) -> None:
    with pytest.raises(arx_spec.SpecError, match=f"other than the release: .*{field}"):
        _plan(config=config)


def test_a_release_without_a_trading_scope_is_refused() -> None:
    release = _release()
    unscoped = arx_spec.ReleaseFacts(**{**release.__dict__, "trading_scope": None})
    with pytest.raises(arx_spec.SpecError, match="no trading_scope"):
        _plan(release=unscoped)


def test_a_release_scope_with_repeated_pairs_is_refused() -> None:
    scope = {**SCOPE, "pairs": ["BTC-USDT", "BTC-USDT"]}
    with pytest.raises(arx_spec.SpecError, match="twice"):
        _plan(release=_release(scope=scope))


def test_strategy_config_may_not_carry_a_trading_section() -> None:
    with pytest.raises(arx_spec.SpecError, match="trading section"):
        _plan(settings=_settings(strategy_config={"trading": {"leverage": {"value": 5}}}))


# -- modes --------------------------------------------------------------------


def test_testnet_without_a_shutdown_policy_is_warned_about_not_refused() -> None:
    settings = _settings()
    del settings["testnet"]["shutdown_policy"]

    plan = _plan(mode="testnet", settings=settings)

    assert "shutdown_policy" not in plan.body["execution_config"]
    assert len(plan.warnings) == 1 and "keeps its open positions" in plan.warnings[0]
    assert _rows(plan)["on stop"] == "keep open positions (no shutdown policy)"


def test_testnet_with_a_shutdown_policy_sends_it_and_warns_of_nothing() -> None:
    plan = _plan(mode="testnet")

    assert plan.warnings == []
    assert plan.body["execution_config"]["shutdown_policy"]["position_policy"] == "flatten"
    assert plan.body["execution_config"]["sandbox"] is None
    assert plan.body["execution_channel"]["channel_type"] == "testnet_venue"


def test_sandbox_sends_its_starting_balances_on_the_sim_channel() -> None:
    body = _plan().body

    assert body["execution_config"]["sandbox"] == {"starting_balances": ["10000 USDT"]}
    assert body["execution_channel"] == {
        "channel_type": "sandbox_sim_engine",
        "engine_binding_id": BINDING,
    }


@pytest.mark.parametrize("mode", ["live", "paper"])
def test_only_sandbox_and_testnet_are_built(mode) -> None:
    with pytest.raises(arx_spec.SpecError, match="sandbox or testnet"):
        _plan(mode=mode)


def test_a_built_spec_has_every_contract_and_a_venue_as_arx_requires() -> None:
    plan = _plan()

    assert set(plan.body["runner_contract_requirements"]) == set(CONTRACTS)
    assert plan.body["venue_source_policy"] == VENUES
    assert arx_refuses(plan.body) is None


@pytest.mark.parametrize("section", sorted(CONTRACTS))
def test_a_missing_runner_contract_is_refused_with_the_lines_to_add(section) -> None:
    contracts = copy.deepcopy(CONTRACTS)
    del contracts[section]

    with pytest.raises(arx_spec.SpecError) as refused:
        _plan(settings=_settings(runner_contract_requirements=contracts))

    assert section in str(refused.value) and "ARX" in str(refused.value)
    added = yaml.safe_load(refused.value.fix.split("\n", 1)[1])
    assert added == {"runner_contract_requirements": {section: CONTRACTS[section]}}


@pytest.mark.parametrize(
    ("connector", "pairs", "venue"),
    [
        ("binance_perpetual", ["BTC-USDT"], "BINANCE"),
        ("okx", ["BTC-USDT"], "OKX"),
        ("sodex_perpetual", ["BTC-USD"], "SODEX_PERPS"),
    ],
)
def test_an_empty_venue_policy_is_refused_with_the_connectors_venue(
    connector, pairs, venue
) -> None:
    scope = {"connector": connector, "pairs": pairs, "leverage": 1}

    with pytest.raises(arx_spec.SpecError) as refused:
        _plan(
            settings=_settings(venue_source_policy=[]),
            config=_config(connector, pairs, 1),
            release=_release(scope),
        )

    assert "venue_source_policy" in str(refused.value) and "ARX" in str(refused.value)
    added = yaml.safe_load(refused.value.fix.split("\n", 1)[1])
    assert added == {"venue_source_policy": [{"venue": venue, "ledger_source": "venue_api"}]}


def _subsets(items):
    items = sorted(items)
    for mask in range(1 << len(items)):
        yield {item for bit, item in enumerate(items) if mask >> bit & 1}


@pytest.mark.parametrize("venues", [[], VENUES])
def test_the_local_rule_refuses_exactly_what_arx_refuses(venues) -> None:
    for kept in _subsets(CONTRACTS):
        settings = _settings(
            venue_source_policy=copy.deepcopy(venues),
            runner_contract_requirements={k: copy.deepcopy(CONTRACTS[k]) for k in kept},
        )
        body = {
            "runner_contract_requirements": settings["runner_contract_requirements"],
            "venue_source_policy": settings["venue_source_policy"],
        }
        try:
            _plan(settings=settings)
            refused_here = False
        except arx_spec.SpecError:
            refused_here = True
        assert refused_here == (arx_refuses(body) is not None), (sorted(kept), venues)


def test_the_valuation_checkpoint_may_be_left_out_as_arx_allows() -> None:
    contracts = copy.deepcopy(CONTRACTS)
    del contracts["reconciliation"]["valuation_checkpoint"]

    plan = _plan(settings=_settings(runner_contract_requirements=contracts))

    assert arx_refuses(plan.body) is None


# -- the body ---------------------------------------------------------------------


def test_the_body_has_exactly_the_documented_fields() -> None:
    plan = _plan()
    sent = plan.request(totp_code="123456", idempotency_key="0b6f4a2e-7c1d-4e3b-8a95-2d4c6e8f1a3b")

    assert set(sent) == {
        "idempotency_key",
        "trading_mode",
        "target_runner_id",
        "artifact_source",
        "strategy_product_id",
        "risk_policy",
        "runner_contract_requirements",
        "venue_source_policy",
        "source_policy_digest",
        "scheduling_policy",
        "scheduling_policy_digest",
        "execution_config",
        "execution_channel",
        "credential_scope",
        "reason",
        "totp_code",
    }
    assert sent["artifact_source"] == {
        "kind": "strategy_release",
        "snapshot": {"strategy_release_id": RELEASE},
    }
    assert sent["strategy_product_id"] == PRODUCT
    assert sent["target_runner_id"] == RUNNER


def test_the_product_is_the_callers_and_nothing_else_is_asked_of_arx() -> None:
    other = "11111111-2222-4333-8444-555555555555"
    plan = arx_spec.build_plan(
        mode="sandbox",
        runner_id=RUNNER,
        product_id=other,
        settings=_settings(),
        config=_config(),
        release=_release(),
    )
    assert plan.body["strategy_product_id"] == other


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_runner_and_product_must_be_real_ids(bad) -> None:
    with pytest.raises(arx_spec.SpecError, match="RUNNER"):
        arx_spec.build_plan(
            mode="sandbox",
            runner_id=bad,
            product_id=PRODUCT,
            settings=_settings(),
            config=_config(),
            release=_release(),
        )


def test_the_risk_policy_id_follows_from_its_content() -> None:
    first = _plan().body["risk_policy"]["policy_id"]
    settings = _settings()
    settings["risk_policy"]["policy"]["cooldown_seconds"] = 600

    assert _plan().body["risk_policy"]["policy_id"] == first
    assert _plan(settings=settings).body["risk_policy"]["policy_id"] != first


# -- what is shown is what is sent ------------------------------------------------


def test_the_summary_covers_every_value_that_decides_what_trades() -> None:
    rows = _rows(_plan())

    assert rows["release"] == "trend/sma_cross 0.2.0"
    assert rows["release manifest"] == "sha256:" + "cd" * 32
    assert rows["release id"] == RELEASE
    assert rows["published from"].startswith("example/strategies @ 0123456789")
    assert rows["mode"] == "sandbox"
    assert rows["runner"] == RUNNER
    assert rows["product"] == PRODUCT
    assert rows["connector"] == "binance_perpetual"
    assert rows["pairs"] == "BTC-USDT, ETH-USDT"
    assert rows["leverage"] == "2"
    assert json.loads(rows["venue source policy"]) == VENUES
    assert rows["credential scope"] == SCOPE_ID
    assert json.loads(rows["strategy_config"]) == {"parameters": {"fast_period": {"value": 12}}}
    assert rows["starting balances"] == "10000 USDT"
    assert rows["max total exposure"] == "10000 USDT"
    assert rows["max daily loss"] == "0.05 of the day's opening value"
    assert rows["risk policy"].endswith(VECTORS[5][2])
    assert rows["request digest"] == _plan().request_digest


def test_the_summary_is_read_from_the_body_that_is_sent() -> None:
    plan = _plan()
    plan.body["execution_config"]["leverage"] = 7
    plan.body["execution_config"]["pairs"] = ["SOL-USDT"]
    plan.body["target_runner_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    plan.body["risk_policy"]["policy"]["max_total_exposure"]["amount"] = "5"

    rows = _rows(plan)
    sent = plan.request(totp_code="123456", idempotency_key="0b6f4a2e-7c1d-4e3b-8a95-2d4c6e8f1a3b")

    assert rows["leverage"] == str(sent["execution_config"]["leverage"]) == "7"
    assert rows["pairs"] == ", ".join(sent["execution_config"]["pairs"]) == "SOL-USDT"
    assert rows["runner"] == sent["target_runner_id"]
    assert rows["max total exposure"] == "5 USDT"


def test_the_request_digest_shown_is_of_the_request_sent() -> None:
    plan = _plan()
    shown = _rows(plan)["request digest"]
    sent = plan.request(totp_code="123456", idempotency_key="0b6f4a2e-7c1d-4e3b-8a95-2d4c6e8f1a3b")

    assert shown == arx_spec.request_digest(sent)
    plan.body["reason"] = "A different reason entirely"
    assert _rows(plan)["request digest"] != shown


def test_a_plan_without_a_release_id_is_refused() -> None:
    with pytest.raises(arx_spec.SpecError, match="the release id is a UUID"):
        _plan(release=_release(release_id=None))
    assert _rows(_plan())["release id"] == RELEASE


def test_the_release_facts_carry_the_id_derived_from_the_receipt() -> None:
    facts = arx_spec.release_facts(_evidence(), "tenant_a")

    assert facts.release_id == arx_spec.release_id_for("tenant_a", "sha256:" + "cd" * 32)
    assert facts.release_id != arx_spec.release_facts(_evidence(), "tenant_b").release_id


def test_the_preview_takes_the_organisation_from_the_kept_session(tmp_path) -> None:
    store = arx_session.HostStore(tmp_path / "arx")
    with pytest.raises(arx_spec.SpecError, match="not signed in") as refused:
        arx_spec.session_tenant(store)
    assert "make arx-login" in refused.value.fix

    with store.transaction() as data:
        data["hosts"]["https://arx.example.com"] = {"tenant_id": "tenant_a"}
        data["hosts"]["https://other.example.com"] = {"tenant_id": "tenant_b"}
    with pytest.raises(arx_spec.SpecError, match="ARX_URL"):
        arx_spec.session_tenant(store)
    assert arx_spec.session_tenant(store, "https://other.example.com") == "tenant_b"


# -- deploy.yaml ----------------------------------------------------------------------


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "deploy.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize("missing", ["log_level", "risk_policy", "runner_contract_requirements"])
def test_a_deploy_file_missing_a_field_is_refused(tmp_path, missing) -> None:
    data = _settings()
    del data[missing]

    with pytest.raises(arx_spec.SpecError, match=f"missing {missing}"):
        arx_spec.load_deploy_file(_write(tmp_path, data))


def test_a_deploy_file_with_an_unknown_field_is_refused(tmp_path) -> None:
    with pytest.raises(arx_spec.SpecError, match="unknown leverage"):
        arx_spec.load_deploy_file(_write(tmp_path, _settings(leverage=3)))


def test_a_mode_section_missing_or_with_unknown_fields_is_refused() -> None:
    settings = _settings()
    del settings["sandbox"]["credential_scope"]
    with pytest.raises(arx_spec.SpecError, match="missing credential_scope"):
        _plan(settings=settings)
    settings = _settings()
    settings["testnet"]["pairs"] = ["BTC-USDT"]
    with pytest.raises(arx_spec.SpecError, match="unknown pairs"):
        _plan(mode="testnet", settings=settings)
    with pytest.raises(arx_spec.SpecError, match="no testnet: section"):
        _plan(mode="testnet", settings=_settings(testnet=None))


def test_a_risk_policy_with_an_unknown_limit_is_refused() -> None:
    settings = _settings()
    settings["risk_policy"]["policy"]["max_orders"] = 3
    with pytest.raises(arx_spec.SpecError, match="unknown max_orders"):
        _plan(settings=settings)


def test_an_unfilled_credential_scope_names_what_to_fill_in() -> None:
    settings = _settings()
    settings["sandbox"]["credential_scope"] = {"scope_id": None, "scope_digest": None}
    with pytest.raises(arx_spec.SpecError) as refused:
        _plan(settings=settings)
    assert "credential_scope.scope_id" in str(refused.value)
    assert "fill in sandbox.credential_scope" in refused.value.fix


def test_the_example_deploy_file_and_the_template_agree() -> None:
    example = (ROOT / "examples/trend/sma_cross/deploy.yaml").read_text()
    template = (ROOT / "templates/strategy/deploy.yaml.jinja").read_text()
    rendered = (
        template.replace("{{ settlement_currency | upper }}", "USDT")
        .replace("{{ settlement_currency }}", "USDT")
        .replace("{{ ledger_venue }}", "BINANCE")
    )
    assert example == rendered
    loaded = arx_spec.load_deploy_file(ROOT / "examples/trend/sma_cross/deploy.yaml")
    arx_spec.check_risk_policy(loaded["risk_policy"]["policy"])
    assert arx_spec.digest(loaded["risk_policy"]["policy"]) == VECTORS[5][2]


def _filled(path: Path) -> dict:
    """A deploy.yaml as written, with the ids only the runner's operator can give."""

    settings = arx_spec.load_deploy_file(path)
    for mode in arx_spec.MODES:
        settings[mode]["engine_binding_id"] = BINDING
        settings[mode]["credential_scope"] = {"scope_id": SCOPE_ID, "scope_digest": SCOPE_DIGEST}
    return settings


@pytest.mark.parametrize("mode", arx_spec.MODES)
def test_the_default_deploy_file_builds_a_spec_arx_accepts(mode) -> None:
    settings = _filled(ROOT / "examples/trend/sma_cross/deploy.yaml")

    plan = _plan(mode=mode, settings=settings)

    assert set(plan.body["runner_contract_requirements"]) == set(CONTRACTS)
    assert plan.body["venue_source_policy"] == VENUES
    assert arx_refuses(plan.body) is None


def _copier_ledger_venues() -> dict[str, str]:
    copier = yaml.safe_load((ROOT / "copier.yml").read_text())
    default = copier["ledger_venue"]["default"]
    assert copier["ledger_venue"]["when"] is False
    literal = default.removeprefix("{{ ").removesuffix("[connector] }}")
    return ast.literal_eval(literal)


def test_a_new_strategys_venue_follows_its_connector() -> None:
    copier = yaml.safe_load((ROOT / "copier.yml").read_text())
    connectors = set(copier["connector"]["choices"].values())
    venues = _copier_ledger_venues()
    template = (ROOT / "templates/strategy/deploy.yaml.jinja").read_text()

    assert set(venues) == connectors
    assert venues == {c: arx_spec.LEDGER_VENUES[c] for c in connectors}
    assert "- venue: {{ ledger_venue }}" in template


def test_the_venue_names_are_the_runners_own() -> None:
    from custos_toolkit_nautilus.adapter.utils import VENUE_MAP

    assert {c: VENUE_MAP[c] for c in arx_spec.LEDGER_VENUES} == arx_spec.LEDGER_VENUES


# -- the command ------------------------------------------------------------------------


def _strategy(tmp_path: Path, deploy: dict | None) -> Path:
    directory = tmp_path / "strategies" / "trend" / "sma_cross"
    directory.mkdir(parents=True)
    (directory / "config.yaml").write_text(yaml.safe_dump(_config()))
    if deploy is not None:
        (directory / "deploy.yaml").write_text(yaml.safe_dump(deploy))
    return tmp_path


def _evidence():
    return SimpleNamespace(
        receipt={
            "strategy_coordinate": "trend/sma_cross",
            "discovery_tag": "trend-sma_cross-0.2.0",
            "release_manifest_digest": "sha256:" + "cd" * 32,
            "producer_repository": "example/strategies",
            "producer_commit": "0123456789abcdef0123456789abcdef01234567",
        },
        trading_scope=copy.deepcopy(SCOPE),
    )


def test_preview_builds_from_the_files_and_the_release_it_reads(tmp_path) -> None:
    root = _strategy(tmp_path, _settings())
    asked = []

    plan = arx_spec.preview(
        "trend/sma_cross",
        mode="sandbox",
        runner_id=RUNNER,
        product_id=PRODUCT,
        version="0.2.0",
        tenant="tenant_a",
        root=root,
        read_release=lambda strategy, version: asked.append((strategy, version)) or _evidence(),
    )

    assert asked == [("trend/sma_cross", "0.2.0")]
    assert _rows(plan)["release"] == "trend/sma_cross 0.2.0"
    assert plan.body["execution_config"]["pairs"] == ["BTC-USDT", "ETH-USDT"]


def test_preview_refuses_before_reading_the_release_when_deploy_yaml_is_missing(tmp_path) -> None:
    root = _strategy(tmp_path, None)

    def unread(*_):
        raise AssertionError("the release was read")

    with pytest.raises(arx_spec.SpecError, match="deploy.yaml is missing"):
        arx_spec.preview(
            "trend/sma_cross",
            mode="sandbox",
            runner_id=RUNNER,
            product_id=PRODUCT,
            tenant="tenant_a",
            root=root,
            read_release=unread,
        )


# -- a deploy inputs file ---------------------------------------------------------------

OTHER_BINDING = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
OTHER_SCOPE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _unfilled(mode: str = "sandbox") -> dict:
    """deploy.yaml as a new strategy has it: the binding and the scope left empty."""

    settings = _settings()
    settings[mode]["engine_binding_id"] = None
    settings[mode]["credential_scope"] = {"scope_id": None, "scope_digest": None}
    return settings


def _inputs(tmp_path: Path, modes: dict | None = None, **top) -> Path:
    data = {
        "schema_version": 1,
        "modes": modes
        if modes is not None
        else {
            "sandbox": {
                "engine_binding_id": OTHER_BINDING,
                "credential_scope": {"scope_id": OTHER_SCOPE_ID, "scope_digest": "cd" * 32},
            }
        },
        **top,
    }
    path = tmp_path / "inputs" / "deploy-inputs.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _preview_with(root: Path, inputs: Path | None, mode: str = "sandbox"):
    return arx_spec.preview(
        "trend/sma_cross",
        mode=mode,
        runner_id=RUNNER,
        product_id=PRODUCT,
        version="0.2.0",
        tenant="tenant_a",
        root=root,
        read_release=lambda strategy, version: _evidence(),
        deploy_inputs=inputs,
    )


def test_a_deploy_inputs_file_fills_what_deploy_yaml_leaves_empty(tmp_path) -> None:
    import hashlib

    root = _strategy(tmp_path, _unfilled())
    inputs = _inputs(tmp_path)

    plan = _preview_with(root, inputs)

    assert plan.body["execution_channel"]["engine_binding_id"] == OTHER_BINDING
    assert plan.body["credential_scope"] == {"scope_id": OTHER_SCOPE_ID, "scope_digest": "cd" * 32}
    assert plan.inputs == {
        "path": str(inputs.resolve()),
        "sha256": hashlib.sha256(inputs.read_bytes()).hexdigest(),
    }
    assert str(inputs.resolve()) in _rows(plan)["deploy inputs"]
    # deploy.yaml itself is not written to.
    written = yaml.safe_load((root / "strategies/trend/sma_cross/deploy.yaml").read_text())
    assert written["sandbox"]["engine_binding_id"] is None


def test_without_a_deploy_inputs_file_nothing_is_recorded(tmp_path) -> None:
    plan = _preview_with(_strategy(tmp_path, _settings()), None)

    assert plan.inputs is None
    assert "deploy inputs" not in _rows(plan)


def test_a_value_in_deploy_yaml_that_the_file_contradicts_is_refused(tmp_path) -> None:
    root = _strategy(tmp_path, _settings())
    inputs = _inputs(tmp_path)

    with pytest.raises(arx_spec.SpecError, match="engine_binding_id") as refused:
        _preview_with(root, inputs)
    assert BINDING in str(refused.value) and OTHER_BINDING in str(refused.value)
    assert str(inputs.resolve()) in str(refused.value)
    assert "null" in refused.value.fix


def test_a_value_in_deploy_yaml_that_the_file_repeats_is_kept(tmp_path) -> None:
    root = _strategy(tmp_path, _settings())
    same = {
        "sandbox": {
            "engine_binding_id": BINDING,
            "credential_scope": {"scope_id": SCOPE_ID, "scope_digest": SCOPE_DIGEST},
        }
    }

    plan = _preview_with(root, _inputs(tmp_path, same))

    assert plan.body["execution_channel"]["engine_binding_id"] == BINDING


def test_a_deploy_inputs_file_without_the_mode_leaves_the_usual_refusal(tmp_path) -> None:
    root = _strategy(tmp_path, _unfilled("testnet"))
    inputs = _inputs(tmp_path)  # sandbox only

    with pytest.raises(arx_spec.SpecError, match="testnet.credential_scope") as refused:
        _preview_with(root, inputs, mode="testnet")
    assert "fill in testnet.credential_scope in deploy.yaml" in refused.value.fix


@pytest.mark.parametrize(
    ("data", "said"),
    [
        ({"schema_version": 2, "modes": {}}, "schema_version"),
        ({"schema_version": 1}, "modes"),
        ({"schema_version": 1, "modes": [], "extra": 1}, "extra"),
        ({"schema_version": 1, "modes": {"sandbox": {"leverage": 3}}}, "leverage"),
        (
            {"schema_version": 1, "modes": {"sandbox": {"credential_scope": {"scope_id": "x"}}}},
            "scope_digest",
        ),
    ],
)
def test_a_deploy_inputs_file_not_of_the_documented_shape_is_refused(tmp_path, data, said) -> None:
    path = tmp_path / "deploy-inputs.json"
    path.write_text(json.dumps(data))

    with pytest.raises(arx_spec.SpecError, match=said):
        _preview_with(_strategy(tmp_path, _unfilled()), path)


def test_a_deploy_inputs_file_that_cannot_be_read_is_refused(tmp_path) -> None:
    missing = tmp_path / "nowhere.json"
    with pytest.raises(arx_spec.SpecError, match="nowhere.json"):
        _preview_with(_strategy(tmp_path, _unfilled()), missing)
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(arx_spec.SpecError, match="broken.json"):
        _preview_with(_strategy(tmp_path / "b", _unfilled()), broken)
