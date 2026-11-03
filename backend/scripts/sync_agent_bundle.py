"""Sync the entire first-party source tree into the Agent_Bundle.

AgentCore deploys `backend/agent_bundle/package_clean/`, a directory that is
**gitignored** and holds a full copy of the first-party packages (`agent/`,
`shared/`) *plus* ~50 vendored third-party distributions (`boto3`, `botocore`,
`pydantic`, `strands`, `mcp`, `pdfminer`, `pikepdf`, `pypdfium2`, `docx`, `lxml`, ...). The vendored tail
is produced by whatever process builds the bundle; the first-party code was
kept in sync by hand-run copies, and `scripts/sync_agent_entrypoint.py` synced
only the single entrypoint file `main.py`.

That partial sync shipped a stale bundle to production: `agent/logging_config.py`
was missing entirely and four other first-party files had not been copied, so
the agent-log-delivery fix went out half-wired with no error to signal it. This
script exists so no first-party file can silently go stale.

Run from anywhere, no arguments:

    backend\\.venv\\Scripts\\python.exe backend\\scripts\\sync_agent_bundle.py

then `cdk deploy`. Re-run it after every edit under `backend/agent/` or
`backend/shared/`.

What it does, and just as importantly what it does not:

**Source of truth is exactly two trees.** `backend/agent/` and
`backend/shared/`. Every `.py` under them is copied, byte-for-byte, to the
corresponding path under `package_clean/agent/` and `package_clean/shared/`,
preserving subdirectory structure (`agent/tools/*.py`, etc.). Nothing outside
those two owned trees is touched. The script never reads, writes, or reasons
about the vendored distributions — the safe framing is "sync the two packages
I own; leave everything else alone", which is closed by construction rather
than by maintaining a deny-list of dependency names.

**It mirrors, so deletions and additions both propagate.** A new first-party
file (as `logging_config.py` once was) appears; a `.py` deleted from source no
longer lingers in the bundle. Removal is scoped strictly to `.py` files under
the two owned bundle subtrees — never non-`.py` files, never vendored
directories — because those two trees are fully owned and reconstructable from
source, and a leftover stale module is exactly the failure this script exists
to prevent.

**The entrypoint is special-cased to the bundle ROOT.** `backend/agent/main.py`
is the tracked source of record for the entrypoint, but the deployed
entrypoint lives at `package_clean/main.py` (the bundle root), *not* under
`agent/`. Its `sys.path.insert(0, dirname(abspath(__file__)))` line is
load-bearing only at that root — see `sync_agent_entrypoint.py`, which this
script subsumes. So `main.py` is excluded from the `agent/` tree copy and
copied to the root instead; a stray `package_clean/agent/main.py` would be
removed by the mirror.

**Bytecode is purged.** `__pycache__` under the two owned subtrees is removed
(plus the root's `main.*.pyc`) so a stale `.pyc` cannot shadow a freshly
copied `.py`. The vendored tree's bytecode is left alone.

**A missing bundle directory is a hard failure.** Non-zero exit, message
naming the path, no directory created — identical to the entrypoint script's
contract. Silently succeeding would let an operator deploy a stale bundle
believing it was synced.

**Every synced file is hash-verified** against its source after copying; a
mismatch is a non-zero exit. On success the script prints the counts and exits
0. Running it twice is idempotent: the second run copies the same bytes and
reports the same counts.

This is a focused sync tool. It is not a dependency installer, not a bundle
builder, and not a deploy wrapper.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
BUNDLE_DIR = BACKEND_DIR / "agent_bundle" / "package_clean"

# The first-party packages this script owns and keeps honest. Anything in the
# bundle that is not one of these is a vendored dependency and is never touched.
OWNED_PACKAGES = ("agent", "shared")

# The entrypoint is the source of record `backend/agent/main.py`, but it is
# deployed at the bundle ROOT, not inside the `agent/` package. It is therefore
# excluded from the `agent/` tree copy and handled separately.
ENTRYPOINT_NAME = "main.py"
ENTRYPOINT_SOURCE = BACKEND_DIR / "agent" / ENTRYPOINT_NAME


def _iter_py_files(root: Path) -> list[Path]:
    """Return every `.py` file under `root`, ignoring `__pycache__`."""
    return sorted(
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _purge_pycache(root: Path) -> list[Path]:
    """Delete every `__pycache__` directory under `root`. Return them."""
    if not root.is_dir():
        return []
    removed = sorted(p for p in root.rglob("__pycache__") if p.is_dir())
    for cache_dir in removed:
        shutil.rmtree(cache_dir, ignore_errors=True)
    return removed


def sync_tree(
    package: str,
    backend_dir: Path = BACKEND_DIR,
    bundle_dir: Path = BUNDLE_DIR,
) -> tuple[list[Path], list[Path]]:
    """Mirror one owned package's `.py` files into the bundle.

    Copies every `.py` under `backend_dir/package` (except the entrypoint,
    which lives at the bundle root) to the matching path under
    `bundle_dir/package`, then removes any `.py` in `bundle_dir/package` that no
    longer exists in the source. Returns `(copied, removed)`.
    """
    source_root = backend_dir / package
    dest_root = bundle_dir / package

    source_files = [
        p for p in _iter_py_files(source_root)
        if not (package == "agent" and p == ENTRYPOINT_SOURCE)
    ]
    wanted_rel = {p.relative_to(source_root) for p in source_files}

    copied: list[Path] = []
    for src in source_files:
        rel = src.relative_to(source_root)
        dest = dest_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)  # byte-for-byte; never rewrite
        copied.append(dest)

    removed: list[Path] = []
    if dest_root.is_dir():
        for existing in _iter_py_files(dest_root):
            rel = existing.relative_to(dest_root)
            if rel not in wanted_rel:
                existing.unlink()
                removed.append(existing)

    return copied, removed


def sync_entrypoint(
    source: Path = ENTRYPOINT_SOURCE,
    bundle_dir: Path = BUNDLE_DIR,
) -> Path:
    """Copy the entrypoint source to the bundle ROOT, byte-for-byte."""
    destination = bundle_dir / ENTRYPOINT_NAME
    shutil.copyfile(source, destination)  # byte-for-byte; never rewrite
    # A stale `.pyc` for the entrypoint at the root can shadow the copy.
    cache_dir = bundle_dir / "__pycache__"
    if cache_dir.is_dir():
        for stale in sorted(cache_dir.glob(Path(ENTRYPOINT_NAME).stem + ".*.pyc")):
            stale.unlink()
    return destination


def _verify(pairs: list[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """Return the (source, dest) pairs whose bytes differ."""
    return [(src, dest) for src, dest in pairs if _sha256(src) != _sha256(dest)]


def sync_bundle(
    backend_dir: Path = BACKEND_DIR,
    bundle_dir: Path = BUNDLE_DIR,
) -> int:
    """Sync both owned trees and the entrypoint into the bundle.

    Returns a process exit status: 0 on success, 1 on any failure (missing
    source, missing bundle directory, or a post-copy hash mismatch).
    """
    if not ENTRYPOINT_SOURCE.is_file():
        print(
            "ERROR: entrypoint source not found: {}".format(ENTRYPOINT_SOURCE),
            file=sys.stderr,
        )
        return 1
    for package in OWNED_PACKAGES:
        source_root = backend_dir / package
        if not source_root.is_dir():
            print(
                "ERROR: first-party source tree not found: {}".format(source_root),
                file=sys.stderr,
            )
            return 1
    if not bundle_dir.is_dir():
        print(
            "ERROR: Agent_Bundle directory not found: {}\n"
            "       Build the bundle before syncing; this script will not "
            "create it.".format(bundle_dir),
            file=sys.stderr,
        )
        return 1

    verify_pairs: list[tuple[Path, Path]] = []
    total_copied = 0
    total_removed = 0

    for package in OWNED_PACKAGES:
        copied, removed = sync_tree(package, backend_dir, bundle_dir)
        total_copied += len(copied)
        total_removed += len(removed)
        source_root = backend_dir / package
        dest_root = bundle_dir / package
        for dest in copied:
            src = source_root / dest.relative_to(dest_root)
            verify_pairs.append((src, dest))
        for purged in _purge_pycache(dest_root):
            print("Removed bytecode cache {}".format(purged))
        for gone in removed:
            print("Removed stale first-party file {}".format(gone))

    entry_dest = sync_entrypoint(ENTRYPOINT_SOURCE, bundle_dir)
    verify_pairs.append((ENTRYPOINT_SOURCE, entry_dest))
    total_copied += 1

    mismatches = _verify(verify_pairs)
    if mismatches:
        for src, dest in mismatches:
            print(
                "ERROR: content mismatch after copy: {} != {}".format(src, dest),
                file=sys.stderr,
            )
        return 1

    print(
        "Synced {} first-party file(s) into {} "
        "({} verified byte-identical, {} stale removed).".format(
            total_copied, bundle_dir, len(verify_pairs), total_removed
        )
    )
    print("Entrypoint {} -> {}".format(ENTRYPOINT_SOURCE, entry_dest))
    return 0


if __name__ == "__main__":
    sys.exit(sync_bundle())
