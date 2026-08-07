# Structure & Schema — PolyPrinter

*v1.0 · 2026-08-07*

---

## 1. Repo structure

```
polyprinter/
├── docker-compose.yml
├── Dockerfile                  # deps baked in — NEVER a /tmp venv
├── pyproject.toml
├── config.yaml                 # committed defaults
├── config-overrides.yaml       # gitignored, written by Telegram /set
├── .env.example                # shape only, never real values
│
├── polyprinter/
│   ├── db/
│   │   ├── schema.sql          # §2 below, single source of truth
│   │   ├── migrations/         # numbered, forward-only
│   │   └── conn.py             # WAL mode, single-writer discipline
│   │
│   ├── sources/                # ALL external I/O lives here
│   │   ├── polymarket_data.py  # data-api.polymarket.com: leaderboard
│   │   │                       # (/v1/leaderboard), activity, positions,
│   │   │                       # closed-positions, trades — see PRD §9
│   │   ├── polymarket_gamma.py # gamma-api.polymarket.com: market resolutions
│   │   │                       # (/markets?condition_ids=&closed=true — the
│   │   │                       # closed=true is NOT optional, see PRD §9)
│   │   ├── polymarket_clob.py  # order book snapshots  [port from oracle/book.py]
│   │   ├── chain.py            # Polygon event subscription
│   │   └── raw_store.py        # persist raw responses before parsing
│   │
│   ├── scout/
│   │   ├── discover.py         # 4 leaderboard windows + non-profit sampling (FR-2)
│   │   ├── dossier.py          # metric computation
│   │   ├── shrinkage.py        # sample-size adjustment
│   │   └── resolutions.py      # outcome ingestion (FR-6)
│   │
│   ├── mandate/
│   │   ├── prompt.py           # dossier → prompt
│   │   ├── issue.py            # LLM call, validate, persist
│   │   ├── schema.py           # pydantic model, hard validation
│   │   └── trigger.py          # delta-detection (FR-5)
│   │
│   ├── mirror/
│   │   ├── watch_poll.py       # phase 2: polling
│   │   ├── watch_events.py     # phase 4: on-chain
│   │   ├── position_model.py   # THEIR running position per token (FR-12)
│   │   ├── decide.py           # mandate lookup + caps → TAKE/SKIP
│   │   ├── sizing.py           # per-trade, portfolio, correlation caps
│   │   └── fills.py            # book-walk simulation
│   │
│   ├── learner/
│   │   ├── calibrate.py
│   │   └── shadow.py           # benchmark portfolio (FR-24)
│   │
│   ├── obs/
│   │   ├── log.py              # structured, to file AND db
│   │   ├── heartbeat.py
│   │   └── llm_log.py          # every call: prompt, response, cost, latency
│   │
│   ├── dashboard/
│   │   ├── server.py           # reads db directly, localhost-bound
│   │   └── templates/
│   │
│   └── telegram/
│       └── bot.py              # only config-write surface
│
├── oracle_legacy/              # ported from v2, DO NOT rewrite
│   ├── clob_v2.py              # signature type 1 / POLY_PROXY — expensive knowledge
│   └── fees.py
│
└── tests/
    ├── test_dossier.py
    ├── test_sizing.py
    ├── test_fills.py
    └── fixtures/               # frozen real API responses
```

**Structural rule:** every external call goes through `sources/` and persists its raw response before anything parses it. When an endpoint shape changes — and it will — you find out from a parse error against stored data, not from silently wrong dossiers.

---

## 2. Database schema

SQLite, WAL mode. Postgres-compatible where cheap.

```sql
-- ─── Traders ────────────────────────────────────────────────────

CREATE TABLE traders (
    address           TEXT PRIMARY KEY,          -- lowercase, always
    alias             TEXT,
    first_seen        TEXT NOT NULL,
    last_trade_at     TEXT,
    active            INTEGER NOT NULL DEFAULT 1,
    discovery_source  TEXT NOT NULL,             -- 'lb_day' | 'lb_week' | 'lb_month'
                                                 -- | 'lb_all' | 'volume_sample'
                                                 -- | 'resolved_count_sample'
                                                 -- (leaderboard windows match the
                                                 -- API's real timePeriod enum —
                                                 -- see docs/PRD.md §9)
    UNIQUE (address)
);

-- Append-only. Never UPDATE. The trajectory is the signal.
CREATE TABLE trader_snapshots (
    id                    INTEGER PRIMARY KEY,
    address               TEXT NOT NULL REFERENCES traders(address),
    scanned_at            TEXT NOT NULL,

    -- performance
    roi_raw               REAL,
    roi_shrunk            REAL,    -- (n*roi + k*pop_mean)/(n+k), k≈30
    capital_deployed_usd  REAL,
    realised_pnl_usd      REAL,
    resolved_positions    INTEGER,
    win_rate              REAL,
    avg_win_usd           REAL,
    avg_loss_usd          REAL,
    win_loss_ratio        REAL,
    concentration_top1    REAL,    -- share of P&L from best market
    concentration_top5    REAL,

    -- copyability
    hold_to_resolution_rate REAL,  -- MOST predictive field
    median_hold_hours       REAL,
    p90_hold_hours          REAL,
    entry_price_p10         REAL,
    entry_price_median      REAL,
    entry_price_p90         REAL,
    sizing_cv               REAL,  -- coeff. of variation: flat vs conviction
    scale_frequency         REAL,  -- share of positions built/trimmed in pieces
    median_market_liquidity REAL,
    category_mix_json       TEXT,  -- {category: {n, roi}}

    -- liveness
    trades_7d             INTEGER,
    trades_30d            INTEGER,
    open_positions        INTEGER
);
CREATE INDEX idx_snap_addr_time ON trader_snapshots(address, scanned_at DESC);

-- ─── Mandates ───────────────────────────────────────────────────

CREATE TABLE mandates (
    id                      INTEGER PRIMARY KEY,
    address                 TEXT NOT NULL REFERENCES traders(address),
    version                 INTEGER NOT NULL,
    snapshot_id             INTEGER REFERENCES trader_snapshots(id),
    llm_call_id             INTEGER REFERENCES llm_calls(id),

    verdict                 TEXT NOT NULL,  -- 'FOLLOW' | 'SKIP' | 'WATCH'
    confidence              TEXT NOT NULL,  -- 'LOW' | 'MED' | 'HIGH'
    max_position_usd        REAL,
    categories_allowed_json TEXT,
    categories_blocked_json TEXT,
    min_entry_price         REAL,
    max_entry_price         REAL,
    min_market_liquidity    REAL,
    reasoning               TEXT NOT NULL,  -- prose, shown verbatim on dashboard

    issued_at               TEXT NOT NULL,
    expires_at              TEXT NOT NULL,
    superseded_by           INTEGER REFERENCES mandates(id),
    UNIQUE (address, version)
);
CREATE INDEX idx_mandate_active ON mandates(address, expires_at)
    WHERE superseded_by IS NULL;

-- ─── Observation ────────────────────────────────────────────────

CREATE TABLE observed_trades (
    id              INTEGER PRIMARY KEY,
    address         TEXT NOT NULL REFERENCES traders(address),
    tx_hash         TEXT NOT NULL,
    log_index       INTEGER NOT NULL,
    market_id       TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL,      -- 'BUY' | 'SELL'
    shares          REAL NOT NULL,
    price           REAL NOT NULL,      -- their fill
    block_ts        TEXT NOT NULL,
    detected_at     TEXT NOT NULL,      -- ours; the delta is our lag
    source          TEXT NOT NULL,      -- 'poll' | 'event'
    their_position_after REAL,          -- running model (FR-12)
    UNIQUE (tx_hash, log_index)         -- idempotency (FR-15)
);
CREATE INDEX idx_obs_addr_time ON observed_trades(address, block_ts DESC);

-- ─── Decisions: EVERY observed trade lands here, take or skip ───

CREATE TABLE decisions (
    id                 INTEGER PRIMARY KEY,
    observed_trade_id  INTEGER NOT NULL UNIQUE REFERENCES observed_trades(id),
    mandate_id         INTEGER REFERENCES mandates(id),
    decided_at         TEXT NOT NULL,
    verdict            TEXT NOT NULL,   -- 'TAKE' | 'SKIP' | 'MIRROR_EXIT'
    skip_reason_code   TEXT,            -- 'NO_MANDATE' | 'MANDATE_EXPIRED'
                                        -- | 'CATEGORY_BLOCKED' | 'PRICE_BAND'
                                        -- | 'LIQUIDITY' | 'PORTFOLIO_CAP'
                                        -- | 'CORRELATION_CAP' | 'NO_CAPITAL'
                                        -- | 'NO_MATCHING_POSITION'
    skip_reason_text   TEXT,
    size_usd           REAL,
    mode               TEXT NOT NULL,   -- 'paper' | 'live' | 'shadow'
    latency_ms         INTEGER
);
CREATE INDEX idx_dec_verdict_time ON decisions(verdict, decided_at DESC);

-- ─── Our positions ──────────────────────────────────────────────

CREATE TABLE positions (
    id                INTEGER PRIMARY KEY,
    decision_id       INTEGER NOT NULL REFERENCES decisions(id),
    address           TEXT NOT NULL,   -- who we're tailing
    market_id         TEXT NOT NULL,
    token_id          TEXT NOT NULL,
    mode              TEXT NOT NULL,

    shares_open       REAL NOT NULL,   -- decrements on partial exits
    shares_total      REAL NOT NULL,
    our_entry_price   REAL NOT NULL,   -- book-walked, fees applied
    their_entry_price REAL NOT NULL,
    cost_usd          REAL NOT NULL,
    opened_at         TEXT NOT NULL,
    closed_at         TEXT,
    status            TEXT NOT NULL    -- 'OPEN' | 'PARTIAL' | 'CLOSED' | 'RESOLVED'
);
CREATE INDEX idx_pos_open ON positions(status, mode) WHERE status != 'CLOSED';

-- Proportional exits (FR-13): one row per leg, not one close per position
CREATE TABLE position_exits (
    id                INTEGER PRIMARY KEY,
    position_id       INTEGER NOT NULL REFERENCES positions(id),
    decision_id       INTEGER NOT NULL REFERENCES decisions(id),
    shares_sold       REAL NOT NULL,
    fraction_of_theirs REAL NOT NULL,  -- they sold X% → we sell X%
    our_exit_price    REAL NOT NULL,
    their_exit_price  REAL NOT NULL,
    proceeds_usd      REAL NOT NULL,
    exited_at         TEXT NOT NULL
);

-- ─── Outcomes & the metric that decides everything ──────────────

CREATE TABLE outcomes (
    id                    INTEGER PRIMARY KEY,
    position_id           INTEGER NOT NULL UNIQUE REFERENCES positions(id),
    resolved_at           TEXT,
    resolution            TEXT,

    our_pnl_usd           REAL NOT NULL,
    our_roi               REAL NOT NULL,
    their_pnl_per_share   REAL,

    -- Copy tax in CENTS of price, not percent. Price IS probability here:
    -- buying at 55c vs 53c costs 2c of a $1 payout, which may be 100% of
    -- the edge. Percent-of-price is the wrong denominator. (audit F3)
    copy_tax_entry_cents  REAL NOT NULL,   -- our_entry - their_entry
    copy_tax_exit_cents   REAL,            -- their_exit - our_exit
    copy_tax_total_cents  REAL,
    their_edge_cents      REAL             -- for the ratio that gates go-live
);

-- ─── Observability ──────────────────────────────────────────────

CREATE TABLE llm_calls (
    id              INTEGER PRIMARY KEY,
    purpose         TEXT NOT NULL,      -- 'mandate' | 'calibration_review'
    model           TEXT NOT NULL,
    prompt          TEXT NOT NULL,      -- full, not truncated
    raw_response    TEXT NOT NULL,
    parsed_ok       INTEGER NOT NULL,
    parse_error     TEXT,
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_usd        REAL,
    latency_ms      INTEGER,
    called_at       TEXT NOT NULL
);

CREATE TABLE heartbeats (
    service     TEXT PRIMARY KEY,       -- 'scout'|'mirror'|'dashboard'|'telegram'
    last_beat   TEXT NOT NULL,
    detail_json TEXT                    -- last event ts, queue depth, etc.
);

CREATE TABLE events (                   -- structured log, queryable
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    service     TEXT NOT NULL,
    level       TEXT NOT NULL,
    message     TEXT NOT NULL,
    context_json TEXT
);
CREATE INDEX idx_events_ts ON events(ts DESC);

CREATE TABLE raw_responses (            -- every external call, before parsing
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    url         TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    status      INTEGER,
    body        TEXT
);
```

---

## 3. Invariants

1. **Every `observed_trades` row has exactly one `decisions` row.** No silent drops. Enforce with a reconciliation query in the daily digest.
2. **Exits are never gated by mandate state.** A `MIRROR_EXIT` decision proceeds with an expired or absent mandate. Only entries consult mandates.
3. **`trader_snapshots` is INSERT-only.** No UPDATE, ever.
4. **`llm_calls.prompt` and `raw_response` are stored in full.** Truncation destroys the only audit trail that matters.
5. **One writer per table family.** Scout owns traders/snapshots/mandates; Mirror owns observed/decisions/positions; Learner owns outcomes. Dashboard reads only.
6. **`mode` is on every decision and position.** Paper, live, and shadow ledgers never mix in a query without an explicit filter.
