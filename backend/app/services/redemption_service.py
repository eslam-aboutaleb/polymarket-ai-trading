"""
On-Chain Auto-Redeem Service (plan 05).

Automatically claims winning shares on resolved Polymarket markets
instead of forfeiting them. Runs on a five-minute cycle (mirroring
the position-lifecycle cadence), advisory-locked via the plan-02
scheduler lock when that module is available.

Flow per resolved position with a known winning outcome:

  1. Compute the CTF ``collectionId``:
     ``uint256(keccak256(abi.encode(parentCollectionId=0,
     conditionId, indexSet)))`` with ``indexSet = 1 << winnerIndex``.
  2. Resolve the holder wallet: EOA directly, or the Polymarket
     proxy wallet (``Exchange.getPolyProxyWalletAddress``).
  3. Verify a redeemable balance via
     ``ConditionalTokens.balanceOf(holder, collectionId) > 0``.
  4. Build, sign and send ``redeemPosition(collectionId)`` — one
     market per transaction — and record the tx hash.
  5. Wait for the receipt and mark the attempt confirmed/failed.

Wallet types: EOA (signature_type=0) calls the Conditional Tokens
contract directly. POLY_PROXY (signature_type=1) holds positions in
the proxy contract, so the redeem is executed through the proxy; on
failure the position is marked ``claim_via_ui`` and the user is
notified with a Polymarket deep-link (backend mirror of
``buildPolymarketEventUrl`` from ``frontend/src/utils/urlSafety.ts``).

Safety controls (all module-level constants because ``app/config.py``
is owned by plan 01 this wave):

  - ``REDEEM_DRY_RUN`` — default ``True`` for the first 30 days; the
    operator flips it after validating against live resolved markets.
    Dry-run builds and logs the transaction but never broadcasts it.
  - ``MAX_REDEEM_VALUE_PER_TX`` — per-transaction value cap (winning
    shares pay $1 each, so position size == redeem value in USDC).
  - ``DAILY_REDEEM_CAP_PER_USER`` — per-user daily attempt cap.
  - ``MIN_GAS_BALANCE_MATIC`` — gas guard; insufficient MATIC
    triggers a user notification instead of a silent failure.

Every attempt (dry-run, submitted, confirmed, failed, claim_via_ui)
is written to the ``RedemptionAttempt`` audit table.

Security: signing uses ``app.security.credential_store.load_wallet_credentials``;
the private key is never logged (ruff bandit S rules are enforced).
"""

import asyncio
import contextlib
import json
import logging
import math
import re
import urllib.parse
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session
from web3 import Web3

from app.models.redemption_attempt import (
    REDEEM_STATUS_CLAIM_VIA_UI,
    REDEEM_STATUS_CONFIRMED,
    REDEEM_STATUS_FAILED,
    REDEEM_STATUS_PENDING,
    REDEEM_STATUS_SUBMITTED,
    RedemptionAttempt,
)
from app.security.credential_store import CredentialStoreError, load_wallet_credentials
from app.utils.database import SessionLocal
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

_redeem_task: asyncio.Task | None = None

# ── Module-level configuration (config.py is owned by plan 01) ──

REDEEM_CHECK_INTERVAL = 300  # 5 minutes, mirrors LIFECYCLE_CHECK_INTERVAL
REDEEM_DRY_RUN = True  # operator flips after 30 days of dry-run validation
MAX_REDEEM_VALUE_PER_TX = 10_000.0  # USDC value cap per redemption tx
DAILY_REDEEM_CAP_PER_USER = 50  # live (submitted/confirmed) attempts per 24h
MIN_GAS_BALANCE_MATIC = 0.05  # MATIC the signing EOA needs for gas
REDEEM_RECEIPT_TIMEOUT = 120  # seconds to wait for a tx receipt
WINNING_OUTCOME_PRICE_THRESHOLD = 0.99  # outcome price that marks a winner
POLYGON_CHAIN_ID = 137

# Polygon RPC endpoints (fallback list, mirrors copy_trade_service.py:76-81).
_POLYGON_RPCS = [
    "https://polygon.drpc.org",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]

# CTF Exchange (neg-risk) and Conditional Tokens (CTF) on Polygon.
_CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
_CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# Deep-link builder constants (mirror frontend/src/utils/urlSafety.ts).
_POLYMARKET_BASE_URL = "https://polymarket.com"
_SAFE_SEGMENT_PATTERN = re.compile(r"^[a-zA-Z0-9._~-]+$")

# Minimal ABIs. redeemPosition burns the caller's winning position
# tokens and pays out USDC; balanceOf reports the position balance
# of a holder for a collectionId.
_REDEEM_ABI = [
    {
        "inputs": [{"name": "collectionId", "type": "uint256"}],
        "name": "redeemPosition",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_CTF_ABI = [
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "collectionId", "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    *_REDEEM_ABI,
]

_EXCHANGE_ABI = [
    {
        "inputs": [{"name": "_addr", "type": "address"}],
        "name": "getPolyProxyWalletAddress",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    *_REDEEM_ABI,
]

# Cross-plan integration (defensive): plan 02's scheduler lock and
# plan 09's alert service. Both degrade gracefully when absent —
# unlocked execution and log-only notification respectively.
try:
    from app.services.alert_service import dispatch as dispatch_alert  # plan 09
except ImportError:
    dispatch_alert = None  # type: ignore[assignment]
    logger.warning("alert_service (plan 09) not available; redemption notifications are log-only")

try:
    from app.utils.scheduler_lock import (  # plan 02
        acquire_scheduler_lock,
        release_scheduler_lock,
        scheduler_heartbeat,
    )
except ImportError:
    acquire_scheduler_lock = None  # type: ignore[assignment]
    release_scheduler_lock = None  # type: ignore[assignment]
    scheduler_heartbeat = None  # type: ignore[assignment]
    logger.warning(
        "scheduler_lock (plan 02) not available; redemption cycles run without an advisory lock"
    )


# ── Pure helpers ─────────────────────────────────────────────────


def compute_collection_id(condition_id: str, winner_outcome_index: int) -> str:
    """Compute the CTF collectionId for a market's winning outcome.

    ``collectionId = uint256(keccak256(abi.encode(
    parentCollectionId=0, conditionId, indexSet)))`` where
    ``indexSet = 1 << winner_outcome_index`` for binary markets
    (and ``1 << i`` for outcome ``i`` on multi-outcome markets).

    Args:
        condition_id: 32-byte hex conditionId (with or without 0x).
        winner_outcome_index: Index of the winning outcome.

    Returns:
        The 32-byte collectionId as a hex string (no 0x prefix).

    Raises:
        ValueError: If the conditionId is not exactly 32 bytes.
    """
    cid_hex = condition_id.strip().removeprefix("0x")
    if len(cid_hex) != 64:
        raise ValueError(f"conditionId must be 32 bytes hex, got {len(cid_hex) // 2} bytes")
    condition_bytes = bytes.fromhex(cid_hex)
    index_set = 1 << winner_outcome_index
    # abi.encode pads every argument to 32 bytes:
    # parentCollectionId (uint256 0) + conditionId (bytes32) + indexSet (uint256).
    encoded = (b"\x00" * 32) + condition_bytes + index_set.to_bytes(32, "big")
    return Web3.keccak(encoded).hex()


def determine_winner_index(outcome_prices: Any) -> int | None:
    """Index of the winning outcome, or None when no outcome is decisive.

    A resolved market prices the winning outcome at ~1.00 and every
    other outcome at ~0.00. Anything below the threshold is treated
    as unresolved so a live market is never auto-redeemed.

    Args:
        outcome_prices: List of outcome price strings/floats.

    Returns:
        The winning outcome index, or None.
    """
    if isinstance(outcome_prices, str):
        try:
            outcome_prices = json.loads(outcome_prices)
        except ValueError:
            outcome_prices = None
    if not isinstance(outcome_prices, (list, tuple)):
        return None
    for index, raw in enumerate(outcome_prices):
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price >= WINNING_OUTCOME_PRICE_THRESHOLD:
            return index
    return None


def _sanitize_url_segment(value: str | None) -> str | None:
    """Validate and URI-encode a Polymarket URL path segment."""
    if not value:
        return None
    trimmed = value.strip()
    if not trimmed or len(trimmed) > 180 or not _SAFE_SEGMENT_PATTERN.match(trimmed):
        return None
    return urllib.parse.quote(trimmed, safe="")


def build_polymarket_event_url(event_slug: str | None, market_slug: str | None) -> str | None:
    """Backend mirror of frontend ``buildPolymarketEventUrl``.

    Mirrors ``frontend/src/utils/urlSafety.ts``: slugs must match a
    conservative safe-segment pattern and are URI-encoded; anything
    unsafe yields None rather than a partially-valid URL.

    Args:
        event_slug: Optional event slug.
        market_slug: Optional market slug.

    Returns:
        The deep-link URL, or None when no safe slug is available.
    """
    safe_event = _sanitize_url_segment(event_slug)
    safe_market = _sanitize_url_segment(market_slug)
    if safe_event and safe_market:
        return f"{_POLYMARKET_BASE_URL}/event/{safe_event}/{safe_market}"
    if safe_market:
        return f"{_POLYMARKET_BASE_URL}/event/{safe_market}"
    if safe_event:
        return f"{_POLYMARKET_BASE_URL}/event/{safe_event}"
    return None


def _to_float(value: Any, default: float | None = None) -> float | None:
    """Coerce an upstream value to a finite float (None when unusable)."""
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _is_valid_condition_id(condition_id: Any) -> bool:
    """True when the value is a 32-byte hex string."""
    if not isinstance(condition_id, str):
        return False
    cid = condition_id.strip().removeprefix("0x")
    return len(cid) == 64 and all(c in "0123456789abcdefABCDEF" for c in cid)


def _parse_redeemable_position(pos: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a redeemable-position record from an upstream position.

    Handles both upstream shapes: ``market`` as a dict (Gamma-style
    nested market object) and ``market`` as a plain condition-id
    string (Data API normalization). A position is redeemable only
    when it carries a valid conditionId, a positive size, and a
    decisive winning outcome (outcomePrices >= 0.99). An explicit
    ``resolved``/``closed`` flag, when present, must be truthy.

    Args:
        pos: Raw position dict from the Polymarket positions endpoint.

    Returns:
        A redeemable-position record, or None when not redeemable.
    """
    market_data = pos.get("market")
    if isinstance(market_data, dict):
        has_flag = "resolved" in market_data or "closed" in market_data
        resolved_flag = (
            bool(market_data.get("resolved") or market_data.get("closed")) if has_flag else None
        )
        title = market_data.get("question") or market_data.get("title") or "Unknown"
        outcome_prices = market_data.get("outcomePrices") or market_data.get("outcome_prices")
        market_slug = market_data.get("marketSlug") or market_data.get("market_slug")
        event_slug = market_data.get("eventSlug") or market_data.get("event_slug")
        nested_market_id = (
            market_data.get("conditionId")
            or market_data.get("condition_id")
            or market_data.get("id")
            or ""
        )
    else:
        has_flag = "resolved" in pos or "closed" in pos
        resolved_flag = bool(pos.get("resolved") or pos.get("closed")) if has_flag else None
        title = pos.get("title") or pos.get("question") or "Unknown"
        outcome_prices = pos.get("outcomePrices") or pos.get("outcome_prices")
        market_slug = pos.get("market_slug") or pos.get("marketSlug")
        event_slug = pos.get("event_slug") or pos.get("eventSlug")
        nested_market_id = ""

    if resolved_flag is False:
        return None

    condition_id = (
        pos.get("conditionId")
        or pos.get("condition_id")
        or (market_data if isinstance(market_data, str) else "")
        or nested_market_id
    )
    if not _is_valid_condition_id(condition_id):
        return None

    size = _to_float(pos.get("size"))
    if size is None or size <= 0:
        return None

    winner_index = determine_winner_index(outcome_prices)
    if winner_index is None:
        return None

    return {
        "market_id": str(condition_id).strip(),
        "condition_id": str(condition_id).strip(),
        "title": title,
        "size": size,
        "winner_index": winner_index,
        "market_slug": market_slug,
        "event_slug": event_slug,
    }


# ── Notification (plan 09 defensive integration) ───────────────


def _notify_user(
    user_id: int,
    event_type: str,
    message: str,
    deep_link: str | None = None,
) -> None:
    """Emit a user notification via the plan-09 alert service.

    Uses the alert catalog's ``redeem_available`` /
    ``redeem_failed`` event types. ``dispatch`` is a coroutine
    and this helper runs inside a worker thread (web3 calls
    are blocking), so it is driven with ``asyncio.run`` in
    inline-delivery mode: background delivery tasks are
    loop-bound and would be cancelled when a temporary loop
    closes. Falls back to log-only when plan 09 is absent or
    delivery fails, so a notification outage can never block
    the redemption flow itself.
    """
    payload: dict[str, Any] = {"message": message}
    if deep_link:
        payload["deep_link"] = deep_link
    if dispatch_alert is not None:
        try:
            asyncio.run(dispatch_alert(event_type, user_id, payload, background=False))
            return
        except Exception as exc:
            logger.warning("alert_service dispatch failed for user %d: %s", user_id, exc)
    logger.info(
        "User %d notification — %s: %s%s",
        user_id,
        event_type,
        message,
        f" [link: {deep_link}]" if deep_link else "",
    )


# ── Web3 plumbing ────────────────────────────────────────────────


def _connect_web3() -> Any:
    """Connect to the first reachable Polygon RPC from the fallback list."""
    for rpc in _POLYGON_RPCS:
        with contextlib.suppress(Exception):
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            if w3.is_connected():
                return w3
    logger.warning("Could not connect to any Polygon RPC for redemption")
    return None


def _resolve_proxy_address(w3: Any, eoa_address: str) -> str | None:
    """Resolve the Polymarket proxy wallet for an EOA, if one exists.

    Calls ``Exchange.getPolyProxyWalletAddress(eoa)`` on-chain. A
    zero address means the EOA holds positions directly (no proxy).
    """
    try:
        exchange = w3.eth.contract(
            address=Web3.to_checksum_address(_CTF_EXCHANGE), abi=_EXCHANGE_ABI
        )
        proxy = exchange.functions.getPolyProxyWalletAddress(
            Web3.to_checksum_address(eoa_address)
        ).call()
    except Exception as exc:
        logger.warning("Proxy lookup failed for %s: %s", eoa_address, exc)
        return None
    if not proxy or proxy == _ZERO_ADDRESS:
        return None
    return proxy


def _redeemable_balance(w3: Any, holder: str, collection_id: str) -> int:
    """Return the holder's redeemable position balance for a collectionId."""
    ctf = w3.eth.contract(address=Web3.to_checksum_address(_CONDITIONAL_TOKENS), abi=_CTF_ABI)
    return int(ctf.functions.balanceOf(Web3.to_checksum_address(holder), collection_id).call())


def _build_redeem_tx(
    w3: Any, sender: str, target_address: str, collection_id: str
) -> dict[str, Any]:
    """Build the redeemPosition(collectionId) transaction.

    The nonce is fetched fresh for every transaction; a failed send
    is never retried with the same nonce (plan risk: never resend a
    tx with the same nonce blindly).
    """
    sender_cs = Web3.to_checksum_address(sender)
    contract = w3.eth.contract(address=Web3.to_checksum_address(target_address), abi=_REDEEM_ABI)
    return contract.functions.redeemPosition(collection_id).build_transaction(
        {
            "from": sender_cs,
            "nonce": w3.eth.get_transaction_count(sender_cs),
            "gasPrice": w3.eth.gas_price,
            "chainId": POLYGON_CHAIN_ID,
        }
    )


def _sign_and_send(w3: Any, private_key: str, tx: dict[str, Any]) -> str:
    """Sign and broadcast a transaction; return the tx hash hex.

    The private key is passed straight to web3 signing and is never
    logged or persisted anywhere else.
    """
    signed = w3.eth.account.sign_transaction(tx, private_key)
    return Web3.to_hex(w3.eth.send_raw_transaction(signed.raw_transaction))


# ── Persistence ──────────────────────────────────────────────────


def _record_attempt(
    db: Session,
    user_id: int,
    market_id: str,
    collection_id: str,
    status: str,
    amount: float,
    tx_hash: str | None = None,
    error: str | None = None,
) -> RedemptionAttempt:
    """Write one redemption attempt to the audit table."""
    attempt = RedemptionAttempt(
        user_id=user_id,
        market_id=market_id,
        collection_id=collection_id,
        tx_hash=tx_hash,
        status=status,
        amount=amount,
        error=error,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    logger.info(
        "Redemption attempt user=%d market=%s status=%s tx=%s",
        user_id,
        market_id,
        status,
        tx_hash,
    )
    return attempt


def _daily_attempt_count(db: Session, user_id: int) -> int:
    """Count live (submitted/confirmed) redemption attempts in the last 24h."""
    cutoff = utc_now() - timedelta(hours=24)
    try:
        return (
            db.query(RedemptionAttempt)
            .filter(
                RedemptionAttempt.user_id == user_id,
                RedemptionAttempt.status.in_((REDEEM_STATUS_SUBMITTED, REDEEM_STATUS_CONFIRMED)),
                RedemptionAttempt.created_at >= cutoff,
            )
            .count()
        )
    except Exception as exc:
        logger.warning("Daily redeem count query failed for user %d: %s", user_id, exc)
        return 0


# ── Core redemption flow ─────────────────────────────────────────


def _redeem_failure(
    db: Session,
    user_id: int,
    market_id: str,
    collection_id: str,
    amount: float,
    position: dict[str, Any],
    proxy_address: str | None,
    exc: Exception,
) -> RedemptionAttempt:
    """Record a failed transaction build/send.

    The proxy redemption path is unverified (plan risk): a
    POLY_PROXY failure falls back to a manual-claim deep-link
    notification instead of failing silently. EOA failures are
    recorded as plain failures.
    """
    error_msg = str(exc)
    logger.error(
        "Redeem tx failed for user %d market %s: %s",
        user_id,
        market_id,
        error_msg,
    )
    if proxy_address:
        deep_link = build_polymarket_event_url(
            position.get("event_slug"), position.get("market_slug")
        )
        _notify_user(
            user_id,
            "redeem_available",
            f"Automatic redemption of '{position.get('title', 'Unknown')}' "
            "failed through the proxy wallet. Claim the payout via the "
            "Polymarket UI.",
            deep_link=deep_link,
        )
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_CLAIM_VIA_UI,
            amount,
            error=error_msg,
        )
    return _record_attempt(
        db,
        user_id,
        market_id,
        collection_id,
        REDEEM_STATUS_FAILED,
        amount,
        error=error_msg,
    )


def _execute_redeem(
    db: Session,
    user_id: int,
    wallet_address: str,
    position: dict[str, Any],
    w3: Any | None = None,
    dry_run: bool | None = None,
) -> RedemptionAttempt | None:
    """Execute the on-chain redemption for one resolved position.

    Synchronous core of the service: every web3 call happens here so
    tests can inject a mocked provider. Flow: safety caps → RPC
    connect → credentials → proxy resolution → balance verification →
    tx build → (dry-run stop) → gas guard → sign + send → receipt.

    Args:
        db: Open database session for attempt records.
        user_id: Owner of the position.
        wallet_address: Signing wallet (EOA) address.
        position: Record from ``_parse_redeemable_position``.
        w3: Optional injected web3 provider (tests).
        dry_run: Optional dry-run override (defaults to REDEEM_DRY_RUN).

    Returns:
        The recorded attempt, or None when nothing is redeemable
        on-chain (zero balance).
    """
    if dry_run is None:
        dry_run = REDEEM_DRY_RUN

    market_id = position["market_id"]
    condition_id = position["condition_id"]
    winner_index = position["winner_index"]
    amount = position["size"]
    title = position.get("title", "Unknown")

    collection_id = compute_collection_id(condition_id, winner_index)

    # Per-user daily redeem cap (live attempts only).
    if _daily_attempt_count(db, user_id) >= DAILY_REDEEM_CAP_PER_USER:
        logger.warning("Daily redeem cap reached for user %d", user_id)
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_FAILED,
            amount,
            error="daily redeem cap reached",
        )

    # Per-transaction value cap (winning shares pay $1 each).
    if amount > MAX_REDEEM_VALUE_PER_TX:
        logger.warning(
            "Position value %.2f exceeds per-tx cap for user %d market %s",
            amount,
            user_id,
            market_id,
        )
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_FAILED,
            amount,
            error=f"position value {amount} exceeds per-tx cap {MAX_REDEEM_VALUE_PER_TX}",
        )

    if w3 is None:
        w3 = _connect_web3()
    if w3 is None:
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_FAILED,
            amount,
            error="no Polygon RPC reachable",
        )

    try:
        credentials = load_wallet_credentials(wallet_address)
    except CredentialStoreError as exc:
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_FAILED,
            amount,
            error=f"credential store error: {exc}",
        )
    if not credentials:
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_FAILED,
            amount,
            error="no wallet credentials stored",
        )

    # Resolve the holder: proxy wallet when one exists, else the EOA.
    proxy_address = _resolve_proxy_address(w3, wallet_address)
    holder = proxy_address or wallet_address
    wallet_type = "POLY_PROXY" if proxy_address else "EOA"

    # Verify a redeemable balance before building any transaction.
    balance = _redeemable_balance(w3, holder, collection_id)
    if balance <= 0:
        logger.debug(
            "No redeemable balance for user %d market %s (holder=%s)",
            user_id,
            market_id,
            holder,
        )
        return None

    logger.info(
        "Redeemable position user=%d market=%s collectionId=%s balance=%s wallet=%s",
        user_id,
        market_id,
        collection_id,
        balance,
        wallet_type,
    )

    # EOA: call the Conditional Tokens contract directly.
    # POLY_PROXY: execute the redeem through the proxy contract.
    target_address = proxy_address or _CONDITIONAL_TOKENS

    try:
        tx = _build_redeem_tx(w3, wallet_address, target_address, collection_id)
    except Exception as exc:
        return _redeem_failure(
            db,
            user_id,
            market_id,
            collection_id,
            amount,
            position,
            proxy_address,
            exc,
        )

    if dry_run:
        logger.info(
            "DRY RUN: would send redeemPosition(collectionId=%s) from %s to %s "
            "(nonce=%s, wallet=%s)",
            collection_id,
            wallet_address,
            target_address,
            tx.get("nonce"),
            wallet_type,
        )
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_PENDING,
            amount,
        )

    # Gas guard: the signing EOA pays the transaction fee.
    gas_balance = w3.eth.get_balance(Web3.to_checksum_address(wallet_address))
    if gas_balance < Web3.to_wei(MIN_GAS_BALANCE_MATIC, "ether"):
        deep_link = build_polymarket_event_url(
            position.get("event_slug"), position.get("market_slug")
        )
        _notify_user(
            user_id,
            "redeem_available",
            f"Market '{title}' is resolved but the wallet lacks MATIC to pay "
            "redemption gas. Add MATIC to claim the payout.",
            deep_link=deep_link,
        )
        return _record_attempt(
            db,
            user_id,
            market_id,
            collection_id,
            REDEEM_STATUS_FAILED,
            amount,
            error="insufficient MATIC for gas",
        )

    try:
        tx_hash = _sign_and_send(w3, credentials["private_key"], tx)
    except Exception as exc:
        return _redeem_failure(
            db,
            user_id,
            market_id,
            collection_id,
            amount,
            position,
            proxy_address,
            exc,
        )

    attempt = _record_attempt(
        db,
        user_id,
        market_id,
        collection_id,
        REDEEM_STATUS_SUBMITTED,
        amount,
        tx_hash=tx_hash,
    )

    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=REDEEM_RECEIPT_TIMEOUT)
        if receipt.get("status") == 1:
            attempt.status = REDEEM_STATUS_CONFIRMED
            attempt.error = None
            logger.info(
                "Redemption confirmed user=%d market=%s tx=%s",
                user_id,
                market_id,
                tx_hash,
            )
        else:
            attempt.status = REDEEM_STATUS_FAILED
            attempt.error = (
                f"transaction reverted on-chain (receipt status={receipt.get('status')})"
            )
            logger.error(
                "Redemption reverted user=%d market=%s tx=%s",
                user_id,
                market_id,
                tx_hash,
            )
    except Exception as exc:
        # Submission succeeded but the receipt is not available yet;
        # the attempt stays 'submitted' and the cycle retries nothing.
        attempt.error = f"receipt not available: {exc}"
        logger.warning(
            "Redemption receipt pending user=%d market=%s tx=%s: %s",
            user_id,
            market_id,
            tx_hash,
            exc,
        )

    db.commit()
    return attempt


def _run_redeem_in_thread(
    user_id: int,
    wallet_address: str,
    position: dict[str, Any],
    dry_run: bool | None = None,
) -> RedemptionAttempt | None:
    """Thread entry point for ``_execute_redeem``.

    SQLAlchemy sessions are not thread-safe, so the worker thread
    opens its own session instead of sharing the caller's.
    """
    db = SessionLocal()
    try:
        return _execute_redeem(db, user_id, wallet_address, position, dry_run=dry_run)
    finally:
        db.close()


# ── Async orchestration ──────────────────────────────────────────


async def _redeem_user_positions(user_id: int, wallet_address: str) -> list[RedemptionAttempt]:
    """Redeem every redeemable position for one user."""
    from app.services.polymarket_service import get_polymarket_service

    service = get_polymarket_service()
    try:
        positions = await service.get_positions(wallet_address)
    except Exception as exc:
        logger.error("Error fetching positions for user %d: %s", user_id, exc)
        return []

    attempts: list[RedemptionAttempt] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        parsed = _parse_redeemable_position(pos)
        if parsed is None:
            continue
        try:
            attempt = await asyncio.to_thread(
                _run_redeem_in_thread, user_id, wallet_address, parsed
            )
        except Exception as exc:
            logger.error(
                "Redemption failed for user %d market %s: %s",
                user_id,
                parsed["market_id"],
                exc,
            )
            continue
        if attempt is not None:
            attempts.append(attempt)
    return attempts


async def run_redeem_cycle() -> dict[str, Any]:
    """Run one redemption cycle across all users.

    The background manager holds the plan-02 advisory lock for
    the whole loop, so concurrent workers never run overlapping
    cycles; a direct call executes a single unlocked cycle.
    """
    summary: dict[str, Any] = {
        "users_checked": 0,
        "attempts": 0,
        "submitted": 0,
        "confirmed": 0,
        "failed": 0,
        "claim_via_ui": 0,
        "dry_run": REDEEM_DRY_RUN,
        "checked_at": utc_now().isoformat(),
    }
    db = SessionLocal()
    try:
        from app.models.user import User

        user_rows = db.query(User.id, User.wallet_address).all()
    finally:
        db.close()

    for user_id, wallet_address in user_rows:
        summary["users_checked"] += 1
        try:
            attempts = await _redeem_user_positions(user_id, wallet_address)
        except Exception as exc:
            logger.error("Redemption cycle failed for user %d: %s", user_id, exc)
            continue
        for attempt in attempts:
            summary["attempts"] += 1
            if attempt.status == REDEEM_STATUS_SUBMITTED:
                summary["submitted"] += 1
            elif attempt.status == REDEEM_STATUS_CONFIRMED:
                summary["confirmed"] += 1
            elif attempt.status == REDEEM_STATUS_FAILED:
                summary["failed"] += 1
            elif attempt.status == REDEEM_STATUS_CLAIM_VIA_UI:
                summary["claim_via_ui"] += 1
    return summary


async def redeem_market_for_user(
    user_id: int,
    wallet_address: str,
    market_id: str,
    winner_outcome_index: int | None = None,
) -> RedemptionAttempt | None:
    """Manually trigger on-chain redemption for one resolved market.

    Fetches the user's positions upstream, locates the market by
    condition id or slug, validates the winning outcome against the
    market's resolved outcomePrices, and executes the redemption in a
    worker thread (web3 calls are blocking).

    Args:
        user_id: Owner of the position.
        wallet_address: Signing wallet (EOA) address.
        market_id: Condition id or market slug of the target market.
        winner_outcome_index: Optional override of the winning
            outcome index (defaults to the outcomePrices-derived
            winner).

    Returns:
        The recorded attempt, or None when no redeemable position on
        that market exists.
    """
    from app.services.polymarket_service import get_polymarket_service

    service = get_polymarket_service()
    positions = await service.get_positions(wallet_address)

    target: dict[str, Any] | None = None
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        parsed = _parse_redeemable_position(pos)
        if parsed is None:
            continue
        if parsed["market_id"] == market_id or parsed.get("market_slug") == market_id:
            target = parsed
            break

    if target is None:
        logger.info("No redeemable position for user %d on market %s", user_id, market_id)
        return None

    if winner_outcome_index is not None:
        target["winner_index"] = int(winner_outcome_index)

    return await asyncio.to_thread(_run_redeem_in_thread, user_id, wallet_address, target)


async def _redeem_loop():
    """Background loop: run a redemption cycle every interval.

    Follows the plan-02 scheduler pattern: acquire the named
    advisory lock before entering the loop (a second worker
    process skips start), refresh the dead-man's-switch
    heartbeat every cycle, and release the lock on shutdown.
    When plan 02 is absent the loop runs unlocked.
    """
    if acquire_scheduler_lock is not None and not acquire_scheduler_lock("redemption"):
        return
    logger.info(
        "Auto-redeem manager started (interval=%ds, dry_run=%s)",
        REDEEM_CHECK_INTERVAL,
        REDEEM_DRY_RUN,
    )
    try:
        while True:
            try:
                summary = await run_redeem_cycle()
                if summary["attempts"]:
                    logger.info("Redemption cycle summary: %s", summary)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Redemption cycle error: %s", exc)
            if scheduler_heartbeat is not None:
                scheduler_heartbeat("redemption")
            await asyncio.sleep(REDEEM_CHECK_INTERVAL)
    finally:
        if release_scheduler_lock is not None:
            release_scheduler_lock("redemption")
        logger.info("Auto-redeem manager stopped")


async def start_redemption_manager():
    """Start the auto-redeem background task."""
    global _redeem_task
    if _redeem_task and not _redeem_task.done():
        logger.warning("Auto-redeem manager already running")
        return
    _redeem_task = asyncio.create_task(_redeem_loop())
    logger.info("Auto-redeem manager started")


async def stop_redemption_manager():
    """Stop the auto-redeem background task."""
    global _redeem_task
    if _redeem_task and not _redeem_task.done():
        _redeem_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _redeem_task
    _redeem_task = None
    logger.info("Auto-redeem manager stopped")


def get_redemption_metrics() -> dict[str, Any]:
    """Return running status and effective safety thresholds."""
    return {
        "running": _redeem_task is not None and not _redeem_task.done(),
        "check_interval_seconds": REDEEM_CHECK_INTERVAL,
        "dry_run": REDEEM_DRY_RUN,
        "max_redeem_value_per_tx": MAX_REDEEM_VALUE_PER_TX,
        "daily_redeem_cap_per_user": DAILY_REDEEM_CAP_PER_USER,
        "min_gas_balance_matic": MIN_GAS_BALANCE_MATIC,
    }
