# Kalshi Edge Analyzer

A real-time scanner for [Kalshi](https://kalshi.com) prediction markets that
continuously evaluates every live market and surfaces the **most optimal
bets** — arbitrage, dutch-book mispricings, and positive-EV fair-value
plays — ranked on a live web dashboard.

It pulls Kalshi's public Trade API (no auth required for read-only market
data), runs a multi-signal edge engine, and streams ranked opportunities to
the browser over WebSockets. Each opportunity comes with an entry price,
fair value, edge %, fractional-Kelly stake suggestion, confidence score,
and a plain-English rationale.

---

## What it looks for

Every poll the engine pulls every open event + market and evaluates several
independent signals per market and per event:

| Strategy | What it is | When it fires |
|---|---|---|
| `yes_no_arbitrage` | Pure two-leg arb on the same market. | `yes_ask + no_ask < $1.00` — buy both sides for under a dollar and one of them is guaranteed to pay $1. |
| `dutch_book_arbitrage` | Cross-market arb inside one event. | For events whose markets are mutually exclusive + exhaustive, `Σ yes_ask < $1.00` ⇒ buying every YES guarantees a profit. |
| `dutch_book_mispricing` | Probability normalization. | Mids inside one event don't sum to 1; the normalized fair vs. each ask reveals which leg is under-priced. |
| `fair_value_yes` / `fair_value_no` | Blended fair value vs. best ask. | A weighted blend of mid, last trade and prior price exceeds the current best ask by a meaningful margin. |

For every signal we compute:

- **Edge** in cents and percent (`fair − entry`).
- **Kelly fraction** for binary contracts:
  `f* = (q − p) / (1 − p)`, where `p` is entry price and `q` is fair price.
  The dashboard's "Kelly" column is `f*` scaled by `KELLY_FRACTION` (default
  ¼ Kelly, the standard de-risked sizing).
- **Suggested stake** = `kelly_scaled × bankroll`.
- **Confidence** = liquidity-, volume- and spread-weighted score in `[0,1]`.
- **Score** = `edge × confidence × strategy_bonus × tanh(edge%)` — used for
  ranking. Arbitrage strategies receive a bonus because their payoff is
  path-independent.

Markets must be `active`/`open` and have at least `MIN_LIQUIDITY_CENTS`
resting liquidity to be considered.

---

## Architecture

```
┌──────────────────┐   poll      ┌──────────────┐    score    ┌─────────────┐
│  Kalshi public   │ ──────────► │ AnalyzerEngine│ ──────────► │ Opportunity │
│  Trade API v2    │             │  (asyncio)    │             │  ranking    │
└──────────────────┘             └──────┬────────┘             └──────┬──────┘
                                        │ WebSocket broadcast         │
                                        ▼                             ▼
                                ┌──────────────────────────────────────────┐
                                │   FastAPI server + live JS dashboard     │
                                └──────────────────────────────────────────┘
```

- `kalshi_analyzer/client.py` — async `httpx` client for `/markets`,
  `/events?with_nested_markets=true`, and per-market orderbook endpoints.
- `kalshi_analyzer/analyzer.py` — pure-Python edge engine (no API calls,
  fully unit-testable).
- `kalshi_analyzer/engine.py` — orchestrator: polls every
  `POLL_INTERVAL_SECONDS`, evaluates, broadcasts.
- `kalshi_analyzer/server.py` — FastAPI app with `/api/opportunities`,
  `/api/health`, `/ws`, and a static dashboard.
- `kalshi_analyzer/static/` — single-file dashboard (vanilla JS, no build
  step), live-updating cards sorted by score with filter chips per
  strategy.

---

## Quick start

```bash
git clone <repo>
cd Kalshi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env

python run.py
# → open http://localhost:8000
```

The dashboard auto-connects over WebSocket and refreshes whenever the
engine completes a poll cycle (default every 5 s).

### Demo mode (offline)

If Kalshi is unreachable or you just want to see the UI without a network,
set `DEMO_MODE=true` in `.env`. The engine will generate a small synthetic
event that intentionally contains a dutch-book arb so the dashboard lights
up immediately.

### Running tests

```bash
pip install pytest
python -m pytest -q tests
```

---

## Configuration

All settings live in `.env` (see `.env.example`):

| Key | Default | What it does |
|---|---|---|
| `KALSHI_BASE_URL` | `https://api.elections.kalshi.com/trade-api/v2` | Public Trade API base. |
| `POLL_INTERVAL_SECONDS` | `5` | How often to refresh markets. |
| `MAX_MARKETS` | `400` | Cap on opportunities returned to the UI. |
| `MIN_LIQUIDITY_CENTS` | `2000` | Filter out illiquid markets ($20 of resting depth). |
| `BANKROLL` | `1000` | Used for the suggested-stake column. |
| `KELLY_FRACTION` | `0.25` | Fractional-Kelly multiplier for stake sizing. |
| `DEMO_MODE` | `false` | Skip Kalshi calls and use synthetic data. |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | Where to bind the FastAPI server. |

No API keys are required for the read-only public endpoints used here.

---

## Notes / disclaimers

- This is a **research and visualization tool**, not an autotrader. It does
  **not** place orders or require Kalshi credentials. To act on a signal
  click "Open on Kalshi ↗" on any card.
- Edges based on the fair-value blend are heuristics, not predictions. The
  arbitrage strategies (`*_arbitrage`) are path-independent and the only
  signals with guaranteed positive payoff *if you can fill both legs at the
  posted prices*; in practice Kalshi's fees and partial fills will eat into
  thin arbs, so check size before sending orders.
- Nothing here is financial advice.
