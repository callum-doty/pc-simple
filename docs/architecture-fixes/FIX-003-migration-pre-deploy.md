# FIX-003: Remove Alembic from App Startup

**Priority:** P0  
**Effort:** ~30 minutes  
**Files affected:** `main.py`, `render.yaml`

---

## Problem

`main.py:73–82` runs `alembic upgrade head` inside the FastAPI lifespan startup handler:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        alembic_cfg = AlembicConfig("alembic.ini")
        alembic_command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations applied successfully.")
    except Exception as migration_err:
        logger.error(f"Alembic migration failed at startup: {migration_err}")
        raise
```

`render.yaml:20–24` already runs `alembic upgrade head` in the web service `buildCommand`
and again in the Celery worker `buildCommand` (line 94–98). The migration therefore runs
at least twice on every deploy before the app even starts serving requests, and a third
time when the lifespan fires.

This is harmless when there is exactly one web instance starting. It becomes a problem
the moment there are two:

1. Render zero-downtime deploy starts a second instance alongside the first.
2. Both instances enter `lifespan` concurrently.
3. Both call `alembic upgrade head` against the same database at the same time.
4. Alembic uses PostgreSQL advisory locks, so one will block on the other. The blocked
   instance may timeout, depending on Render's health check timing, causing a failed
   deploy or a partial startup.

Additionally, running migrations on every app startup couples schema management to
application deployment in a way that makes rollbacks harder — a migration failure
prevents the app from starting, with no way to serve even the previous version.

---

## Solution

Migrations should run exactly once per deploy, before any application instance starts,
in the build phase — not at runtime. `render.yaml` already does this correctly in
`buildCommand`. Remove the duplicate from `main.py`.

---

## Implementation Steps

### Step 1 — Remove Alembic from `main.py` lifespan

In `main.py`, delete lines 72–82:

```python
# DELETE THIS BLOCK:
try:
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command

    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Alembic migrations applied successfully.")
except Exception as migration_err:
    logger.error(f"Alembic migration failed at startup: {migration_err}")
    raise
```

Replace with a log line confirming the expectation:

```python
logger.info("Schema migrations are applied by the build process before startup.")
```

Remove the unused imports (`AlembicConfig`, `alembic_command`) from within that block
if they are not used elsewhere in the file.

---

### Step 2 — Verify `render.yaml` buildCommand is sufficient

The web service `buildCommand` (render.yaml:20–24) already contains:

```yaml
buildCommand: |
  pip install --upgrade pip
  pip install -r requirements.txt
  alembic upgrade head
  python backfill_keyword_mappings.py
```

This runs during the build phase before any instance of the service starts. No changes
needed here.

The cron job `dropbox-ingest` (render.yaml:163–166) also runs `alembic upgrade head`
in its buildCommand. This is redundant but harmless for a cron job that runs as a
single process — leave it in place.

---

### Step 3 — Confirm `alembic` and `alembic.ini` are present in the build container

The build runs from the repo root where `alembic.ini` lives. Verify by checking that
`alembic.ini` is not in `.gitignore`. If it is, Render's build will fail silently.

```bash
grep alembic.ini .gitignore
```

If found, remove it from `.gitignore`.

---

### Step 4 — Local development

Developers running `python main.py` locally will no longer have auto-migration on
startup. Update the development setup instructions in `README.md` or `CLAUDE.md` to
document the manual migration step:

```bash
alembic upgrade head
python main.py
```

If auto-migration in development is genuinely useful (to avoid "forgot to migrate"
errors), add an environment-specific guard:

```python
# Only auto-migrate in development, never in production
if settings.debug:
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    alembic_cfg = AlembicConfig("alembic.ini")
    alembic_command.upgrade(alembic_cfg, "head")
    logger.info("Dev auto-migration applied.")
```

This keeps the convenience without the production risk.

---

## Acceptance Criteria

- [ ] `main.py` lifespan no longer calls `alembic_command.upgrade` in production mode.
- [ ] A Render deploy applies migrations exactly once (in buildCommand), before any
      instance starts.
- [ ] Starting two web instances simultaneously does not cause a migration conflict.
- [ ] Local `python main.py` still works (either via dev-mode guard or documented
      manual step).
- [ ] `alembic.ini` is committed to the repo and not in `.gitignore`.

---

## Notes

Render `buildCommand` runs before the previous version of the service is replaced.
This means migrations run against the live database while the old version of the app
is still running. This is safe as long as migrations are backward-compatible with the
previous version of the code (additive changes: new nullable columns, new indexes).
Destructive migrations (dropping columns, changing types) require a two-phase deploy
strategy regardless of where migrations run — this fix does not change that constraint.
