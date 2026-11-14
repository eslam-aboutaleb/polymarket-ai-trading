#!/usr/bin/env python3
"""Verify/replay captured public data without network, credentials, orders or hindsight.

Run: uv run --project backend --frozen python scripts/acceptance-replay-report.py DATASET.json.gz
Optional --pack-directory DIR writes a content-addressed bundle from collector receipts.
"""

import argparse
import base64
import gzip
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/shared"))
from py_clob_client_v2.utilities import generate_orderbook_summary_hash, parse_raw_orderbook_summary

from app.services.analytics.replay.ledger import ACCOUNTING_CONTEXT
from app.services.intelligence.resolution_rules import parse_resolution_rule
from app.services.market_data.valuation_fees import archived_taker_fee

ORIGINS = {
    "gamma-api.polymarket.com",
    "clob.polymarket.com",
    "polymarket.com",
    "api.exchange.coinbase.com",
}


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def load(path):
    with gzip.open(path, "rb") as stream:
        data = json.loads(stream.read(20_000_001))
    if len(data["requests"]) > 5000:
        raise ValueError("Dataset request bound exceeded")
    raw = {}
    for digest, encoded in data["raw"].items():
        body = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(body).hexdigest() != digest:
            raise ValueError("Raw source checksum mismatch")
        raw[digest] = body
    for row in data["requests"]:
        url = urlsplit(row["url"])
        if url.scheme != "https" or url.hostname not in ORIGINS or url.username or url.password:
            raise ValueError("Unapproved source origin")
        if len(raw[row["sha256"]]) != row["byte_count"]:
            raise ValueError("Raw byte count mismatch")
        if epoch(row["requested_at"]) > epoch(row["received_at"]):
            raise ValueError("Receipt precedes request")
    return data, raw


def pack(directory, target):
    requests = [json.loads(s) for s in (directory / "requests.jsonl").read_text().splitlines()]
    rtds = [json.loads(s) for s in (directory / "rtds.jsonl").read_text().splitlines()]
    digests = {r["sha256"] for r in requests + rtds}
    data = {
        "schema": "public-acceptance-replay/v1",
        "requests": requests,
        "rtds_spot_receipts": rtds,
        "hypothesis": json.loads((directory / "hypothesis.json").read_text()),
        "frozen_model": json.loads((directory / "frequency-model-frozen.json").read_text()),
        "raw": {
            h: base64.b64encode((directory / "raw" / h).read_bytes()).decode()
            for h in sorted(digests)
        },
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(
        gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), mtime=0)
    )


def analyze(path):
    with localcontext(ACCOUNTING_CONTEXT):
        return _analyze(path)


def _analyze(path):
    data, raw = load(path)
    rows = data["requests"]
    parsed = {
        h: json.loads(body, parse_float=Decimal)
        for h, body in raw.items()
        if body and body[:1] in (b"{", b"[")
    }
    labels, metadata, candles = {}, {}, {}
    for row in rows:
        if row["status"] != 200:
            continue
        key = (row["context"].get("symbol"), row["context"].get("window_start"))
        payload = parsed[row["sha256"]]
        if row["kind"] in {"gamma_market", "clob_market"}:
            metadata.setdefault((row["kind"], *key), []).append((row, payload))
        if row["kind"] == "clob_market":
            winners = [t for t in payload.get("tokens", []) if t.get("winner") is True]
            if (
                payload.get("closed") is True
                and len(winners) == 1
                and winners[0]["outcome"] in {"Up", "Down"}
            ):
                labels[key] = (winners[0]["outcome"] == "Up", row)
        if row["kind"] == "coinbase_historical_candles":
            candles[row["context"]["symbol"]] = {int(c[0]): c for c in payload}
    finalized = {}
    for key, label in labels.items():
        for _receipt, gamma in reversed(metadata.get(("gamma_market", *key), [])):
            if gamma.get("closed") is not True or gamma.get("umaResolutionStatus") != "resolved":
                continue
            outcomes, prices = gamma.get("outcomes"), gamma.get("outcomePrices")
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            prices = json.loads(prices) if isinstance(prices, str) else prices
            if (
                outcomes == ["Up", "Down"]
                and prices in (["1", "0"], ["0", "1"])
                and (prices[0] == "1") == label[0]
            ):
                finalized[key] = label
                break
    labels = finalized
    freeze = data["frozen_model"]
    if epoch(freeze["frozen_at"]) >= freeze["holdout_start"]:
        raise ValueError("Model was not frozen before its holdout")
    # Every training label was captured before model freeze. No historical
    # receipt is backdated into a historical prediction feature.
    for digest in freeze["training_receipt_hashes"]:
        if not any(
            r["kind"] == "clob_market"
            and r["sha256"] == digest
            and epoch(r["received_at"]) <= epoch(freeze["frozen_at"])
            for r in rows
        ):
            raise ValueError("Training label unavailable when model froze")
    training = {}
    for digest in freeze["training_receipt_hashes"]:
        matching = [
            r
            for r in rows
            if r["kind"] == "clob_market"
            and r["sha256"] == digest
            and epoch(r["received_at"]) <= epoch(freeze["frozen_at"])
        ]
        row = matching[0]
        key = row["context"]["symbol"], row["context"]["window_start"]
        if key in training or key not in labels or key[1] + 300 > epoch(freeze["frozen_at"]):
            raise ValueError("Duplicate, unavailable or nonhistorical training market")
        training[key] = labels[key][0]
    for symbol, counts in freeze["training_counts"].items():
        n = sum(key[0] == symbol for key in training)
        up = sum(won for key, won in training.items() if key[0] == symbol)
        if counts != {"n": n, "up": up}:
            raise ValueError("Training counts differ from raw finalized observations")
        with localcontext() as context:
            context.prec = 28  # Precision of the separately frozen source model.
            expected = (Decimal(up) + 1) / (Decimal(n) + 2)
        if Decimal(freeze["probabilities"][symbol]) != expected:
            raise ValueError("Frozen probability differs from its declared training formula")
    rule_audit = []
    for key, captures in metadata.items():
        if key[0] != "clob_market":
            continue
        row, payload = captures[0]
        spec = parse_resolution_rule(
            payload["condition_id"], payload, datetime.fromisoformat(row["received_at"])
        )
        rule_audit.append(
            {
                "condition_id": payload["condition_id"],
                "raw_sha256": row["sha256"],
                "observed_at": row["received_at"],
                "source_hash": spec.source_hash,
                "review_status": spec.review_status,
                "abstain_reasons": spec.abstain_reasons,
                "tradable_edge_allowed": False,
            }
        )
    forecasts, excluded, seen = [], Counter(), set()
    for row in rows:
        if row["kind"] != "clob_book" or row["status"] != 200:
            continue
        symbol, start = row["context"]["symbol"], row["context"]["window_start"]
        key = symbol, start
        receipt = epoch(row["received_at"])
        if (
            start < freeze["holdout_start"]
            or not start + 60 <= receipt < start + 300
            or key in seen
        ):
            continue

        def prior(kind, key=key, receipt=receipt):
            return [
                (r, p)
                for r, p in metadata.get((kind, *key), [])
                if epoch(r["received_at"]) <= receipt
            ]

        gamma, clob = prior("gamma_market"), prior("clob_market")
        if not gamma or not clob or key not in labels:
            excluded["missing_metadata_or_finalized_outcome"] += 1
            continue
        grow, g = gamma[-1]
        crow, c = clob[-1]
        up = next((t["token_id"] for t in c["tokens"] if t["outcome"] == "Up"), None)
        book = parsed[row["sha256"]]
        if book["asset_id"] != up:
            continue
        source = int(book["timestamp"]) / 1000
        if not 0 <= receipt - source <= 60 or book["market"] != c["condition_id"]:
            excluded["stale_or_wrong_identity_book"] += 1
            continue
        # Native public CLOB checksum, not an assertion based on price shape.
        computed = generate_orderbook_summary_hash(parse_raw_orderbook_summary(book))
        if computed != book.get("hash"):
            excluded["checksum_mismatch"] += 1
            continue
        bids = [Decimal(v["price"]) for v in book["bids"] if Decimal(v["size"]) > 0]
        asks = sorted(
            [
                (Decimal(v["price"]), Decimal(v["size"]))
                for v in book["asks"]
                if Decimal(v["size"]) > 0
            ]
        )
        if not bids or not asks or max(bids) >= asks[0][0]:
            excluded["invalid_or_empty_book"] += 1
            continue
        qty, gross, curve = Decimal(0), Decimal(0), []
        for price, size in asks:
            take = min(size, Decimal(10) - qty)
            if take <= 0:
                break
            qty += take
            gross += take * price
            curve.append({"price": str(price), "shares": str(take)})
        if qty != 10:
            excluded["insufficient_ten_share_depth"] += 1
            continue
        try:
            fee, version = archived_taker_fee({"clob": c, "gamma": g}, curve)
        except ValueError:
            excluded["unknown_fee_schedule"] += 1
            continue
        model = Decimal(freeze["probabilities"][symbol])
        mid = (max(bids) + asks[0][0]) / 2
        outcome, label = labels[key]
        target = Decimal(int(outcome))
        vwap = gross / qty
        forecasts.append(
            {
                "symbol": symbol,
                "window_start": start,
                "condition_id": c["condition_id"],
                "decision_available_at": row["received_at"],
                "book_source_at_ms": book["timestamp"],
                "book_sha256": row["sha256"],
                "gamma_sha256": grow["sha256"],
                "clob_sha256": crow["sha256"],
                "outcome_sha256": label["sha256"],
                "outcome_received_at": label["received_at"],
                "p_reconstructed": str(model),
                "p_market_midpoint": str(mid),
                "outcome_up": bool(outcome),
                "brier_reconstructed": str((model - target) ** 2),
                "brier_market": str((mid - target) ** 2),
                "vwap_ten_shares": str(vwap),
                "fee_upper_bound": str(fee),
                "fee_schedule_version": version,
                "net_sensitivity_per_share": {
                    str(s): str(model - vwap - fee / qty - s)
                    for s in map(Decimal, ["0", ".005", ".01", ".02"])
                },
                "market_baseline_net_per_share": str(mid - vwap - fee / qty),
                "modeled_settlement_cashflow_after_fee": str(target * qty - gross - fee),
                "actual_fill": False,
            }
        )
        seen.add(key)
    counterexamples = []
    # This diagnostic uses historical minute candles fetched AFTER settlement;
    # it is deliberately excluded from OOS features and availability claims.
    for (symbol, start), (won, label) in sorted(labels.items()):
        series = candles.get(symbol, {})
        if start in series and start + 240 in series:
            opened, closed = Decimal(series[start][3]), Decimal(series[start + 240][4])
            if closed > opened and not won:
                counterexamples.append(
                    {
                        "symbol": symbol,
                        "window_start": start,
                        "external_open": str(opened),
                        "external_final_minute_close": str(closed),
                        "actual_polymarket_winner": "Down",
                        "outcome_sha256": label["sha256"],
                        "interpretation": (
                            "Exchange candles rose; official winner was Down. "
                            "Minute close is not exact TWAP. "
                            "Diagnostic backfill, never OOS forecast input."
                        ),
                    }
                )

    def mean(key):
        return (
            str(sum((Decimal(f[key]) for f in forecasts), Decimal(0)) / len(forecasts))
            if forecasts
            else None
        )

    return {
        "schema": "acceptance-shadow-report/v1",
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "request_count": len(rows),
        "request_states": dict(Counter(str(r["status"]) for r in rows)),
        "raw_source_count": len(raw),
        "resolved_market_count": len(labels),
        "rule_shadow_audit": rule_audit,
        "rule_verification_counts": dict(Counter(r["review_status"] for r in rule_audit)),
        "frozen_model": freeze,
        "out_of_sample_count": len(forecasts),
        "brier_reconstructed": mean("brier_reconstructed"),
        "brier_market_midpoint": mean("brier_market"),
        "excluded": dict(excluded),
        "forecasts": forecasts,
        "opportunity_ranking": [
            f["condition_id"]
            for f in sorted(
                forecasts, key=lambda f: Decimal(f["net_sensitivity_per_share"]["0"]), reverse=True
            )
        ],
        "oracle_counterexamples": counterexamples,
        "oracle_verified": False,
        "limitations": [
            (
                "Eight forward outcomes are a smoke study, not statistical validation or "
                "demonstrated alpha."
            ),
            "Historical bot forecasts and actual fills never existed in this dataset.",
            (
                "Reconstructed frequency benchmark is not an independently approved calibrated "
                "forecast."
            ),
            (
                "Displayed terminal references and RTDS spot do not supply the complete "
                "authoritative Chainlink TWAP history."
            ),
            (
                "Production valuation/oracle tooling must abstain where verified rules, "
                "calibration or settlement series are absent."
            ),
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--pack-directory", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.pack_directory:
        pack(args.pack_directory, args.dataset)
    with localcontext(ACCOUNTING_CONTEXT):
        report = analyze(args.dataset)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
