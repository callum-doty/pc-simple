"""
Boots the Celery app under the environment render.yaml actually gives each
service.

This exists because celery-beat shipped with ENVIRONMENT=production and only
four env vars, and production config requires five secrets it was never given.
Every existing test passed: they run in-process, where settings are already
loaded and cached. The failure only appeared as a crash loop after deploy.

Rather than assert a list of variable names, these tests read the manifest and
start a real subprocess with exactly those variables, so a service whose env is
too thin to import the app fails here instead of in the deploy log.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Stand-ins for values Render resolves at deploy time (fromDatabase/fromService).
# Only the shape matters — nothing connects during import.
PLACEHOLDERS = {
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/testdb",
    "REDIS_URL": "redis://localhost:6379/0",
}


def _manifest_services():
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load((REPO / "render.yaml").read_text())["services"]


def _service_env(service):
    """The environment Render will hand this service, with secrets stubbed."""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "RENDER": "true"}
    for var in service.get("envVars", []):
        key = var["key"]
        if "value" in var:
            env[key] = str(var["value"])
        else:
            env[key] = PLACEHOLDERS.get(key, f"stub-{key.lower()}")
    return env


def _boot(env, snippet):
    return subprocess.run(
        [sys.executable, "-c", snippet],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.mark.parametrize("name", ["celery-worker", "celery-beat"])
def test_celery_app_imports_under_manifest_env(name):
    service = next(s for s in _manifest_services() if s["name"] == name)
    result = _boot(_service_env(service), "from worker import celery_app")

    assert result.returncode == 0, (
        f"{name} cannot import worker.celery_app with the env render.yaml gives it "
        f"(`celery -A worker.celery_app` would fail to boot):\n{result.stderr[-2000:]}"
    )


def test_beat_can_resolve_every_task_it_schedules():
    """
    Beat publishes by name. If the scheduled name is not registered, the worker
    logs "Received unregistered task" and the work silently never runs. Checked
    in beat's own environment so a config failure cannot mask it.
    """
    service = next(s for s in _manifest_services() if s["name"] == "celery-beat")
    result = _boot(
        _service_env(service),
        "from worker import celery_app as a;"
        "missing=[e['task'] for e in a.conf.beat_schedule.values() if e['task'] not in a.tasks];"
        "raise SystemExit('unregistered: %s' % missing if missing else 0)",
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_beat_is_not_granted_secrets_it_does_not_need():
    """
    Beat only publishes a task name to the broker. Giving it the LLM keys or the
    app session secret widens their blast radius for no gain.
    """
    service = next(s for s in _manifest_services() if s["name"] == "celery-beat")
    granted = {v["key"] for v in service.get("envVars", [])}

    unnecessary = granted & {
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "APP_PASSWORD",
        "SESSION_SECRET_KEY",
        "S3_ACCESS_KEY",
        "S3_SECRET_KEY",
    }
    assert not unnecessary, f"celery-beat is granted secrets it never uses: {sorted(unnecessary)}"
