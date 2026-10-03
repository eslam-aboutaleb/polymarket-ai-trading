/**
 * Builders for external Polymarket and Polygonscan links that refuse unsafe input.
 *
 * Every builder validates before interpolating: slugs must match a conservative safe-segment pattern
 * and are URI-encoded, wallets must be 20-byte hex addresses, and tx hashes must be 32-byte hex.
 * Anything that fails validation returns `null` rather than a partially-valid URL.
 *
 * @module utils/urlSafety
 */

const POLYMARKET_BASE_URL = "https://polymarket.com";
const POLYGONSCAN_BASE_URL = "https://polygonscan.com";

const SAFE_SEGMENT_PATTERN = /^[a-zA-Z0-9._~-]+$/;
const ETH_ADDRESS_PATTERN = /^0x[a-fA-F0-9]{40}$/;
const TX_HASH_PATTERN = /^0x[a-fA-F0-9]{64}$/;

function sanitizePathSegment(value: string | null | undefined): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (trimmed.length === 0 || trimmed.length > 180) return null;
  if (!SAFE_SEGMENT_PATTERN.test(trimmed)) return null;
  return encodeURIComponent(trimmed);
}

export function buildPolymarketEventUrl(
  eventSlug?: string | null,
  marketSlug?: string | null,
): string | null {
  const safeEvent = sanitizePathSegment(eventSlug || null);
  const safeMarket = sanitizePathSegment(marketSlug || null);

  if (safeEvent && safeMarket) {
    return `${POLYMARKET_BASE_URL}/event/${safeEvent}/${safeMarket}`;
  }
  if (safeMarket) {
    return `${POLYMARKET_BASE_URL}/event/${safeMarket}`;
  }
  if (safeEvent) {
    return `${POLYMARKET_BASE_URL}/event/${safeEvent}`;
  }
  return null;
}

export function buildPolymarketProfileUrl(wallet: string): string | null {
  if (!ETH_ADDRESS_PATTERN.test(wallet)) return null;
  return `${POLYMARKET_BASE_URL}/profile/${wallet.toLowerCase()}`;
}

export function buildPolygonscanAddressUrl(wallet: string): string | null {
  if (!ETH_ADDRESS_PATTERN.test(wallet)) return null;
  return `${POLYGONSCAN_BASE_URL}/address/${wallet}`;
}

export function buildPolygonscanTxUrl(txHash: string): string | null {
  if (!TX_HASH_PATTERN.test(txHash)) return null;
  return `${POLYGONSCAN_BASE_URL}/tx/${txHash}`;
}
