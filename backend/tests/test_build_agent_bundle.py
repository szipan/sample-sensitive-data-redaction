"""Tests for scripts/build_agent_bundle.py (Requirements 1.3, 1.4, 3.2, 3.3, 3.4).

This file must not contain the removed PDF library's names as literals, so they
are built by concatenation below.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_agent_bundle.py"
_spec = importlib.util.spec_from_file_location("build_agent_bundle_under_test", _SCRIPT)
bundle = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bundle
_spec.loader.exec_module(bundle)

# the removed PDF library (names built by concatenation)
REMOVED = "py" + "mu" + "pdf"
REMOVED_TOP = "fi" + "tz"

PLANTED_NAMES = [
    REMOVED + "-1.24.0-cp312-cp312-manylinux_2_28_aarch64.whl",  # wheel
    REMOVED.upper() + "-1.24.0-py3-none-any.whl",  # upper-case wheel
    REMOVED,  # top-level dir
    REMOVED.capitalize(),  # mixed case dir
    REMOVED + "b",  # companion package
    REMOVED + "b-1.0.dist-info",
    REMOVED + "-fonts",  # fonts package built from the prefix
    REMOVED + "_fonts-1.0.dist-info",
    REMOVED + "-1.24.0.dist-info",
    REMOVED.upper() + "-1.24.0.DIST-INFO",
    REMOVED_TOP,  # legacy top-level dir
    REMOVED_TOP + ".py",  # module file
    REMOVED_TOP.upper() + ".py",
    REMOVED_TOP + "-1.0.dist-info",
]


def _populate_clean(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name in ("pdfminer_six-20260107-py3-none-any.whl",
                 "pikepdf-10.17.0-cp312-cp312-manylinux_2_28_aarch64.whl",
                 "pypdfium2-5.14.0-py3-none-manylinux2014_aarch64.whl"):
        (root / name).write_bytes(b"")
    for d in ("pdfminer", "pikepdf", "pypdfium2", "pypdfium2_raw",
              "pdfminer_six-20260107.dist-info", "pikepdf-10.17.0.dist-info"):
        (root / d).mkdir()


@pytest.fixture
def trees(tmp_path):
    deps = tmp_path / "deps"
    clean = tmp_path / "package_clean"
    _populate_clean(deps)
    _populate_clean(clean)
    return deps, clean


def _plant(root: Path, name: str) -> None:
    p = root / name
    if name.lower().endswith((".whl", ".py")):
        p.write_bytes(b"")
    else:
        p.mkdir()


def test_clean_tree_passes(trees):
    deps, clean = trees
    bundle.check_no_removed_library(deps, clean)


@pytest.mark.parametrize("which", ["deps", "clean"])
@pytest.mark.parametrize("name", PLANTED_NAMES)
def test_planted_removed_library_fails(trees, which, name):
    deps, clean = trees
    _plant(deps if which == "deps" else clean, name)
    with pytest.raises(SystemExit) as exc:
        bundle.check_no_removed_library(deps, clean)
    assert exc.value.code not in (0, None)


# --- download stage --------------------------------------------------------


def _completed(code: int):
    return subprocess.CompletedProcess(args=[], returncode=code)


def test_download_deps_nonzero_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "DEPS_DIR", tmp_path / "deps")
    with mock.patch.object(bundle.subprocess, "run", return_value=_completed(1)):
        with pytest.raises(RuntimeError):
            bundle.download_deps()


def test_main_stops_after_download_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "DEPS_DIR", tmp_path / "deps")
    with mock.patch.object(bundle.subprocess, "run", return_value=_completed(2)), \
            mock.patch.object(bundle, "install_package") as install, \
            mock.patch.object(bundle, "build_package_clean") as clean, \
            mock.patch.object(bundle, "overlay_first_party") as overlay, \
            mock.patch.object(bundle, "verify_bundle") as verify:
        with pytest.raises(RuntimeError):
            bundle.main()
    install.assert_not_called()
    clean.assert_not_called()
    overlay.assert_not_called()
    verify.assert_not_called()


def test_download_command_platform_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "DEPS_DIR", tmp_path / "deps")
    with mock.patch.object(bundle.subprocess, "run", return_value=_completed(0)) as run:
        bundle.download_deps()
    cmd = run.call_args.args[0]
    assert "download" in cmd
    platforms = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--platform"]
    assert platforms == list(bundle.PLATFORMS)
    assert "--abi" not in cmd
    assert not any(a.startswith("--abi=") for a in cmd)
    # each tag is its own --platform argument (never comma/space joined)
    assert all("," not in p and " " not in p for p in platforms)


def test_install_command_platform_flags_and_no_abi(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle, "PACKAGE_DIR", tmp_path / "package")
    with mock.patch.object(bundle.subprocess, "run", return_value=_completed(0)) as run:
        assert bundle.install_package() == "pip-install-target"
    cmd = run.call_args.args[0]
    platforms = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--platform"]
    assert platforms == list(bundle.PLATFORMS)
    assert "--abi" not in cmd


# --- constants and wheel-only / linux-only checks --------------------------


def test_platforms_exactly_two_values():
    assert set(bundle.PLATFORMS) == {"manylinux2014_aarch64", "manylinux_2_28_aarch64"}
    assert len(bundle.PLATFORMS) == 2


@pytest.mark.parametrize("name", ["foo-1.0.tar.gz", "bar-2.0.zip", "BAZ-1.TAR.GZ"])
def test_check_no_sdists_fails_on_sdist(tmp_path, name):
    (tmp_path / "ok-1.0-py3-none-any.whl").write_bytes(b"")
    (tmp_path / name).write_bytes(b"")
    with pytest.raises(AssertionError):
        bundle.check_no_sdists(tmp_path)


def test_check_no_sdists_passes_on_wheels(tmp_path):
    (tmp_path / "a-1.0-py3-none-any.whl").write_bytes(b"")
    (tmp_path / "b-1.0-cp312-cp312-manylinux2014_aarch64.whl").write_bytes(b"")
    bundle.check_no_sdists(tmp_path)


@pytest.mark.parametrize("name", ["x.pyd", "sub/y.dll", "sub/Z.DLL"])
def test_check_no_windows_binaries_fails(tmp_path, name):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"")
    with pytest.raises(AssertionError):
        bundle.check_no_windows_binaries(tmp_path)


def test_check_no_windows_binaries_passes(tmp_path):
    (tmp_path / "lib.so").write_bytes(b"")
    (tmp_path / "m.py").write_text("")
    bundle.check_no_windows_binaries(tmp_path)


def test_verify_bundle_runs_all_checks():
    with mock.patch.object(bundle, "check_no_removed_library") as a, \
            mock.patch.object(bundle, "check_no_sdists") as b, \
            mock.patch.object(bundle, "check_no_windows_binaries") as c:
        bundle.verify_bundle()
    a.assert_called_once()
    b.assert_called_once()
    c.assert_called_once()
