---
name: run-polyprinter
description: Build, run, and drive PolyPrinter (Polymarket trader-scouting dashboard + Scout + Mirror services). Use when asked to start polyprinter, run the dashboard, run Scout or Mirror against live Polymarket data, seed the demo decision, or smoke-test the stack.
---

PolyPrinter is a three-service Docker Compose app: `scout` (a batch job
that pulls live Polymarket data and computes trader dossiers), `mirror`
(polling-mode trade detection against Scout's current watchlist — Phase 2,
paper only, no Mandates yet so every entry resolves to SKIP/NO_MANDATE by
design), and `dashboard` (a read-only Flask server on `127.0.0.1:8765`).
Drive it with `.claude/skills/run-polyprinter/driver.sh` — a curl-based
smoke script that builds, launches, seeds the Phase 0 fixture, runs Scout
and Mirror against **live** Polymarket data, and asserts on every
dashboard route. All paths below are relative to the repo root
(`/opt/polyprinter`), not to this skill directory.

## Prerequisites

Docker + Docker Compose v2. Nothing else — no credentials, no API keys.
Every Polymarket endpoint Scout/dashboard use (leaderboard, positions,
activity, resolutions) is public and unauthenticated (verified live, see
`docs/PRD.md` §9).

```bash
docker --version         # tested against 29.7.2
docker compose version   # tested against v5.4.0
```

## Build

```bash
docker compose build
```

## Run (agent path)

```bash
bash .claude/skills/run-polyprinter/driver.sh
```

This does everything, in order, against the real stack: build → reset the
local db → `docker compose up -d dashboard` → check the port is published
loopback-only → seed the Phase 0 demo decision and confirm it renders →
hit every dashboard route and check status codes → run Scout against
**live** Polymarket data (`--leaderboard-limit 5`, ~1-2 min) → confirm real
trader rows rendered on `/traders` → run Mirror once against **live**
Polymarket data (polling mode, paper) → confirm 100% decision coverage
(every `observed_trades` row has a `decisions` row — invariant 1) and that
a `mirror` heartbeat exists (FR-19) → run the pytest suite → `docker
compose down`. Exits non-zero on the first failed assertion; prints
`ok`/`FAIL` per check.

To just look at the dashboard interactively instead of running the full
driver:

```bash
docker compose up -d dashboard
docker compose exec dashboard python scripts/seed_demo_decision.py   # Phase 0 fixture
docker compose run --rm scout python -m polyprinter.scout.run --leaderboard-limit 10   # fast Scout pass
docker compose run --rm mirror python -m polyprinter.mirror.run   # one Mirror poll cycle
curl -s http://127.0.0.1:8765/traders | less
# or open http://127.0.0.1:8765/ in a browser on the same machine
```

A **default-scale** Scout run (`docker compose run --rm scout python -m
polyprinter.scout.run`, no `--leaderboard-limit`) pulls ~200+ unique
candidates and takes roughly 7-8 minutes — fine for the daily service loop
(`--loop`), too slow for iterating. Always pass `--leaderboard-limit` (5-10)
when driving this interactively.

## Run (human path)

```bash
docker compose up -d          # all three services: dashboard + scout --loop + mirror --loop
open http://127.0.0.1:8765/   # only reachable from the same machine
docker compose down
```

## Test

```bash
docker run --rm -v "$PWD/tests:/app/tests" polyprinter-scout \
  sh -c "pip install --no-cache-dir pytest -q && python -m pytest tests -q"
```

45 tests. Scout's against frozen real API fixtures in `tests/fixtures/`
(captured live 2026-08-07) — a shape change in the real API breaks these
before it breaks a live run. Mirror's against a temp-file db (real SQLite,
real schema, via `db/conn.get_connection(tmp_path / "test.db")`) with a
fake data client — no network, but real inserts, real foreign keys, real
invariant checks.

---

## Gotchas

- **Flask bound to `127.0.0.1` *inside* the container is unreachable from
  the host, even with the port published.** Docker's port-forwarding
  arrives via the container's `eth0`, not its loopback — `127.0.0.1:8765`
  inside the container only accepts connections from that same network
  namespace. `dashboard/server.py` binds `0.0.0.0` *inside* the container;
  the actual "localhost only" guarantee is enforced one layer out, by
  `docker-compose.yml`'s `"127.0.0.1:8765:8765"` port mapping, which
  restricts the **host's** exposure to loopback. The driver's port-binding
  check (`docker compose port dashboard 8765`) verifies this from outside.

- **`pip install .` leaves the source copy on disk, and that's a
  correctness bug, not just clutter.** After `COPY polyprinter
  ./polyprinter && pip install .`, both `/app/polyprinter` (source) and
  the site-packages copy exist. `python -m x` / `python -c` put the cwd
  (`/app`) on `sys.path[0]` and resolve to the source copy; but `python
  path/to/script.py` (file-path invocation, e.g. running
  `scripts/seed_demo_decision.py` directly) puts the **script's own
  directory** on `sys.path[0]` instead, skips `/app`, and silently
  resolves `import polyprinter` to the *other* copy in site-packages — a
  different module object with its own `__file__`. Any code that derives
  paths via `Path(__file__).resolve().parents[N]` (db path, log dir,
  config dir) then points at the wrong place, silently, with no error —
  each invocation style gets a different, seemingly-empty database. Fixed
  two ways: the Dockerfile removes the redundant source copy after
  install (`rm -rf ./polyprinter ./build ./polyprinter.egg-info`), and
  `config.py`/`conn.py`/`obs/log.py` derive paths from `POLYPRINTER_HOME`
  (an env var, set in the Dockerfile) or cwd — never from `__file__` —
  so it's correct regardless of which copy (if this class of bug ever
  recurs) actually gets imported.

- **`setuptools` drops non-`.py` files by default.** `db/schema.sql` and
  `dashboard/templates/*.html` are read at runtime but aren't Python —
  without an explicit `[tool.setuptools.package-data]` entry in
  `pyproject.toml`, the installed package silently has no schema (first
  db connection crashes with `FileNotFoundError`) or no templates (every
  page 500s with `jinja2.exceptions.TemplateNotFound`). Both showed up
  only once the built image was actually run, not from reading the code.

- **Adjacent SQL string literals aren't Python string concatenation.**
  Python concatenates adjacent literals (`"a" "b"` → `"ab"`), but that
  only applies to literals *in Python source* — two adjacent quoted
  strings sitting inside one big triple-quoted SQL string passed to
  `conn.execute()` are just invalid SQL syntax. Build the string in
  Python first, then interpolate it as a single bound parameter.

- **The published Polymarket rate limits are generous in aggregate but
  Scout still gets 429s (and the occasional 408).** Scout fires many
  requests in a tight loop across candidates with zero throttling; that
  burst can exceed a short 10-second window even when the day's total
  request count is nowhere near the documented limit. `sources/retry.py`
  retries 408/429/5xx with backoff (honoring `Retry-After` when present) —
  without it, ~2 of every 30 candidates failed their dossier fetch
  mid-run. 408 was added after a live full-scale run (2026-08-07) lost one
  candidate to a bare timeout that would've succeeded on retry.

- **`driver.sh` used to wipe the live dashboard's database, silently — twice.**
  It resets to a clean db on every run (`rm -f data/polyprinter.db*`) so
  its own assertions are deterministic — but that file is the *same* db
  the `dashboard`/`scout` services read in normal use. Running the driver
  after a real Scout pass looked, from the dashboard, exactly like data
  loss: every count back to zero. First fix (2026-08-07): move the live db
  aside to `data/polyprinter.db.driver-backup` before resetting, restore it
  via an `EXIT` trap. That fix had its own bug (caught 2026-08-08 during a
  driver run, which wiped a real 212-trader db a second time): the backup
  filenames still started with `polyprinter.db`, so the *same* `rm -f
  data/polyprinter.db*` reset line matched and deleted the backups too,
  before the trap ever got a chance to restore them — a glob doesn't know
  a name it matches is "the backup," it just matches the string. Fixed for
  real by parking the backup in its own subdirectory
  (`data/.driver-backup/`), which no glob this script uses on `data/*`
  reaches. Lesson: when protecting a file from a wildcard delete, moving
  it to a name the *same* wildcard still matches isn't protection.

- **`docker compose run` vs `docker compose exec`, and the agent
  sandbox's filesystem is NOT the Docker daemon's filesystem.** In this
  environment, files written into a bind-mounted volume by a container
  are consistently visible to *other containers* (dockerd's own view is
  self-consistent) but are **not** reliably visible via the agent's own
  `ls`/`cat`/`sqlite3` on the same host path — the agent's shell and the
  Docker daemon do not share a filesystem view here even though paths
  look identical as strings. Verify container-written state by execing
  back *into* a container (`docker compose exec ... python -c "..."`) or
  over the network (`curl` against a published port), never by reading
  the bind-mount path directly from the agent's own shell.

## Troubleshooting

- **`curl: (7) Failed to connect` / all routes return connection refused**
  right after `docker compose up -d dashboard`: give it ~2s to start
  (`sleep 2`) before the first request — Flask's dev server needs a
  moment.
- **`jinja2.exceptions.TemplateNotFound: <name>.html`**: package-data glob
  in `pyproject.toml` doesn't match a template — check
  `[tool.setuptools.package-data]` includes `dashboard/templates/*.html`,
  then rebuild (`docker compose build`).
- **`FileNotFoundError: .../polyprinter/db/schema.sql`**: same class of
  bug — check `package-data` includes `db/schema.sql` and
  `db/migrations/*.sql`, rebuild.
- **A script run as `python scripts/foo.py` behaves differently from the
  same code run as `python -m polyprinter.x`** (e.g. writes to a
  DB/log location the other doesn't see): check for a stray
  `/app/polyprinter` source copy alongside the site-packages install
  (`docker compose exec <svc> find / -xdev -iname "*polyprinter*"
  -maxdepth 6`) — should show exactly one hit under `site-packages`.
- **Scout run looks hung with no log output for minutes**: it's not
  hung, it's just slow at default scale (~200+ candidates × several
  requests each ≈ 7-8 min). Use `--leaderboard-limit 5` for a fast
  interactive check; watch for `dossier.progress` lines every 10
  candidates.
- **Mirror's first poll of a newly-watched trader almost always shows 0
  new decisions, and that's correct, not a bug.** A trader only enters
  the watchlist once Scout has scored them; from that moment Mirror
  watches *forward* (real-time detection, not a backfill — FR-11), so the
  very first cycle only covers however many seconds have passed since
  they were first added. Give it a few `mirror.poll_interval_seconds`
  cycles (default 60s) before expecting a real TRADE to show up, even
  against genuinely active top-20 traders.
- **Almost every Mirror decision is `SKIP`/`NO_MANDATE`, and that's the
  correct Phase 2 state, not a bug either.** Mandates don't exist until
  Phase 3 — `mirror/decide.py` checks for one on every BUY and finds
  none. This is what "100% decision coverage" (Phase 2's actual exit
  criterion) is proving: the full detect → decide → write pipeline works
  end-to-end, safely, before any real money-shaped decision is possible.
  `SELL`s resolve to `SKIP`/`NO_MATCHING_POSITION` for the same root
  reason — we never took the entry, so there's nothing of ours to exit.
- **`mirror/decide.py` doesn't check a mandate's category-allow/block or
  minimum-liquidity fields, on purpose.** Neither a trade's category nor
  its market's liquidity is available on an `observed_trades` row —
  `scout/dossier.py` hit the identical gap computing
  `category_mix_json`/`median_market_liquidity` (see that file's field
  comments; both need a gamma-api lookup per market that hasn't been
  built). `mirror/fills.py`'s book-walk is similarly real code with no
  live order-book source wired to it yet, for the same reason: no
  verified-live CLOB endpoint, and the `oracle_legacy/` code the repo
  structure doc says to port fee logic from doesn't exist in this repo.
  All of this is inert anyway until Phase 3 makes a TAKE possible at all
  — see the point above.
