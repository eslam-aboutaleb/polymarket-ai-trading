"""Issue #20: exact LP joint model, typed request layer, API, MCP tool, scoring."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import sys
import types
from datetime import UTC, datetime, timedelta
from fractions import Fraction as F
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.routes.auth.auth import get_current_user_from_token
from app.config import get_settings
from app.main import app as fastapi_app
from app.services.intelligence import event_logic as el
from app.services.intelligence import event_logic_skill as skill

RNG = random.Random(2020)  # noqa: S311
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).parents[2]


def rp():
    return F(RNG.randint(0, 100), 100)


def v(name):
    return el.Var(name)


def model(marginals, relations=()):
    return el.JointModel({k: F(str(x)) for k, x in marginals.items()}, list(relations))


def rel(kind, *events, exhaustive=False):
    return el.Relation(kind, tuple(events), exhaustive, "evidence-hash", True)


# ---- exact LP ----------------------------------------------------------------------
def test_lp_solver_exact_small_programs():
    # min x1 s.t. x1 + x2 = 1, x1 - x2 = -1 -> x1=0
    assert el.solve_lp([[1, 1], [1, -1]], [1, -1], [1, 0]) == 0
    # infeasible: x1 = 1 and x1 = 2
    assert el.solve_lp([[1], [1]], [1, 2], [0]) is None
    # redundant equality row
    assert el.solve_lp([[1, 1], [2, 2]], [1, 2], [1, 0]) == 0


def test_lp_bounds_equal_frechet_for_unconstrained_pairs():
    for _ in range(60):
        pa, pb = rp(), rp()
        m = model({"a": pa, "b": pb})
        assert m.bounds(el.And((v("a"), v("b")))) == el.frechet_and(pa, pb)
        assert m.bounds(el.Or((v("a"), v("b")))) == el.frechet_or(pa, pb)


def test_lp_three_event_bounds_match_known_closed_form():
    for _ in range(40):
        ps = [rp() for _ in range(3)]
        m = model(dict(zip("abc", ps, strict=True)))
        lo, hi = m.bounds(el.And((v("a"), v("b"), v("c"))))
        assert lo == max(F(0), sum(ps) - 2) and hi == min(ps)


def test_lp_de_morgan_and_complement_exact():
    for _ in range(30):
        pa, pb = rp(), rp()
        m = model({"a": pa, "b": pb})
        a, b = v("a"), v("b")
        assert m.bounds(el.Not(el.And((a, b)))) == m.bounds(el.Or((el.Not(a), el.Not(b))))
        assert m.bounds(el.Not(el.Or((a, b)))) == m.bounds(el.And((el.Not(a), el.Not(b))))
        assert m.bounds(el.Not(a)) == (1 - pa, 1 - pa)


def test_lp_bounds_hold_for_every_random_true_joint():
    """Any genuine joint distribution must fall inside the reported range."""
    for _ in range(80):
        w = [RNG.randint(0, 9) + 1 for _ in range(8)]
        s = sum(w)
        joint = {}
        for i, wt in enumerate(w):
            joint[tuple(bool(i >> k & 1) for k in range(3))] = F(wt, s)
        marg = {n: sum(p for k, p in joint.items() if k[i]) for i, n in enumerate("abc")}
        m = model(marg)
        expr = el.Or((el.And((v("a"), el.Not(v("b")))), v("c")))
        truth = sum(
            p for k, p in joint.items() if el.evaluate(expr, dict(zip("abc", k, strict=True)))
        )
        lo, hi = m.bounds(expr)
        assert lo <= truth <= hi


def test_relations_constrain_ranges_exactly():
    # a implies b: P(a and b) == P(a); P(a or b) == P(b)
    m = model({"a": "0.3", "b": "0.5"}, [rel("implies", "a", "b")])
    assert m.bounds(el.And((v("a"), v("b")))) == (F(3, 10), F(3, 10))
    assert m.bounds(el.Or((v("a"), v("b")))) == (F(1, 2), F(1, 2))
    # exclusive: intersection exactly 0, union is the sum
    m = model({"a": "0.3", "b": "0.4"}, [rel("exclusive_group", "a", "b")])
    assert m.bounds(el.And((v("a"), v("b")))) == (0, 0)
    assert m.bounds(el.Or((v("a"), v("b")))) == (F(7, 10), F(7, 10))
    # nested thresholds pin the whole chain
    m = model(
        {"ge90": "0.6", "ge100": "0.3", "ge110": "0.1"},
        [rel("thresholds", "ge90", "ge100", "ge110")],
    )
    assert m.bounds(el.And((v("ge110"), v("ge90")))) == (F(1, 10), F(1, 10))


def test_infeasible_models_and_minimal_conflict():
    m = model({"a": "0.7", "b": "0.5"}, [rel("implies", "a", "b")])
    assert not m.feasible()
    assert m.bounds(v("a")) is None
    assert set(m.minimal_conflict()) == {"marginal:a", "marginal:b", "implies:a|b"}
    # unrelated extra constraints are not blamed
    m2 = model(
        {"a": "0.7", "b": "0.5", "c": "0.2"},
        [rel("implies", "a", "b"), rel("implies", "c", "b")],
    )
    assert set(m2.minimal_conflict()) == {"marginal:a", "marginal:b", "implies:a|b"}


def test_too_many_events_rejected():
    with pytest.raises(ValueError):
        el.JointModel({f"e{i}": F(1, 2) for i in range(el.MAX_EVENTS + 1)}, [])


# ---- Bayes -------------------------------------------------------------------------
def test_bayes_requires_provenance_calibration_and_sample():
    ok = el.bayes_update(
        "0.5",
        ("0.8", "0.9"),
        ("0.1", "0.2"),
        provenance="observed",
        sample_size=50,
        calibrated=True,
    )
    assert ok["status"] == "ok"
    lo, hi = ok["posterior"]
    assert lo == F(1, 2) * F(8, 10) / (F(1, 2) * F(8, 10) + F(1, 2) * F(2, 10))
    assert hi == F(1, 2) * F(9, 10) / (F(1, 2) * F(9, 10) + F(1, 2) * F(1, 10))
    for kwargs, reason in [
        ({"provenance": "forecast"}, "LIKELIHOOD_PROVENANCE_UNDEFINED"),
        ({"calibrated": False}, "LIKELIHOOD_NOT_CALIBRATED"),
        ({"sample_size": 5}, "LIKELIHOOD_SAMPLE_TOO_SMALL"),
        ({"sample_size": True}, "LIKELIHOOD_SAMPLE_TOO_SMALL"),
    ]:
        base = {"provenance": "observed", "sample_size": 50, "calibrated": True} | kwargs
        r = el.bayes_update("0.5", ("0.8", "0.9"), ("0.1", "0.2"), **base)
        assert r["status"] == "abstain" and reason in r["reasons"] and r["posterior"] is None
    zero = el.bayes_update(
        "0.5", ("0", "0"), ("0", "0"), provenance="observed", sample_size=50, calibrated=True
    )
    assert zero["reasons"] == ["EVIDENCE_PROBABILITY_ZERO"]


# ---- typed request layer ---------------------------------------------------------------
def ev(name, p, source="observed", age=60, **kw):
    return {
        "name": name,
        "p": p,
        "source": source,
        "as_of": (NOW - timedelta(seconds=age)).isoformat(),
        **kw,
    }


def relation(kind, events, **kw):
    return {"type": kind, "events": events, "approved": True, "evidence_hash": "sha256:abc", **kw}


def run(payload):
    payload = {"decision_time": NOW.isoformat(), **payload}
    return skill.analyze(skill.EventLogicRequest.model_validate(payload))


def codes(result):
    return {c["code"] for c in result["conflicts"]}


def test_coherent_request_returns_ranges_graph_and_assumptions():
    r = run(
        {
            "events": [
                ev("btc_ge_90k", "0.6", rule_hash="h1", condition_id="0x1", source_id="gamma"),
                ev("btc_ge_100k", "0.3", rule_hash="h2"),
                ev("eth_up", "0.5", source="forecast"),
            ],
            "relations": [relation("thresholds", ["btc_ge_90k", "btc_ge_100k"])],
            "joint": [["btc_ge_90k", "btc_ge_100k"], ["btc_ge_100k", "eth_up"]],
            "conditional": [{"event": "btc_ge_100k", "given": "btc_ge_90k"}],
            "expressions": [
                {
                    "name": "either",
                    "expr": {"or": [{"var": "btc_ge_100k"}, {"var": "eth_up"}]},
                }
            ],
        }
    )
    assert r["status"] == "ok" and r["execution_eligible"] is False
    j0, j1 = r["joint"]
    assert (j0["lower"], j0["upper"], j0["dependence"]) == ("0.3", "0.3", "implied_by_constraints")
    # unrelated pair: Frechet range, not the 0.15 independence product
    assert (j1["lower"], j1["upper"], j1["dependence"]) == ("0", "0.3", "unknown")
    assert r["conditional"][0]["lower"] == "0.5"
    assert r["expressions"][0]["lower"] == "0.5" and r["expressions"][0]["upper"] == "0.8"
    nodes = {n["name"]: n for n in r["evidence_graph"]["nodes"]}
    assert nodes["btc_ge_90k"]["source_id"] == "gamma" and nodes["eth_up"]["source"] == "forecast"
    assert all(n["as_of"] for n in nodes.values())
    assert r["evidence_graph"]["edges"][0]["evidence_hash"] == "sha256:abc"
    assert any("no independence" in a for a in r["assumptions"])


def test_infeasible_request_reports_conflict_and_never_repairs():
    r = run(
        {
            "events": [ev("yes", "0.6"), ev("no", "0.5")],
            "relations": [relation("complement", ["yes", "no"])],
        }
    )
    assert r["status"] == "abstain" and r["reason"] == "INFEASIBLE_CONSTRAINTS"
    assert "COMPLEMENT_SUM_NOT_ONE" in codes(r)
    assert set(r["minimal_conflicting_constraints"]) == {
        "marginal:yes",
        "marginal:no",
        "complement:yes|no",
    }
    assert "joint" not in r and r["normalization"].startswith("none")


def test_pairwise_check_misses_but_lp_catches_global_infeasibility():
    # complement(a,b) and complement(b,c) force c == a; exclusive(a,c) then forces both
    # false, contradicting p(a) = 0.5. Every pairwise sum check passes on its own.
    payload = {
        "events": [ev("a", "0.5"), ev("b", "0.5"), ev("c", "0.5")],
        "relations": [
            relation("complement", ["a", "b"]),
            relation("complement", ["b", "c"]),
            relation("exclusive_group", ["a", "c"]),
        ],
    }
    req = skill.EventLogicRequest.model_validate({"decision_time": NOW.isoformat(), **payload})
    graph = el.EventConstraintGraph()
    for e in req.events:
        graph.add_marginal(e.name, e.p)
    graph.complement_pair("a", "b")
    graph.complement_pair("b", "c")
    graph.exclusive_group(["a", "c"])
    assert graph.check()["status"] == "ok"  # pairwise logic alone is fooled
    r = run(payload)
    assert r["status"] == "abstain" and r["reason"] == "INFEASIBLE_CONSTRAINTS"
    conflict = set(r["minimal_conflicting_constraints"])
    # Any single marginal is enough to pin the contradiction, so check shape not identity.
    assert {"complement:a|b", "complement:b|c", "exclusive_group:a|c"} <= conflict
    assert len([c for c in conflict if c.startswith("marginal:")]) == 1


def test_zero_denominator_conditional_abstains_but_request_ok():
    r = run(
        {
            "events": [ev("a", "0.4"), ev("b", "0")],
            "conditional": [{"event": "a", "given": "b"}],
        }
    )
    assert r["status"] == "ok"
    assert r["conditional"][0]["reason"] == "CONDITIONING_EVENT_ZERO_PROBABILITY"
    assert "lower" not in r["conditional"][0]


def test_temporal_leakage_and_stale_inputs_rejected():
    r = run({"events": [ev("a", "0.4", age=-10), ev("b", "0.5", age=7200)]})
    assert codes(r) == {"TEMPORAL_LEAKAGE", "STALE_INPUT"}
    fresh = run({"events": [ev("b", "0.5", age=7200)], "max_age_seconds": 86400})
    assert fresh["status"] == "ok"


def test_naive_timestamps_rejected_by_schema():
    with pytest.raises(ValueError, match="timezone"):
        run(
            {
                "events": [
                    {"name": "a", "p": "0.5", "source": "observed", "as_of": "2026-11-20T11:00:00"}
                ]
            }
        )


def test_unapproved_relation_is_never_used():
    r = run(
        {
            "events": [ev("a", "0.7"), ev("b", "0.5")],
            "relations": [
                {"type": "implies", "events": ["a", "b"], "approved": False, "evidence_hash": "x"}
            ],
        }
    )
    assert "UNAPPROVED_RELATION" in codes(r) and r["status"] == "abstain"
    missing = run(
        {
            "events": [ev("a", "0.7"), ev("b", "0.5")],
            "relations": [{"type": "implies", "events": ["a", "b"], "approved": True}],
        }
    )
    assert "UNAPPROVED_RELATION" in codes(missing)


def test_revised_rule_invalidates_relation_and_same_market_other_oracle_is_not_identical():
    revised = run(
        {
            "events": [ev("a", "0.3", rule_hash="new"), ev("b", "0.5", rule_hash="hb")],
            "relations": [relation("implies", ["a", "b"], rule_hashes={"a": "old", "b": "hb"})],
        }
    )
    assert "RULE_REVISED" in codes(revised)
    other_oracle = run(
        {
            "events": [
                ev("m_chainlink", "0.5", rule_hash="chainlink"),
                ev("m_coinbase", "0.5", rule_hash="coinbase"),
            ],
            "relations": [relation("identical", ["m_chainlink", "m_coinbase"])],
        }
    )
    assert "RULE_MISMATCH_FOR_IDENTICAL" in codes(other_oracle)
    same = run(
        {
            "events": [ev("x", "0.5", rule_hash="r"), ev("y", "0.5", rule_hash="r")],
            "relations": [relation("identical", ["x", "y"])],
            "joint": [["x", "y"]],
        }
    )
    assert same["joint"][0]["lower"] == "0.5" == same["joint"][0]["upper"]


def test_reversed_implication_non_exhaustive_group_and_unknown_events():
    # reversed implication (b implies a) with p(b) > p(a) is infeasible
    r = run(
        {
            "events": [ev("a", "0.3"), ev("b", "0.5")],
            "relations": [relation("implies", ["b", "a"])],
        }
    )
    assert r["status"] == "abstain"
    # non-exhaustive group: total < 1 is allowed and reported as an assumption
    g = run(
        {
            "events": [ev("x", "0.3"), ev("y", "0.2")],
            "relations": [relation("exclusive_group", ["x", "y"])],
        }
    )
    assert g["status"] == "ok"
    exhaustive = run(
        {
            "events": [ev("x", "0.3"), ev("y", "0.2")],
            "relations": [relation("exclusive_group", ["x", "y"], exhaustive=True)],
        }
    )
    assert exhaustive["status"] == "abstain"
    unknown = run(
        {
            "events": [ev("x", "0.3")],
            "joint": [["x", "ghost"]],
            "relations": [relation("implies", ["x", "ghost"])],
        }
    )
    assert {"QUERY_UNKNOWN_EVENT", "RELATION_UNKNOWN_EVENT"} <= codes(unknown)


@pytest.mark.parametrize(
    "expr",
    [
        {"var": "ghost"},
        {"xor": [{"var": "a"}]},
        {"__import__": "os"},
        {"not": {"var": "a"}, "extra": 1},
        {"call": [1]},
    ],
)
def test_expression_parser_rejects_unsupported_input(expr):
    r = run({"events": [ev("a", "0.5")], "expressions": [{"name": "q", "expr": expr}]})
    assert "EXPRESSION_REJECTED" in codes(r)


def test_expression_size_bound():
    deep = {"var": "a"}
    for _ in range(skill.MAX_EXPR_DEPTH + 2):
        deep = {"not": deep}
    r = run({"events": [ev("a", "0.5")], "expressions": [{"name": "q", "expr": deep}]})
    assert r["conflicts"][0]["detail"] == "EXPRESSION_TOO_LARGE"


def test_bayes_through_request_and_prior_label():
    base = {
        "events": [ev("h", "0.5", source="forecast")],
        "bayes": [
            {
                "hypothesis": "h",
                "like_given_h": ["0.8", "0.9"],
                "like_given_not_h": ["0.1", "0.2"],
                "likelihood_source": "historical",
                "sample_size": 100,
                "calibrated": True,
            }
        ],
    }
    r = run(base)
    assert r["bayes"][0]["status"] == "ok" and r["bayes"][0]["prior_source"] == "forecast"
    base["bayes"][0]["calibrated"] = False
    assert run(base)["bayes"][0]["status"] == "abstain"


def test_schema_rejects_extra_fields_and_out_of_range_probabilities():
    with pytest.raises(ValueError):
        skill.EventLogicRequest.model_validate(
            {"decision_time": NOW.isoformat(), "events": [ev("a", "1.2")]}
        )
    with pytest.raises(ValueError):
        skill.EventLogicRequest.model_validate(
            {"decision_time": NOW.isoformat(), "events": [ev("a", "0.5")], "order": "buy"}
        )
    with pytest.raises(ValueError):
        skill.EventLogicRequest.model_validate(
            {
                "decision_time": NOW.isoformat(),
                "events": [ev(f"e{i}", "0.5") for i in range(el.MAX_EVENTS + 1)],
            }
        )


def test_coherent_synthetic_distributions_never_flagged_as_arbitrage():
    for _ in range(25):
        # exact decimals only: use /100 weights summing to 100
        raw = [RNG.randint(1, 50) for _ in range(2)]
        raw.append(100 - sum(raw))
        r = run(
            {
                "events": [ev(n, f"{x / 100:.2f}") for n, x in zip("abc", raw, strict=True)],
                "relations": [relation("exclusive_group", ["a", "b", "c"], exhaustive=True)],
            }
        )
        assert r["status"] == "ok", r


# ---- scoring on observable joint states ---------------------------------------------------
def test_scoring_requires_observable_joint_and_reports_no_improvement():
    assert (
        skill.score_joint_forecasts([{"pa": "0.5", "pb": "0.5", "a": None, "b": True}])["reason"]
        == "NO_OBSERVABLE_JOINT_STATES"
    )
    # naive == coherent whenever independence is already inside the Frechet range
    recs = [
        {"cluster": str(i), "pa": "0.5", "pb": "0.5", "a": i % 2 == 0, "b": i % 3 == 0}
        for i in range(40)
    ]
    out = skill.score_joint_forecasts(recs)
    assert out["verdict"] == "NO_IMPROVEMENT" and out["brier_improvement"] == "0"
    assert out["claims_alpha"] is False


def test_scoring_improves_only_where_independence_is_impossible_and_clusters_dedupe():
    recs = [
        {
            "cluster": "same",
            "pa": "0.9",
            "pb": "0.9",
            "pab_independent": "0.5",
            "a": True,
            "b": True,
        }
    ] * 5
    out = skill.score_joint_forecasts(recs, min_events=1)
    assert out["effective_independent_events"] == 1
    assert out["verdict"] == "IMPROVED"  # 0.5 is below the Frechet floor 0.8
    assert skill.score_joint_forecasts(recs)["verdict"] == "INSUFFICIENT_SAMPLE"


# ---- API, MCP tool, schema ---------------------------------------------------------------
def test_api_flag_validation_and_read_only_response(monkeypatch):
    fastapi_app.dependency_overrides[get_current_user_from_token] = lambda: {"user_id": 1}
    client = TestClient(fastapi_app)
    get_settings.cache_clear()
    body = {
        "decision_time": NOW.isoformat(),
        "events": [ev("a", "0.3"), ev("b", "0.5")],
        "joint": [["a", "b"]],
    }
    try:
        assert client.get("/api/analysis/event-logic/schema").status_code == 503
        assert client.post("/api/analysis/event-logic/analyze", json=body).status_code == 503
        monkeypatch.setenv("EVENT_LOGIC_ENABLED", "true")
        get_settings.cache_clear()
        schema = client.get("/api/analysis/event-logic/schema").json()
        assert schema["read_only"] is True and schema["version"] == "event-logic-v1"
        ok = client.post("/api/analysis/event-logic/analyze", json=body)
        assert ok.status_code == 200
        assert ok.json()["joint"][0]["upper"] == "0.3" and ok.json()["execution_eligible"] is False
        bad = client.post("/api/analysis/event-logic/analyze", json={**body, "order": "x"})
        assert bad.status_code == 422
        # unauthenticated callers are refused
        fastapi_app.dependency_overrides.clear()
        assert client.post("/api/analysis/event-logic/analyze", json=body).status_code in {401, 403}
    finally:
        fastapi_app.dependency_overrides.clear()
        get_settings.cache_clear()


def _server(monkeypatch):
    if "ddgs" not in sys.modules and importlib.util.find_spec("ddgs") is None:
        monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=object))
    mcp_dir = str(ROOT / "services" / "mcps" / "research")
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)
    import server

    return server


def test_mcp_tool_is_registered_read_only_and_schema_matches_model(monkeypatch):
    server = _server(monkeypatch)
    described = server.TOOLS["describe_event_logic"]({})
    assert described["read_only"] is True and described["version"] == "event-logic-v1"
    assert described["input_schema"] == skill.EventLogicRequest.model_json_schema()
    assert described["input_schema"]["additionalProperties"] is False
    assert not any("order" in name for name in server.TOOLS if "event_logic" in name)


def test_mcp_bridge_rejects_unsafe_or_wrong_version_responses(monkeypatch):
    server = _server(monkeypatch)

    class Boom:
        def __init__(self, *a, **k):
            raise AssertionError("not reached")

    class Resp:
        def __init__(self, payload, status=200):
            self.status_code, self._payload = status, payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            yield json.dumps(self._payload).encode()

    class Client:
        payload: dict = {}

        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kw):
            assert method == "POST" and url.endswith("/api/analysis/event-logic/analyze")
            return Resp(Client.payload)

    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    good = {"schema_version": "event-logic-v1", "execution_eligible": False, "status": "ok"}
    Client.payload = good
    assert asyncio.run(server._structured_skill_research("event_logic", {}))["status"] == "ok"
    for bad in ({**good, "execution_eligible": True}, {**good, "schema_version": "other"}):
        Client.payload = bad
        r = asyncio.run(server._structured_skill_research("event_logic", {}))
        assert r["reason"] == "READ_ONLY_SKILL_UNAVAILABLE"


def test_shared_client_allows_event_logic_and_still_blocks_execution_tools():
    from unittest.mock import AsyncMock

    from polymarket_mcp import ResearchMCPClient

    client = ResearchMCPClient()
    client._call_tool = AsyncMock(
        return_value={
            "schema_version": "event-logic-v1",
            "execution_eligible": False,
            "status": "ok",
        }
    )
    assert asyncio.run(client.prediction_market_skill("event_logic", {}))["status"] == "ok"
    client._call_tool = AsyncMock(
        return_value={"schema_version": "event-logic-v1", "execution_eligible": True}
    )
    assert (
        asyncio.run(client.prediction_market_skill("event_logic", {}))["reason"]
        == "SKILL_TOOL_UNAVAILABLE_OR_UNSAFE"
    )
    with pytest.raises(ValueError, match="read-only"):
        asyncio.run(client.prediction_market_skill("submit_order", {}))


def test_skill_module_has_no_execution_or_network_dependencies():
    import ast

    banned = {"httpx", "requests", "aiohttp", "socket", "subprocess", "urllib", "py_clob_client_v2"}
    for path in [
        ROOT / "backend/app/services/intelligence/event_logic.py",
        ROOT / "backend/app/services/intelligence/event_logic_skill.py",
    ]:
        tree = ast.parse(path.read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not imported & banned
        assert "pre_trade_gate" not in path.read_text()
        assert "live_trading_policy" not in path.read_text()


def test_non_dict_expression_rejected_by_schema():
    with pytest.raises(ValueError):
        run({"events": [ev("a", "0.5")], "expressions": [{"name": "q", "expr": "a"}]})


def test_shadow_baseline_reproduces_and_is_honest():
    spec = importlib.util.spec_from_file_location(
        "event_logic_shadow", ROOT / "scripts/event-logic-shadow.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.report()
    stored = json.loads((ROOT / "user guide/skills/event-logic/shadow-baseline.json").read_text())
    assert result == stored
    assert result["scoring"]["effective_independent_events"] == 1
    assert result["scoring"]["verdict"] == "INSUFFICIENT_SAMPLE"
    assert result["scoring"]["claims_alpha"] is False
    assert all(p["independence_inside_frechet"] for p in result["pairs"])


def test_evidence_registry_entry_gates_promotion():
    entry = json.loads((ROOT / "user guide/skills/event-logic/evidence.json").read_text())
    assert entry["status"] == "implemented_detector"
    assert entry["execution_eligible"] is False and entry["production_approved"] is False
    assert entry["gates"]["pre_trade_gate_authoritative"] is True
    assert entry["version"] == skill.SKILL_VERSION
    assert (ROOT / "user guide/skills/event-logic/SKILL.md").read_text().strip()
