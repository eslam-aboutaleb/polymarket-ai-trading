"""Capture the public market stream and replay a buy without submitting an order."""

import argparse
import json
import os
import time
from decimal import Decimal
from pathlib import Path

import httpx
from btc_paper_benchmark import CLOB, OUTCOMES, event, get
from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from app.services.trading.btc_book_replay import replay
from app.services.trading.btc_venue_evidence import taker_coefficient

WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def clock_ms():
    return time.time_ns() // 1_000_000


def capture(path, start, seconds):
    with httpx.Client(timeout=10, trust_env=False) as client:
        market = event(client, start)
        condition = market["conditionId"]
        tokens = json.loads(market["clobTokenIds"])
        info = get(client, CLOB + "/clob-markets/" + condition)
    if json.loads(market["outcomes"]) != list(OUTCOMES) or {
        (str(x["t"]), x["o"]) for x in info["t"]
    } != set(zip(tokens, OUTCOMES, strict=True)):
        raise ValueError("Capture market token identity mismatch")

    rate = taker_coefficient(info, dict(zip(OUTCOMES, tokens, strict=True)))
    if info.get("c", condition) != condition:
        raise ValueError("Capture condition mismatch")

    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        index = 0

        def write(kind, **data):
            nonlocal index
            row = {"kind": kind, "index": index, "received_ms": clock_ms(), **data}
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
            stream.flush()
            index += 1

        write(
            "start",
            condition=condition,
            tokens=tokens,
            start=start,
            end=start + 300,
            tick=str(info["mts"]),
            minimum=str(info["mos"]),
            source=WS,
            venue_taker_rate=str(rate),
        )
        deadline = time.monotonic() + seconds
        try:
            with connect(WS, open_timeout=6, close_timeout=2, max_size=2_000_000) as socket:
                socket.send(
                    json.dumps(
                        {
                            "assets_ids": tokens,
                            "type": "market",
                            "custom_feature_enabled": True,
                        }
                    )
                )
                next_ping = time.monotonic() + 10
                last_received = time.monotonic()
                while time.monotonic() < deadline and stream.tell() < 50_000_000:
                    if time.monotonic() >= next_ping:
                        socket.send("PING")
                        next_ping = time.monotonic() + 10
                    try:
                        frame = socket.recv(timeout=min(1, max(0, deadline - time.monotonic())))
                    except TimeoutError:
                        if time.monotonic() - last_received > 15:
                            raise TimeoutError("Market stream heartbeat timed out") from None
                        continue
                    last_received = time.monotonic()
                    if frame in ("PONG", b"PONG"):
                        write("heartbeat")
                        continue
                    received = clock_ms()
                    try:
                        data = json.loads(frame)
                        for item in data if isinstance(data, list) else [data]:
                            if not isinstance(item, dict):
                                raise ValueError("Invalid event")
                            write("event", event=item, received_ms=received)
                    except (ValueError, UnicodeError):
                        write("gap", reason="malformed_frame")
        except (OSError, TimeoutError, WebSocketException) as exc:
            write("gap", reason=type(exc).__name__)
            raise
        finally:
            write("end")
    return {"path": str(path), "records": index}


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("capture")
    record.add_argument("path", type=Path)
    record.add_argument("--start", type=int)
    record.add_argument("--seconds", type=int, choices=range(1, 301), default=30)
    analyze = sub.add_parser("replay")
    analyze.add_argument("path", type=Path)
    analyze.add_argument("--benchmark-log", type=Path)
    analyze.add_argument("--record-result", action="store_true")
    analyze.add_argument("--token")
    analyze.add_argument("--arrival-ms", type=int)
    analyze.add_argument("--limit", type=Decimal)
    analyze.add_argument("--notional", type=Decimal, default=Decimal("5"))
    args = parser.parse_args()
    if args.command == "capture":
        result = capture(args.path, args.start or int(time.time()) // 300 * 300, args.seconds)
    else:
        if args.path.stat().st_size > 52_000_000:
            raise ValueError("Capture exceeds replay size bound")
        rows = [json.loads(line) for line in args.path.read_text().splitlines()]
        if not rows:
            parser.error("capture is empty")
        if args.record_result and not args.benchmark_log:
            parser.error("--record-result requires --benchmark-log")
        if args.benchmark_log:
            with args.benchmark_log.open() as stream:
                intents = [
                    row
                    for row in map(json.loads, stream)
                    if row["kind"] == "replay_intent"
                    and row["condition"] == rows[0]["condition"]
                    and row["start"] == rows[0]["start"]
                ]
            if len(intents) != 1:
                parser.error(
                    "benchmark log must contain exactly one intent for the captured window"
                )
            intent = intents[0]
            token, arrival = intent["token"], intent["arrival_ms"]
            limit, notional = Decimal(intent["limit"]), Decimal(intent["notional"])
        else:
            if args.token is None or args.arrival_ms is None or args.limit is None:
                parser.error("supply --benchmark-log or --token, --arrival-ms and --limit")
            token, arrival, limit, notional = args.token, args.arrival_ms, args.limit, args.notional
        result = replay(rows, token=token, arrival_ms=arrival, limit=limit, notional=notional)
        if args.record_result:
            record = {
                "kind": "market_replay",
                "start": rows[0]["start"],
                "condition": rows[0]["condition"],
                "capture": str(args.path),
                "arrival_ms": arrival,
                "result": result,
            }
            with args.benchmark_log.open("a") as stream:
                stream.write(json.dumps(record, default=str) + "\n")
    print(json.dumps(result, default=str))  # noqa: T201 - CLI result


if __name__ == "__main__":
    main()
