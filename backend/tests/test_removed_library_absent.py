"""Checks that the removed PDF library is absent from the environment and repo.

All searched names are built by concatenation so this file itself never
contains the removed identifiers as literals.
"""

from __future__ import annotations

import ast
import importlib
import importlib.metadata as md
import io
import re
import tokenize
from pathlib import Path

import pytest

_LIB = "py" + "mu" + "pdf"
_LEGACY = "fi" + "tz"
# Spec directory / feature-tag token. It is the only allowed occurrence of the
# removed name: property tests carry "Feature: <spec-name>, Property N" tags and
# requirements-dev.txt refers to the spec by name. It is stripped before matching;
# any other occurrence fails.
_SPEC_TOKEN = _LIB + "-replacement"

_LEGACY_API = [
    "page" + ".rect",
    "get_" + "pixmap",
    "raw" + "dict",
    "add_redact" + "_annot",
    "apply_" + "redactions",
    "replace_" + "image",
    "PDF_REDACT_" + "IMAGE_NONE",
]

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

_EXCLUDED_DIRS = {
    ".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".hypothesis",
    ".pytest_cache", "cdk.out", "dist", "build", "agent_bundle", ".mypy_cache",
    ".ruff_cache", "site-packages",
}
_SPEC_DIR = REPO_ROOT / ".kiro" / "specs" / _SPEC_TOKEN
_EXEMPT = {_SPEC_DIR / n for n in ("requirements.md", "design.md", "tasks.md")}

_NAME_RE = re.compile("|".join(re.escape(n) for n in (_LIB, _LEGACY)), re.IGNORECASE)
_LEGACY_NAME_RE = re.compile(r"\b" + re.escape(_LEGACY) + r"\b", re.IGNORECASE)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_no_installed_distribution() -> None:
    bad = []
    for dist in md.distributions():
        name = _norm(dist.metadata["Name"] or "")
        if name.startswith(_norm(_LIB)) or name == _norm(_LEGACY):
            bad.append(name)
    assert not bad, bad


@pytest.mark.parametrize("module", [_LIB, _LEGACY])
def test_import_raises(module: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module)


def _iter_text_files():
    stack = [REPO_ROOT]
    while stack:
        d = stack.pop()
        for p in d.iterdir():
            if p.is_dir():
                if p.name in _EXCLUDED_DIRS or p.name.endswith(".egg-info"):
                    continue
                stack.append(p)
            elif p.is_file() and p not in _EXEMPT:
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if b"\0" in data:
                    continue
                try:
                    yield p, data.decode("utf-8")
                except UnicodeDecodeError:
                    continue


def test_repo_has_no_removed_library_mentions() -> None:
    hits = []
    for path, text in _iter_text_files():
        cleaned = text.replace(_SPEC_TOKEN, "")
        for i, line in enumerate(cleaned.splitlines(), 1):
            if _NAME_RE.search(line):
                hits.append(f"{path.relative_to(REPO_ROOT)}:{i}")
    assert not hits, hits[:50]


_PDF_MODULES = [
    "extraction_pdf", "ocr_pdf", "redaction_pdf", "pdf_glyphs",
    "pdf_geometry", "pdf_content_edit", "pdf_images",
]

_REF_RES = [
    re.compile(r"\btask\s*\d", re.IGNORECASE),
    re.compile(r"\brequirements?\s*\d", re.IGNORECASE),
    re.compile(r"\brequirements?\s*[:#]?\s*\d", re.IGNORECASE),
]


def _comments_and_docstrings(path: Path) -> list[tuple[int, str]]:
    src = path.read_text(encoding="utf-8")
    out: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            out.append((tok.start[0], tok.string))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                line = node.body[0].lineno if node.body else 1
                out.append((line, doc))
    return out


@pytest.mark.parametrize("module", _PDF_MODULES)
def test_pdf_module_comments_clean(module: str) -> None:
    path = BACKEND / "agent" / "tools" / f"{module}.py"
    if not path.exists():
        pytest.skip(f"{module} not present")
    problems = []
    for line, text in _comments_and_docstrings(path):
        scrubbed = text.replace(_SPEC_TOKEN, "")
        for rx in _REF_RES:
            if rx.search(scrubbed):
                problems.append((line, "ref", rx.pattern))
        if _NAME_RE.search(scrubbed):
            problems.append((line, "library name", ""))
        for api in _LEGACY_API:
            if re.search(re.escape(api), scrubbed, re.IGNORECASE):
                problems.append((line, "legacy api", api))
    assert not problems, problems
