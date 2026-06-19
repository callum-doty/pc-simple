# FIX-005: Enforce S3 Storage in Production

**Priority:** P1  
**Effort:** ~1 hour  
**Files affected:** `config.py`, `main.py` (startup validation)

---

## Problem

`render.yaml` sets `STORAGE_TYPE=s3` for both the web service and the Celery worker.
However, nothing in the application validates this at startup. A misconfigured
environment (missing S3 credentials, wrong `STORAGE_TYPE` value) silently falls back
to local disk or Render disk storage.

The Render disk failure mode is severe:
- Files uploaded via the web instance are written to that instance's local disk.
- Celery workers running on separate containers have no access to the web instance's disk.
- Preview generation fails because the worker cannot read the file the web service wrote.
- Files appear uploaded (status QUEUED) but processing fails with "file not found".
- On any Render service migration, resize, or upgrade, the disk is wiped. All files lost.

This is already documented in `docs/BACKBLAZE_CONFIGURATION.md`, but documentation
alone is not a production safeguard. The application should refuse to start if S3
is required but not correctly configured.

---

## Solution

Add a startup validation function that checks S3 configuration in production and
fails loudly rather than silently falling back to local storage. This is a simple
guard that costs 30 minutes to implement and prevents a class of data loss events.

---

## Implementation Steps

### Step 1 — Add `validate_storage_config` to `config.py`

In `config.py`, add a method to the settings class or as a standalone function:

```python
def validate_storage_config(settings) -> None:
    """
    Validates storage configuration at startup.
    Raises RuntimeError if production is configured to use S3 but credentials are missing.
    Call this from the app lifespan after migrations complete.
    """
    if settings.environment not in ("production", "worker"):
        return  # Skip validation in development

    if settings.storage_type == "s3":
        missing = []
        if not settings.s3_bucket:
            missing.append("S3_BUCKET")
        if not settings.s3_access_key:
            missing.append("S3_ACCESS_KEY")
        if not settings.s3_secret_key:
            missing.append("S3_SECRET_KEY")
        if not settings.s3_region and not settings.s3_endpoint_url:
            missing.append("S3_REGION or S3_ENDPOINT_URL")

        if missing:
            raise RuntimeError(
                f"STORAGE_TYPE=s3 but required S3 credentials are missing: "
                f"{', '.join(missing)}. "
                f"Configure these in the Render environment variables or set "
                f"STORAGE_TYPE=local to use local disk (not recommended for production)."
            )

    elif settings.storage_type == "render_disk":
        import logging
        logging.getLogger(__name__).warning(
            "STORAGE_TYPE=render_disk is set. This storage backend is tied to a single "
            "container and data will be lost on service migrations or scaling. "
            "Use STORAGE_TYPE=s3 with Backblaze B2 for production."
        )

    elif settings.storage_type == "local":
        import logging
        logging.getLogger(__name__).warning(
            "STORAGE_TYPE=local in production. Files are stored on the container's "
            "ephemeral filesystem. Use STORAGE_TYPE=s3 for production deployments."
        )
```

---

### Step 2 — Call validation from `main.py` lifespan

In `main.py` lifespan, after the migration log line (approximately line 84), add:

```python
from config import validate_storage_config
try:
    validate_storage_config(settings)
    logger.info(f"Storage configuration validated: type={settings.storage_type}")
except RuntimeError as storage_err:
    logger.error(f"Storage configuration error: {storage_err}")
    raise  # Fail startup — better to fail loudly than silently lose data
```

A `RuntimeError` raised here will prevent the app from starting, which is the correct
behavior. Render's health check will fail, and the deploy will be rolled back. This
is preferable to the app starting and silently writing files to a disk that workers
cannot access.

---

### Step 3 — Add connectivity probe (optional but recommended)

After credential validation, do a lightweight probe to verify the S3 bucket is
reachable. This catches wrong bucket names or revoked credentials:

```python
def probe_s3_connectivity(settings) -> None:
    """
    Attempts a lightweight S3 HEAD operation to verify credentials and bucket access.
    Only called in production when STORAGE_TYPE=s3.
    """
    if settings.storage_type != "s3" or settings.environment not in ("production", "worker"):
        return

    import boto3
    from botocore.exceptions import ClientError, NoCredentialsError

    try:
        client = boto3.client(
            "s3",
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url or None,
        )
        client.head_bucket(Bucket=settings.s3_bucket)
        logger.info(f"S3 bucket '{settings.s3_bucket}' is accessible.")
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        raise RuntimeError(
            f"S3 bucket '{settings.s3_bucket}' is not accessible (error {error_code}). "
            f"Check S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, and S3_ENDPOINT_URL."
        ) from e
    except NoCredentialsError:
        raise RuntimeError("S3 credentials are invalid or not configured correctly.")
    except Exception as e:
        logger.warning(f"S3 connectivity probe failed: {e}. Proceeding anyway.")
        # Non-fatal: probe failure may be transient; credential validation already passed.
```

Call `probe_s3_connectivity(settings)` after `validate_storage_config(settings)` in
the lifespan. Note: make the probe non-fatal (log warning, don't raise) if you want
to tolerate transient S3 unavailability at startup. Make it fatal if you require
a confirmed-working bucket before serving any requests.

---

### Step 4 — Add a `/health/storage` check (already exists — verify it covers this)

`main.py:600` already has a `/health/storage` endpoint. Confirm it tests actual
S3 write access, not just that the storage type is configured. If it only checks
configuration, add a lightweight write probe:

```python
# In the existing /health/storage handler
test_key = f"health_check_{datetime.utcnow().timestamp()}.txt"
storage_service.save_file_sync(b"health check", test_key)
storage_service.delete_file_sync(test_key)
```

This makes the health endpoint meaningful for monitoring rather than just a config check.

---

## Acceptance Criteria

- [ ] Starting the application with `STORAGE_TYPE=s3` and missing `S3_BUCKET` raises
      a `RuntimeError` at startup and prevents the app from serving requests.
- [ ] Starting with `STORAGE_TYPE=render_disk` in production logs a WARNING but does
      not prevent startup (allowing operators to make an informed choice).
- [ ] Starting with correct S3 credentials logs a confirmation message.
- [ ] The `/health/storage` endpoint returns an error if the S3 bucket is unreachable,
      not just if the configuration is absent.
- [ ] Development startup (ENVIRONMENT=development) skips all storage validation.

---

## Notes

`render.yaml` already sets `STORAGE_TYPE=s3` for both services. The validation in
this fix will confirm that the corresponding `S3_*` credentials are also set before
the app starts accepting uploads. This closes the gap between "configured to use S3"
and "actually able to use S3".

The Render cron job (`dropbox-ingest`) also needs S3 to write downloaded files. Its
`ENVIRONMENT=worker`, so `validate_storage_config` will run there too — correct behavior.
