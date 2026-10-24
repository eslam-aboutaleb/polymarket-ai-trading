"""Actual CLOB v2 I/O. No fallback fills, wallet guessing or submission retries."""

from datetime import UTC, datetime
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from app.services.market_data.polymarket_service import POLYMARKET_CLOB_API


def build_client(wallet: str, stored: dict):
    from eth_account import Account
    from py_clob_client_v2 import ApiCreds, ClobClient

    key = stored["private_key"]
    signer = Account.from_key(key).address
    signature_type = stored.get("signature_type", 0 if signer.lower() == wallet.lower() else 1)
    funder = stored.get("funder") or wallet
    creds = stored.get("clob_creds")
    client = ClobClient(
        host=POLYMARKET_CLOB_API,
        chain_id=137,
        key=key,
        signature_type=signature_type,
        funder=funder,
        creds=ApiCreds(**creds) if creds else None,
        retry_on_error=False,
    )
    if not creds:
        client.set_api_creds(client.create_or_derive_api_key())
    return client


def balance(client, token_id: str | None = None) -> Decimal:
    from py_clob_client_v2 import AssetType, BalanceAllowanceParams

    payload = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL if token_id else AssetType.COLLATERAL,
            token_id=token_id,
        )
    )
    value = Decimal(str(payload["balance"])) / Decimal(1_000_000)
    if not value.is_finite() or value < 0:
        raise ValueError("Invalid authenticated balance")
    return value


def prepare(client, token_id: str, side: str, price: Decimal, shares: Decimal):
    from py_clob_client_v2 import OrderArgs, PartialCreateOrderOptions
    from py_clob_client_v2.config import get_contract_config
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import ExchangeOrderBuilderV2

    tick = client.get_tick_size(token_id)
    tick_d = Decimal(tick)
    price = (price / tick_d).to_integral_value(
        rounding=ROUND_DOWN if side == "BUY" else ROUND_UP,
    ) * tick_d
    if not 0 < price < 1:
        raise ValueError("No valid tick within the authorized price limit")
    neg_risk = client.get_neg_risk(token_id)
    signed = client.create_order(
        OrderArgs(token_id=token_id, side=side, price=float(price), size=float(shares)),
        PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk, version=2),
    )
    config = get_contract_config(137)
    exchange = config.neg_risk_exchange_v2 if neg_risk else config.exchange_v2
    builder = ExchangeOrderBuilderV2(exchange, 137, client.signer)
    order_hash = builder.build_order_hash(builder.build_order_typed_data(signed))
    # The actual signed amounts, rather than float estimates, own reservations.
    actual_shares = Decimal(signed.takerAmount if side == "BUY" else signed.makerAmount) / 1_000_000
    notional = Decimal(signed.makerAmount if side == "BUY" else signed.takerAmount) / 1_000_000
    return signed, order_hash, actual_shares, notional


def submit(client, signed, order_type: str, post_only: bool = False) -> dict:
    return client.post_order(signed, order_type, post_only=post_only)


def cancel(client, order_hash: str) -> bool:
    from py_clob_client_v2 import OrderPayload

    response = client.cancel_order(OrderPayload(orderID=order_hash))
    return isinstance(response, dict) and order_hash in response.get("canceled", [])


def settled_fills(client, order_hash: str, trade_ids: list[str]) -> dict | None:
    """Only settled, attributable authenticated fills prove execution."""
    from py_clob_client_v2 import TradeParams

    shares = Decimal(0)
    notional = Decimal(0)
    seen = set()
    timestamps = []
    for trade_id in set(trade_ids):
        trades = client.get_trades(TradeParams(id=trade_id))
        if not isinstance(trades, list):
            return None
        for trade in trades:
            identity = trade.get("id")
            if identity in seen or identity != trade_id:
                continue
            seen.add(identity)
            if trade.get("status") != "CONFIRMED" or not trade.get("transaction_hash"):
                return None
            try:
                timestamp = datetime.fromtimestamp(float(trade["match_time"]), tz=UTC)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                timestamp = None
            if timestamp is not None:
                timestamps.append(timestamp)
            if trade.get("taker_order_id") == order_hash:
                pieces = [(trade["size"], trade["price"])]
            else:
                pieces = [
                    (maker["matched_amount"], maker["price"])
                    for maker in trade.get("maker_orders", [])
                    if maker.get("order_id") == order_hash
                ]
            for size, price in pieces:
                quantity, execution_price = Decimal(str(size)), Decimal(str(price))
                if not quantity.is_finite() or not execution_price.is_finite():
                    return None
                if quantity <= 0 or not 0 < execution_price < 1:
                    return None
                shares += quantity
                notional += quantity * execution_price
    if shares <= 0 or len(seen) != len(set(trade_ids)):
        return None
    return {
        "shares": shares,
        "notional": notional,
        "price": notional / shares,
        "filled_at": max(timestamps) if len(timestamps) == len(seen) else None,
    }
