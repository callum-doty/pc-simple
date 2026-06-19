# Architecture Fixes

Seven targeted fixes identified from the Staff Engineer architecture review and critique.
Ordered by operational risk — implement P0 fixes before P1, P1 before P2.

---

## Fix Index

| ID | Title | Priority | Effort | Status |
|----|-------|----------|--------|--------|
| [FIX-001](FIX-001-zombie-idempotency.md) | Zombie task recovery + processing lock | P0 | 3 hrs | TODO |
| [FIX-002](FIX-002-llm-cost-governance.md) | LLM cost governance + retry checkpointing | P0 | 4 hrs | TODO |
| [FIX-003](FIX-003-migration-pre-deploy.md) | Remove Alembic from app startup | P0 | 30 min | TODO |
| [FIX-004](FIX-004-cors-restriction.md) | Restrict CORS from wildcard to specific origin | P0 | 30 min | TODO |
| [FIX-005](FIX-005-s3-production-enforcement.md) | Enforce S3 storage in production startup | P1 | 1 hr | TODO |
| [FIX-006](FIX-006-main-py-extraction.md) | Extract routes out of main.py | P1 | 1–2 days | TODO |
| [FIX-007](FIX-007-jsonb-typed-schemas.md) | Typed Pydantic schemas for JSONB fields | P1 | 1 day | TODO |

---

## Risk Context

These fixes target three failure categories identified in the review:

**Silent data loss (P0)**
FIX-001 and FIX-002. A worker crash or an LLM retry storm are the most likely causes
of a real production incident. Both are invisible without explicit instrumentation.

**Deployment correctness (P0)**
FIX-003. Alembic running inside the app lifespan is currently harmless on a single
instance, but will cause migration conflicts the first time a second web instance starts
or a zero-downtime deploy is attempted. Fix it before that moment arrives.

**Security baseline (P0)**
FIX-004. CORS wildcard is a one-line fix with no tradeoffs for a single-domain app.

**Scaling prerequisites (P1)**
FIX-005, FIX-006, FIX-007. These are not emergencies today but become blockers at
10x data or two engineers working simultaneously.
