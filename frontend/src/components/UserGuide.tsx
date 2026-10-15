/**
 * Documentation page rendering the static guide sections with a sticky table of contents.
 *
 * Content is the hard-coded sections array, filtered by a text search across titles, descriptions
 * and items. Each card expands/collapses, and the header button drives the TutorialOverlay tour
 * through useTutorialState.
 *
 * @module components/UserGuide
 */
import { useState } from "react";
import TutorialOverlay, { useTutorialState } from "./TutorialOverlay";

/* ─── Section data ─── */

interface GuideSection {
  id: string;
  icon: string;
  title: string;
  description: string;
  items: { heading: string; body: string }[];
}

const sections: GuideSection[] = [
  {
    id: "getting-started",
    icon: "🚀",
    title: "Getting Started",
    description: "Everything you need to begin trading on Polymarket with the AI bot.",
    items: [
      {
        heading: "Connect Your Wallet",
        body: "Sign in using your Ethereum wallet on the Login page. The bot uses your wallet signature to create a secure session — no password needed. Your private key is encrypted and stored securely for automated trading.",
      },
      {
        heading: "Fund Your Account",
        body: "Deposit USDC to your Polymarket wallet through the Polygon network. The bot needs USDC in your account to place trades. You can view your balance on the Dashboard at any time.",
      },
      {
        heading: "Explore the Dashboard",
        body: "The Dashboard shows your portfolio value, open positions with live prices, PnL tracking, and a following feed of traders you track. It refreshes every 15 seconds to keep prices up-to-date.",
      },
    ],
  },
  {
    id: "copy-trading",
    icon: "📋",
    title: "Copy Trading",
    description: "Automatically mirror trades from top Polymarket traders in real time.",
    items: [
      {
        heading: "Find Top Traders",
        body: "Visit the Leaderboard to browse top-performing traders. Click any trader to see their profile, trading history, and win rate. Use the 'Follow' button to add them to your watchlist.",
      },
      {
        heading: "Enable Copy Trading",
        body: "On the Copy Trading page, toggle copy trading ON for any followed trader. Configure your sizing mode: 'fixed' uses a set USDC amount per trade, 'proportional' copies the trader's position as a ratio of their wallet size.",
      },
      {
        heading: "Risk Controls",
        body: "Set a maximum position size, daily loss limit, and minimum trade size to protect your capital. The bot enforces these limits automatically and will skip trades that would exceed them.",
      },
      {
        heading: "Multi-Phase Execution",
        body: "Orders use a 3-phase execution strategy: (1) original price, (2) price adjustment of 1-3 cents for better fill, (3) aggressive fill at reduced size. This maximizes fill rates in fast markets.",
      },
      {
        heading: "Trade Aggregation",
        body: "When a followed trader makes multiple small trades rapidly, the bot aggregates them into a single VWAP (Volume Weighted Average Price) order. This reduces slippage and transaction costs.",
      },
    ],
  },
  {
    id: "market-making",
    icon: "💹",
    title: "Market Making",
    description: "Provide liquidity to markets and earn the bid-ask spread automatically.",
    items: [
      {
        heading: "What is Market Making?",
        body: "Market making places both buy and sell orders around the current market price, earning the spread between them. It works best in liquid markets with consistent volume.",
      },
      {
        heading: "Bands Strategy",
        body: "Creates multiple order levels (bands) around the midpoint price. Each band has progressively wider spreads. Configure: number of bands (1-20), min/max spread, and order size per band.",
      },
      {
        heading: "AMM Strategy",
        body: "Uses an Automated Market Maker curve (constant product formula) to determine order prices. Better for markets with low liquidity. Configure: AMM liquidity amount and min/max price range.",
      },
      {
        heading: "Configuration",
        body: "Go to 'Market Making' → 'New Config'. Enter the Condition ID and Token IDs for the market (find these on Polymarket), set your strategy params, and toggle 'enabled'. Click Start to begin.",
      },
      {
        heading: "Monitoring",
        body: "View running market makers, total orders placed/cancelled, volume in USDC, and current open orders. Use Sync to force an immediate rebalance. Stop any time to cancel all open orders.",
      },
    ],
  },
  {
    id: "backtesting",
    icon: "📊",
    title: "Backtesting",
    description: "Test trading strategies against historical data before risking real capital.",
    items: [
      {
        heading: "Copy Trade Replay",
        body: "Replay historical copy trades with different parameters. Enter a followed wallet address, date range, max position size, and daily loss limit to see how the strategy would have performed.",
      },
      {
        heading: "Indicator Strategies",
        body: "Test technical indicator strategies: RSI Mean Reversion (buy oversold, sell overbought), MACD Crossover (follow momentum), and Bollinger Bounce (buy at lower band, sell at upper band).",
      },
      {
        heading: "Understanding Results",
        body: "Each backtest shows: Total PnL, Win Rate, Sharpe Ratio (risk-adjusted returns — above 1 is good), Max Drawdown (worst peak-to-trough loss), Profit Factor (gross profit / gross loss — above 1 is profitable), and a full trade log.",
      },
      {
        heading: "Tips for Better Backtests",
        body: "Use longer date ranges for more reliable results. Compare multiple strategies on the same market. Watch the max consecutive losses metric — it shows how much drawdown you need to endure. High Sharpe ratios (>1.5) with high trade counts are the most reliable signals.",
      },
    ],
  },
  {
    id: "inverse-bot",
    icon: "🔄",
    title: "Inverse Bot",
    description: "AI-powered position reversal for markets where sentiment shifts.",
    items: [
      {
        heading: "How It Works",
        body: "The Inverse Bot uses AI analysis (GPT/Claude) to evaluate your open positions every 5 minutes. It looks at market data, web sentiment, and X (Twitter) signals to decide if your position should be reversed.",
      },
      {
        heading: "Enable for Positions",
        body: "On the Trades page, find any open position and click 'Enable Inverse Bot'. The bot will start monitoring that position and reverse it if the AI confidence exceeds the threshold.",
      },
      {
        heading: "Understanding Signals",
        body: "Each evaluation shows: Signal (HOLD/REVERSE), Confidence (0-100%), Reasoning from the AI, and summaries of web/X data. Only positions with REVERSE signal and high confidence are acted upon.",
      },
    ],
  },
  {
    id: "stop-loss",
    icon: "🛡️",
    title: "Stop Loss & Take Profit",
    description: "Automatically exit positions to protect gains or limit losses.",
    items: [
      {
        heading: "Setting Stop Loss",
        body: "On the Dashboard or Trades page, click any position to set a stop-loss price. When the market price drops to your target, the bot automatically sells. Checks happen every 10 seconds.",
      },
      {
        heading: "Take Profit Orders",
        body: "Set a take-profit price above your entry. The bot monitors and sells when the price reaches your target — locking in gains automatically without watching the market.",
      },
      {
        heading: "Cash Out",
        body: "Use the Cash Out button on any position to immediately sell at the current market price. Useful for quick exits when you want instant execution.",
      },
    ],
  },
  {
    id: "emergency-stop",
    icon: "🚨",
    title: "Emergency Stop & Panic Sell",
    description: "Instantly halt all trading and optionally liquidate every open position.",
    items: [
      {
        heading: "Emergency Stop",
        body: "Click the Emergency Stop button on the Dashboard or use the API endpoint POST /api/trades/emergency-stop. This immediately halts all copy-trading, cancels pending orders, and sets a global 'paused' flag so no new trades execute.",
      },
      {
        heading: "Panic Sell All",
        body: "Pass { sell_all: true } when triggering emergency stop to also liquidate every open position at market price. The bot processes sells sequentially with a 200ms cooldown to avoid rate limits.",
      },
      {
        heading: "Resume Trading",
        body: "Once conditions stabilize, click Resume Trading or call POST /api/trades/resume-trading. This clears the paused flag so the bot resumes normal copy-trading activity.",
      },
    ],
  },
  {
    id: "multi-layer-risk",
    icon: "🔐",
    title: "Multi-Layer Risk Protection",
    description:
      "Six separate safety caps that stack together to protect your capital automatically.",
    items: [
      {
        heading: "Per-Trade Cap",
        body: "Each individual trade is capped at your configured max_position_size. Any order exceeding this limit is automatically scaled down before submission.",
      },
      {
        heading: "Daily Loss Limit",
        body: "Tracks realized losses for the calendar day. Once losses reach your daily_loss_limit threshold, all copy-trading is paused until midnight UTC. Prevents spiraling losses in volatile markets.",
      },
      {
        heading: "Drawdown Guard",
        body: "Monitors your running PnL since the start. If the total drawdown hits max_drawdown_pct (e.g. 15%), the bot halts all trading. This protects against sustained losing streaks across multiple days.",
      },
      {
        heading: "Cooldown After Consecutive Losses",
        body: "If you hit N consecutive losing trades (configurable via cooldown_after_n_losses), the bot pauses for a set number of minutes before resuming. Helps break an emotional or mechanical losing cycle.",
      },
      {
        heading: "Exposure Ceiling",
        body: "Limits the total value of all open positions combined. Even if individual trades pass their per-trade cap, the bot won't add more exposure above your max_open_exposure setting.",
      },
      {
        heading: "Configure in Settings",
        body: "All risk parameters can be adjusted on the Settings page under 'Risk Management'. Changes take effect immediately — no restart needed.",
      },
    ],
  },
  {
    id: "dynamic-sizing",
    icon: "📈",
    title: "Dynamic Streak-Based Sizing",
    description: "Automatically adjust position sizes based on your recent win/loss streaks.",
    items: [
      {
        heading: "How It Works",
        body: "The bot tracks your recent consecutive wins and losses. On winning streaks it gradually increases position size (up to 1.5×), and on losing streaks it reduces size (down to 0.5×). This 'anti-martingale' approach rides hot streaks and limits damage during cold ones.",
      },
      {
        heading: "Configuration",
        body: "Enable streak-based sizing in Settings → Dynamic Sizing. Set streak_sizing_enabled to true, and adjust streak_sizing_max_multiplier (default 1.5) and streak_sizing_min_multiplier (default 0.5) to control the range.",
      },
      {
        heading: "Streak Counter",
        body: "The streak counter resets to 0 whenever the trade outcome flips (win→loss or loss→win). The multiplier scales linearly: each consecutive win adds +10% up to the max, and each loss reduces by -10% down to the min.",
      },
    ],
  },
  {
    id: "kelly-sizing",
    icon: "🧮",
    title: "Kelly Sizing & Edge",
    description: "How the trade ticket turns a probability advantage into a suggested stake.",
    items: [
      {
        heading: "What Kelly Sizing Means",
        body: "Kelly sizing is a formula that converts your estimated edge into a stake size. The larger your edge — how far the model's probability sits from the market price — the more the formula suggests you stake, and when the edge is small or negative it shrinks the stake toward zero. It exists so you do not bet too much on a thin advantage, or too little on a real one.",
      },
      {
        heading: "Where You See It",
        body: "The trade ticket shows a 'Kelly suggestion' line with the current edge, for example 'edge 12% · model'. It pre-fills the amount field only while you have not typed an amount yourself, and stays fully editable afterwards. Treat it as a starting point, never as an instruction.",
      },
      {
        heading: "Edge in Plain English",
        body: "Edge is the difference between the model's probability and the market's price. A market trading at 60¢ is the crowd saying '60% likely'; if the model computes 72%, the edge is 12 percentage points. A positive edge means the model thinks the outcome is underpriced, and 'no edge' appears when there is nothing to act on.",
      },
      {
        heading: "Caps and Limits",
        body: "Suggestions are capped by your risk settings — per-trade cap, daily loss limit and exposure ceiling — so a large Kelly number never becomes an oversized order. The Kelly fraction itself is configurable in Settings → Risk Management, and a smaller fraction scales every suggestion down proportionally.",
      },
    ],
  },
  {
    id: "simulation-mode",
    icon: "🧪",
    title: "Simulation / Dry-Run Mode",
    description: "Paper-trade with real market data without risking actual funds.",
    items: [
      {
        heading: "Enable Simulation",
        body: "Toggle simulation_mode ON in Settings. When active, the bot processes every signal and logs trades exactly as it would normally, but skips sending actual orders to Polymarket.",
      },
      {
        heading: "Review Simulated Trades",
        body: "All simulated trades are stored with a 'simulated' tag. View them on the Trades page to evaluate strategy performance before switching to live mode.",
      },
      {
        heading: "Go Live",
        body: "When you're satisfied with simulated results, turn simulation_mode OFF and the bot will start executing real trades immediately. Your simulated history is preserved for comparison.",
      },
    ],
  },
  {
    id: "trader-quality",
    icon: "⭐",
    title: "Trader Quality Scoring",
    description: "AI-powered composite scoring to rank traders you follow by overall quality.",
    items: [
      {
        heading: "Composite Score (0-100)",
        body: "Each trader receives a score from 0 to 100 based on four equally-weighted sub-scores: Win Rate, Consistency, Risk-Adjusted Returns, and Activity. The score updates automatically and is visible on the Leaderboard.",
      },
      {
        heading: "Quality Tiers",
        body: "Traders are assigned a tier based on their score: S-Tier (85+), A-Tier (70-84), B-Tier (55-69), C-Tier (40-54), and D-Tier (below 40). Tiers appear as badges on the Leaderboard next to each trader.",
      },
      {
        heading: "Rescore All Traders",
        body: "Use the 'Rescore' button or call POST /api/trades/traders/rescore to recompute scores for all tracked traders. This is useful after a batch of new trades or when you add new traders to follow.",
      },
    ],
  },
  {
    id: "arbitrage-detection",
    icon: "💰",
    title: "Arbitrage Detection",
    description: "Automatically scan markets for complement and spread mispricing opportunities.",
    items: [
      {
        heading: "Complement Arbitrage",
        body: "When the combined price of Yes + No tokens in a market drops below $1.00, there's a risk-free profit opportunity. The bot scans all markets and flags pairs where the combined cost is less than $0.98.",
      },
      {
        heading: "Spread Arbitrage",
        body: "When the bid-ask spread on a single outcome exceeds 5%, the bot flags the market. Large spreads may indicate temporary inefficiency that can be exploited by placing limit orders.",
      },
      {
        heading: "Background Monitor",
        body: "The arbitrage scanner runs automatically every 2 minutes in the background. View detected opportunities via GET /api/trades/arbitrage/opportunities, or trigger an immediate scan with POST /api/trades/arbitrage/scan.",
      },
    ],
  },
  {
    id: "latency-arbitrage",
    icon: "⚡",
    title: "Latency Arbitrage",
    description: "Trading the gap between the model's price and Polymarket's slower one.",
    items: [
      {
        heading: "How It Works",
        body: "Latency arbitrage compares two prices for the same thing: the model's probability for BTC/ETH/SOL up-down markets, computed from the Binance spot feed, and the price Polymarket is showing right now. When the spot feed has already moved and Polymarket has not caught up, that gap is the edge.",
      },
      {
        heading: "Reading the Board",
        body: "'Model P' is the model's probability, 'Market P' is Polymarket's current price, 'Edge' is the gap between them, 'Feed lag' is how old the Binance reading behind the row is, and 'Countdown' is the time left in the market's window. Only rows above your edge threshold are listed.",
      },
      {
        heading: "Paper First",
        body: "The engine ships paper-first. With simulation mode on, fills are simulated and no real orders are ever submitted; watch simulated trades settle before turning simulation mode off.",
      },
      {
        heading: "Tuning the Engine",
        body: "Set the edge threshold, max notional per trade and daily loss limit in Engine Settings. A higher edge threshold means fewer, safer entries, while a higher max notional means larger positions on the entries you do take.",
      },
    ],
  },
  {
    id: "breakeven-probability",
    icon: "⚖️",
    title: "Breakeven Probability",
    description: "The win rate a price needs before a trade stops losing money.",
    items: [
      {
        heading: "What Breakeven Means",
        body: "A share pays $1 when the outcome happens and $0 when it does not, so buying at 62.1¢ needs the outcome to be true more than 62.1% of the time to break even. Below that percentage the trade loses on average, even though any single trade can still win.",
      },
      {
        heading: "On the Trade Ticket",
        body: "The 'Breakeven framing' line shows this as 'Need > 62.1%' when you are buying and 'Selling near 62.1%' when you are selling — in both cases the current price is the fair value you are trading against.",
      },
      {
        heading: "Using It as a Filter",
        body: "Compare breakeven against your own confidence before entering: if you believe the true chance is below the breakeven probability, a buy is a negative-expectancy bet, and above it the bet has an edge. Breakeven ignores fees, so leave a margin.",
      },
    ],
  },
  {
    id: "ops-center",
    icon: "🧭",
    title: "Ops Center & Analysis Lab",
    description: "Operational controls and endpoint-level testing tools for advanced users.",
    items: [
      {
        heading: "What Ops Center Is For",
        body: "Ops Center is a control room for backend-driven features. It exposes risk controls, Binance signal endpoints, analysis endpoints, and market browse checks in one place for fast operational workflows.",
      },
      {
        heading: "Recommended Usage Order",
        body: "Run Analysis Health first to confirm AI backends are up. Then run Market Scan, Risk Assessment, or Trade Plan with your payloads. Finally run Trader Analysis or Copy Trade Eval for account-level decision support.",
      },
      {
        heading: "Risk Controls in Ops",
        body: "Use Emergency Stop to halt activity instantly, Resume Trading to re-enable execution, Rescore Traders to refresh ranking quality, and Scan Arbitrage to refresh opportunity detection.",
      },
      {
        heading: "Analysis Lab Output",
        body: "All analysis actions return raw JSON output in the same panel. Use this to validate backend response shape and reasoning before wiring additional automation on top.",
      },
      {
        heading: "When To Use Markets Browse",
        body: "Use Category Browse to validate that frontend market inventory matches backend browse categories and counts. This is useful when verifying parity with external market listings.",
      },
    ],
  },
  {
    id: "markets-analysis",
    icon: "🧠",
    title: "Markets & AI Analysis",
    description: "Discover markets and get AI-powered insights.",
    items: [
      {
        heading: "Browse Markets",
        body: "The Markets page shows all active Polymarket events. Search by keyword, filter by category. Click any market to see details, current prices, and volume.",
      },
      {
        heading: "AI Opportunities",
        body: "The Opportunities page uses AI scoring to rank markets by potential. Each opportunity shows a confidence score, recommended action, and detailed reasoning. Add promising markets to your watchlist.",
      },
      {
        heading: "Deep Analysis",
        body: "Click 'Analyze' on any market for AI deep analysis. The system evaluates: probability accuracy, market dynamics, news sentiment, trading patterns, risk factors, and provides a recommended action with confidence level.",
      },
    ],
  },
  {
    id: "position-lifecycle",
    icon: "♻️",
    title: "Position Lifecycle Management",
    description: "Automated position health monitoring and cleanup.",
    items: [
      {
        heading: "Auto-Redeem Resolved Markets",
        body: "When a market resolves (outcome determined), the bot automatically detects it and prepares to redeem your position. No need to manually check for resolved markets.",
      },
      {
        heading: "Stale Position Detection",
        body: "Positions held longer than 7 days are flagged as 'stale'. This helps identify forgotten positions that may be tying up capital without active management.",
      },
      {
        heading: "Duplicate Merging",
        body: "If you have multiple buy orders on the same market, the bot detects them and calculates your true weighted-average entry price, giving you an accurate view of your actual position.",
      },
    ],
  },
  {
    id: "settings",
    icon: "⚙️",
    title: "Settings & Configuration",
    description: "Customize your bot's behavior and risk parameters.",
    items: [
      {
        heading: "Trading Configuration",
        body: "Set default sizing mode (fixed/proportional), maximum position size, slippage tolerance, and daily loss limits. These apply globally unless overridden on individual configs.",
      },
      {
        heading: "AI Backend",
        body: "Choose your AI provider: OpenAI (GPT-4), Anthropic (Claude), Google (Gemini), Groq, or local Ollama. Admins can switch providers in Settings → AI Backend.",
      },
      {
        heading: "Notifications",
        body: "Configure email notifications for trade executions, stop-loss triggers, and market alerts. Set up your SMTP settings in the backend environment variables.",
      },
      {
        heading: "Security",
        body: "Your private key is encrypted with Fernet encryption and stored securely. JWT tokens expire after 15 minutes and refresh automatically. All API requests are rate-limited to 100/minute.",
      },
    ],
  },
  {
    id: "model-governance",
    icon: "🧪",
    title: "Model Drift, Prompt Versions & Verdict Feedback",
    description:
      "How the AI's own forecast quality is measured, versioned and rated on the Calibration page.",
    items: [
      {
        heading: "Model Drift",
        body: "The Calibration page tracks mean Brier score per model and task over the selected window and draws the 0.25 coin-flip baseline as a reference line. A pair is flagged DRIFT only when it has at least 20 resolved verdicts and either sits more than 0.02 above that baseline or is rising by more than 0.002 Brier per day; pairs below the sample gate show LOW SAMPLES rather than being flagged on noise. Drift covers every verdict on the instance, so the section is admin-only.",
      },
      {
        heading: "Prompt Versions",
        body: "Every verdict records the 12-character content hash of the prompt store that produced it, so calibration can be grouped by prompt version and answer 'did the last prompt change help?'. Versions are listed best mean Brier first, and verdicts written before prompt versioning are grouped under (unversioned) instead of being backfilled into a version that never produced them.",
      },
      {
        heading: "Rate a Verdict",
        body: "Each row of the recent-forecasts table carries a five-star rating with an optional comment. Use the arrow keys to move between stars and Enter to save, or click them directly; Withdraw removes your rating. Your rating stays private to you while the community mean and rating count are shown beside it. Ratings are a prompt-authoring signal only — nothing retrains or reconfigures the bot automatically.",
      },
      {
        heading: "A/B Experiments (Admins)",
        body: "Admins get an experiment panel on the Calibration page: create an experiment with a name, task, variant traffic percentage and JSON configs for each arm, then pause, resume or retire it. 'Compare arms' aggregates the resolved verdicts written under each arm over the window and reports whether the variant is better, the control is better, there is no difference, or there are insufficient samples (20 resolved verdicts per arm). Only one experiment per task can be active at a time; a conflicting create or resume is rejected.",
      },
    ],
  },
  {
    id: "cross-venue-signals",
    icon: "🌐",
    title: "Cross-Venue Signals",
    description:
      "Compare the same question's price across prediction venues — read-only, signal-only.",
    items: [
      {
        heading: "What the Page Shows",
        body: "The Venues page lists the questions that more than one venue prices, each venue's yes price and the gap between them, plus every cached market with its 24-hour volume and when it was last fetched. Kalshi is the first adapter: it is US-regulated, has a public API and trades real money, which is why it was chosen first.",
      },
      {
        heading: "Signal-Only, Never Executable",
        body: "Every row is a comparison, not an instruction. The page carries a permanent warning to that effect and no order button exists — acting on a mismatch means opening the venue itself and placing the trade manually after confirming the two questions really describe the same event.",
      },
      {
        heading: "Adapters Are Off By Default",
        body: "Each venue has its own feature flag and ships disabled, and an enabled adapter only reads that venue's documented public endpoint once every five minutes. An adapter that is enabled but has never fetched is called out as a data-quality warning, so a stale or empty table is never mistaken for 'the venues agree'.",
      },
      {
        heading: "Why Prices Can Differ",
        body: "A gap can be a real difference in information between venues, a difference in fees or settlement rules, or a stale row — which is why each mismatch shows both venues' questions and the data age. Treat a small gap on a thin market as noise rather than an opportunity.",
      },
    ],
  },
  {
    id: "orchestrator",
    icon: "\u{1F916}",
    title: "AI Orchestrator",
    description: "Run several analysis steps as one plan and get a single merged verdict.",
    items: [
      {
        heading: "Pick an Intent",
        body: "The Orchestrator page ships three plans. Market Deep Dive analyses one market with web research, assesses its risk and builds a trade plan. Opportunity Sweep scans the markets you paste in, deep-dives the three busiest and assesses the top one. Trader Diligence profiles a trader, evaluates whether to copy the trade, then assesses the risk of doing it.",
      },
      {
        heading: "Read the Timeline",
        body: "Each step reports its status, how long it took and which backend ran it — the LLM chain for most providers, the CLI agent when your provider resolves to GitHub Models. Independent steps (the three deep dives in a sweep) run at the same time, which is why their durations overlap.",
      },
      {
        heading: "Partial Results",
        body: "A step that fails or times out does not abort the plan: it is marked failed, the steps that depended only on it are skipped, and you still get a synthesis with a partial badge. When the LLM cannot produce the merged prose, the synthesis falls back to the raw step outputs so you can see exactly what each step returned.",
      },
    ],
  },
  {
    id: "command-palette",
    icon: "\u{1F4AC}",
    title: "Ask the AI (Command Palette)",
    description:
      "Ask a question in plain language and watch the orchestrator answer it step by step.",
    items: [
      {
        heading: "Opening It",
        body: "On the Ask the AI page, press the Open palette button or the keyboard shortcut (\u2318K on macOS, Ctrl+K elsewhere) while that page is open. The input, the microphone button and the Ask button are all keyboard reachable; arrow keys move through the recent and example questions, Enter runs the highlighted one, Escape closes the palette and returns focus to the button that opened it. Your last five questions are remembered on this device and offered above the examples.",
      },
      {
        heading: "How a Question Is Routed",
        body: "The palette adds no natural-language model of its own. It reads the question for a wallet address, a price, a position size and the market it names, then picks one of the Orchestrator's three plans — Market Deep Dive for a single market, Trader Diligence when a wallet address appears, Opportunity Sweep when you ask for several markets at once — and streams that plan. The planner it chose is shown as a chip beside the answer, so a surprising route is always explainable.",
      },
      {
        heading: "Reading a Streaming Answer",
        body: "Each analysis step appears as it completes, so a slow plan shows its progress rather than a spinner, and a step that fails is marked failed while the others continue. The merged answer arrives at the end, with a partial badge whenever a step failed and the prose is degraded. If the connection drops mid-run, everything received so far stays on screen with an error and a Try again button instead of disappearing.",
      },
      {
        heading: "Voice Input",
        body: "The microphone button is optional and only appears where your browser implements the Web Speech API: Chrome and Edge support it, Safari 14.1 and later support it on HTTPS pages and only after a tap, and Firefox on desktop has no recognition API, so the button stays disabled there. Recognition runs on the browser vendor's service, so treat it as a convenience for short questions rather than something to dictate into a sensitive field.",
      },
      {
        heading: "What the Palette Cannot Do",
        body: "It is read-only by design. The palette never places, sizes, cancels or approves an order — every action it suggests is a link to an existing page such as Markets, AI Opportunities, the Leaderboard or the Orchestrator, where you decide what to do next. An answer is analysis, not an instruction, and the trade plan inside one is still only a plan.",
      },
    ],
  },
  {
    id: "troubleshooting",
    icon: "🔧",
    title: "Troubleshooting",
    description: "Common issues and how to resolve them.",
    items: [
      {
        heading: "Orders Not Filling",
        body: "Orders may not fill if the market has low liquidity or your price is too far from market. Try reducing position size or enabling multi-phase execution which automatically adjusts prices.",
      },
      {
        heading: "Copy Trade Delays",
        body: "The bot monitors trades via WebSocket for real-time detection and HTTP polling as fallback. Typical delay is 1-5 seconds. If you notice longer delays, check the backend logs for connection issues.",
      },
      {
        heading: "Balance Shows Zero",
        body: "Ensure you have USDC on Polygon network in your Polymarket wallet. The bot queries your on-chain balance. If the issue persists, try refreshing or logging out and back in.",
      },
      {
        heading: "AI Analysis Timeout",
        body: "AI analysis calls have a 60-second timeout. If your analysis times out, the LLM backend may be overloaded. Try again or switch to a faster provider in Settings.",
      },
    ],
  },
];

/* ─── Table of Contents ─── */

function TableOfContents({
  sections,
  activeSection,
  onSelect,
}: {
  sections: GuideSection[];
  activeSection: string;
  onSelect: (id: string) => void;
}) {
  return (
    <nav className="surface-panel p-4 space-y-1 sticky top-24">
      <h3 className="text-xs font-bold uppercase tracking-wider text-muted mb-3">Contents</h3>
      {sections.map((s) => (
        <button
          key={s.id}
          onClick={() => onSelect(s.id)}
          className={`block w-full text-left px-3 py-2 rounded-md text-sm transition ${
            activeSection === s.id
              ? "bg-[var(--accent-soft)] text-[var(--accent)] font-semibold"
              : "text-soft hover:bg-[var(--bg-soft)] hover:text-[var(--text-primary)]"
          }`}
        >
          <span className="mr-2">{s.icon}</span>
          {s.title}
        </button>
      ))}
    </nav>
  );
}

/* ─── Section Card ─── */

function SectionCard({ section }: { section: GuideSection }) {
  const [expanded, setExpanded] = useState(true);

  return (
    <div id={section.id} className="surface-panel p-6 scroll-mt-24">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between text-left group"
      >
        <div className="flex items-center gap-3">
          <span className="text-2xl">{section.icon}</span>
          <div>
            <h2 className="text-lg font-bold text-white group-hover:text-[var(--accent)] transition">
              {section.title}
            </h2>
            <p className="text-sm text-soft mt-0.5">{section.description}</p>
          </div>
        </div>
        <svg
          className={`w-5 h-5 text-muted transition-transform ${expanded ? "rotate-180" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {expanded && (
        <div className="mt-5 space-y-4 pl-11">
          {section.items.map((item, idx) => (
            <div key={idx} className="surface-soft p-4">
              <h3 className="text-sm font-semibold text-white mb-1.5">{item.heading}</h3>
              <p className="text-sm text-soft leading-relaxed">{item.body}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/* ─── Main component ─── */

export default function UserGuide() {
  const [activeSection, setActiveSection] = useState(sections[0].id);
  const [search, setSearch] = useState("");
  const {
    showTutorial,
    seen: tutorialSeen,
    startTutorial,
    completeTutorial,
    dismissTutorial,
    resetTutorial,
  } = useTutorialState();

  const handleSelect = (id: string) => {
    setActiveSection(id);
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  const filtered = search.trim()
    ? sections.filter(
        (s) =>
          s.title.toLowerCase().includes(search.toLowerCase()) ||
          s.description.toLowerCase().includes(search.toLowerCase()) ||
          s.items.some(
            (i) =>
              i.heading.toLowerCase().includes(search.toLowerCase()) ||
              i.body.toLowerCase().includes(search.toLowerCase()),
          ),
      )
    : sections;

  return (
    <div className="page-enter max-w-7xl mx-auto p-6 space-y-6">
      {/* Header */}
      <div
        className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4"
        data-tutorial="guide"
      >
        <div>
          <h1 className="text-2xl font-bold text-white">📖 Your Guide</h1>
          <p className="text-soft text-sm mt-1">
            Everything you need to know about using the Polymarket AI Trading Bot
          </p>
        </div>

        <div className="flex items-center gap-3">
          {/* Start / Restart Tutorial Button */}
          <button
            onClick={() => {
              resetTutorial();
              startTutorial();
            }}
            className="btn-accent text-sm flex items-center gap-2"
          >
            <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M14.752 11.168l-3.197-2.132A1 1 0 0010 9.87v4.263a1 1 0 001.555.832l3.197-2.132a1 1 0 000-1.664z"
              />
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
            {tutorialSeen ? "Restart Tutorial" : "Start Tutorial"}
          </button>

          {/* Search */}
          <div className="w-full sm:w-72">
            <input
              type="text"
              placeholder="Search guide..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input-theme w-full text-sm"
            />
          </div>
        </div>
      </div>

      {/* Layout */}
      <div className="flex gap-6">
        {/* TOC sidebar (hidden on mobile) */}
        <div className="hidden lg:block w-64 flex-shrink-0">
          <TableOfContents
            sections={filtered}
            activeSection={activeSection}
            onSelect={handleSelect}
          />
        </div>

        {/* Content */}
        <div className="flex-1 space-y-4">
          {filtered.length === 0 && (
            <div className="surface-panel p-8 text-center">
              <p className="text-soft">No sections match your search.</p>
            </div>
          )}
          {filtered.map((section) => (
            <SectionCard key={section.id} section={section} />
          ))}

          {/* Quick tips footer */}
          <div className="surface-panel p-6 border-l-4 border-[var(--accent)]">
            <h3 className="text-sm font-bold text-white mb-2">💡 Pro Tips</h3>
            <ul className="text-sm text-soft space-y-1.5 list-disc list-inside">
              <li>Start with small amounts while learning the bot's behavior</li>
              <li>Always backtest a strategy before running it with real funds</li>
              <li>Use stop-losses on every position to protect your capital</li>
              <li>Monitor the Leaderboard regularly to find new traders to follow</li>
              <li>Check AI analysis before entering large positions manually</li>
              <li>
                Use the keyboard shortcut{" "}
                <span className="mono text-xs bg-[var(--bg-soft)] px-1.5 py-0.5 rounded">/</span> to
                quickly search markets
              </li>
            </ul>
          </div>
        </div>
      </div>

      {/* Tutorial overlay */}
      {showTutorial && (
        <TutorialOverlay onComplete={completeTutorial} onDismiss={dismissTutorial} />
      )}
    </div>
  );
}
