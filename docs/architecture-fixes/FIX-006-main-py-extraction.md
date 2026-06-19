# FIX-006: Extract Routes Out of main.py

**Priority:** P1  
**Effort:** 1–2 days  
**Files affected:** `main.py`, `api/` directory (new and existing files)

---

## Problem

`main.py` contains approximately 50 route handlers spread across ~1,570 lines.
The `api/` directory exists and has two routers (`documents.py`, `dashboard.py`),
but the majority of the application's routes are still defined directly in `main.py`.

Current route groups still in `main.py` (by prefix):

| Prefix | Handler count | Should move to |
|--------|--------------|----------------|
| `/api/documents/search`, `/api/search/*` | ~6 | `api/search.py` |
| `/api/taxonomy/*` | ~7 | `api/taxonomy.py` |
| `/api/review/*`, `/api/facets/*` | ~5 | `api/review.py` |
| `/api/admin/*`, `/api/ai/*`, `/api/tasks/*`, `/api/stats/*` | ~8 | `api/admin.py` |
| `/`, `/login`, `/logout`, `/search`, `/upload`, `/admin/*`, `/review/*` | ~8 | `api/pages.py` |
| `/health`, `/health/*`, `/previews/*` | ~4 | `api/system.py` |

The result for a team of two engineers: any feature that touches routes will produce
merge conflicts in `main.py`. It also means that every route review requires scrolling
through 1,500 lines of mixed startup code, middleware, and business logic.

This is not a performance problem or a correctness problem today. It becomes a
coordination problem the moment more than one person works on routes simultaneously.

---

## Solution

Incrementally move route groups to dedicated files in `api/`. Each group is independent
— they can be migrated in separate PRs with no risk of breaking other groups. Start
with the smallest or most isolated group to build confidence in the pattern.

**The pattern is already established.** `api/documents.py` and `api/dashboard.py` show
exactly how routers are structured. Copy the same pattern for each new file.

---

## Route Group Inventory

### Group 1 — Search (`api/search.py`) — Start here

Routes to move from `main.py`:
```
main.py:830   GET  /api/documents/search
main.py:1464  GET  /api/search/canonical/{canonical_term}
main.py:1479  GET  /api/search/verbatim/{verbatim_term}
main.py:1559  GET  /api/search/top-queries
```

These are read-only query routes with no side effects. Lowest migration risk.

---

### Group 2 — Taxonomy (`api/taxonomy.py`)

Routes to move from `main.py`:
```
main.py:1261  GET  /api/taxonomy/categories
main.py:1275  GET  /api/taxonomy/categories/{primary_category}/subcategories
main.py:1290  GET  /api/taxonomy/hierarchy
main.py:1304  GET  /api/taxonomy/filter-data
main.py:1317  GET  /api/taxonomy/canonical-terms
main.py:1330  GET  /api/taxonomy/search
main.py:1345  GET  /api/taxonomy/stats
```

All read-only. No write operations. Clean to migrate.

---

### Group 3 — Review & Facets (`api/review.py`)

Routes to move from `main.py`:
```
main.py:929   GET   /api/facets/years
main.py:953   GET   /api/review/dates/count
main.py:968   GET   /api/review/dates
main.py:1005  POST  /api/review/dates/{document_id}
main.py:1061  GET   /api/facets/clients
```

Contains one write operation (`POST /api/review/dates/{document_id}`). Review carefully
for side effects before moving.

---

### Group 4 — Admin & System (`api/admin.py`)

Routes to move from `main.py`:
```
main.py:708   POST  /api/admin/clear-cache
main.py:1360  GET   /api/ai/info
main.py:1374  GET   /api/ai/analysis-types
main.py:1388  POST  /api/documents/{document_id}/analyze
main.py:1423  GET   /api/tasks/{task_id}/status
main.py:1435  GET   /api/stats
main.py:1494  GET   /api/documents/{document_id}/mappings
main.py:1513  GET   /api/stats/mappings
main.py:1526  POST  /api/admin/backfill-features
```

Contains admin write operations. Low external usage risk but review auth guards before
moving — confirm `AuthenticationMiddleware` covers these routes in the new location.

---

### Group 5 — Page Routes (`api/pages.py`)

Routes to move from `main.py`:
```
main.py:448   GET   /login
main.py:504   POST  /login
main.py:571   GET   /logout
main.py:578   GET   /
main.py:1234  GET   /search
main.py:1241  GET   /upload
main.py:1248  GET   /admin/dashboard
main.py:1254  GET   /review/dates
```

These render Jinja2 templates. They need access to the `templates` object. Either
pass `templates` via a router factory function, or use FastAPI's `Request`-level
template rendering (already the pattern in `api/dashboard.py`).

---

### Group 6 — System & Health (`api/system.py`)

Routes to move from `main.py`:
```
main.py:381   GET  /previews/{filename}
main.py:594   GET  /health
main.py:600   GET  /health/storage
main.py:653   GET  /health/session
```

Health checks and file serving. Move last — these are critical paths and should be
verified carefully after migration.

---

## Implementation Steps for Each Group

The pattern is the same for each group. Illustrated for Group 1 (Search):

### Step 1 — Create `api/search.py`

```python
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from database import get_db
from services.search_service import SearchService
# ... other imports specific to these routes ...

router = APIRouter()


@router.get("/documents/search")
async def search_documents(
    request: Request,
    q: str = "",
    # ... existing parameters ...
    db: Session = Depends(get_db),
):
    # Exact copy of the handler body from main.py:830
    ...


@router.get("/search/canonical/{canonical_term}")
async def search_by_canonical(canonical_term: str, db: Session = Depends(get_db)):
    # Exact copy from main.py:1464
    ...

# ... remaining routes ...
```

### Step 2 — Register the router in `main.py`

Add to `main.py` alongside the existing router registrations (lines 319–320):

```python
from api.search import router as search_router
from api.taxonomy import router as taxonomy_router
# ...

app.include_router(search_router, prefix="/api", tags=["Search"])
app.include_router(taxonomy_router, prefix="/api", tags=["Taxonomy"])
```

### Step 3 — Delete the moved routes from `main.py`

After confirming the router-registered version works, delete the corresponding
`@app.get` / `@app.post` blocks from `main.py`. Do not leave both — duplicates
cause route conflicts.

### Step 4 — Verify with curl or browser

For each migrated route, confirm:
- The endpoint responds at the same URL.
- Auth middleware still applies (test with and without session cookie).
- Rate limiting still applies.
- Response shape is identical to before.

---

## Migration Order (Recommended)

Do these in separate commits or PRs to keep diffs reviewable:

1. Search (Group 1) — read-only, lowest risk
2. Taxonomy (Group 2) — read-only, well-isolated
3. Review & Facets (Group 3) — one write op, test carefully
4. Page Routes (Group 5) — template rendering, confirm Jinja2 access
5. Admin & System (Group 4) — admin writes, confirm auth
6. Health & Previews (Group 6) — critical paths, do last

After all groups are migrated, `main.py` should contain only:
- Imports
- App initialization (`app = FastAPI(...)`)
- Middleware registration
- Lifespan handler
- Router include statements

Target: `main.py` under 150 lines.

---

## Acceptance Criteria

- [ ] All routes are registered via `APIRouter` instances in files under `api/`.
- [ ] `main.py` contains no `@app.get`, `@app.post`, `@app.put`, or `@app.delete` decorators.
- [ ] All existing URL paths respond identically after migration (same status codes, same response shapes).
- [ ] Authentication middleware applies to all routes in the new location.
- [ ] Rate limiting applies to all routes in the new location.
- [ ] `main.py` is under 200 lines.

---

## Notes

**Do not change route logic during migration.** Move handlers exactly as-is. Refactoring
and migration are separate tasks — mixing them makes rollback harder.

**Rate limiting.** The `slowapi` limiter uses `@limiter.limit` decorators on individual
handlers. These decorators need to be present on the new route handlers. Copy them
verbatim.

**Dependency injection.** `Depends(get_db)` works the same way inside a router as
it does on a direct app route. No changes needed.

**Templates.** Routes that use Jinja2 templates need access to the `templates` object
currently created in `main.py`. Either move the `templates` instantiation to a shared
`dependencies.py` module and import it, or pass it as a parameter to a router factory.
The simplest approach: import a module-level `templates` from a new `api/templates.py`.
