# agent

AgentCore Redaction Agent for Amazon Bedrock AgentCore Runtime. The pipeline
tools (extract, OCR, match, detect PII, redact, verify structure, store,
update status) are plain, strands-free functions in `tools/`; `redaction_agent.py`
registers all of them on a Strands `Agent` (via `strands.tool` + `Agent`),
while `orchestration.py` (`handle_job`) executes them in a fixed, deterministic
order. The Agent's reasoning loop is not run — control flow stays deterministic.

## Deploying

AgentCore deploys `backend/agent_bundle/package_clean/`, which is **gitignored**
and holds a full copy of the first-party packages (`agent/`, `shared/`) plus the
vendored third-party dependencies. The first-party code is the source of record;
the bundle copy is derived from it.

Before every `cdk deploy`, sync the entire first-party tree into the bundle:

```powershell
backend\.venv\Scripts\python.exe backend\scripts\sync_agent_bundle.py
cdk deploy
```

`sync_agent_bundle.py` copies every `.py` under `backend/agent/` and
`backend/shared/` into the bundle byte-for-byte (mirroring additions and
deletions), copies the `main.py` entrypoint to the bundle root, purges stale
bytecode, and hash-verifies every synced file. It never touches the vendored
dependencies. It fails loudly (non-zero exit) if the bundle directory is absent.

Do **not** hand-copy individual files or sync only `main.py`: a partial sync is
what once shipped a stale bundle with the log-delivery fix half-wired.
`scripts/sync_agent_entrypoint.py` is kept only as a backward-compatible shim
that now runs the same full sync.

After deploy, confirm records actually arrive: invoke a job and check the
runtime's CloudWatch log group holds `agent.*` / `shared.*` events, not only the
SDK's `bedrock_agentcore.app` events.
