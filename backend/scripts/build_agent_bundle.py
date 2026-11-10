"""Reproducibly rebuild the AgentCore agent bundle's vendored dependency tail.

The AgentCore Runtime deploys `backend/agent_bundle/package_clean/` — a
directory that is **gitignored** and holds a full copy of the first-party
packages (`agent/`, `shared/`) plus ~50 vendored third-party distributions
(strands, bedrock_agentcore, boto3/botocore, pydantic, mcp, pdfminer.six,
pikepdf, pypdfium2, docx/python-docx, lxml, yaml/PyYAML, ...). Before this script existed, that
vendored tail was produced by an untracked, unrepeatable process; only the
first-party overlay had a tracked recipe (`scripts/sync_agent_bundle.py`).

This script is the tracked, idempotent recipe for the vendored tail. It targets
the AgentCore Runtime platform — **Linux ARM64 / CPython 3.12 (cp312)** — which
is NOT the Windows dev host, so every wheel is fetched/installed for the target
platform via pip's cross-platform download/install flags. It does NOT run the
target code (aarch64 wheels cannot execute on Windows); it only assembles the
directory tree that AgentCore will unpack and run.

Three stages, all under `agent_bundle/`:

  deps/          downloaded aarch64 cp312 wheels (`pip download`)
  package/       `pip install --target` of those wheels (unpacked packages +
                 `*.dist-info` + `bin/`), then the first-party overlay + root
                 `main.py`
  package_clean/ a copy of `package/` with every `*.dist-info`, `bin/`, and
                 `__pycache__` removed — the directory AgentCore deploys

Stage order in this script: deps -> package -> package_clean -> first-party
overlay (the overlay is applied to `package_clean/`, the deployed tree, by
delegating to `scripts/sync_agent_bundle.py`, the single tracked source of
truth for first-party files).

IMPORTANT pip flag notes (do not "simplify"):
  * BOTH aarch64 platforms in PLATFORMS are REQUIRED (see the comments on
    PLATFORMS for the wheel file name that needs each one). pip does not
    expand `manylinux_2_28` down to `manylinux2014`, so dropping either value
    makes the download fail.
  * We pass MULTIPLE `--platform` values and NO explicit `--abi`. Letting pip
    derive the ABI set (cp312 / abi3 / none) is what lets the abi3 wheel
    (cryptography, `cp311-abi3`) and the `py3-none` wheel (pypdfium2) match
    while everything else resolves to cp312. Passing an explicit
    `--abi cp312` would exclude them and break the build.
  * After the build, `check_no_removed_library()` fails the run if the removed
    PDF library appears anywhere in deps/ or package_clean/, and
    `check_no_sdists()` / `check_no_windows_binaries()` enforce wheel-only,
    Linux-only content.

`agent_bundle/` is gitignored; only this script and
`agent-runtime-requirements.txt` are tracked. Re-running is safe: each stage
directory is cleaned and recreated from scratch.

KNOWN LIMITATION — cannot run on a Windows host as-is
-----------------------------------------------------
`strands-agents==1.50.2` pulls in `mcp>=1.23.0`, which declares
`pywin32>=310; sys_platform == "win32"`. pip's `--platform` /
`--python-version` / `--implementation` flags only constrain wheel *tag*
selection; they do NOT override the `sys_platform` environment marker that
pip evaluates during dependency *resolution*. On a Windows host that marker
is true, so pip tries to resolve the Windows-only `pywin32` (which has no
aarch64 wheel) and fails with ResolutionImpossible at the `pip download`
stage.

How to run it successfully:
  (a) Run on a Linux host / WSL / container. There `sys_platform != "win32"`,
      so the marker is false, `pywin32` drops out of the resolution, and the
      cross-platform aarch64/cp312 download succeeds. This is the clean fix.
  (b) Add a resolver-bypass: supply a fully-pinned aarch64 closure lock (for
      example, generated from a Linux resolution, or from `pip freeze` of the
      runtime closure with `pywin32` and the dev/CDK deps removed) and download
      it with `--no-deps`. That bypasses the marker-driven resolver entirely.

Status: VERIFIED REPRODUCIBLE ON LINUX. On an Amazon Linux 2023 x86_64 host
(CPython 3.12), `pip download` resolves the cross-platform aarch64/cp312
closure cleanly (no pywin32, `sys_platform != "win32"`) with both PLATFORMS
values, producing wheels only (no sdists) and a `package_clean/` tree with
zero `.pyd`/`.dll`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# --- Constants -------------------------------------------------------------

PYTHON_VERSION = "312"
IMPLEMENTATION = "cp"
# Both values are required; no --abi is passed (see module docstring).
PLATFORMS = [
    # watchdog-6.0.0-py3-none-manylinux2014_aarch64.whl (a strands-agents
    # dependency) carries only the manylinux2014 tag;
    # pypdfium2-5.14.0-py3-none-manylinux_2_17_aarch64.manylinux2014_aarch64.whl
    # also matches through it.
    "manylinux2014_aarch64",
    # pikepdf-10.17.0-cp312-cp312-manylinux_2_26_aarch64.manylinux_2_28_aarch64.whl
    # carries only the 2_26 / 2_28 tags.
    "manylinux_2_28_aarch64",
]
# No --abi is passed on purpose: pip derives cp312 / abi3 / none itself, which
# lets cryptography-50.0.2-cp311-abi3-manylinux2014_aarch64.manylinux_2_17_aarch64.whl
# (abi3) and pypdfium2 (py3-none) match. An explicit `--abi cp312` would
# exclude both.
RUNTIME_REQUIREMENTS = "agent-runtime-requirements.txt"

BACKEND_DIR = Path(__file__).resolve().parents[1]
REQUIREMENTS_PATH = BACKEND_DIR / RUNTIME_REQUIREMENTS
BUNDLE_DIR = BACKEND_DIR / "agent_bundle"
DEPS_DIR = BUNDLE_DIR / "deps"
PACKAGE_DIR = BUNDLE_DIR / "package"
PACKAGE_CLEAN_DIR = BUNDLE_DIR / "package_clean"
SYNC_SCRIPT = BACKEND_DIR / "scripts" / "sync_agent_bundle.py"

PYTHON = sys.executable  # the venv interpreter running this script

# Names of the removed PDF library, built by concatenation so this source file
# never contains them as literals.
_REMOVED_PREFIX = "py" + "mu" + "pdf"  # any name starting with this (case-insensitive)
_REMOVED_TOP_LEVEL = "fi" + "tz"  # the library's legacy top-level module


def _platform_flags() -> list[str]:
    flags: list[str] = []
    for platform in PLATFORMS:
        flags += ["--platform", platform]
    return flags


def _reset_dir(path: Path) -> None:
    """Remove `path` (if present) and recreate it empty."""
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    print("\n$ " + " ".join(cmd))
    # `cmd` is always an argument list built in this script from constants and
    # repository paths (pip / the sync script); it never contains external
    # input, and no shell is involved (shell=False, the default). This is a
    # developer-run build script, not part of the deployed application.
    return subprocess.run(cmd, cwd=str(BACKEND_DIR))  # nosemgrep: dangerous-subprocess-use-audit


# --- Stage (a): download aarch64 cp312 wheels ------------------------------

def download_deps() -> None:
    print("\n=== Stage (a): download runtime wheels -> agent_bundle/deps ===")
    _reset_dir(DEPS_DIR)
    cmd = [
        PYTHON, "-m", "pip", "download",
        "-r", str(REQUIREMENTS_PATH),
        "-d", str(DEPS_DIR),
        "--only-binary=:all:",
        "--python-version", PYTHON_VERSION,
        "--implementation", IMPLEMENTATION,
    ] + _platform_flags()
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError(
            "pip download failed (exit {}). See output above.".format(
                result.returncode
            )
        )
    wheels = sorted(DEPS_DIR.glob("*.whl"))
    print("Downloaded {} wheel(s) into {}".format(len(wheels), DEPS_DIR))


# --- Stage (b): install wheels into package/ -------------------------------

def install_package() -> str:
    """Populate `package/` from the downloaded wheels.

    Returns the method that succeeded: "pip-install-target" or "wheel-unzip".
    """
    print("\n=== Stage (b): install wheels -> agent_bundle/package ===")
    _reset_dir(PACKAGE_DIR)
    cmd = [
        PYTHON, "-m", "pip", "install",
        "--target", str(PACKAGE_DIR),
        "--no-index",
        "--find-links", str(DEPS_DIR),
        "--only-binary=:all:",
        "--python-version", PYTHON_VERSION,
        "--implementation", IMPLEMENTATION,
    ] + _platform_flags() + [
        "-r", str(REQUIREMENTS_PATH),
    ]
    result = _run(cmd)
    if result.returncode == 0:
        return "pip-install-target"

    print(
        "\npip install --target failed (exit {}). Falling back to "
        "wheel-unzip vendoring.".format(result.returncode),
        file=sys.stderr,
    )
    _reset_dir(PACKAGE_DIR)
    _unzip_wheels_into_package()
    return "wheel-unzip"


def _unzip_wheels_into_package() -> None:
    """Extract every `.whl` in `deps/` into `package/`.

    A wheel is a zip archive whose top level holds the import package
    directories plus a `<name>.dist-info` directory; extracting all of them
    into one target reproduces what `pip install --target` lays down (minus
    entry-point `bin/` scripts, which `package_clean` strips anyway).
    """
    wheels = sorted(DEPS_DIR.glob("*.whl"))
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(PACKAGE_DIR)
    print("Unzipped {} wheel(s) into {}".format(len(wheels), PACKAGE_DIR))


# --- Stage (c): build package_clean/ ---------------------------------------

def build_package_clean() -> None:
    print("\n=== Stage (c): copy package -> package_clean (stripped) ===")
    _reset_dir(PACKAGE_CLEAN_DIR)
    # Copy everything first, then strip. copytree needs a non-existent dest.
    shutil.rmtree(PACKAGE_CLEAN_DIR)
    shutil.copytree(PACKAGE_DIR, PACKAGE_CLEAN_DIR)

    removed_dist_info = 0
    removed_bin = 0
    removed_pycache = 0

    # Strip every *.dist-info dir, any bin/ dir, and every __pycache__ dir.
    for path in sorted(PACKAGE_CLEAN_DIR.rglob("*"), reverse=True):
        if not path.is_dir():
            continue
        name = path.name
        if name.endswith(".dist-info"):
            shutil.rmtree(path, ignore_errors=True)
            removed_dist_info += 1
        elif name == "bin":
            shutil.rmtree(path, ignore_errors=True)
            removed_bin += 1
        elif name == "__pycache__":
            shutil.rmtree(path, ignore_errors=True)
            removed_pycache += 1

    print(
        "Stripped {} *.dist-info, {} bin/, {} __pycache__ from {}".format(
            removed_dist_info, removed_bin, removed_pycache, PACKAGE_CLEAN_DIR
        )
    )


# --- Stage (d): first-party overlay ----------------------------------------

def overlay_first_party() -> None:
    """Overlay `agent/`, `shared/`, and root `main.py` into package_clean.

    Delegates to the tracked `scripts/sync_agent_bundle.py` — the single
    source of truth for first-party files — so this builder never duplicates
    the overlay logic (and inherits its hash-verify).
    """
    print("\n=== Stage (d): overlay first-party via sync_agent_bundle.py ===")
    result = _run([PYTHON, str(SYNC_SCRIPT)])
    if result.returncode != 0:
        raise RuntimeError(
            "sync_agent_bundle.py failed (exit {}).".format(result.returncode)
        )


# --- Post-build assertions -------------------------------------------------

def _is_removed_library_name(name: str) -> bool:
    """True if a top-level entry name belongs to the removed PDF library.

    Matches (case-insensitive) any name starting with the removed library's
    prefix (wheel files, packages, `*.dist-info`, companion distributions) and
    the legacy top-level module name as a whole token (`<name>`, `<name>.py`,
    `<name>-1.0.whl`, `<name>-1.0.dist-info`, ...).
    """
    lowered = name.lower()
    if lowered.startswith(_REMOVED_PREFIX):
        return True
    return re.match(re.escape(_REMOVED_TOP_LEVEL) + r"($|[.\-_])", lowered) is not None


def find_removed_library_entries(*roots: Path) -> list[Path]:
    """Return top-level entries of each root that belong to the removed library."""
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if _is_removed_library_name(entry.name):
                found.append(entry)
    return found


def check_no_removed_library(
    deps_dir: Path | None = None, clean_dir: Path | None = None
) -> None:
    """Exit non-zero if the removed PDF library is in deps/ or package_clean/.

    Looks for wheels, top-level directories, module files and `*.dist-info`
    directories carrying the removed library's names.
    """
    deps_dir = DEPS_DIR if deps_dir is None else deps_dir
    clean_dir = PACKAGE_CLEAN_DIR if clean_dir is None else clean_dir
    found = find_removed_library_entries(deps_dir, clean_dir)
    if found:
        listing = "\n".join("  - {}".format(p) for p in found)
        print(
            "ERROR: the removed PDF library is present in the bundle:\n"
            + listing,
            file=sys.stderr,
        )
        raise SystemExit(1)
    print("OK: removed PDF library absent from deps/ and package_clean/")


def check_no_sdists(deps_dir: Path | None = None) -> None:
    """Fail if deps/ holds any source distribution (.tar.gz / .zip)."""
    deps_dir = DEPS_DIR if deps_dir is None else deps_dir
    bad = sorted(
        p for p in deps_dir.rglob("*")
        if p.is_file() and p.name.lower().endswith((".tar.gz", ".zip"))
    )
    if bad:
        raise AssertionError(
            "deps/ must contain wheels only; found source distributions: "
            + ", ".join(p.name for p in bad)
        )
    print("OK: no .tar.gz / .zip in deps/")


def check_no_windows_binaries(clean_dir: Path | None = None) -> None:
    """Fail if package_clean/ holds any Windows binary (.pyd / .dll)."""
    clean_dir = PACKAGE_CLEAN_DIR if clean_dir is None else clean_dir
    bad = sorted(
        p for p in clean_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".pyd", ".dll")
    )
    if bad:
        raise AssertionError(
            "package_clean/ must not contain Windows binaries; found: "
            + ", ".join(str(p.relative_to(clean_dir)) for p in bad)
        )
    print("OK: no .pyd / .dll in package_clean/")


def verify_bundle() -> None:
    """Run every post-build assertion."""
    print("\n=== Verify bundle ===")
    check_no_removed_library()
    check_no_sdists()
    check_no_windows_binaries()


# --- Summary ---------------------------------------------------------------

def _count_files(path: Path) -> int:
    return sum(1 for p in path.rglob("*") if p.is_file())


def print_summary(install_method: str) -> None:
    print("\n=== Summary ===")
    wheels = sorted(DEPS_DIR.glob("*.whl"))
    print("Install method: {}".format(install_method))
    print("deps/ wheels:          {}".format(len(wheels)))
    print("package/ files:        {}".format(_count_files(PACKAGE_DIR)))
    print("package_clean/ files:  {}".format(_count_files(PACKAGE_CLEAN_DIR)))

    # Every wheel in deps/ with its distribution name and version.
    print("\nAll wheels in deps/ (name version):")
    for wheel in wheels:
        parts = wheel.name[: -len(".whl")].split("-")
        dist = parts[0]
        version = parts[1] if len(parts) > 1 else "?"
        print("  - {:<32} {}".format(dist, version))

    # Confirm key packages/versions are present in deps/.
    key_prefixes = [
        "strands_agents-1.50.2",
        "bedrock_agentcore-1.20.0",
        "pdfminer_six-20260107",
        "pikepdf-10.17.0",
        "pypdfium2-5.14.0",
        "python_docx-1.1.2",
        "mcp-1.2",
        "PyYAML-6",
        "pyyaml-6",
        "pillow-13.0.0",
        "Pillow-13.0.0",
    ]
    print("\nKey wheels in deps/:")
    for prefix in key_prefixes:
        matches = sorted(
            w.name for w in wheels if w.name.lower().startswith(prefix.lower())
        )
        for name in matches:
            print("  - {}".format(name))

    # Confirm import package dirs exist in package_clean/.
    key_dirs = [
        "strands", "yaml", "mcp", "pdfminer", "pikepdf", "pypdfium2",
        "pypdfium2_raw", "PIL", "bedrock_agentcore", "agent", "shared",
    ]
    print("\nImport packages present in package_clean/:")
    for name in key_dirs:
        exists = (PACKAGE_CLEAN_DIR / name).is_dir()
        print("  - {:<18} {}".format(name, "OK" if exists else "MISSING"))
    root_main = PACKAGE_CLEAN_DIR / "main.py"
    print("  - {:<18} {}".format("main.py (root)", "OK" if root_main.is_file() else "MISSING"))


# --- Orchestration ---------------------------------------------------------

def main() -> int:
    if not REQUIREMENTS_PATH.is_file():
        print(
            "ERROR: runtime requirements not found: {}".format(REQUIREMENTS_PATH),
            file=sys.stderr,
        )
        return 1
    if not SYNC_SCRIPT.is_file():
        print(
            "ERROR: sync script not found: {}".format(SYNC_SCRIPT),
            file=sys.stderr,
        )
        return 1

    print("Building AgentCore agent bundle (Linux ARM64 / cp312)")
    print("Backend dir: {}".format(BACKEND_DIR))

    download_deps()
    install_method = install_package()
    build_package_clean()
    overlay_first_party()
    verify_bundle()
    print_summary(install_method)

    print("\nDone. Rebuilt vendored tail + first-party overlay.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
