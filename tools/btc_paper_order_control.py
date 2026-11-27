"""Exercise native paper order submission and settlement without a strategy signal."""

import argparse
import json
import time
from decimal import Decimal
from pathlib import Path

import btc_paper_benchmark as benchmark
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log", type=Path)
    parser.add_argument("--account-config", type=Path, required=True)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--attempt", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()
    account = json.loads(args.account_config.read_text())[0]
    if account["label"] != "BTC Execution Control Paper":
        parser.error("Use the dedicated BTC Execution Control Paper account")
    benchmark.LOG = args.log
    with httpx.Client(timeout=8, trust_env=False) as client:
        if args.score_only:
            deadline = time.monotonic() + (600 if args.wait else 0)
            while True:
                benchmark.score_available(client, account)
                with args.log.open() as stream:
                    rows = list(map(json.loads, stream))
                settled = next(
                    (r for r in reversed(rows) if r["kind"] == "ledger_check" and r.get("settled")),
                    None,
                )
                if settled:
                    print(json.dumps(settled["valuation"], default=str))  # noqa: T201
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(15)
            return
        if args.log.exists():
            parser.error("Use a fresh control log")
        start = int(time.time()) // 300 * 300
        if time.time() > start + 235:
            parser.error("Start the execution control earlier in a five-minute round")
        market = benchmark.event(client, start)
        if not market.get("acceptingOrders") or json.loads(market["outcomes"]) != ["Up", "Down"]:
            parser.error("No active exact BTC window")
        tokens = json.loads(market["clobTokenIds"])
        fees = benchmark.fee_evidence(client, market["conditionId"], tokens)
        quotes, _, _ = benchmark.order_books(client, tokens)
        # Use a small executable offer that can satisfy venue minimum quantity.
        candidates = [
            (name, q)
            for name, q in quotes.items()
            if q.minimum_shares <= min(q.visible_shares, Decimal(5) / q.ask)
        ]
        if not candidates:
            parser.error("No displayed depth for a 5 USDC control order")
        side, quote = min(candidates, key=lambda item: item[1].ask)
        benchmark.save("inputs", start=start, evidence={"control": True}, fees=fees)
        benchmark.save("forecast", start=start, condition=market["conditionId"], forecasts={})
        result = benchmark.execute(
            benchmark.PAPER_CONTAINER,
            "/app/.venv/bin/python",
            benchmark.PAPER,
            {
                "user": account["user_id"],
                "account": account["account_id"],
                "condition": market["conditionId"],
                "token": tokens[benchmark.OUTCOMES.index(side)],
                "start": start,
                "deadline": start + 300,
                "limit": str(min(quote.ask + quote.tick, Decimal(1) - quote.tick)),
                "p_up": None,
                "decision_at": benchmark.now(),
                "max_loss_at_risk": "6",
                "intent_key": f"btc-control:{market['conditionId']}:{args.attempt}",
            },
        )
        benchmark.save("execution", start=start, order=result)
        print(json.dumps({k: result.get(k) for k in ("status", "reason", "shares", "order_id")}))  # noqa: T201


if __name__ == "__main__":
    main()
