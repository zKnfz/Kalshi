# Contributing

Thanks for hacking on the Kalshi Edge Analyzer. This guide covers the
local-dev loop: setup, tests, demo mode, and the conventions we use
for new signals.

## Setup

```bash
git clone <repo>
cd Kalshi
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest
cp .env.example .env
```

Everything below assumes the venv is active.

## Running the app locally

### Against live Kalshi (default)

```bash
python run.py
# → http://localhost:8000
```

The server polls Kalshi's public Trade API every `POLL_INTERVAL_SECONDS`
(jittered) and pushes diff-only updates to the dashboard over a
WebSocket. No credentials are required.

### Offline / demo mode

```bash
DEMO_MODE=true python run.py
```

Demo mode synthesizes a small event set tuned so **all five** signal
types fire on every tick:

| Demo event | Fires |
|---|---|
| `ARB-DEMO` | `yes_no_arbitrage` |
| `FED-DEMO` | `dutch_book_arbitrage` |
| `WX-DEMO`  | `dutch_book_mispricing` |
| `ECON-DEMO` | `fair_value_yes` / `fair_value_no` |

Use demo mode any time you're iterating on the analyzer, the UI, or the
WebSocket protocol — you don't have to be online and you'll get
deterministic, controllable data.

## Running the tests

```bash
python -m pytest -q tests
```

The suite is fast (well under a second) and covers:

- Kelly math (closed form ↔ explicit `b·q` form equivalence,
  user-supplied numerical example, zero-edge ⇒ zero stake).
- Sizing: `MAX_BET_PCT` and per-strategy bankroll sub-cap enforcement.
- Pre-signal filters: `MAX_SPREAD_CENTS`, `MIN_VOLUME_24H`, status,
  liquidity.
- Stale-last decay: confidence drops and the fair-value blend's
  bias toward old prints decays back toward mid.
- Dedup: a market that lights up under multiple strategies merges into
  one row with `signal_types[]`.
- Demo coverage: every signal type fires.
- Engine integration: mock-poll across three ticks asserting
  `added → empty diff → updated` and that `first_seen` is preserved.

Add a test for every new signal or scoring change.

## Module map

| File | Purpose |
|---|---|
| `analyzer.py` | Pure scoring / signal logic (no I/O). |
| `fees.py` | Official Kalshi fee model. |
| `auth.py` | RSA-PSS request signing. |
| `client.py` | Async REST client with backoff. |
| `ws_client.py` | Native WebSocket client (auth required). |
| `polymarket.py` | Gamma client + cross-platform arb. |
| `paper.py` | Paper-trading engine (persisted ledger). |
| `execution.py` | Order router with circuit breakers. |
| `alerts.py` | Telegram + Discord webhook notifier. |
| `backtest.py` | JSONL snapshot recorder + replay. |
| `cli.py` | `python -m kalshi_analyzer.cli ...`. |
| `engine.py` | Glues everything together; the only stateful place. |
| `server.py` | FastAPI + dashboard. |

## Adding a new signal

1. Implement an `analyze_<your_signal>(market_or_event)` function in
   `kalshi_analyzer/analyzer.py` that returns either an `Opportunity`
   or `list[Opportunity]`. Use `_build_opportunity()` to get
   `MIN_EDGE_PCT`, sizing, scoring, and stale-last confidence handling
   for free.
2. Call it from `evaluate_markets()` so deduplication picks it up. If
   it's a per-event signal, gate it behind `mx and len(tradable) >= 2`
   so the heuristic mutually-exclusive detector still applies.
3. Add a label entry to `STRATEGY_LABEL` in
   `kalshi_analyzer/static/app.js` so it renders as a chip on the
   dashboard.
4. Extend `_build_demo_events()` in `kalshi_analyzer/engine.py` with a
   demo event that fires your signal — and a test in
   `tests/test_analyzer.py` that asserts the same.

## Coding conventions

- Pure-Python, no external services in `analyzer.py`. Tests must be
  able to construct `Market` / `Event` objects and call the analyzer
  with no network. The engine is responsible for I/O.
- Comments explain non-obvious *why*, not the *what* the code already
  says.
- Prices live in **integer cents** inside `Market` and convert to
  floats in `[0, 1]` (`_safe_cents_to_dollars`) right before any
  arithmetic.
- All Kalshi payload differences (legacy `yes_bid`, new
  `yes_bid_dollars`) are absorbed in `Market.from_api`. The rest of
  the code only sees normalized fields.

## Commit and PR conventions

- One logical change per commit. Multi-area edits use a leading scope
  tag, e.g. `analyzer:` / `engine:` / `ui:` / `tests:` / `docs:`.
- Do not amend or force-push.
- Add tests for behavior changes; bug fixes get a regression test.
- Update the relevant `.env.example` keys and the README config table
  in the same commit as the setting they describe.
