# Kalshi Edge Analyzer

A real-time scanner for [Kalshi](https://kalshi.com) prediction markets. It
polls Kalshi's public Trade API every few seconds, evaluates every live
market with a multi-signal edge engine, and streams ranked betting
opportunities to a live web dashboard over WebSockets.

Each opportunity comes with an entry price, fair value, edge %, a
fractional-Kelly stake suggestion (capped by `MAX_BET_PCT` and a
per-strategy bankroll sub-cap), confidence score, plain-English
rationale, and a 👁 "first seen N minutes ago" age label so you can
spot illusory long-stale arbs at a glance.

---

## What it looks for

| Strategy | What it is | Trigger |
|---|---|---|
| `yes_no_arbitrage` | Pure two-leg arb on one market. | `yes_ask + no_ask < $1.00` — buy both sides for under a dollar; one is guaranteed to pay $1. |
| `dutch_book_arbitrage` | Cross-market arb inside one event. | `Σ yes_ask < $1.00` across a mutually-exclusive event basket. The basket is additionally checked for **simultaneous fill feasibility** against `MIN_FILL_QTY`. |
| `dutch_book_mispricing` | Probability normalization. | Mids inside one event don't sum to 1; normalized YES fair vs. each leg's YES/NO ask reveals which legs are under-priced. |
| `fair_value_yes` / `fair_value_no` | Blended fair value vs. best ask. | A recency-weighted blend of mid + last + prior (with **exponential decay on stale last_price**) exceeds the best ask by ≥ `MIN_EDGE_PCT`. |

A market that lights up under multiple signals is deduplicated into one
row whose `signal_types` array lists every matching strategy.

### Scoring & sizing

For every opportunity the engine computes:

- **Edge** in cents and percent (`fair − entry`).
- **Kelly fraction** for a binary YES contract (see derivation below),
  then scaled by `KELLY_FRACTION` (default ¼ Kelly), then **hard-capped
  by `MAX_BET_PCT`** of bankroll regardless of Kelly output.
- **Suggested stake** = `scaled_kelly × bankroll`, further capped by
  the per-strategy bankroll share (`ARB_BANKROLL_SHARE` for
  arb/mispricing, `FAIRVALUE_BANKROLL_SHARE` for fair-value).
- **Confidence** in `[0, 1]` from liquidity, 24h volume, open interest,
  spread, *and* `last_trade_age_seconds` (stale prints are decayed).
- **Score** for ranking: `edge × confidence × strategy_bonus × tanh(edge%)`.

### Kelly derivation (cited)

A Kalshi YES contract bought at price `p` pays `$1` if YES resolves true,
`$0` otherwise. Spending fraction `f` of bankroll `B` buys `f·B/p`
contracts. If `q` is your *estimated* probability that YES resolves true,

- with prob `q`: bankroll → `B · (1 + f · (1−p)/p)`
- with prob `1−q`: bankroll → `B · (1 − f)`

Maximizing expected log-growth (Kelly, 1956) gives the binary form:

```
   b = (1 − p) / p
   f* = (b · q − (1 − q)) / b
```

which simplifies algebraically to the closed form used in the code:

```
   f* = (q − p) / (1 − p)
```

Both forms are mathematically identical at every `(p, q) ∈ (0, 1)²` —
the closed form is preferred because it stays numerically stable as
`p → 0` (whereas `b → ∞`). When `q = p` (zero edge) it is exactly `0`.

`tests/test_analyzer.py` pins the equivalence at five (p, q) points and
verifies the user-supplied example (p=0.40, q=0.60 → ⅓ Kelly).

---

## Architecture

```
┌──────────────────┐   poll(±jitter)  ┌──────────────┐    score    ┌─────────────┐
│  Kalshi public   │ ───────────────► │ AnalyzerEngine│ ──────────► │ Opportunity │
│  Trade API v2    │   429/503 backoff │  (asyncio)    │   ranking   │   diff      │
└──────────────────┘                  └──────┬────────┘             └──────┬──────┘
                                             │ snapshot + delta            │
                                             ▼                             ▼
                                  ┌──────────────────────────────────────────┐
                                  │   FastAPI server + live JS dashboard     │
                                  │   • snapshot on connect                  │
                                  │   • added / updated / removed deltas     │
                                  │   • heartbeat when no diff               │
                                  └──────────────────────────────────────────┘
```

- `kalshi_analyzer/client.py` — async `httpx` client with dedicated 429 /
  503 exponential backoff and general retry policy.
- `kalshi_analyzer/analyzer.py` — pure-Python edge engine (no API
  calls, fully unit-tested).
- `kalshi_analyzer/engine.py` — async orchestrator: jittered polling,
  first-seen tracking, snapshot + delta production.
- `kalshi_analyzer/server.py` — FastAPI app: REST + diff-only WS.
- `kalshi_analyzer/static/` — single-file dashboard (vanilla JS, no
  build step) with stacked signal-type chips, min-edge slider,
  hide-infeasible toggle, age labels, mobile responsive layout, and a
  per-row "copy trade params" button.

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

### Demo mode (offline)

`DEMO_MODE=true` in `.env` skips Kalshi and uses a tuned synthetic
event set that lights up **every** signal type at once: `yes_no_arbitrage`,
`dutch_book_arbitrage`, `dutch_book_mispricing`, `fair_value_yes`, and
`fair_value_no`.

### Running tests

```bash
pip install pytest
python -m pytest -q tests
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) for the contributor workflow.

---

## Configuration reference

All settings live in `.env` (see `.env.example`).

### Polling and rate limits

| Name | Type | Default | Description |
|---|---|---|---|
| `KALSHI_BASE_URL` | str | `https://api.elections.kalshi.com/trade-api/v2` | Public Trade API base URL. |
| `POLL_INTERVAL_SECONDS` | float | `5` | Mean seconds between polls. |
| `POLL_JITTER_PCT` | float | `0.15` | ±jitter applied to each interval to avoid rate-limit fingerprinting (15 % by default ⇒ actual interval in `[4.25s, 5.75s]`). |
| `ORDERBOOK_REFRESH_SECONDS` | float | `15` | Reserved for future per-market depth refresh. |
| `MAX_MARKETS` | int | `400` | Cap on opportunities returned to the UI. |

### Pre-signal noise filters

| Name | Type | Default | Description |
|---|---|---|---|
| `MIN_LIQUIDITY_CENTS` | int | `2000` | Reject markets whose `liquidity` proxy (max of liquidity, volume·100, open-interest·100) is below this. |
| `MAX_SPREAD_CENTS` | int | `15` | Reject markets with bid-ask spread wider than this — eliminates most fake arbs on illiquid books. |
| `MIN_VOLUME_24H` | int | `0` | Reject markets with fewer than N contracts traded in the last 24h. Set above `0` to suppress stale books. |
| `STALE_LAST_AGE_SECONDS` | int | `60` | When `last_trade_age_seconds` exceeds this, the `last_price` weight in the fair-value blend halves every additional `STALE_LAST_AGE_SECONDS` (exponential decay). Confidence is also decayed on the same clock at a slower constant. |
| `RECENCY_WEIGHTS` | csv floats | `0.50,0.35,0.15` | Mid / last / prior weights inside `consensus_fair_price` (before stale-last decay). |
| `MIN_EDGE_PCT` | float | `0.5` | Pre-reject any opportunity whose edge as a percentage of entry price is below this. |
| `MIN_FILL_QTY` | int | `25` | Dutch-book baskets are flagged `fill_feasible=False` if any leg's top-of-book ask size is below this. |

### Sizing and bankroll

| Name | Type | Default | Description |
|---|---|---|---|
| `BANKROLL` | float | `1000` | Bankroll used to convert Kelly fractions into dollar stakes. |
| `KELLY_FRACTION` | float | `0.25` | Fractional-Kelly multiplier (¼-Kelly is the standard de-risked sizing for correlated markets). |
| `MAX_BET_PCT` | float | `0.05` | Hard cap on per-bet stake as a fraction of bankroll regardless of Kelly output. |
| `ARB_BANKROLL_SHARE` | float | `0.60` | Per-strategy bankroll sub-cap for arb / mispricing signals. |
| `FAIRVALUE_BANKROLL_SHARE` | float | `0.30` | Per-strategy bankroll sub-cap for fair-value signals. |

### Mode and binding

| Name | Type | Default | Description |
|---|---|---|---|
| `DEMO_MODE` | bool | `false` | Skip Kalshi and synthesize markets that fire all five signal types. |
| `HOST` | str | `0.0.0.0` | FastAPI host. |
| `PORT` | int | `8000` | FastAPI port. |

### Native WebSocket feed (advanced, opt-in)

The dashboard ships with REST polling, which works without credentials.
Kalshi *also* offers a native WebSocket market-data feed
(`wss://api.elections.kalshi.com/trade-api/ws/v2`) — that endpoint
requires **RSA-PSS-signed authentication**, so to use it you need an API
key pair from your Kalshi dashboard.

| Name | Type | Default | Description |
|---|---|---|---|
| `USE_NATIVE_WS` | bool | `false` | Reserved switch for the upcoming authenticated WS client. |
| `KALSHI_KEY_ID` | str | `""` | Your Kalshi API key ID. |
| `KALSHI_PRIVATE_KEY_PATH` | str | `""` | Filesystem path to the matching RSA private key. |

Leave these unset to stay on REST polling.

---

## Kalshi rate limits

Kalshi enforces per-key rate limits on the Trade API. Public read-only
endpoints (the ones this app uses) are less strict than authenticated
trading endpoints, but they are still throttled.

The poller is designed to stay well inside published limits:

1. **One request bundle per `POLL_INTERVAL_SECONDS` (default 5 s).**
   A single bundle fetches `/events?with_nested_markets=true` paginated
   up to 4 pages × 200 events = 800 events, i.e. roughly 1 request per
   200 events scanned.
2. **`POLL_JITTER_PCT` randomizes the interval ±15 %** so the request
   pattern is not a perfectly periodic fingerprint.
3. **Dedicated 429 / 503 backoff.** When Kalshi responds with `429 Too
   Many Requests` or `503 Service Unavailable`, the client switches to
   an exponential backoff that starts at 4 s and caps at 64 s (up to
   6 attempts), honoring any `Retry-After` header. This is separate
   from the general 1 s / 2 s / 4 s retry used for 5xx / network errors.
4. **No automatic depth fan-out.** The bulk `/markets/orderbooks`
   endpoint is auth-only and is not called. The single-market
   `/markets/{ticker}/orderbook` endpoint is used only on demand.
5. **No retries on 4xx other than 429.** Bad requests fail fast.

If you start seeing sustained 429s, raise `POLL_INTERVAL_SECONDS`
(e.g. to 10 s) — the analyzer will simply update half as often.

---

## Notes / disclaimers

- This is a **research and visualization tool**, not an autotrader. It
  does not place orders or require Kalshi credentials.
- Arbitrage strategies (`yes_no_arbitrage`, `dutch_book_arbitrage`) are
  the only signals with guaranteed positive payoff *if you fill every
  leg at the posted prices*. Kalshi fees, partial fills and queue
  position will erode thin arbs, so check size before sending orders.
- Nothing here is financial advice.
