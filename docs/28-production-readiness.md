# 28 — Production readiness

Written 2026-09-05 against `3f4d121`. `docs/26-first-live-run.md` is the gate
for the *game*: what to settle before a write reaches a real marketplace. This
document is the gate for the *machine*: the secrets, the database, the two
servers, and the flags that decide whether a run writes at all. Every path,
symbol and default named here was read in the tree at `3f4d121`; where something
does **not** exist, that is stated as a finding rather than filled in with a
plausible name.

The one rule, as in `docs/26`: **nothing here may be skipped because the
previous item looked fine.** Each item is here because something in this
repository is already in the state it warns about.

## 0. The six gates, on one page

| # | Gate | Where it bites | Status today |
|---|---|---|---|
| 1 | Secrets | `docs/GITLAB.md`, `.env`, `~/.travian/.web_keys` | **A live credential is committed.** Act first, read the rest after |
| 2 | The database | `~/.travian/travian_web.db` | `create_all`, no migrations, no downgrade |
| 3 | The servers | :80 and :8001, one checkout | A frontend build **is** a production deploy |
| 4 | The live flags | `TRAVIAN_TRADE_ROUTE_LIVE`, `execution_mode` | Live writes default **on** |
| 5 | The game gate | `docs/26-first-live-run.md` | Not yet run. Do not skip it |
| 6 | Crash recovery | the trace, `run-history`, the undo panel | All three exist; rehearse the undo once |

---

## 1. Secrets

### 1.1 The GitLab root password — rotate it, and rotate it first

`docs/GITLAB.md` contains the local GitLab instance's `root` password in
plaintext under an "Access" table. It is **not only in history**:
`git ls-files docs/GITLAB.md` returns the path, so the credential is in the
working tree at `3f4d121` and in every commit that ships this file.
`git remote -v` gives `https://github.com/tomer60300/travian-auto-player.git`.
The repository's own `.gitignore` records the situation in a comment: it now
allowlists `.claude/` contents one file at a time rather than denylisting,
because a blanket rule is "untenable in a public repo with a recorded
credential exposure".

**The order of operations matters, because deleting the line does nothing.**

1. **Change the password in GitLab.** Rotation is the only fix. A published
   secret stays published: removing it from the file, and even rewriting
   history, does not un-publish what was already cloned, cached or scraped.
2. **Then** remove the value from `docs/GITLAB.md`, leaving the instructions
   and a pointer to wherever the new one is kept — which is not this repository.
3. **Then** decide about history separately. A rewrite is a distinct, disruptive
   operation and it is not what makes the account safe; step 1 is.

### 1.2 How to find the others — the method, not the values

Any credential ever committed is still reachable. The method:

- `git log -S'<fragment>' --all --oneline` — every commit whose diff **changed
  the number of occurrences** of that string, across all refs. This is the tool
  for "was this ever in the tree", and it is the one to reach for first because
  it searches content rather than filenames.
- `git log -p --all -- <path>` — the full history of one file you already
  suspect, so you can see the value's introduction and removal.
- `git log --diff-filter=D --name-only --all` — files that were **deleted**, the
  common hiding place: a secrets file removed in a later commit is still in
  every earlier one.
- `git grep -n '<fragment>' $(git rev-list --all)` — the exhaustive form. Slow
  on this history; use it to confirm, not to explore.

Search for the shapes rather than the values you remember: `password`,
`Initial Password`, `token`, `secret`, `api_key`, `Bearer `, `-----BEGIN`, and
the local usernames. Remember `archive/pre-scrub-*` branches if any still exist
locally — they exist precisely because they hold pre-scrub content, and they
must never be pushed.

**Do not print a found value anywhere it will persist.** Not into a pull
request, not into a commit message, not into an issue, and not into an agent
session (see 1.5). Redirect to a file outside the repository, act on it, delete
the file.

### 1.3 `.env`

`.env` is gitignored — it is the second line of `.gitignore`, under a
`# Credentials & secrets` heading, alongside `*.env.local`, `run.ps1` and
`agent-logs/`. `Settings` in `src/travian_api/config.py` reads it with
`"env_prefix": "TRAVIAN_"`, so every field below is `TRAVIAN_<FIELD>` in the
file. It holds `TRAVIAN_USERNAME` and `TRAVIAN_PASSWORD` — the game account.

**On permissions, one thing to know.** `web/auth.py` has
`_warn_if_world_readable`, and its first statement is `if os.name == "nt":
return`. Its docstring says why: `stat()` on Windows reports a synthetic `0o666`
with no relationship to the NTFS ACL, so the warning fired unconditionally and
told operators to run a `chmod` that cannot change anything. **So on this
machine there is no permission warning at all, for `.env` or for the key file.**
Restrict them with `icacls` and verify by hand; nothing in the app will tell you
if they are wide open.

### 1.4 The key file, which is a secret of the same class

`web/auth.py` declares `KEYS_FILE = DB_DIR / ".web_keys"`, where `DB_DIR` is the
directory of whatever `TRAVIAN_DB_PATH` points at — `~/.travian` by default. It
is a JSON object holding `jwt_secret` and `fernet_key`, generated on first boot
(`get_or_create_keys`), and the Fernet key is what encrypts the stored Travian
credential rows in the database.

Two consequences that decide how you back up:

- **The database without the key file is useless for credentials.** The comment
  in `auth.py` states the design: keys live next to the database they encrypt
  for, so moving or reusing a database elsewhere without its keys cannot
  decrypt its own credential rows.
- **The database *with* the key file is a copy of the credentials.** Back them
  up together, and treat the pair with the care you would give the password
  itself. A backup on a shared drive is a credential on a shared drive.

There is a one-time migration path in `get_or_create_keys` for deployments whose
keys were left behind in `~/.travian` when the database moved; it copies rather
than regenerating, because regenerating would orphan every credential row.

### 1.5 Never paste a credential into an agent session

The repository has a guard, and it is worth knowing exactly what it does and
does not cover. `.claude/settings.json` registers two `PreToolUse` hooks:

- on `Edit|Write`, any `file_path` matching `\.(env|secret|key)` is **blocked**;
- on `Bash`, a command matching `git commit.*--no-verify`, `rm -rf /`,
  `DROP TABLE`, `git push --force` or `git reset --hard` is **blocked**.

Those hooks protect the *files* and the *history*. They cannot un-see a value
typed into a prompt. A secret pasted into a session is in that session's
transcript, in whatever logs the transcript reaches, and in the context of every
subsequent turn. If an agent needs a credential, put it in `.env` and tell the
agent the variable's name.

### 1.6 One guard worth not weakening

`Settings.allowed_server_hosts` defaults to `travian.com` and is described as an
SSRF allowlist; its comment spells out the attack it closes — a
credential-bearing login pointed at a phishing host that merely *looks* like a
Travian server. Only hosts under a listed suffix are accepted. Widen it only for
a real fan or regional TLD, and never to a bare wildcard.

---

## 2. The live database

### 2.1 Where it is

`src/travian_api/web/models/db.py`:

- `_DB_PATH = os.environ.get("TRAVIAN_DB_PATH", str(Path.home()/".travian"/"travian_web.db"))`
- `DB_DIR = Path(_DB_PATH).parent`, created at import time
- `DATABASE_URL = f"sqlite+aiosqlite:///{_DB_PATH}"`

So the default production database is `~/.travian/travian_web.db`, and — from
§1.4 — its keys are `~/.travian/.web_keys`. Two more directories under
`~/.travian` matter operationally: `traces/` (`TRACE_DIR` in
`services/distribution/execution_trace.py`) and the per-user session data
directories the session manager creates with mode `0o700` where it can.

### 2.2 What "no migrations" actually means

`init_db()` does exactly two things:

```
await conn.run_sync(Base.metadata.create_all)
# then, for each table in _COLUMN_BACKFILLS:
#   PRAGMA table_info(<table>) and ALTER TABLE ... ADD COLUMN for any missing one
```

`_COLUMN_BACKFILLS` is a hand-maintained dict in the same module, currently
covering two columns on `travian_credentials` (`label`, `last_connected`) and
one on `recon_credentials` (`server_url`). The module comment states the reason
in one sentence: `create_all()` creates missing **tables** but never ALTERs
existing ones, so upgrading a live database needs these backfilled or every
query naming them fails with `no such column`.

Read that as a table of what is and is not handled:

| schema change | what happens on next boot |
|---|---|
| A brand-new table | Created by `create_all`. Handled |
| A new column on an existing table | **Not** created. Handled only if you add it to `_COLUMN_BACKFILLS` yourself. Otherwise every query naming it raises `no such column` |
| A changed column type, a rename, a drop | Nothing happens. The old column stays, the new name does not exist |
| A new constraint, a new index, a new unique | Nothing happens |
| Any downgrade | Nothing. There is no downgrade path at all |

There is no Alembic in this project. That is a deliberate simplicity for a
single-operator tool, and the cost is that **the backup is the rollback**. A
migration/verification story is on the phase-6 candidate list; until it exists,
2.3 is the whole safety net.

### 2.3 Back up before any schema change

"Any schema change" means: any commit that touches `web/models/db.py`, and any
deploy whose diff you have not read for one.

1. **Stop the writers first.** This is SQLite through aiosqlite. A file copy
   taken while a transaction is in flight can be torn, and a torn SQLite file
   opens cleanly and fails later. Stop the farm and queue loops, and take the
   copy while nothing is running.
2. **Copy with SQLite's own backup, not with `copy`.** The API that does this
   correctly ships with Python — `sqlite3.Connection.backup` — so it needs no
   extra tool installed:

   ```
   python -c "import sqlite3,pathlib; s=sqlite3.connect(pathlib.Path.home()/'.travian'/'travian_web.db'); d=sqlite3.connect('travian_web.db.bak'); s.backup(d); d.close(); s.close()"
   ```

   The `sqlite3` command-line shell's `.backup` does the same thing if it is
   installed; it is not a dependency of this project, so do not assume it is.
3. **Copy `.web_keys` alongside it** (§1.4), to the same protected place.
4. **Copy `~/.travian/traces/`** if any recent live run might still need
   undoing — the undo reconstructs from the trace, and the run-history report is
   *computed from* those files rather than stored anywhere else.
5. **Verify the copy before trusting it.** Open the backup and read one row:
   `python -c "import sqlite3;print(sqlite3.connect('travian_web.db.bak').execute('select count(*) from users').fetchone())"`.
   A backup nobody has opened is a hope.

---

## 3. The two servers

### 3.1 The exact processes

From the operator's own process list — this is the authority for what is
running, not the repository:

- **Port 80, production.** `python.exe` from the checkout's `.venv`, running
  `.venv/Scripts/uvicorn.exe travian_api.web.app:app --host 0.0.0.0 --port 80`,
  with the working directory set to the checkout.
- **Port 8001, debug.** The same, with `--port 8001`.

Two notes on that command line, both worth knowing before you retype it:

- The module path is `travian_api.web.app:app` — the **installed package**, not
  `src.travian_api.web.app:app`. `CLAUDE.md`'s debug-server line uses the `src.`
  form; both resolve in this checkout (`[tool.setuptools.packages.find] where =
  ["src"]`), but they are not the same import and the running process uses the
  first.
- `python -m travian_api.web` / the `travian-web` console script is a **third**
  way in, declared in `pyproject.toml` as `travian-web = "travian_api.web:main"`
  — and `main()` in `web/app.py` hardcodes `port=8001`. It cannot start the
  production server. Do not reach for it expecting :80.

Nothing runs on port 8000. Older docs and prompts that call 8000 "production"
are stale.

### 3.2 :80 is never restarted without explicit permission

That is the standing rule, and the reason is in §3.3: both servers run from the
same checkout and serve the same built directory, so almost nothing you would
restart :80 *for* actually requires it.

- A **frontend** change needs no restart at all (§3.3).
- A **Python** change needs a restart of :8001 only, which you may do freely.
- A **`.env`** change needs neither restart — settings are rebuilt per session
  (§4.2), so a reconnect is enough.

### 3.3 A frontend build IS a production deploy

Three facts, each read in the tree:

- `frontend/package.json` — `"build": "vite build"`.
- `frontend/vite.config.js` — `build.outDir: '../src/travian_api/web/static'`.
- `.gitignore` — `src/travian_api/web/static/`.

So `npm run build` overwrites the exact directory the server on :80 serves, and
that directory is **untracked**: there is no previous version in git to check
out. `web/app.py` mounts it at import time (`if ui_build_exists(STATIC_DIR)`),
and the SPA fallback serves a fresh `index.html` with `_SPA_NO_CACHE_HEADERS` on
every request — which is why no restart is needed for the new bundle to be live,
and equally why there is no window in which the old one is still being served.

**There is no staging step between debug and production.** Verification for a
frontend change is `npx eslint .` plus `npm test` plus the Playwright specs —
never a build. Deploying is a separate act that the operator authorises
explicitly. If you must be able to go back, copy the existing `static/`
directory somewhere before building; nothing else will preserve it.

### 3.4 How to verify a server is healthy after a restart

**There is no health endpoint.** Verified rather than assumed: no route matching
`/health`, `/healthz`, `/ping` or `/version` exists anywhere under
`src/travian_api/` — the only `/status` routes are
`travian_auth.get_status`, `recon`'s and `captcha`'s, and all of them require
`Depends(get_current_user)` and speak about the *game* session rather than the
process.

What you can use today, in ascending cost:

| check | what a success proves | what it does not prove |
|---|---|---|
| `GET /openapi.json` | The process bound the port **and the lifespan completed** — `init_db()` runs inside `lifespan` before the app serves, so a startup failure means no port at all. `app = FastAPI(title=…, version="1.0.0", lifespan=lifespan)` passes no `openapi_url=None`, so FastAPI's built-in schema route exists | Nothing about the game session, credentials or the frontend bundle |
| `GET /` | Whether a **frontend build is present**: `serve_spa` returns `index.html`, and with no build the app registers `serve_ui_not_built` instead, which returns a JSON body explaining that no static assets were shipped. This is the check to run right after a deploy | Anything about the API |
| `GET /api/<anything>` unauthenticated | Routing and the auth dependency are alive — a `401` is a *successful* outcome for this check | The database, unless the route touches it |
| the run-history route, authenticated | The whole stack including the database | It costs a login |

**Proposal, not current behaviour.** The gap worth closing is that none of the
above touches the database, and `no such column` after a schema change (§2.2) is
exactly the failure that boots fine and fails later. A minimal addition:

```python
# src/travian_api/web/routes/health.py — PROPOSED, does not exist at 3f4d121
@router.get("/api/health")
async def get_health(db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "version": app.version, "db": "ok"}
```

Unauthenticated, no game request, one trivial query, and it must be registered
**before** the `/{full_path:path}` catch-all that `web/app.py` mounts last.
Until that exists, `GET /openapi.json` then `GET /` is the restart check.

### 3.5 What has not been reviewed

`web/app.py` binds `0.0.0.0` in both configurations, which puts both servers on
the LAN. It adds a `SecurityHeadersMiddleware` (CSP, `X-Frame-Options: DENY`,
`nosniff`, referrer policy — with a relaxed dev CSP behind `_DEV_MODE`) and a
`CORSMiddleware` allowlisting three localhost origins with
`allow_credentials=True`. The SPA handler resolves and containment-checks paths
before serving, with a comment naming the traversal it closes (`/../../.env`).
None of that has had a security review in this cycle; it is named here so its
absence is on the record. See `docs/27-bug-map.md` §J.

---

## 4. The live-flag class

### 4.1 Live writes default ON

`src/travian_api/config.py`:

```
trade_route_live: bool = Field(default=True, description="… Defaults ON since 2026-08-27 …")
```

With `"env_prefix": "TRAVIAN_"`, the variable is `TRAVIAN_TRADE_ROUTE_LIVE`.
**Unset, the switch is on.** The default was changed at the operator's explicit
instruction because the opt-in reverted to preview-only on every server restart.
`web/sessions.py` is the only place that reads it, passing
`live_enabled=self.settings.trade_route_live` into `TradeRouteService`.

**Do not "correct" `TradeRouteService.__init__`'s `live_enabled: bool = False`.**
That is the library's own safe default, which every test and every direct
construction depends on, and `web/sessions.py` is the one caller that overrides
it. Its comment says so, and so does `docs/26` step 0.2.

### 4.2 What an operator must set for a preview-only day

1. **Put it in `.env`, not in the shell:** `TRAVIAN_TRADE_ROUTE_LIVE=false`. A
   running process's environment cannot be changed from outside, so an
   `export`/`$env:` in your terminal changes nothing about the server.
2. **Stop the loops.** Reconnect and disconnect are both refused with a `409`
   while background operations are running, and the refusal names them —
   `web/sessions.py` raises `Cannot disconnect while operations are running:
   <labels>. Stop them first.` The detached job holds the session's
   `HttpClient`, and closing it underneath makes every following request in the
   job fail.
3. **Reconnect the session.** Settings are rebuilt per session: the session
   constructor does `base_settings = Settings()` and then `model_copy(update=…)`
   for the per-user identity fields. So a reconnect picks up the new `.env`.
   **No server restart**, and never restart :80 without asking.
4. **Read it back off the response.** Every `/execute` response carries
   `live_enabled`, and a dry run reports it truthfully. `docs/26` step 2 makes
   the pair `live_enabled: true` with `dry_run: true` the proof of which mode
   you are in — read it before trusting anything else.

Set it back to `true` (or unset it) for the day you intend to write.

### 4.3 `execution_mode` is now the consent, and it is required

`ExecuteRequest.execution_mode: Literal["preview", "live"]` is what the handler
reads, resolved once at the request boundary. The rules, each pinned by a test
in `tests/test_distribution_http_contract.py`:

| body | outcome |
|---|---|
| neither field | previews |
| `dry_run: true` | previews |
| `dry_run: false` **alone** | **422**, naming the field: consent must be stated, not inferred from an absent boolean |
| `execution_mode: "live"` | writes — the only thing that does |
| `execution_mode: "live"` + `dry_run: true` | **422**, contradictory |
| `dry_run: null` | **422**, refused rather than read as omitted |
| an unknown mode | **422** |

Two independent brakes therefore have to be open before anything is written: the
env flag and the request's own `execution_mode`. A live execute with the env flag
false is refused with a `409` naming it, and a dry run never depends on the flag
at all.

### 4.4 The other defaults that change what a run does

Not everything in `Settings` is cosmetic. These are the ones whose default value
changes the behaviour of a run rather than its appearance:

| variable | default | what it decides |
|---|---|---|
| `TRAVIAN_TRADE_ROUTE_LIVE` | **`true`** | whether trade-route writes reach the account at all (§4.1) |
| `TRAVIAN_STEALTH` | `true` | whether human-like pacing applies. Off, every request goes out as fast as the code can make it |
| `TRAVIAN_STEALTH_MAX_DAILY_HOURS` | `16.0` | the activity budget. `clients/http_client.py` passes it into `ActivityScheduler(max_daily_hours=…)`, and exhausting it is one of the things that produces a `stopped_early` run |
| `TRAVIAN_STEALTH_MIN_GAP` / `_MAX_GAP` | `1.0` / `2.5` s | the inter-request gap, so the wall time of a sweep |
| `TRAVIAN_ALLOWED_SERVER_HOSTS` | `travian.com` | which hosts may receive a login (§1.6) |
| `TRAVIAN_DB_PATH` | `~/.travian/travian_web.db` | which database — **and, through `DB_DIR`, which key file** (§1.4). Changing it points the app at a different account store |
| `TRAVIAN_DEBUG` | `false` | debug behaviour, and `_DEV_MODE` in `web/app.py` relaxes the CSP |

Two request-level defaults belong in the same class even though they are not env
vars, because they bound what a single run can do to the account:
`ExecuteRequest.max_game_rows_per_run` defaults to `24` — one day of hourly rows
— and `0` still means unbounded if a caller asks for it; and
`max_routes_per_run` defaults to `3` with `le=50`, where `0` means **reconcile
only** — read, disable what the plan no longer wants, create nothing. The page
always sends both explicitly, so the server default governs an API caller rather
than the UI. `docs/27-bug-map.md` §C records what
happened when that default was `0`.

---

## 5. The game gate

**Before any live run, work `docs/26-first-live-run.md` from step 0.** It is not
a summary of this document from the other side; it is a different set of
failures. In particular it settles three *measurements* the plan's own numbers
rest on (the merchant capacity reading, one timed leg, the snapshot's
capacities), and it sequences the first writes so that a failure names its own
cause: one create-only canary, a rehearsed undo, then a restoration canary, then
a deletion canary, then four widening steps.

`docs/27-bug-map.md` §J lists the eight observations that run must produce and
the four game facts still unverified. Those are the reason the protocol exists;
this document only gets the machine into a state where it can be followed.

---

## 6. After a crash mid-run

A run that dies partway through has written to the game and not reported what it
wrote. Three artifacts survive it, and they are read in this order.

### 6.1 The trace — the only record of the "before"

`services/distribution/execution_trace.py` writes
`~/.travian/traces/exec-<run_id>.jsonl` (`TRACE_DIR = Path.home()/".travian"/"traces"`),
append-only, one JSON object per line. It is the only record of what each
village looked like **before** the run, because the game returns no id when it
creates a route — which is why `docs/26` step 0.3 refuses a live run whose trace
file cannot be opened, before the first game request.

The events to look for, all present in `web/routes/distribution.py`:
`run_start` (with `canary`, `execution_mode_requested`,
`execution_mode_resolved` and `env_brake_open`), `origin_read` (the pre-write
inventory in full — every row's id, destination, cargo, departure minute,
enabled and visible flags), `verified` (the page after the write, same shape),
`read_back_disagreed`, `window_pruned` (whose `status` is the first real
observation of what a DELETE answers with), `restore_attempted` / `restored`
with `already_enabled_ids`, `enabled_by_request_ids` and
`restoration_completed`, and `canary_settled`. `read_inventories(run_id)` reads
the inventories back out.

**Keep the trace.** Copy the file before doing anything else; it is the input to
both of the next two steps.

### 6.2 `run-history`, and what makes `needs_attention` true

`GET /api/distribution/run-history` computes its answer *from the trace files* —
nothing else stores it. `services/distribution/run_history.py` derives
`RunSummary.needs_attention` as a boolean OR over, verbatim from the source:
`created_unverified`, `not_created`, `outstanding`, `problems`,
`gold_club_blocked`, `stopped_early`, `verify_failures`, unsettled read-backs,
`schedule_mismatch_origins`, a recorded run failure — **or `not complete`.**

That last clause is the crash case, and it is the reason to check here first: a
run whose `run_end` event never landed is flagged as needing attention on the
strength of its own absence. The page renders this in the run-history disclosure
on the planner (`ResourcePlanner.jsx`), where `r.failed || r.needs_attention`
picks the warning tone.

`RunSummary` also carries `created`, `created_unverified`, `not_created`,
`created_game_rows`, `live_game_rows`, `disabled`, `re_enabled`,
`cargo_updated`, `deferred` and `outstanding` — enough to say what the dead run
had done when it stopped.

### 6.3 The undo panel — three actions, only the first free of consequence

`frontend/src/components/RevertRunPanel.jsx`, backed by
`POST /api/distribution/routes/revert-plan`:

1. **"Check what undoing this would take (~2 requests per village the run
   touched)"** — read-only. It re-reads the origins and returns what the run
   created and what it switched. Nothing in the game changes.
2. **"Disable those routes now (reversible)"** — `apply_disable: true`. The
   created rows stop shipping and are still re-enableable.
3. **"Delete those routes for good (disables first)"** — `apply_delete: true`.
   Irreversible. The field's own description says why it is separate and off by
   default, and that disabling happens first regardless so the resources stop
   moving even if the removal then fails.

Details that matter when you are working from a crash:

- **The request field is `origins`, not `only_origins`.** `RevertPlanRequest`
  declares `trace_id`, `origins`, `apply_disable`, `apply_delete` and
  `map_span`. (`only_origins` is an `ExecuteRequest` field. `docs/26` §4.4 calls
  the revert's narrowing `only_origins`; that is the wrong name.) Without
  `origins`, the Check re-reads every origin the run touched at two page loads
  each — about fifty reads on this account before any write.
- `trace_id` is constrained to `^[0-9a-f]{12}$`, and the comment explains that
  this is not cosmetic: unvalidated it was an authenticated arbitrary-`.jsonl`
  read through `../`, and worse than a read, because the wrong file becomes the
  "before" inventory and every currently-live route then looks newly created —
  which `apply_disable` would act on.
- `map_span` must be odd here too, by the same validator `PlanRequest` uses, and
  the comment says it belongs here most: the span turns a marketplace row's map
  id back into coordinates, and `apply_delete` acts on that reading.
- The revert takes **the same lock** the execute takes, so it is refused with a
  `409` while a run is in flight. That was not true until `949224c`; before it,
  the one irreversible endpoint had strictly fewer guards than the reversible
  one, and a concurrent execute's fresh creates were deleted.
- **The undo does not change any enabled flag on a pre-existing row.** The
  response's `restore_state` — the panel's "Routes this run switched, to put
  back:" — names each row and the state to put it *back* to, in both directions.
  Read the arrow. The undo is complete only when a fresh Check returns that list
  empty. And note the attribution limit `docs/26` §3 records: the diff compares
  the pre-run inventory against now, so on an account with a dual it cannot tell
  your manual edit from the app's.
- `must_delete_by_hand` is the panel's list of what the app could not remove.

### 6.4 The order, after a crash

1. Copy the trace file, and the database + `.web_keys` if the crash was not
   obviously a network failure.
2. Read `run-history` for that run: is it `complete`, and what does
   `needs_attention` fold in.
3. Read the trace's `problems`-bearing events and the `origin_read` /
   `verified` pair for every origin the run reached.
4. **Open the game** for those origins before writing anything. A
   `not_created` or `created_unverified` count means the response and the
   marketplace may disagree, and a second create on top is how duplicates
   accumulate.
5. Only then Check → Disable → Delete, one origin at a time, narrowed with
   `origins`.
6. Rehearse this once with nothing at stake first — `docs/26` §3 exists for
   exactly that, and it is the only time the undo is exercised against the game
   with nothing to lose.
