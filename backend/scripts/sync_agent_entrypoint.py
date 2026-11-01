"""Backward-compatible shim — the sync is now the whole first-party tree.

This script used to copy only `backend/agent/main.py` into the Agent_Bundle
root. That partial sync is exactly what shipped a stale bundle to production:
a deploy that synced only `main.py` left `agent/logging_config.py` missing and
four other first-party files un-copied, half-wiring the agent-log-delivery fix
with no error to signal it.

The fix is `scripts/sync_agent_bundle.py`, which syncs the entire first-party
source tree (`backend/agent/` and `backend/shared/`) into the bundle, the
entrypoint included. This module is kept only so the historically-documented
command still works — and so that running it can no longer do the partial,
footgun thing it used to. It now performs the full sync by delegating to
`sync_agent_bundle.sync_bundle`.

Run from anywhere, no arguments (prefer the new name going forward):

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\sync_agent_bundle.py

The entrypoint's special handling is unchanged and now lives in
`sync_agent_bundle.py`: `backend/agent/main.py` is the source of record, copied
byte-for-byte to the bundle ROOT (not under `agent/`), where its
`sys.path.insert(0, dirname(abspath(__file__)))` line is load-bearing.
"""

import sys

from sync_agent_bundle import sync_bundle

if __name__ == "__main__":
    print(
        "NOTE: sync_agent_entrypoint.py now runs the full first-party bundle "
        "sync via sync_agent_bundle.py.",
        file=sys.stderr,
    )
    sys.exit(sync_bundle())
