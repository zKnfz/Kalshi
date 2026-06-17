# Kalshi Edge Analyzer

A real-time scanner and **execution sandbox** for [Kalshi](https://kalshi.com)
prediction markets, with optional **Polymarket cross-platform arbitrage**
detection. It polls Kalshi's public Trade API every few seconds (or
streams over Kalshi's native authenticated WebSocket feed), evaluates
every live market with a multi-signal edge engine, computes
**fee-adjusted** net edge, and streams ranked opportunities to a live
web dashboard.

Three execution modes:

- **`off`** (default) — scan only, no orders sent. Use this while
  tuning the analyzer.
- **`paper`** — simulated fills with realistic slippage and the
  official Kalshi fee schedule. P&L is tracked to disk so it survives
  restarts.
- **`live`** — orders are sent to Kalshi via `POST /portfolio/orders`
  using RSA-PSS-signed authentication. **Live mode is gated by a stack
  of circuit breakers** (kill switch, daily-loss limit, MAX_BET_PCT
  cap, position-already-held guard) and ships **disabled by default**.

The point is that everything new in this revision — fees, paper
trading, kill switches, alerts, Polymarket arbs, snapshot backtests —
is wired up so you can iterate from "scanner" to "responsible
auto-trader" entirely inside this repo.

---

## What it looks for

| Strategy | Trigger |
|---|---|
| `yes_no_arbitrage` | `yes_ask + no_ask < $1.00` on a single market, **net** of both legs' Kalshi fees. |
| `dutch_book_arbitrage` | `Σ yes_ask < $1.00` across a mutually-exclusive event basket, with a simultaneous-fill feasibility check against `MIN_FILL_QTY`. |
| `dutch_book_mispricing` | Sum of mids inside an event ≠ 1; normalized YES fair vs. each leg's YES/NO ask reveals which legs are mispriced. |
| `fair_value_yes` / `fair_value_no` | Recency-weighted blend of mid + last + prior (exponential decay on stale `last_price`) exceeds the best ask by ≥ `MIN_NET_EDGE_PCT` after fees. |
| `cross_platform_arbitrage` | A Kalshi market and a Polymarket market with matching titles sum to less than $1.00 across the two venues, net of Kalshi's fee and a 2% Polymarket fee buffer. Requires `POLYMARKET_ENABLED=true`. |

A market that lights up under multiple signals is **deduplicated** into
one row whose `signal_types[]` lists every matching strategy.

### Scoring & sizing

For every opportunity the engine computes:

- **Gross edge** (`edge_pct`) and **net edge after fees** (`net_edge_pct`).
- **Fee per contract** using Kalshi's published taker / maker formula
  (with the S&P-Nasdaq halved-coefficient exception).
- **Kelly fraction** computed against `entry_price + fee` so the stake
  reflects the true risk-adjusted edge, then scaled by
  `KELLY_FRACTION` (default ¼-Kelly).
- **Hard cap** at `MAX_BET_PCT × BANKROLL` per single order.
- **Per-strategy bankroll sub-caps** (`ARB_BANKROLL_SHARE`,
  `FAIRVALUE_BANKROLL_SHARE`) so arb and fair-value pools can't drain
  each other.
- **Confidence** in `[0, 1]` from liquidity, 24h volume, open
  interest, spread, *and* `last_trade_age_seconds` (stale prints
  decayed).

### Kelly derivation

Buying a Kalshi YES contract at price `p` and risking fraction `f` of
bankroll `B` gives bankroll `B(1 + f(1-p)/p)` with probability `q` (your
fair) and `B(1 - f)` otherwise. Maximizing expected log growth gives:

```
   b = (1 - p) / p
   f* = (b · q − (1 − q)) / b   =   (q − p) / (1 − p)
```

Both forms are identical at every `(p, q) ∈ (0, 1)²`; the closed form
is preferred because it stays numerically stable as `p → 0`. Fees are
incorporated by replacing `p` with `p + per_contract_fee(p)` inside
the formula above (you must clear `fee` worth of edge before the
trade is profitable).

`tests/test_analyzer.py` and `tests/test_fees.py` pin both the
equivalence and the user-supplied numerical example.

### Fees — Kalshi's exact formula

Per the [Kalshi fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf):

```
taker_fee  = ceil_cents( 0.07   · contracts · price · (1 - price) )
maker_fee  = ceil_cents( 0.0175 · contracts · price · (1 - price) )
```

with `0.035` / `0.00875` coefficients on S&P 500 (`INX*`) and
Nasdaq-100 (`NASDAQ100*`) markets. The ceil is taken once over the
whole order — a 100-contract order at $0.50 pays $1.75 in fees, not
100 × $0.02 = $2.00. The analyzer uses the per-contract upper bound
(conservative) when pre-filtering; the executor uses the aggregate
formula when sending orders.

---

## Architecture

```
┌──────────────────┐  poll(±jit)/auth-ws ┌──────────────┐    score+fees   ┌──────────────┐
│  Kalshi  REST    │ ──────────────────► │ AnalyzerEngine│ ─────────────► │ Opportunity  │
│  Kalshi  WS v2   │ ──────────────────► │  (asyncio)    │   per-tick mtm │   diff       │
│  Polymarket Gamma│ ──────────────────► │               │                │              │
└──────────────────┘   429/503 backoff   └──────┬────────┘                └──────┬───────┘
                                                │  snapshot + delta              │
                                                ▼                                ▼
                              ┌─────────────────────────────────────────────────────────┐
                              │  FastAPI server + live JS dashboard                     │
                              │  + Telegram/Discord alerts                              │
                              │  + Executor (off | paper | live) with circuit breakers  │
                              │  + PaperEngine ledger (persisted)                       │
                              └─────────────────────────────────────────────────────────┘
```

Modules:

| File | What it does |
|---|---|
| `kalshi_analyzer/client.py` | Async REST client (auth-aware). 429/503 backoff with `Retry-After`. |
| `kalshi_analyzer/auth.py` | RSA-PSS request signer. |
| `kalshi_analyzer/ws_client.py` | Native Kalshi WebSocket client (opt-in, auth-gated). |
| `kalshi_analyzer/fees.py` | Official Kalshi fee model. |
| `kalshi_analyzer/analyzer.py` | Pure-Python edge engine (no I/O, fully unit-tested). |
| `kalshi_analyzer/engine.py` | Async orchestrator: jittered polling, scoring, dedup, broadcast, snapshot recording. |
| `kalshi_analyzer/polymarket.py` | Polymarket Gamma client + Kalshi↔Polymarket matcher + cross-platform arb. |
| `kalshi_analyzer/execution.py` | Order router with circuit breakers. |
| `kalshi_analyzer/paper.py` | Paper-trading engine (fills, slippage, P&L, persistence). |
| `kalshi_analyzer/alerts.py` | Telegram + Discord webhook notifier with cooldowns. |
| `kalshi_analyzer/backtest.py` | JSONL snapshot recorder + replay engine. |
| `kalshi_analyzer/cli.py` | `python -m kalshi_analyzer.cli {replay,kill-switch on,...}`. |
| `kalshi_analyzer/server.py` | FastAPI app: REST + diff-only WS + dashboard. |

---

## Quick start

```bash
git clone <repo>
cd Kalshi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
# → http://localhost:8000
```

### Demo mode (offline)

`DEMO_MODE=true` skips Kalshi and uses synthetic events that fire
every signal type. Useful for iterating on the analyzer or the UI.

### Paper trading

```bash
EXECUTION_MODE=paper python run.py
# (or set it in .env)
```

- Every opportunity in the dashboard is also scored for execution
  feasibility; the executor logs to `paper_state.json` and the running
  P&L is surfaced both in the topbar and at `GET /api/paper`.
- All circuit breakers apply (kill switch, MAX_DAILY_LOSS, MAX_BET_PCT,
  already-held guard).

### Live trading (advanced — read the safety section first)

1. Generate an RSA key pair in your Kalshi account dashboard.
2. Store the private key on disk and set `KALSHI_KEY_ID` +
   `KALSHI_PRIVATE_KEY_PATH` in `.env`.
3. Set `EXECUTION_MODE=live`. **Do not skip the paper-mode shakedown.**
4. Pre-trip the kill switch and verify it blocks orders, then clear it:
   ```bash
   python -m kalshi_analyzer.cli kill-switch on
   python -m kalshi_analyzer.cli kill-switch off
   ```

### Snapshot recording + replay backtest

```bash
BACKTEST_RECORDING=true python run.py    # leave running to collect ticks
# ...
python -m kalshi_analyzer.cli replay --bankroll 1000 --min-edge 3
```

Outputs ROI, realized/unrealized P&L, fees paid, and per-strategy
order acceptance counts so you can directly answer
*"does X% gross edge convert to net profit?"*.

---

## Configuration reference

See `.env.example` for the canonical list; the tables below group
settings by concern.

### Polling and rate limits

| Name | Type | Default | Description |
|---|---|---|---|
| `KALSHI_BASE_URL` | str | `https://api.elections.kalshi.com/trade-api/v2` | Public Trade API base. |
| `POLL_INTERVAL_SECONDS` | float | `5` | Mean seconds between REST polls. |
| `POLL_JITTER_PCT` | float | `0.15` | ±jitter applied to each interval to avoid fingerprinting. |
| `MAX_MARKETS` | int | `400` | Cap on opportunities returned to the UI. |

### Pre-signal noise filters

| Name | Type | Default | Description |
|---|---|---|---|
| `MIN_LIQUIDITY_CENTS` | int | `2000` | Reject markets whose liquidity proxy is below this. |
| `MAX_SPREAD_CENTS` | int | `15` | Reject markets with bid-ask spread wider than this. |
| `MIN_VOLUME_24H` | int | `0` | Reject markets with fewer than N contracts traded in 24h. |
| `STALE_LAST_AGE_SECONDS` | int | `60` | Exponential-decay threshold for the `last_price` weight + confidence. |
| `RECENCY_WEIGHTS` | csv | `0.50,0.35,0.15` | Mid / last / prior weights in the fair-value blend. |
| `MIN_EDGE_PCT` | float | `0.5` | Reject sub-noise gross edge. |
| `MIN_NET_EDGE_PCT` | float | `1.0` | **Reject opportunities whose edge after Kalshi fees is below this.** |
| `MIN_FILL_QTY` | int | `25` | Dutch-book baskets flagged infeasible if any leg's top-of-book size is below this. |

### Sizing and bankroll

| Name | Type | Default | Description |
|---|---|---|---|
| `BANKROLL` | float | `1000` | Bankroll used to convert Kelly fractions into dollar stakes. |
| `KELLY_FRACTION` | float | `0.25` | Fractional-Kelly multiplier. |
| `MAX_BET_PCT` | float | `0.05` | Hard cap on per-bet stake as a fraction of bankroll. |
| `ARB_BANKROLL_SHARE` | float | `0.60` | Per-strategy bankroll sub-cap for arb / mispricing. |
| `FAIRVALUE_BANKROLL_SHARE` | float | `0.30` | Per-strategy bankroll sub-cap for fair-value. |
| `ASSUME_TAKER_FEES` | bool | `true` | Use the 0.07 taker coefficient (vs. 0.0175 maker) when computing net edge. |

### Execution and safety (READ THIS BEFORE LIVE)

| Name | Type | Default | Description |
|---|---|---|---|
| `EXECUTION_MODE` | str | `off` | `off` \| `paper` \| `live`. |
| `MAX_DAILY_LOSS` | float | `50.0` | Realized-loss budget per UTC day. Once breached, the executor refuses orders until midnight UTC. |
| `KILL_SWITCH` | bool | `false` | If true, every order is rejected. |
| `KILL_SWITCH_FILE` | path | `/tmp/kalshi-kill-switch` | If this path exists, every order is rejected — letting an external process trip the breaker via `touch`. |
| `PAPER_SLIPPAGE_CENTS` | float | `1.0` | Slippage added to taker fills in paper mode. |
| `PAPER_STATE_PATH` | path | `./paper_state.json` | Persisted paper ledger. |
| `POSITION_STATE_PATH` | path | `./positions.json` | Reserved for the live-mode position tracker. |
| `KALSHI_KEY_ID` | str | `""` | Your Kalshi API key ID (live mode only). |
| `KALSHI_PRIVATE_KEY_PATH` | path | `""` | RSA private key for signing requests. |
| `USE_NATIVE_WS` | bool | `false` | Enable the native authenticated WebSocket market feed (requires creds). |

### Alerts

| Name | Type | Default | Description |
|---|---|---|---|
| `ALERT_MIN_EDGE_PCT` | float | `5.0` | Webhook threshold (net edge %). |
| `ALERT_COOLDOWN_SECONDS` | float | `300` | Minimum gap between alerts for the same (ticker, side, strategy). |
| `TELEGRAM_BOT_TOKEN` | str | `""` | Bot token. |
| `TELEGRAM_CHAT_ID` | str | `""` | Target chat ID. |
| `DISCORD_WEBHOOK_URL` | str | `""` | Webhook URL. |

### Polymarket cross-platform arb

| Name | Type | Default | Description |
|---|---|---|---|
| `POLYMARKET_ENABLED` | bool | `false` | Master switch — when true, the engine pulls Polymarket every minute and emits `cross_platform_arbitrage` opportunities. |
| `POLYMARKET_BASE_URL` | str | `https://gamma-api.polymarket.com` | Discovery API. |
| `POLYMARKET_CLOB_URL` | str | `https://clob.polymarket.com` | (Reserved for future direct CLOB integration.) |
| `POLYMARKET_MATCH_PATH` | path | `./polymarket_map.json` | Hand-curated `{kalshi_ticker: polymarket_slug_or_conditionId}` map; always wins over automatic title matching. |

### Backtest

| Name | Type | Default | Description |
|---|---|---|---|
| `BACKTEST_RECORDING` | bool | `false` | When true, every tick appends to the JSONL file. |
| `BACKTEST_SNAPSHOT_PATH` | path | `./snapshots.jsonl` | Append-only snapshot stream. |

---

## Safety

This stack is now capable of submitting real orders to Kalshi. A few
deliberate guard rails make that hard to do by accident:

1. **`EXECUTION_MODE=off` is the default.** You must change it to
   `paper` or `live` to send any order, anywhere.
2. **`live` additionally requires authentic creds.** Without
   `KALSHI_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` the executor fails
   safely with a rejection reason.
3. **Kill switch by file.** A separate process (or a CLI command, or
   your monitoring system) can disable trading instantly:
   ```bash
   python -m kalshi_analyzer.cli kill-switch on
   ```
4. **Daily realized-loss circuit breaker.** Once `MAX_DAILY_LOSS` is
   hit (paper or live counters are independent), the executor refuses
   every order until midnight UTC.
5. **Per-bet cap.** No single order can exceed
   `MAX_BET_PCT × BANKROLL` in notional cost regardless of what
   Kelly suggests.
6. **Position-already-held guard.** The same `(ticker, side)` can't
   be entered twice in a session unless you pass
   `allow_pyramid=True` explicitly.

None of this is a substitute for understanding what you're trading.
Run in `paper` mode for at least a session before flipping `live`.

## Kalshi rate limits

See the original write-up — same defenses still apply (jittered polls,
dedicated 429/503 backoff, fail-fast on 4xx, single-request bundle
per tick). The native WebSocket feed (`USE_NATIVE_WS=true`) replaces
the REST polling cadence with push-based updates when enabled.

## Disclaimer

This is open-source software for research and personal use. It is not
financial advice, not a brokerage, not an investment vehicle, and not
endorsed by Kalshi or Polymarket. Arbitrage signals assume you can
actually fill every leg at the posted prices, which thin order books
will frequently prevent. Use the paper-mode + backtest tooling first.
