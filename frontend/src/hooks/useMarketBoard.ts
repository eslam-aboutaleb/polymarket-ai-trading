/**
 * Paginated market-board feed shared by the Markets board and the Opportunities
 * ("Easy Trades") board.
 *
 * Pages through `marketsService.searchMarkets` (category/query/sort browse) or the
 * offset-paginated `portfolioService.getCombinedMarkets` feed behind
 * `useInfiniteQuery`: the first load is the initial fetch, `loadMore` appends the
 * next page. The first load can pre-fetch several pages and stop early once enough
 * event groups are visible (what Markets' board needs to fill a screen).
 *
 * Callers keep their own row-level state (AI scores, price flashes) and merge the
 * rows they get back; the hook only owns pagination and fetch state.
 *
 * @module hooks/useMarketBoard
 */

import { useCallback, useMemo, useRef, useState } from "react";
import { useInfiniteQuery } from "react-query";
import { marketsService } from "../services/marketsService";
import { portfolioService } from "../services/portfolioService";
import { getApiErrorMessage } from "../utils/apiError";
import { useDismissableError } from "./useDismissableError";

/** Which feed to page through. */
export type MarketBoardSource = "search" | "combined";

/** One page of rows plus the metadata needed to request the next one. */
export interface MarketBoardPage<T> {
  markets: T[];
  total: number;
  /** Row offset this page was requested at. */
  offset: number;
  /** Rows the service returned (before any de-duplication). */
  count: number;
  hasMore: boolean;
}

export interface UseMarketBoardOptions<T> {
  /** Feed to page through (default: the tag/query search feed). */
  source?: MarketBoardSource;
  /** Text query (search feed only). */
  q?: string;
  /** Category tag (search feed only). */
  tag?: string;
  /** Sort key (search feed only). */
  sort?: string;
  /** Rows per request. */
  limit?: number;
  /** Pages fetched on the first load (default 1). */
  initialPages?: number;
  /** Stop the first load early once this many event groups are visible. */
  minVisibleGroups?: number;
  /** Counts visible event groups for the early-stop check. */
  countGroups?: (markets: T[]) => number;
  /** De-duplicates rows across pages; return "" to keep duplicates. */
  rowKey?: (market: T) => string;
  /** Message for a failed first load. */
  errorMessage?: string;
  /** Message for a failed page append (defaults to `errorMessage`). */
  loadMoreErrorMessage?: string;
}

export interface UseMarketBoardResult<T> {
  /** Rows loaded so far, in order. */
  markets: T[];
  /** Total row count reported by the last page. */
  total: number;
  /** True while more pages can be requested. */
  hasMore: boolean;
  /** True during the first load. */
  loading: boolean;
  /** True while a page is being appended. */
  loadingMore: boolean;
  /** Current error message if any. */
  error: string | null;
  /** Hides the error banner until the next failure. */
  dismissError: () => void;
  /** Re-fetches the board. */
  refresh: () => void;
  /** Appends the next page; resolves with the new rows and post-fetch state. */
  loadMore: () => Promise<{ added: T[]; hasMore: boolean; error: string | null }>;
}

export function useMarketBoard<T extends object>(
  options: UseMarketBoardOptions<T> = {},
): UseMarketBoardResult<T> {
  const {
    source = "search",
    q,
    tag,
    sort,
    limit = 60,
    initialPages = 1,
    minVisibleGroups,
    countGroups,
    rowKey,
    errorMessage = "Failed to load markets",
    loadMoreErrorMessage,
  } = options;
  const appendErrorMessage = loadMoreErrorMessage ?? errorMessage;

  // The pagination helpers below must stay referentially stable, so the
  // caller-supplied grouping key is read through a ref.
  const rowKeyRef = useRef(rowKey);
  rowKeyRef.current = rowKey;
  const countGroupsRef = useRef(countGroups);
  countGroupsRef.current = countGroups;

  const fetchPage = useCallback(
    async (offset: number): Promise<MarketBoardPage<T>> => {
      if (source === "combined") {
        const res = await portfolioService.getCombinedMarkets(limit, offset);
        const markets = (res.markets || []) as T[];
        return {
          markets,
          total: res.total,
          offset: res.offset ?? offset,
          count: res.count ?? markets.length,
          hasMore: res.has_more,
        };
      }

      const maxPages = Math.max(1, initialPages);
      let merged: T[] = [];
      let nextOffset = offset;
      let total = 0;
      let hasMore = false;
      let consumed = 0;

      for (let page = 0; page < maxPages; page += 1) {
        const res = await marketsService.searchMarkets({
          q: q || undefined,
          tag: tag || undefined,
          limit,
          offset: nextOffset,
          sort,
        });
        const rows = (res.markets || []) as T[];
        merged = merged.concat(rows);
        total = res.total;
        hasMore = res.has_more;
        consumed += rows.length;

        const isLastRequestedPage = page + 1 >= maxPages;
        const visibleGroups = countGroupsRef.current?.(merged) ?? 0;
        const filledScreen = minVisibleGroups != null && visibleGroups >= minVisibleGroups;
        if (isLastRequestedPage || filledScreen || !hasMore || rows.length === 0) break;

        nextOffset += rows.length;
      }

      return { markets: merged, total, offset: nextOffset, count: consumed, hasMore };
    },
    [source, q, tag, limit, sort, initialPages, minVisibleGroups],
  );

  const query = useInfiniteQuery<MarketBoardPage<T>>(
    ["market-board", source, q ?? null, tag ?? null, sort ?? null, limit, initialPages],
    ({ pageParam = 0 }) => fetchPage(pageParam as number),
    {
      getNextPageParam: (lastPage) =>
        lastPage.hasMore && lastPage.count > 0 ? lastPage.offset + lastPage.count : undefined,
    },
  );

  const { data, fetchNextPage, refetch, isLoading, isFetchingNextPage, hasNextPage } = query;

  // Read the current page count at click time so `loadMore` can return only
  // the rows it appended.
  const dataRef = useRef(data);
  dataRef.current = data;

  const pages = data?.pages ?? [];

  const markets = useMemo(() => {
    const key = rowKeyRef.current;
    // eslint-disable-next-line react-hooks/exhaustive-deps
    if (!key) return pages.flatMap((page) => page.markets);
    const seen = new Set<string>();
    const rows: T[] = [];
    for (const page of pages) {
      for (const row of page.markets) {
        const rowKeyValue = key(row);
        if (rowKeyValue) {
          if (seen.has(rowKeyValue)) continue;
          seen.add(rowKeyValue);
        }
        rows.push(row);
      }
    }
    return rows;
  }, [pages]);

  const total = pages.length > 0 ? pages[pages.length - 1].total : 0;

  // A failed first load and a failed page append share one dismissable banner;
  // only their copy differs. react-query keeps the failure until the next
  // successful fetch, so dismissal is tracked against `errorUpdatedAt`.
  const [dismissedErrorAt, setDismissedErrorAt] = useState(0);
  const error =
    query.isError && query.errorUpdatedAt !== dismissedErrorAt
      ? getApiErrorMessage(query.error, query.isRefetchError ? appendErrorMessage : errorMessage)
      : null;
  const dismissError = useCallback(
    () => setDismissedErrorAt(query.errorUpdatedAt),
    [query.errorUpdatedAt],
  );
  const refresh = useCallback(() => {
    void refetch();
  }, [refetch]);

  const loadMore = useCallback(async () => {
    const before = dataRef.current?.pages.length ?? 0;
    const result = await fetchNextPage();
    const added = (result.data?.pages ?? []).slice(before).flatMap((page) => page.markets);
    return {
      added,
      hasMore: result.hasNextPage ?? false,
      error: result.isRefetchError ? getApiErrorMessage(result.error, appendErrorMessage) : null,
    };
  }, [fetchNextPage, appendErrorMessage]);

  return {
    markets,
    total,
    hasMore: hasNextPage ?? false,
    loading: isLoading,
    loadingMore: isFetchingNextPage,
    error,
    dismissError,
    refresh,
    loadMore,
  };
}
