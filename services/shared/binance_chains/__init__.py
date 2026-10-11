"""Canonical Binance web3 chain-id map shared by every Binance integration.

Three deployments talk to the Binance Skills Hub and used to carry their own
chain maps:

* ``services/mcps/research/binance_skills.py`` — the AI path (research MCP
  server, used by llm-chain and cli-agent),
* ``backend/app/api/routes/binance_signals.py`` — the dashboard path.

They disagreed about Solana: the research copy sent ``"CT_501"`` while the
backend route sent ``"501"``, so the two paths could never share a cache key
and one of them always returned an empty result set.

Verified Solana chain id: ``CT_501``
------------------------------------
Live check against the smart-money endpoint on 2026-10-11
(``POST https://web3.binance.com/bapi/defi/v1/public/wallet-direct/buw/wallet/web/signal/smart-money``,
research-style body ``{"smartSignalType": "", "page": 1, "pageSize": 50, "chainId": ...}``)::

    chainId "CT_501" -> {"code": "000000", "data": [50 rows], every row
                         echoing "chainId": "CT_501"}
    chainId "501"    -> {"code": "000000", "data": []}
    chainId "999999" -> {"code": "000000", "data": []}   (deliberately bogus id)

``"501"`` answers exactly like the deliberately bogus ``"999999"`` — an unknown
chain id is silently mapped to "no rows" (HTTP 200, ``code: "000000"``), so an
empty dashboard section is indistinguishable from a bad id. ``"CT_501"`` is the
only Solana id Binance accepts and is the value pinned here. ``1``
(ethereum), ``56`` (bsc), ``8453`` (base) and ``42161`` (arbitrum) were probed
the same way; ``137``/``42161`` return zero rows, which the API reports the
same way, so those ids are carried as-is from the route's map.

Tracked debt (deliberately NOT fixed here)
------------------------------------------
Only the chain map is unified. The two implementations still differ
upstream-side, and both paths are live with pinned request shapes:

* Different request *bodies* to the same smart-money URL — the research module
  sends ``smartSignalType``/``page``/``pageSize`` while the backend route sends
  ``type``/``pageIndex``/``pageSize``.
* Different endpoints for the other concepts: the research module uses
  ``bapi/defi/v1/public/wallet-direct/buw/wallet/web/ranking/*`` paths and the
  backend route uses its own ``ranking/*`` paths for social hype, trending,
  inflow, PnL leaderboard and token data.

Unifying those bodies/endpoints needs its own plan with live verification.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_CHAIN_ID",
    "SOLANA_CHAIN_ID",
    "SUPPORTED_CHAINS",
    "chain_to_platform",
]

# chain name (lower case) -> Binance web3 chainId. Solana is the verified
# ``CT_501`` — see the module docstring for the live-curl evidence.
SUPPORTED_CHAINS: dict[str, str] = {
    "ethereum": "1",
    "polygon": "137",
    "bsc": "56",
    "arbitrum": "42161",
    "base": "8453",
    "solana": "CT_501",
}

# Unknown chain names fall back to ethereum, which is the dashboard's
# documented default and what the route has always sent.
DEFAULT_CHAIN_ID = "1"

# Smart-money flow for this bot is read from Solana, where the highest-signal
# data lives (see ResearchMCPClient._BINANCE_CHAIN).
SOLANA_CHAIN_ID = SUPPORTED_CHAINS["solana"]


def chain_to_platform(chain: str, default: str = DEFAULT_CHAIN_ID) -> str:
    """Map a human-readable chain name onto its Binance web3 chain id.

    Args:
        chain: Chain name in any case, with optional surrounding whitespace.
        default: Chain id to fall back to for unknown/empty names.

    Returns:
        The Binance chain id (the platform identifier Binance's web3 API
        accepts in ``chainId`` fields).
    """
    return SUPPORTED_CHAINS.get((chain or "").strip().lower(), default)
