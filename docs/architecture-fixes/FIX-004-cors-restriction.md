# FIX-004: Restrict CORS from Wildcard to Specific Origin

**Priority:** P0  
**Effort:** ~30 minutes  
**Files affected:** `main.py`, `config.py`

---

## Problem

`main.py:241–243` configures CORS with a wildcard origin:

```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

`allow_origins=["*"]` combined with `allow_credentials=True` is rejected by all modern
browsers — browsers block credentialed requests to wildcard CORS origins. This means
the CORS configuration as written is effectively broken for credentialed cross-origin
requests, which is the only kind that matters for a session-authenticated app.

The wildcard is therefore providing a false sense of security (appears permissive) while
also not working as intended (can't actually be used with credentials). The correct
configuration is to specify the exact allowed origin(s).

---

## Solution

Replace `allow_origins=["*"]` with the explicit origin(s) where the app is served.
Read allowed origins from configuration so they can be set per-environment without
changing code.

---

## Implementation Steps

### Step 1 — Add `allowed_origins` to `config.py`

In `config.py`, add to the base `Settings` class:

```python
allowed_origins: str = ""  # Comma-separated list of allowed origins
```

In `ProductionSettings` (or `RenderSettings`), there is no good default — origins
must be explicitly configured. Add a validator that warns if this is empty in production:

```python
@validator("allowed_origins")
def validate_allowed_origins(cls, v, values):
    if not v and values.get("environment") == "production":
        import logging
        logging.getLogger(__name__).warning(
            "ALLOWED_ORIGINS is not set. CORS will deny all cross-origin requests."
        )
    return v

def get_allowed_origins_list(self) -> list[str]:
    """Parse comma-separated origins into a list. Returns [] if empty."""
    if not self.allowed_origins:
        return []
    return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
```

---

### Step 2 — Update CORS middleware in `main.py`

Replace the existing CORS middleware block (lines 240–250) with:

```python
allowed_origins = settings.get_allowed_origins_list()

if not allowed_origins:
    logger.warning(
        "ALLOWED_ORIGINS is not configured. CORS is disabled. "
        "Set ALLOWED_ORIGINS=https://your-domain.onrender.com in environment variables."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)
```

---

### Step 3 — Set `ALLOWED_ORIGINS` in `render.yaml`

Add to the web service `envVars` section (render.yaml, after line 76):

```yaml
- key: ALLOWED_ORIGINS
  value: "https://document-catalog-app.onrender.com"
```

Replace `document-catalog-app.onrender.com` with the actual Render service URL.
If a custom domain is configured, add both:

```yaml
- key: ALLOWED_ORIGINS
  value: "https://document-catalog-app.onrender.com,https://your-custom-domain.com"
```

---

### Step 4 — Local development

For local development, `ALLOWED_ORIGINS` should be set to `http://localhost:8000`
or left empty (CORS is irrelevant for same-origin local development). The dev
environment typically doesn't make cross-origin requests to itself, so an empty
list in development is acceptable.

If you use a separate frontend dev server (e.g., on port 3000) during development,
set `ALLOWED_ORIGINS=http://localhost:3000` in the local `.env` file.

---

## Acceptance Criteria

- [ ] `allow_origins=["*"]` no longer appears in `main.py`.
- [ ] CORS middleware reads allowed origins from `ALLOWED_ORIGINS` environment variable.
- [ ] Setting `ALLOWED_ORIGINS` to the Render service URL results in CORS headers
      that permit requests from that origin.
- [ ] An unset `ALLOWED_ORIGINS` logs a warning and disables CORS rather than
      permitting all origins.
- [ ] `render.yaml` includes `ALLOWED_ORIGINS` set to the production URL.

---

## Notes

For this application — a server-rendered Jinja2 app where the browser and server share
the same origin — CORS is only relevant for:

1. API calls from the Render domain to the same Render domain (same-origin, not affected by CORS).
2. External callers (e.g., a separate frontend, a third-party integration, or API scripts).

If there are no external callers today, the correct CORS configuration is an empty
allowed origins list (effectively disabling CORS). Add origins only when an explicit
cross-origin caller exists.

Do not use `allow_origins=["*"]` as a debugging shortcut. It is not equivalent to
"allow everything" when `allow_credentials=True` — it breaks credentialed requests
in all browsers.
