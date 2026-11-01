"""The `agent` package. Configures log delivery on import.

This is the belt to the tracked entrypoint's braces (agent-log-delivery
design.md "Component 2: The entrypoint becomes version-controlled").
`backend/agent/main.py` calls `configure_logging()` explicitly, but the file
that actually runs in production is `agent_bundle/package_clean/main.py`,
which is gitignored and synced from it by `scripts/sync_agent_entrypoint.py`.
Deploy without re-running that sync and a stale bundle copy silently reverts
the fix — and the symptom is indistinguishable from the original defect, since
both look like "no application records in CloudWatch".

Configuring here makes delivery hold regardless of what the bundle copy
contains: every path to `handle_job` goes through `import agent.orchestration`,
which initializes this package first. Both halves are wanted, and idempotence
makes belt-and-braces nearly free.

**On the import-time side effect.** Configuring logging when a library module
is imported is normally bad practice, so: why it is acceptable here.

- `backend/agent/` is not a library. It has one consumer, the
  Agent_Runtime_Process, and no external importer whose logging configuration
  could be clobbered.
- The call is idempotent by construction — `configure_logging` keys on a
  marker attribute stamped on the handler, not on a module-level flag — so
  import-then-explicit-call installs one handler, not two.
- It is additive: it installs a handler and sets levels, and removes nothing.
- It is stdlib-only, so it cannot fail on a missing dependency. That matters
  more than it sounds: a raise here would take the process down before a
  single job is dispatched.
- It does not disturb the test suite. `pytest` captures stdout and surfaces it
  only for failing tests, and `caplog` attaches its own handler to root, which
  ours neither replaces nor filters.

No circular import: `agent.logging_config` imports only the standard library,
so importing it from here cannot reach back into this package.
"""

from agent.logging_config import configure_logging

configure_logging()
