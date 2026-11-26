"""Run a bounded, paper-only BTC forecasting benchmark against public markets."""

# ruff: noqa: T201 (command-line progress and container JSON output)
import argparse
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory

import httpx

from app.services.trading.btc_agent_contract import (
    MARKET_WEIGHT,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    evaluate_agent_reply,
)
from app.services.trading.btc_paper_decision import (
    FEE,
    MAX_ASK_INCREASE,
    MAX_LOSS_AT_RISK,
    MAX_MIDPOINT_MOVE,
    MAX_PROXY_MOVE_BPS,
    MAX_PROXY_RANGE_BPS,
    MAX_QUOTE_AGE_SECONDS,
    MAX_SPREAD,
    MIN_EDGE,
    Quote,
    brier_score,
    decide,
    eligible_arrival_quote,
    probability,
    proxy_baselines,
    spike_safe_limit,
    stress_reasons,
    visible_buy_estimate,
)
from app.services.trading.btc_regime_scorecard import prior_skill
from app.services.trading.btc_twap_forecast import settlement_probability
from app.services.trading.btc_venue_evidence import (
    reported_reference,
    resolved_reference_side,
    taker_coefficient,
    taker_fee,
)

ROOT = Path.home() / ".local/share/polymarket-attribution-test"
LOG = ROOT / "btc-five-minute-benchmark.jsonl"
AGENT_CONTAINER = os.getenv("BTC_BENCHMARK_AGENT_CONTAINER", "polymarket-cli-agent-1")
PAPER_CONTAINER = os.getenv("BTC_BENCHMARK_PAPER_CONTAINER", "polymarket-backend-1")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
BINANCE = "https://api.binance.com"
CLI = """
import asyncio,json,sys
from dataclasses import replace
from src.cli_agent import CopilotCLIAgent

async def run():
 agent=CopilotCLIAgent()
 payload=json.load(sys.stdin)
 config=replace(agent._llm_config_for_task("quick_analysis"),temperature=0)
 reply=await agent._run_analysis(payload["inputs"],system_prompt=payload["system"],llm=config)
 print(json.dumps(reply))
asyncio.run(run())
"""
PAPER = """
import asyncio,json,sys
from decimal import Decimal
from app.utils.database import SessionLocal
from app.services.analytics.paper_accounting import (
 settings_for_account, submit_paper_order, settle_captured, account_state, value_account,
)
from app.services.analytics.paper_evidence import refresh_evidence
async def run():
 x=json.load(sys.stdin)
 with SessionLocal() as db:
  user,account=x['user'],x['account']
  s=settings_for_account(db,user,account)
  if not s or s.simulation_mode is not True:raise RuntimeError('Paper account required')
  if x.get('token'):
   state=account_state(db,user,account)
   if state.fee_bps != Decimal('100'):
    raise ValueError('Paper account fee differs from benchmark policy')
   captured=await refresh_evidence(db,x['condition'],allow_up_down=True)
  else:
   import httpx
   from app.services.market_data.replay_capture import PublicVenueCapture, capture_market

   class ExactWindowCapture(PublicVenueCapture):
    allow_up_down=True

    async def get(self,url,params=None):
     if url.startswith('https://gamma-api.polymarket.com/') and url.endswith('/markets'):
      slug=f"btc-updown-{x.get('duration',300)//60}m-{x['start']}"
      market=await super().get(
       f'https://gamma-api.polymarket.com/markets/slug/{slug}',{'closed':'true'}
      )
      return [market] if market.get('conditionId')==x['condition'] else []
     return await super().get(url,params)

   async with asyncio.timeout(12), httpx.AsyncClient(timeout=5) as client:
    await capture_market(db,x['condition'],ExactWindowCapture(client))
   captured=True
  if not captured:
   db.rollback()
   print(json.dumps({'status':'abstain','reason':'native evidence unavailable'}))
   return
  if x.get('token'):
   from time import time
   if time()>=min(x['deadline']-20,x.get('minute_deadline') or x['deadline']):
    db.rollback()
    print(json.dumps({'status':'abstain','reason':'expired execution window'}))
    return
   portfolio=value_account(db,state)
   open_cost=sum(Decimal(p['cost_basis']) for p in portfolio['positions'].values())
   loss=max(Decimal(0),-Decimal(portfolio['realized_pnl']))
   if loss+open_cost+Decimal('5.05')>Decimal(x['max_loss_at_risk']):
    db.rollback()
    print(json.dumps({'status':'abstain','reason':'PAPER_LOSS_BUDGET'}))
    return
   result=submit_paper_order(db,user_id=user,account_id=account,token_id=x['token'],side='BUY',amount=Decimal('5'),strategy='manual',limit_price=Decimal(x['limit']),intent_key=x['intent_key'],source_identity={'kind':'app_agent_btc_minute','p_up':x['p_up'],'decision_at':x['decision_at']})
  else:
   settle_captured(db,user,account)
   result=value_account(db,account_state(db,user,account))
  db.commit()
  print(json.dumps(result,default=str))
asyncio.run(run())
"""


def now():
    return datetime.now(UTC).isoformat()


def save(kind, **d):
    with os.fdopen(os.open(LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600), "a") as f:
        f.write(json.dumps(dict(kind=kind, at=now(), **d), default=str) + "\n")


def execute(container, interpreter, code, data):
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("Docker CLI is required to run the benchmark")
    p = subprocess.run(  # noqa: S603 - Docker exec without a shell, JSON on stdin
        [executable, "exec", "-i", container, interpreter, "-c", code],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        timeout=40,
    )
    if p.returncode:
        raise RuntimeError((p.stderr.strip().splitlines() or ["Docker exec failed"])[-1][:200])
    return json.loads(p.stdout.splitlines()[-1])


def get(c, url, **params):
    for attempt in range(2):
        try:
            r = c.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.TransportError:
            if attempt:
                raise
            time.sleep(1)
    raise RuntimeError("GET failed")


def event(c, start, duration=300):
    e = get(c, GAMMA + "/events", slug=f"btc-updown-{duration // 60}m-{start}")
    if len(e) != 1:
        raise RuntimeError("Venue did not return exact BTC window")
    return e[0]["markets"][0]


VENUE_OPEN = "https://polymarket.com/api/crypto/crypto-price"
OUTCOMES = ("Up", "Down")
SHADOW_DELAY_SECONDS = 2


def iso(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def venue_reference(client, start, duration=300):
    response = get(
        client,
        VENUE_OPEN,
        symbol="BTC",
        eventStartTime=iso(start),
        endDate=iso(start + duration),
        variant="fiveminute" if duration == 300 else "fifteen",
    )
    return reported_reference(response, start, time.time() + 1)


def opening_reference(client, start, rules, duration=300):
    if "btc-usd-twap-60s-streams" not in rules:
        return None
    try:
        result = venue_reference(client, start, duration)
    except (httpx.HTTPError, KeyError, TypeError, ValueError, InvalidOperation):
        return None
    return {
        "price": result["openPrice"],
        "window_start": iso(start),
        "source": VENUE_OPEN,
        "source_at_ms": result["timestamp"],
        "basis": "venue-displayed BTC/USD TWAP opening reference",
    }


def fee_evidence(client, condition, tokens):
    url = CLOB + "/clob-markets/" + condition
    payload = get(client, url)
    if payload.get("c", condition) != condition:
        raise ValueError("Fee schedule condition mismatch")
    rate = taker_coefficient(payload, dict(zip(OUTCOMES, tokens, strict=True)))
    return {"source": url, "observed_at": now(), "curve": payload["fd"], "rate": str(rate)}


def order_books(client, tokens):
    raw = [get(client, CLOB + "/book", token_id=token) for token in tokens]
    quotes = {}
    midpoints = {}
    depth = {}
    observed = time.time()
    for side, token, book in zip(OUTCOMES, tokens, raw, strict=True):
        if str(book["asset_id"]) != token:
            raise ValueError("Book token identity mismatch")
        observed_at = int(book["timestamp"]) / 1000
        if not 0 <= observed - observed_at <= MAX_QUOTE_AGE_SECONDS:
            continue
        if not book["asks"] or not book["bids"]:
            continue
        levels = [(Decimal(x["price"]), Decimal(x["size"])) for x in book["asks"]]
        if any(
            not (price.is_finite() and 0 < price <= 1 and size.is_finite() and size > 0)
            for price, size in levels
        ):
            raise ValueError("Invalid order-book depth")
        depth[side] = sorted(levels)[:10]
        ask = depth[side][0][0]
        bid = max(Decimal(x["price"]) for x in book["bids"])
        if not 0 <= bid <= ask <= 1:
            raise ValueError("Invalid bid/ask")
        midpoints[side] = (bid + ask) / 2
        quotes[side] = Quote(
            ask,
            Decimal(book["tick_size"]),
            observed_at,
            sum(Decimal(x["size"]) for x in book["asks"] if Decimal(x["price"]) == ask),
            Decimal(book["min_order_size"]),
            bid,
        )
    if not midpoints:
        raise ValueError("No two-sided book in either outcome")
    if len(midpoints) == 1:
        midpoint = midpoints["Up"] if "Up" in midpoints else 1 - midpoints["Down"]
    else:
        midpoint = midpoints["Up"] / sum(midpoints.values())
    return quotes, midpoint, depth


def model_input(evidence):
    """Keep auditable raw proxy levels out of the model payload."""
    proxy = evidence["proxy"]
    return {
        **evidence,
        "reference_available": evidence["opening_reference"] is not None,
        "proxy": {
            "source": proxy["source"],
            "opening_bar_at": proxy["opening_bar_at"],
            "observed_at": proxy["observed_at"],
            "same_source_return_bps": proxy["same_source_return_bps"],
            "same_source_range_bps": proxy["same_source_range_bps"],
            "settlement_reference": False,
        },
    }


def proxy_window(client, start):
    bars = get(
        client,
        BINANCE + "/api/v3/klines",
        symbol="BTCUSDT",
        interval="1m",
        startTime=start * 1000,
        limit=5,
    )
    bars = [bar for bar in bars if start * 1000 <= bar[0] <= time.time() * 1000]
    if not bars or bars[0][0] != start * 1000:
        raise ValueError("Missing exact Binance proxy opening bar")
    last = Decimal(bars[-1][4])
    proxy_open = Decimal(bars[0][1])
    low = min(Decimal(bar[3]) for bar in bars)
    high = max(Decimal(bar[2]) for bar in bars)
    if not all(x.is_finite() and x > 0 for x in (last, proxy_open, low, high)) or high < low:
        raise ValueError("Invalid proxy price or range")
    return proxy_open, last, (high - low) / proxy_open * 10000


def twap_shadow(client, start, proxy_open, proxy_latest, sampled, duration=300):
    """Record only a forecast available before the settlement averaging window."""
    rows = get(
        client,
        BINANCE + "/api/v3/klines",
        symbol="BTCUSDT",
        interval="1m",
        startTime=(start - 720) * 1000,
        endTime=int(sampled * 1000),
        limit=20,
    )
    eligible = {row[0]: float(row[4]) for row in rows if row[6] < sampled * 1000}
    timestamps = sorted(eligible)
    if any(b - a != 60000 for a, b in zip(timestamps, timestamps[1:], strict=False)):
        return None
    probability_up = settlement_probability(
        proxy_open=float(proxy_open),
        proxy_latest=float(proxy_latest),
        closed_proxy_prices=[eligible[k] for k in timestamps],
        seconds_remaining=start + duration - sampled,
    )
    save("twap_shadow_inputs", start=start, sampled_at=iso(sampled), closed_prices=eligible)
    return Decimal(str(probability_up)) if probability_up is not None else None


def forecast(
    client,
    start,
    account,
    *,
    forecast_only=False,
    minute=None,
    guidance="",
    duration=300,
    evidence_cutoff=None,
    validated=False,
    after_cooldown=False,
):
    m = event(client, start, duration)
    tokens = json.loads(m["clobTokenIds"])
    if m.get("acceptingOrders") is not True or json.loads(m["outcomes"]) != list(OUTCOMES):
        raise ValueError("Window is not executable")
    close = start + duration
    if datetime.fromisoformat(m["endDate"]).timestamp() != close:
        raise ValueError("Market does not match the expected BTC window")
    fees = None
    try:
        fees = fee_evidence(client, m["conditionId"], tokens)
    except (httpx.HTTPError, KeyError, TypeError, ValueError, InvalidOperation):
        save("fee_schedule_unavailable", start=start, condition=m["conditionId"])
    while True:
        reference = opening_reference(client, start, m["description"], duration)
        save(
            "venue_evidence",
            start=start,
            condition=m["conditionId"],
            fees=fees,
            reference=reference,
        )
        try:
            quotes, midpoint, forecast_depth = order_books(client, tokens)
            if evidence_cutoff is None or reference is not None:
                break
        except (httpx.HTTPError, ValueError):
            if evidence_cutoff is None:
                raise
        if evidence_cutoff is None or time.time() >= evidence_cutoff:
            raise ValueError("DATA_NOT_READY_BEFORE_CUTOFF")
        time.sleep(min(5, max(0, evidence_cutoff - time.time())))
    proxy_open, last, range_bps = proxy_window(client, start)
    sampled = time.time()
    risk_start = max(start, int(sampled) // 60 * 60 - 60) if after_cooldown else start
    risk_open, risk_latest, risk_range = (
        proxy_window(client, risk_start) if risk_start != start else (proxy_open, last, range_bps)
    )
    change = last / proxy_open - 1
    baselines = proxy_baselines(change)
    twap = None
    if reference:
        try:
            twap = twap_shadow(client, start, proxy_open, last, sampled, duration)
        except (httpx.HTTPError, ValueError, KeyError):
            save("twap_shadow_unavailable", start=start)

    evidence = {
        "condition": m["conditionId"],
        "opens_at": iso(start),
        "closes_at": iso(close),
        "rules": m["description"],
        "opening_reference": reference,
        "proxy": {
            "source": "Binance BTCUSDT",
            "open": str(proxy_open),
            "opening_bar_at": iso(start),
            "settlement_reference": False,
            "latest": str(last),
            "same_source_return_bps": str(change * 10000),
            "same_source_range_bps": str(range_bps),
            "observed_at": now(),
        },
        "up_market_midpoint": str(midpoint),
        "asks": {side: str(q.ask) for side, q in quotes.items()},
    }
    save("inputs", start=start, evidence=evidence, forecast_depth=forecast_depth, fees=fees)
    model_evidence = model_input(evidence)
    model_evidence["proxy_twap_probability"] = str(twap) if twap is not None else None
    reply = execute(
        AGENT_CONTAINER,
        "python",
        CLI,
        {"system": SYSTEM_PROMPT + guidance, "inputs": json.dumps(model_evidence)},
    )
    try:
        signal = evaluate_agent_reply(
            reply, midpoint=midpoint, twap=twap, reference_verified=reference is not None
        )
    except (ValueError, InvalidOperation):
        save("model_unavailable", start=start, reply=reply)
        return None
    if time.time() >= close:
        raise ValueError("Model response arrived after the window closed")
    adjusted = signal.actionable
    if validated and signal.evidence == "proxy_twap":
        # Only the independently validated proxy model can support a larger adjustment.
        delta = twap - midpoint
        adjusted = midpoint + min(abs(delta) / 2, Decimal(".08")).copy_sign(delta)
    forecasts = {
        "agent_raw": signal.raw,
        "agent_bounded": signal.bounded,
        "agent_market_anchored": adjusted,
        "market": midpoint,
        **baselines,
    }
    if twap is not None:
        forecasts["proxy_twap"] = twap
    save(
        "forecast",
        start=start,
        condition=m["conditionId"],
        forecasts=forecasts,
        response=asdict(signal),
        prompt_version=PROMPT_VERSION,
        duration=duration,
    )
    _, execution_proxy, execution_range = proxy_window(client, risk_start)
    fresh, execution_midpoint, decision_depth = order_books(client, tokens)
    decision = decide(
        adjusted,
        fresh,
        now=time.time(),
        close_at=close,
        reference_verified=reference is not None,
        market_midpoint=execution_midpoint,
        venue_taker_rate=Decimal(fees["rate"]) if fees else Decimal(0),
    )
    side = decision.action
    proxy_move = max(
        (risk_latest / risk_open - 1) * 10000,
        (execution_proxy / risk_open - 1) * 10000,
        (execution_proxy / risk_latest - 1) * 10000,
        key=abs,
    )
    blocks = stress_reasons(
        proxy_move_bps=proxy_move,
        proxy_range_bps=max(risk_range, execution_range),
        forecast_midpoint=midpoint,
        execution_midpoint=execution_midpoint,
        forecast_ask=quotes[side].ask if side in quotes else None,
        quote=fresh.get(side),
    )
    if fees is None:
        blocks += ("VENUE_FEE_UNVERIFIED",)
    if signal.evidence == "baseline":
        blocks += ("MODEL_EDGE_UNVERIFIED",)
    save(
        "decision",
        start=start,
        quotes={side: asdict(q) for side, q in fresh.items()},
        decision=asdict(decision),
        execution_midpoint=str(execution_midpoint),
        decision_depth=decision_depth,
        spike_blocks=blocks,
        effective_action="HOLD" if blocks else side,
        proxy_move_bps=proxy_move,
        proxy_range_bps=max(risk_range, execution_range),
        risk_start=risk_start,
    )
    print(
        iso(start),
        {k: str(v) for k, v in forecasts.items()},
        "HOLD" if blocks else decision.action,
        blocks or decision.reason,
        flush=True,
    )
    if blocks or decision.action == "HOLD":
        if set(blocks) & {
            "MARKET_REPRICED",
            "WIDE_SPREAD",
            "ASK_REPRICED",
            "PROXY_SPIKE",
            "PROXY_RANGE_SPIKE",
        }:
            return "RETRY_STRESS"
        return None
    side = decision.action
    execution_limit = spike_safe_limit(fresh[side], decision.limit)
    displayed = visible_buy_estimate(decision_depth[side], execution_limit, Decimal("5"))
    save(
        "visible_depth_estimate", start=start, side=side, estimate=displayed, limit=execution_limit
    )
    decision_clock = time.time()
    save(
        "replay_intent",
        start=start,
        condition=m["conditionId"],
        token=tokens[OUTCOMES.index(side)],
        limit=execution_limit,
        notional="5",
        arrival_ms=int((decision_clock + SHADOW_DELAY_SECONDS) * 1000),
    )
    time.sleep(SHADOW_DELAY_SECONDS)
    if time.time() >= close - 20:
        save("delayed_depth", start=start, status="expired")
        return None
    delayed_quotes, delayed_midpoint, delayed_depth = order_books(client, tokens)
    later = delayed_quotes.get(side)
    if not eligible_arrival_quote(
        later, decision_at=decision_clock, delay_seconds=SHADOW_DELAY_SECONDS, now=time.time()
    ):
        save("delayed_depth", start=start, status="no_post_decision_quote")
        return None
    delayed_estimate = visible_buy_estimate(delayed_depth[side], execution_limit, Decimal("5"))
    _, arrival_proxy, arrival_range = proxy_window(client, risk_start)
    delay_blocks = stress_reasons(
        proxy_move_bps=max(proxy_move, (arrival_proxy / execution_proxy - 1) * 10000, key=abs),
        proxy_range_bps=max(execution_range, arrival_range),
        forecast_midpoint=execution_midpoint,
        execution_midpoint=delayed_midpoint,
        forecast_ask=fresh[side].ask,
        quote=later,
    )
    eligible = (
        delayed_estimate["shares"] >= later.minimum_shares
        and not delay_blocks
        and time.time() < close - 20
        and time.time() - later.observed_at <= MAX_QUOTE_AGE_SECONDS
        and (delayed_midpoint > Decimal(".5") if side == "Up" else delayed_midpoint < Decimal(".5"))
    )
    save(
        "delayed_depth",
        start=start,
        status="eligible" if eligible else "hold",
        modeled_delay_seconds=SHADOW_DELAY_SECONDS,
        elapsed_seconds=time.time() - decision_clock,
        book_observed_at=later.observed_at,
        estimate=delayed_estimate,
        blocks=delay_blocks,
    )
    if forecast_only or not eligible:
        return None
    x = {
        "user": account["user_id"],
        "account": account["account_id"],
        "condition": m["conditionId"],
        "start": start,
        "duration": duration,
        "deadline": close,
        "token": tokens[OUTCOMES.index(decision.action)],
        "limit": str(execution_limit),
        "p_up": str(adjusted),
        "decision_at": now(),
        "max_loss_at_risk": str(MAX_LOSS_AT_RISK),
        "minute_deadline": minute + 60 if minute is not None else None,
        "intent_key": f"btc-minute:{m['conditionId']}"
        + (f":{minute}" if minute is not None else ""),
    }
    result = execute(PAPER_CONTAINER, "/app/.venv/bin/python", PAPER, x)
    actual_vwap = result.get("fill_price")
    slippage = (
        Decimal(actual_vwap) - displayed["vwap"]
        if actual_vwap is not None and displayed["vwap"] is not None
        else None
    )
    save(
        "execution",
        start=start,
        order=result,
        vwap_difference=slippage,
        decision_best_ask=fresh[side].ask,
        estimated_shares=displayed["shares"],
        venue_fee_estimate=sum(
            (
                taker_fee(Decimal(f["price"]), Decimal(f["shares"]), Decimal(fees["rate"]))
                for f in result.get("fills", [])
            ),
            Decimal(0),
        )
        if result.get("fills")
        else None,
    )
    print("paper", result.get("status"), result.get("reason"), flush=True)
    return x


def score_available(client, account=None):
    if not LOG.exists():
        return
    rows = [json.loads(line) for line in LOG.read_text().splitlines()]
    rows = [x for x in rows if not x.get("diagnostic")]
    done = {x["start"] for x in rows if x["kind"] == "score"}
    for row in rows:
        if (
            row["kind"] != "forecast"
            or row["start"] in done
            or time.time() < row["start"] + row.get("duration", 300)
        ):
            continue
        cid = row["condition"]
        try:
            status = get(client, "https://data-api.polymarket.com/v2/resolutions", condition=cid)[
                "data"
            ]
            clob = get(client, CLOB + "/markets/" + cid)
        except httpx.HTTPError:
            continue
        winners = [t["outcome"] for t in clob.get("tokens", []) if t.get("winner") is True]
        resolution = next(
            (x for x in status if x["condition_id"] == cid and x["status"] == "resolved"), None
        )
        if (
            not resolution
            or not clob.get("closed")
            or len(winners) != 1
            or winners[0] not in OUTCOMES
        ):
            continue
        up = winners[0] == "Up"
        if resolution["payouts"] != ([1000000, 0] if up else [0, 1000000]):
            continue
        scores = {k: brier_score(probability(p), up) for k, p in row["forecasts"].items()}
        execution = next(
            (x for x in rows if x["kind"] == "execution" and x["start"] == row["start"]),
            {},
        )
        order = execution.get("order", {})
        pnl = None
        if order.get("status") in {"filled", "partial"}:
            token = next(t for t in clob["tokens"] if str(t["token_id"]) == str(order["token_id"]))
            pnl = (
                Decimal(order["shares"]) * int(token["winner"])
                - Decimal(order["notional_usdc"])
                - Decimal(order["fee_usdc"])
            )
        save(
            "score",
            start=row["start"],
            winner=winners[0],
            scores=scores,
            modeled_settlement_pnl=pnl,
            venue_fee_adjusted_pnl=(
                pnl + Decimal(order["fee_usdc"]) - Decimal(execution["venue_fee_estimate"])
                if pnl is not None and execution.get("venue_fee_estimate") is not None
                else None
            ),
            resolution_transaction=resolution.get("transaction_hash"),
            duration=row.get("duration", 300),
            resolved_at=resolution.get("resolved_at"),
        )
        done.add(row["start"])
        print("resolved", iso(row["start"]), winners[0], scores, "paper pnl", pnl, flush=True)

    reference_done = {
        x["start"] for x in rows if x["kind"] == "venue_reference_close" and x.get("matches")
    }
    twap_rounds = {
        x["start"]
        for x in rows
        if x["kind"] == "inputs"
        and (x["evidence"].get("opening_reference") or {}).get("source") == VENUE_OPEN
    }
    durations = {x["start"]: x.get("duration", 300) for x in rows if x["kind"] == "forecast"}
    for score in (x for x in rows if x["kind"] == "score"):
        start = score["start"]
        if start not in twap_rounds - reference_done:
            continue
        try:
            reported = venue_reference(client, start, durations.get(start, 300))
            winner = resolved_reference_side(reported)
        except (httpx.HTTPError, KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if winner is not None:
            save(
                "venue_reference_close",
                start=start,
                source=VENUE_OPEN,
                reported=reported,
                matches=winner == score["winner"],
            )

    if account:
        rows = [json.loads(line) for line in LOG.read_text().splitlines()]
        verified = {x["start"] for x in rows if x["kind"] == "ledger_check" and x.get("settled")}
        for row in rows:
            if row["kind"] != "execution" or row["start"] not in done - verified:
                continue
            order = row["order"]
            if (
                order["status"] not in {"filled", "partial"}
                or order["account_id"] != account["account_id"]
            ):
                continue
            try:
                value = execute(
                    PAPER_CONTAINER,
                    "/app/.venv/bin/python",
                    PAPER,
                    {
                        "user": account["user_id"],
                        "account": account["account_id"],
                        "condition": order["condition_id"],
                        "start": row["start"],
                        "duration": durations.get(row["start"], 300),
                    },
                )
            except (RuntimeError, subprocess.TimeoutExpired):
                continue
            settled = "positions" in value and order["token_id"] not in value["positions"]
            save("ledger_check", start=row["start"], settled=settled, valuation=value)


def run_evidence_window(client, start, account, *, duration, forecast_only, validated):
    """Repeat a pre-submission stress hold once, on new evidence."""
    global LOG
    canonical = LOG
    with TemporaryDirectory() as directory:
        for attempt in range(2):
            LOG = Path(directory) / f"{attempt}.jsonl"
            outcome = None
            retry_at = (int(time.time()) // 60 + 1) * 60 + 3
            try:
                outcome = forecast(
                    client,
                    start,
                    account,
                    forecast_only=forecast_only,
                    duration=duration,
                    evidence_cutoff=min(start + duration - 60, start + 150),
                    validated=validated,
                    after_cooldown=attempt > 0,
                )
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                RuntimeError,
                subprocess.TimeoutExpired,
            ) as exc:
                save("unavailable", start=start, reason=str(exc))
            finally:
                attempt_log, LOG = LOG, canonical
                retry = (
                    outcome == "RETRY_STRESS"
                    and attempt == 0
                    and retry_at + 20 < start + duration - 60
                )
                if attempt_log.exists():
                    with (
                        attempt_log.open() as stream,
                        os.fdopen(
                            os.open(LOG, os.O_APPEND | os.O_WRONLY | os.O_CREAT, 0o600), "a"
                        ) as out,
                    ):
                        for line in stream:
                            entry = json.loads(line)
                            out.write(
                                json.dumps({**entry, "diagnostic": retry, "attempt": attempt + 1})
                                + "\n"
                            )
            if not retry:
                return
            time.sleep(max(0, retry_at - time.time()))


def main():
    global LOG

    parser = argparse.ArgumentParser()
    parser.add_argument("--windows", type=int, choices=range(1, 13), default=3)
    parser.add_argument("--start", type=int, help="Optional first window epoch")
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--window-minutes", type=int, choices=(5, 15), default=5)
    parser.add_argument("--validation-log", type=Path)

    parser.add_argument("--forecast-only", action="store_true")
    parser.add_argument("--reconcile-paper", action="store_true")
    parser.add_argument("--log", type=Path, default=LOG)
    parser.add_argument("--account-config", type=Path, default=ROOT / "btc-minute-account.json")
    args = parser.parse_args()
    LOG = args.log
    with httpx.Client(timeout=12, trust_env=False) as client:
        if args.score_only:
            account = (
                json.loads(args.account_config.read_text())[0] if args.reconcile_paper else None
            )
            score_available(client, account)
            return
        account = None if args.forecast_only else json.loads(args.account_config.read_text())[0]
        duration = args.window_minutes * 60
        first = int(time.time()) // duration * duration
        if time.time() - first > 75:
            first += duration
        if args.start is not None:
            if args.start % duration or args.start + 75 < time.time():
                parser.error("--start must identify an upcoming window of the selected duration")
            first = args.start
        validated = False
        if args.validation_log:
            with args.validation_log.open() as previous:
                validated = prior_skill(map(json.loads, previous), first, duration)
            if not validated:
                parser.error(
                    "candidate requires superior matched TWAP scores on 20 earlier windows"
                )
        if LOG.exists():
            parser.error("choose a new --log for each benchmark cohort")
        save(
            "batch",
            start=first,
            windows=args.windows,
            duration=duration,
            policy="validated-proxy" if validated else "control",
            forecast_only=args.forecast_only,
            entry_offset_seconds=60,
            fee_rate=FEE,
            minimum_edge_per_share=MIN_EDGE,
            market_weight=MARKET_WEIGHT,
            prompt_version=PROMPT_VERSION,
            decision_policy="verified-evidence-and-spike-v4",
            modeled_order_delay_seconds=SHADOW_DELAY_SECONDS,
            risk_limits={
                "proxy_move_bps": MAX_PROXY_MOVE_BPS,
                "proxy_range_bps": MAX_PROXY_RANGE_BPS,
                "midpoint_move": MAX_MIDPOINT_MOVE,
                "spread": MAX_SPREAD,
                "ask_increase": MAX_ASK_INCREASE,
                "loss_at_risk": MAX_LOSS_AT_RISK,
                "final_limit_buffer_ticks": 1,
            },
            momentum_up=Decimal(".60"),
            momentum_down=Decimal(".40"),
            mean_reversion="inverse of momentum",
        )
        for index in range(args.windows):
            start = first + index * duration
            while time.time() < start + 60:
                if LOG.exists():
                    score_available(client, account)
                time.sleep(min(20, max(0, start + 60 - time.time())))
            try:
                run_evidence_window(
                    client,
                    start,
                    account,
                    forecast_only=args.forecast_only,
                    duration=duration,
                    validated=validated,
                )
            except (
                httpx.HTTPError,
                ValueError,
                KeyError,
                RuntimeError,
                subprocess.TimeoutExpired,
            ) as exc:
                save("unavailable", start=start, reason=str(exc))
                print("unavailable", iso(start), str(exc), flush=True)
        end = first + args.windows * duration
        while time.time() < end + 300:
            score_available(client, account)
            rows = [json.loads(line) for line in LOG.read_text().splitlines()]
            finished = {
                x["start"]
                for x in rows
                if x["kind"] in ("score", "unavailable", "model_unavailable")
            }
            if all(first + i * duration in finished for i in range(args.windows)):
                break
            time.sleep(20)
        scores = [x["scores"] for x in rows if x["kind"] == "score" and first <= x["start"] < end]
        batch_rows = [x for x in rows if not x.get("diagnostic") and first <= x["start"] < end]
        trade_pnl = [
            Decimal(x["modeled_settlement_pnl"])
            for x in batch_rows
            if x["kind"] == "score" and x["modeled_settlement_pnl"] is not None
        ]
        print(
            "paper results",
            {
                "spike_blocks": sum(bool(x.get("spike_blocks")) for x in batch_rows),
                "settled_fills": len(trade_pnl),
                "wins": sum(x > 0 for x in trade_pnl),
                "losses": sum(x < 0 for x in trade_pnl),
                "modeled_net_pnl": str(sum(trade_pnl, Decimal(0))) if trade_pnl else None,
            },
            flush=True,
        )
        print(
            "scored windows",
            len(scores),
            "mean Brier",
            {
                k: {
                    "n": sum(k in s for s in scores),
                    "brier": float(mean(Decimal(s[k]) for s in scores if k in s)),
                    "matched_market_brier": float(
                        mean(Decimal(s["market"]) for s in scores if k in s)
                    ),
                }
                for k in sorted(set().union(*scores))
            },
            flush=True,
        )


if __name__ == "__main__":
    main()
