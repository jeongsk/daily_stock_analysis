# World Monitor Self-hosting Implementation Plan

**Spec:** `docs/superpowers/specs/2026-07-27-worldmonitor-self-hosting-design.md`

## Phase 1: Pin the upstream boundary

1. Add `external/worldmonitor` as a Git submodule pinned to World Monitor commit
   `6c48a33c97cd643d87ee3a4ed2b54aacbb1cbc3b`.
2. Record the expected upstream commit in the integration runner.
3. Add validation covering an uninitialized or mismatched submodule.

## Phase 2: Compose orchestration

1. Add a World Monitor Compose overlay using the pinned upstream application Dockerfile and
   the official Redis image.
2. Build `worldmonitor`, `redis-rest`, `ais-relay`, and the seeder runtime from the pinned submodule.
3. Keep Redis and relay ports internal and attach DSA services to the shared integration network.
4. Add required-secret checks, persistent Redis storage, health checks, log rotation, and the configurable seeder loop.
5. Add `scripts/worldmonitor-stack.sh` with `validate`, `up`, `down`, `status`, `logs`, and `seed`.
6. Test shell validation and render the combined Compose configuration.

## Phase 3: DSA connection boundary

1. Add World Monitor configuration parsing and registry entries.
2. Implement a small read-only status client with bounded HTTP timeouts and sanitized errors.
3. Expose the optional component status without changing DSA liveness semantics.
4. Cover disabled, healthy, degraded, unreachable, misconfigured, and secret-redaction paths.

## Phase 4: Documentation and verification

1. Update `.env.example`, Chinese and English deployment documentation, intelligence-source documentation, and the flat `[Unreleased]` changelog.
2. Run focused Python tests, shell syntax checks, Compose rendering, Python compilation, and `./scripts/ci_gate.sh`.
3. If Docker/network access is available, pull/build and smoke-test the integrated stack; otherwise record the exact unverified paths.
4. Review the final diff for scope, license boundaries, rollback accuracy, and accidental secrets.

No commit, tag, push, or remote mutation is part of this plan.
