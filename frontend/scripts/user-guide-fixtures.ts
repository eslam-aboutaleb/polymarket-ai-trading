/** Synthetic documentation data only. No credentials, exchange or paid provider calls. */
import type { Page } from "@playwright/test";

export const wallet = "0x" + "11".repeat(20);
const cid = "0x" + "ab".repeat(32);
const stamp = "2026-10-23T12:00:00Z";
const market = {
  id: cid,
  condition_id: cid,
  question: "Demo: Will the measure pass?",
  slug: "demo-measure",
  outcomes: ["Yes", "No"],
  outcomePrices: ["0.7", "0.3"],
  volume24hr: 2000,
  volume: 8000,
  liquidity: 1500,
  endDate: "2026-11-03T23:59:00Z",
  tokens: [
    { outcome: "Yes", token_id: "11" },
    { outcome: "No", token_id: "22" },
  ],
};
const positions = [
  {
    title: market.question,
    market: cid,
    condition_id: cid,
    asset: "11",
    asset_id: "11",
    outcome: "Yes",
    size: 25,
    avgPrice: 0.4,
    curPrice: 0.7,
    pnl: 7.5,
  },
];
const settings = {
  ai_backend: "llm_chain",
  preferred_llm_provider: "openai",
  preferred_llm_model: "demo-model",
  simulation_mode: true,
  paper_balance: 1000,
  copy_trading_enabled: false,
  risk_mode: "fixed_amount",
  max_position_size: 25,
  daily_loss_limit: 20,
  monthly_loss_limit: 100,
  mirror_percentage: 5,
  fixed_trade_amount: 10,
  require_ai_approval: true,
  daily_digest_enabled: false,
  follow_email_notifications_enabled: false,
  max_drawdown_pct: 10,
  total_loss_halt_pct: 20,
  dynamic_sizing_enabled: false,
  inverse_bot_enabled: false,
  inverse_bot_default_size_mode: "full_notional",
  inverse_bot_fixed_amount: 10,
  inverse_bot_confidence_threshold: 80,
  inverse_bot_cooldown_minutes: 30,
  inverse_bot_max_reversals_per_day: 2,
  kelly_fraction: 0.25,
  trading_halted: false,
  halt_reason: null,
  cooldown_until: null,
  peak_capital: 1000,
  initial_capital: 1000,
  consecutive_wins: 0,
  consecutive_losses: 0,
};
const account = {
  id: 1,
  owner_user_id: 1,
  name: "Demo Personal",
  kind: "personal",
  is_default: true,
  created_at: stamp,
};
const summary = {
  wallet_address: wallet,
  usdc_balance: 1000,
  matic_balance: 0.5,
  active_positions: 1,
  total_positions: 1,
  total_invested: 10,
  total_current_value: 17.5,
  total_pnl: 7.5,
  pnl_percentage: 75,
  win_rate: 0,
  resolved_trades: 0,
  positions,
};
const execution = {
  strategy: "manual",
  submissions: 0,
  filled: 0,
  fill_rate: null,
  avg_slippage_bps: null,
  median_slippage_bps: null,
  total_fees: 0,
  gross_realized_pnl: 0,
  net_realized_pnl: 0,
  fee_drag_ratio: null,
  win_rate: null,
  avg_win: null,
  avg_loss: null,
  avg_fee: null,
  avg_risk_per_trade: null,
  edge_raw: null,
  edge_score: null,
  missing_fee: 0,
  missing_expected_price: 0,
  closed_trades: 0,
  wins: 0,
  losses: 0,
};

export async function installGuideFixtures(page: Page, anonymous = false) {
  const unknown = new Set<string>();
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    // Keep the established real-API research, replay and maker capture scenarios separate.
    let data: unknown;
    if (path === "/api/auth/me") {
      if (anonymous) return route.fulfill({ status: 401, json: { detail: "Sign in" } });
      data = { id: 1, wallet_address: wallet, is_admin: true };
    } else if (path === "/api/auth/refresh")
      return route.fulfill({ status: 401, json: { detail: "Demo session required" } });
    else if (path === "/api/settings" || path === "/api/settings/copy-trading") data = settings;
    else if (path === "/api/settings/profile")
      data = {
        wallet_address: wallet,
        display_name: "Demo researcher",
        email: null,
        phone: null,
        profile_picture_url: null,
      };
    else if (path === "/api/paper/account") data = { initialized: false, account_id: 1 };
    else if (path === "/api/settings/paper-summary")
      data = { simulation_mode: true, paper_balance: 1000, paper_pnl: 0, simulated_trades: 0 };
    else if (path === "/api/auth/trading-key")
      data = { has_trading_key: false, has_clob_credentials: false };
    else if (path === "/api/accounts") data = [account];
    else if (path === "/api/accounts/active") data = account;
    else if (/\/api\/accounts\/\d+\/settings/.test(path)) data = settings;
    else if (path === "/api/portfolio/summary") data = summary;
    else if (path === "/api/portfolio/balance") data = { ...summary, chain: "Polygon" };
    else if (path === "/api/portfolio/positions") data = { positions, count: 1 };
    else if (path === "/api/portfolio/positions/prices")
      data = { prices: { "11": 0.7 }, ts: stamp };
    else if (path.startsWith("/api/portfolio/equity/snapshots")) data = [];
    else if (path.startsWith("/api/portfolio/equity/summary"))
      data = {
        period: "30d",
        start: 1000,
        end: 1000,
        delta: 0,
        delta_pct: 0,
        max_drawdown_pct: 0,
        points: 0,
      };
    else if (path.startsWith("/api/portfolio/markets"))
      data = { markets: [market], count: 1, total: 1, offset: 0, has_more: false, ts: stamp };
    else if (path === "/api/markets/categories")
      data = {
        categories: [
          { id: "politics", label: "Politics", icon: "🏛️", description: "Public policy" },
          { id: "crypto", label: "Crypto", icon: "₿", description: "Crypto" },
        ],
      };
    else if (path === "/api/markets/search" || path === "/api/markets/browse")
      data = { markets: [market], total: 1, count: 1, offset: 0, has_more: false };
    else if (path.includes("/leaderboard"))
      data = {
        entries: [
          {
            rank: 1,
            address: "0x" + "aa".repeat(20),
            display_name: "Demo trader",
            profit_loss: 0,
            volume: 1000,
            markets_traded: 3,
            positions_value: 100,
            pnl_24h: 0,
            pnl_7d: 0,
            pnl_30d: 0,
            volume_24h: 100,
            is_followed: false,
            is_notification_followed: false,
          },
        ],
        period: "month",
        count: 0,
        updated_at: stamp,
        total: 1,
        forecasters: [],
        rows: [],
      };
    else if (path === "/api/trades/history")
      data = { wallet_address: wallet, trades: [], count: 0, has_more: false };
    else if (path === "/api/trades/copy-trades/pnl") data = { daily_pnl: 0 };
    else if (path.includes("/copy-trades/attribution"))
      data = { traders: [], total_pnl: 0, window_days: 30 };
    else if (path.includes("/evaluations")) data = { evaluations: [], count: 0 };
    else if (/following|copy-trades|stop-loss|take-profit|inverse-bot\/positions/.test(path))
      data = [];
    else if (path.includes("/inverse-bot/metrics"))
      data = { total_positions: 0, enabled_positions: 0, total_reversals: 0, recent_actions: [] };
    else if (path === "/api/copy-trading/experiments") data = { runs: [] };
    else if (path === "/api/notifications") data = [];
    else if (path.startsWith("/api/notifications/")) data = [];
    else if (path === "/api/portfolio/risk-report")
      data = {
        metrics: {
          portfolio_value: 1017.5,
          usdc_balance: 1000,
          unrealized_pnl: 7.5,
          positions_count: 1,
          positions: [
            {
              condition_id: cid,
              market_title: market.question,
              outcome: "Yes",
              size: 25,
              value: 17.5,
              weight: 17.5 / 1017.5,
              cost_basis: 10,
              concentration_flag: false,
            },
          ],
          loss_bound: 10,
          max_position_weight: 0.0172,
          concentration_flags: 0,
          clusters: [],
          catalysts: [],
        },
        narrative: {
          overall_risk_rating: "Demo — review positions",
          var_estimate: "Unavailable",
          concentration_flags: [],
          correlated_clusters: [],
          tail_scenarios: [],
          hedges: [],
        },
        generated_at: stamp,
      };
    else if (path === "/api/trades/analytics/summary")
      data = {
        user_id: 1,
        window_days: 30,
        generated_at: stamp,
        per_strategy: {},
        totals: execution,
        data_quality: { legacy_trades: 0, missing_expected_price: 0, missing_fee: 0 },
      };
    else if (path === "/api/trades/analytics/edge-score") data = { strategies: [] };
    else if (path === "/api/trades/analytics/by-strategy") data = { strategies: [], rows: [] };
    else if (path === "/api/trades/analytics/trades") data = { trades: [], count: 0, total: 0 };
    else if (path === "/api/analysis/calibration/summary")
      data = {
        window_days: 30,
        scope: "user",
        generated_at: stamp,
        sample_count: 0,
        resolved_count: 0,
        effective_sample_count: 0,
        mean_brier: null,
        mean_log_loss: null,
        brier_vs_baseline: 0,
        reliability: [],
        by_model: [],
        by_task: [],
        data_quality: { missing_model: 0, legacy_excluded: 0, invalid_legacy_resolution: 0 },
      };
    else if (path === "/api/analysis/calibration/forecasts") data = { forecasts: [], count: 0 };
    else if (path.includes("calibration/summary/drift"))
      data = { series: [], min_samples_for_drift: 20 };
    else if (path.includes("calibration/summary/by-prompt")) data = { versions: [] };
    else if (path === "/api/analysis/experiments") data = { experiments: [], count: 0 };
    else if (path === "/api/backtesting/runs") data = { runs: [], total: 0 };
    else if (path === "/api/backtesting/strategies")
      data = {
        strategies: [
          {
            type: "copy_trade",
            name: "Copy Trade Replay",
            description: "Replay supplied historical trades",
            parameters: {},
          },
          {
            type: "indicator",
            name: "RSI Mean Reversion",
            description: "Indicator research",
            parameters: {},
          },
        ],
      };
    else if (path === "/api/latency-arb/config")
      data = {
        enabled: false,
        edge_threshold: 0.05,
        max_notional: 10,
        symbols: ["BTC", "ETH"],
        windows: [5, 15],
        late_entry: false,
        daily_loss_limit: 20,
        alert_on_opportunity: false,
      };
    else if (path === "/api/latency-arb/opportunities")
      data = {
        opportunities: [],
        engine: {
          running: false,
          live_mode: false,
          last_cycle_at: null,
          cycle_seconds: 5,
          symbols: ["BTC", "ETH"],
          windows: [5, 15],
        },
        latency: {
          samples: 0,
          feed_lag_p50_ms: 0,
          feed_lag_p95_ms: 0,
          total_p50_ms: 0,
          total_p95_ms: 0,
        },
      };
    else if (path === "/api/latency-arb/trades") data = { trades: [], count: 0, total: 0 };
    else if (path === "/api/news/feed") data = { articles: [], count: 0 };
    else if (path === "/api/whales/config")
      data = {
        min_notional: 10000,
        auto_copy: false,
        watchlist: [],
        whale_set_size: 20,
      };
    else if (path === "/api/whales/events") data = { events: [], count: 0, total: 0 };
    else if (path === "/api/binance/dashboard")
      data = {
        enabled: false,
        smart_money_signals: [],
        social_hype: [],
        trending_tokens: [],
        smart_money_inflow: [],
        pnl_leaderboard: [],
        fetched_at: stamp,
      };
    else if (path === "/api/trades/arbitrage/opportunities") data = [];
    else if (path === "/api/trades/emergency-stop") data = null;
    else if (path === "/api/venues/adapters")
      data = {
        adapters: ["polymarket", "kalshi", "manifold", "metaculus"].map((name) => ({
          name,
          enabled: false,
          last_fetch_at: null,
          last_error: null,
          markets: 0,
        })),
        scheduler: {},
      };
    else if (path === "/api/venues/markets") data = { markets: [], total: 0 };
    else if (path === "/api/venues/markets/cross") data = { groups: [], query: "" };
    else if (path === "/api/venues/mismatches")
      data = { mismatches: [], intra_polymarket: [], count: 0, total: 0 };
    else if (path === "/api/debug/health")
      data = {
        uptime_seconds: 60,
        uptime_human: "1 minute",
        server_time: stamp,
        python_version: "3.11",
        services: [
          {
            name: "Documentation demo",
            status: "healthy",
            latency_ms: 1,
            detail: "Synthetic fixture",
          },
        ],
      };
    else if (path === "/api/debug/stats")
      data = {
        total_requests: 0,
        total_errors: 0,
        total_warnings: 0,
        error_rate: 0,
        endpoints: [],
      };
    else if (path === "/api/debug/logs") data = { total: 0, filtered: 0, entries: [] };
    else if (path === "/api/settings/backends/status")
      data = {
        llm_chain: {
          name: "Research demo",
          description: "Synthetic provider status",
          status: "healthy",
          healthy: true,
        },
        cli_agent: {
          name: "CLI agent",
          description: "Not configured in demo",
          status: "unconfigured",
          healthy: false,
        },
      };
    else if (path === "/api/settings/llm/providers")
      data = {
        providers: [
          {
            id: "openai",
            name: "OpenAI",
            backend: "llm_chain",
            models: ["demo-model"],
            description: "Documentation fixture",
            requires_api_key: true,
          },
        ],
        default_provider: "openai",
        default_model: "demo-model",
      };
    else if (path === "/api/settings/llm/current")
      data = {
        provider: "openai",
        model: "demo-model",
        effective_provider: "openai",
        available_models: ["demo-model"],
      };
    else if (path === "/api/settings/admin/providers")
      data = { providers: [], default_provider: "openai", default_model: "demo-model" };
    else if (path === "/api/settings/admin/users") data = { users: [], total: 0 };
    else if (/api-keys/.test(path)) data = [];
    else if (path.endsWith("/subscription"))
      data = {
        subscription: {
          plan: "free",
          status: "active",
          provider: "none",
          cancel_at_period_end: false,
          current_period_end: null,
        },
        usage: {
          plan: "free",
          plan_label: "Free",
          status: "active",
          accounts: { used: 1, limit: 1, unlimited: false },
          followed_traders: { used: 0, limit: 5, unlimited: false },
          quotas: [],
          capabilities: [],
        },
      };
    else if (path.endsWith("/plans"))
      data = { plans: [], current_plan: "free", email_on_file: false, billing_configured: false };
    else if (path.startsWith("/api/collaboration/consensus/"))
      data = {
        count: 0,
        consensus: null,
        ai_probability: null,
        market_price: 0.7,
        gap: null,
        ai_gap: null,
        disagreement: null,
        contributors: [],
        n_votes: 0,
        n_contributors: 0,
        advisory: "Demo; no forecasts yet",
      };
    else if (path.startsWith("/api/collaboration/annotations/"))
      data = { annotations: [], count: 0 };
    else if (path.startsWith("/api/collaboration/"))
      data = { forecasters: [], forecasts: [], count: 0, followed: [], following: [] };
    else if (path === "/api/market-maker/configs") data = [];
    else if (path === "/api/market-maker/metrics")
      data = { active_configs: 0, running_configs: 0, total_orders_placed: 0, total_volume: 0 };
    else if (path === "/api/trades/size-suggestion")
      data = {
        suggested_size_usdc: 10,
        kelly_fraction: 0.25,
        edge_source: "Synthetic example",
        bankroll_usdc: 1000,
      };
    else if (path === "/api/auth/logout") data = { success: true };
    else if (path.endsWith("/stream"))
      return route.fulfill({ contentType: "text/event-stream", body: 'data: {"done":true}\n\n' });
    else {
      unknown.add(`${method} ${path}`);
      data = {};
    }
    return route.fulfill({ json: data });
  });
  return unknown;
}
