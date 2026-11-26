"""A bounded paper-only check each minute with rationale and evidence review."""

import argparse
import json
import subprocess
import time
from pathlib import Path

import btc_paper_benchmark as benchmark
import httpx

from app.services.trading.btc_agent_contract import reference_rationale_conflict

GUIDANCE = {
    "contradiction": " Use reference_available to verify whether the opening value was supplied.",
    "missing_reference": " If the opening reference is unavailable, state that limitation.",
    "weak_support": " State whether the proxy supports an adjustment and mention its uncertainty.",
    "execution_quality": " Do not infer executable liquidity from a forecast.",
    "malformed": " Return exactly the three requested JSON fields and no surrounding prose.",
    "ready": " State why the supplied evidence justifies the size of your forecast adjustment.",
}


def review(rows):
    latest = {x["kind"]: x for x in rows}
    decision = latest.get("decision", {})
    signal = latest.get("forecast", {}).get("response", {})
    result = latest.get("execution", {}).get("order", {})
    reason = decision.get("decision", {}).get("reason") or latest.get("unavailable", {}).get(
        "reason"
    )
    blocks = decision.get("spike_blocks", [])
    reference = (latest.get("venue_evidence") or {}).get("reference")
    conflict = reference_rationale_conflict(signal.get("reason") or "", reference is not None)
    if conflict:
        feedback = "contradiction"
    elif "model_unavailable" in latest:
        feedback = "malformed"
    elif reason == "MISSING_CHAINLINK_OPEN":
        feedback = "missing_reference"
    elif "MODEL_EDGE_UNVERIFIED" in blocks:
        feedback = "weak_support"
    elif (
        blocks
        or "unavailable" in latest
        or latest.get("delayed_depth", {}).get("status")
        in ("expired", "hold", "no_post_decision_quote")
        or result.get("status") in ("abstain", "unfilled", "rejected")
    ):
        feedback = "execution_quality"
    else:
        feedback = "ready"
    return {
        "action": decision.get("effective_action", "NO_DECISION"),
        "reason": reason,
        "blocks": blocks,
        "order_status": result.get("status"),
        "order_reason": result.get("reason"),
        "rationale": signal.get("reason"),
        "feedback": feedback,
        "reference_rationale_conflict": conflict,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--account-config", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    account = json.loads(args.account_config.read_text())[0]
    args.directory.mkdir(mode=0o700, exist_ok=args.resume)
    review_log = args.directory / "review.jsonl"
    completed = [json.loads(x) for x in review_log.read_text().splitlines()] if args.resume else []
    logs = [args.directory / f"{row['minute']}.jsonl" for row in completed]
    first = completed[0]["minute"] if completed else (int(time.time()) // 60 + 1) * 60
    guidance = ""
    if logs:
        prior = review(map(json.loads, logs[-1].read_text().splitlines()))
        guidance = GUIDANCE[prior["feedback"]]
    with httpx.Client(timeout=8, trust_env=False) as client:
        for number in range(len(completed), 10):
            minute = first + 60 * number
            start = minute // 300 * 300
            time.sleep(max(0, minute + 3 - time.time()))
            benchmark.LOG = args.directory / f"{minute}.jsonl"
            logs.append(benchmark.LOG)
            if time.time() >= minute + 40:
                benchmark.save("unavailable", start=start, reason="MISSED_MINUTE_DEADLINE")
            else:
                try:
                    benchmark.forecast(client, start, account, minute=minute, guidance=guidance)
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    httpx.HTTPError,
                    subprocess.TimeoutExpired,
                ) as exc:
                    benchmark.save("unavailable", start=start, reason=str(exc))
            evidence = [json.loads(line) for line in benchmark.LOG.read_text().splitlines()]
            result = review(evidence)
            result.update(minute=minute, window=start, check=number + 1, applied_guidance=guidance)
            with review_log.open("a") as stream:
                stream.write(json.dumps(result) + "\n")
            print(json.dumps(result), flush=True)  # noqa: T201
            guidance = GUIDANCE[result["feedback"]]

        # Final outcomes arrive on five-minute boundaries, not each check.
        end = (first + 540) // 300 * 300 + 300
        while time.time() < end + 180:
            pending = 0
            for path in logs:
                benchmark.LOG = path
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                if any(x["kind"] == "forecast" for x in rows) and not any(
                    x["kind"] == "score" for x in rows
                ):
                    benchmark.score_available(client, account)
                    pending += 1
            if not pending:
                break
            time.sleep(20)


if __name__ == "__main__":
    main()
