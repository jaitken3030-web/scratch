# arbot — Kalshi ⇄ Polymarket arbitrage bot

An asyncio bot that scans manually approved pairs of prediction markets
across Kalshi and Polymarket for cross-platform arbitrage: when buying
YES on one platform and NO on the other costs less than the guaranteed
$1 payout (after both platforms' fees), it flags — and optionally
executes — the trade.

**Dry-run is the default.** Nothing is sent to an exchange until you
explicitly set `execution.dry_run: false` in `config.yaml`.

## How it works

```
pairs.yaml (manual whitelist)          .env (API keys)
        │                                  │
        ▼                                  ▼
  BookStore ── polls Kalshi + Polymarket CLOB order books
        │       (best bid/ask + full ask-side depth)
        ▼
  arb math ── walks both ask ladders level by level,
        │      adds Kalshi + Polymarket fees, sizes the fill
        ▼
  Scanner ── records every opportunity (taken or skipped, with reason)
        │      checks kill switch / loss limit / exposure caps
        ▼
  Executor ── dry-run: logs + ledgers intended trades
        │      live: thinner leg first, then hedge; timeout ⇒ cancel + unwind
        ▼
  SQLite ledger + append-only audit log ──> `arbot report`
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'          # add '.[polymarket-live]' for live Polymarket orders

cp config.example.yaml config.yaml
cp pairs.example.yaml pairs.yaml
cp .env.example .env             # then fill in your keys
```

Credentials go in `.env` (never committed):

| Variable | Purpose |
|---|---|
| `KALSHI_API_KEY_ID` | Kalshi API key id (Account → API keys) |
| `KALSHI_PRIVATE_KEY_PATH` | Path to the RSA private key Kalshi gave you |
| `POLYMARKET_API_KEY/SECRET/PASSPHRASE` | Polymarket CLOB API credentials |
| `POLYMARKET_PRIVATE_KEY` | Wallet key — only needed for **live** order signing |
| `ALERT_WEBHOOK_URL` | Optional webhook for safety alerts |

Run the tests:

```bash
python -m pytest
```

## Workflow

### 1. Find candidate pairs (you approve every one by hand)

```bash
arbot suggest-pairs                 # fetches open markets from both platforms
arbot suggest-pairs --min-score 0.7 # stricter fuzzy matching
```

This prints YAML snippets fuzzy-matched by title. The bot **never**
trades from these suggestions directly: every candidate is emitted with
`enabled: false` and a `notes` field prompting you to verify resolution
criteria. Paste the ones you approve into `pairs.yaml`, write down the
resolution differences you found in `notes`, and set `enabled: true`.

### 2. Scan (dry-run)

```bash
arbot run
```

Polls order books for every enabled pair at `poll_interval_s`, computes
both arb directions — (YES on Kalshi + NO on Polymarket) and (NO on
Kalshi + YES on Polymarket) — using real ask prices, walking book depth,
subtracting Kalshi's `0.07·C·P·(1−P)` fee and Polymarket's
`rate·min(p,1−p)` fee, and flags anything whose net edge exceeds
`min_edge_bps`. Every opportunity is written to the ledger, including
the ones it skips and why.

### 3. Review

```bash
arbot report                    # positions, locked capital, edges seen vs taken, skip reasons
arbot report --include-dry-run  # count simulated (paper) fills as positions
```

### 4. Go live (when you're ready)

Set `execution.dry_run: false` in `config.yaml`. Live execution:

1. Sizes the trade by `max_usd_per_arb`, `max_total_exposure_usd`,
   `per_market_exposure_usd`, and `max_book_depth_pct` (never take more
   than that fraction of visible depth).
2. Places the **thinner/cheaper leg first** as a limit order.
3. Once it fills, places the second leg. If the second leg doesn't fill
   within `second_leg_timeout_s`, it cancels the remainder, **unwinds
   the first leg with a market order**, records the realized loss, and
   writes an incident to the audit log.
4. Alerts (log + webhook) on any unhedged position.

## Safety rails

- **Kill switch**: `touch KILL` (path configurable) halts all trading
  instantly; checked every cycle and before every order.
- **Daily loss limit**: realized P&L below `-daily_loss_limit_usd`
  halts trading for the day and fires an alert.
- **Exposure caps**: per-market and total caps shrink or block new arbs.
- **Append-only audit log**: SQLite triggers reject UPDATE/DELETE on
  `audit_log`, and every event is mirrored to a JSON-lines file.

## ⚠️ Resolution-mismatch risk (read this before enabling any pair)

The entire "guaranteed $1" argument rests on one assumption: **the two
markets resolve identically in every state of the world.** If they can
diverge, this is not an arbitrage — it's a correlated bet with tail risk
of losing *both* legs (you bought YES on A and NO on B; if A resolves NO
and B resolves YES, both expire worthless).

Titles that look identical routinely hide differences in:

- **Resolution source**: Kalshi resolves from a named source per its
  contract terms; Polymarket resolves via its UMA-based oracle and the
  market's own description. The same event can be scored differently.
- **Deadlines and time zones**: "by March 31" ET vs UTC, market close
  vs settlement date, "announced" vs "takes effect".
- **Edge cases**: candidate drops out, event postponed, tie/void rules,
  intermeeting Fed moves, revised economic data, "official" vs
  preliminary figures.
- **Void/refund behavior**: one platform may void and refund while the
  other resolves NO — you lose one leg outright.
- **Early resolution**: Polymarket markets can resolve as soon as the
  oracle allows; Kalshi waits for its source. Capital can be locked on
  one side long after the other paid out (that's a financing cost, and
  a reinvestment-risk window).

That's why matching is manual: read the **full rules** of both markets,
write the differences into the pair's `notes` field, and only set
`enabled: true` if you would bet the whole spread on identical
resolution. When in doubt, size down or skip — a 200 bps edge does not
compensate for a 2% chance of a 100% loss on both legs.

Other real-world gaps this bot does not remove: fill risk between legs
(mitigated but not eliminated by the unwind logic), fee/rule changes,
API downtime at the worst moment, and capital lockup until resolution.

## Package layout

```
src/arbot/
  config.py        config.yaml + .env loading
  models.py        books, pairs, legs, opportunities, orders
  fees.py          Kalshi + Polymarket fee models
  arb.py           depth-walking arb math (both directions)
  pairs.py         pairs.yaml whitelist + fuzzy-match suggester
  clients/         Kalshi REST (RSA-signed) + Polymarket CLOB clients
  scanner.py       BookStore polling + scan/execute loop
  executor.py      dry-run + live execution, leg ordering, unwind
  safety.py        kill switch, loss limit, exposure caps, alerts
  ledger.py        SQLite: opportunities/orders/fills/P&L + audit log
  report.py        `arbot report`
  cli.py           argparse entry points
tests/
  fixtures/        recorded API responses (order books, market lists)
  test_arb.py      arb math unit tests (fixed fixtures)
  test_fees.py     fee model unit tests
  test_executor.py dry-run, happy path, leg2-timeout unwind
  test_paper_trading.py  recorded-response integration test
```

## Disclaimer

This is trading software. It can lose money in ways it wasn't designed
to anticipate. Regulatory treatment of cross-platform prediction-market
trading varies by jurisdiction — make sure you're allowed to use both
platforms before funding anything. No warranty.
