from datetime import UTC, datetime

from app.services.trading.btc_regime_scorecard import prior_skill


def iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def test_alternative_policy_requires_earlier_distinct_matched_cohorts():
    first = 12000
    earlier = [
        {
            "kind": "score",
            "start": 300 * i,
            "duration": 300,
            "scores": {"proxy_twap": ".10", "market": ".20"},
            "resolved_at": iso(300 * i + 310),
            "at": iso(300 * i + 320),
        }
        for i in range(20)
    ]
    assert prior_skill(earlier, first, 300)
    assert not prior_skill(earlier, first, 900)
    assert not prior_skill(earlier[:19] + [earlier[0]], first, 300)
    assert not prior_skill(earlier[:19] + [{**earlier[-1], "at": iso(first + 1)}], first, 300)
    assert not prior_skill(
        earlier[:19] + [{**earlier[-1], "scores": {"proxy_twap": "NaN", "market": ".20"}}],
        first,
        300,
    )
