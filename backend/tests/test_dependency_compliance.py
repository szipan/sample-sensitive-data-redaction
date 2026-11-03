"""Dependency compliance tests (PDF library replacement spec, task 15.3).

Checks the dependency manifests, THIRD-PARTY-LICENSES and README for
consistency: exact pins, no removed legacy library, license table agreement,
no GPL-family licenses, MPL-2.0 handling and bundled native libraries.

Validates: Requirements 1.1, 2.2, 2.5, 2.6, 2.8, 2.9, 2.10, 2.11, 2.14, 3.8
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent

# Removed library names are built by concatenation so this file never contains
# the literal names.
LEGACY_PREFIX = "py" + "mu" + "pdf"
LEGACY_TOP = "fi" + "tz"

MANIFESTS = {
    "requirements.txt": BACKEND / "requirements.txt",
    "agent-runtime-requirements.txt": BACKEND / "agent-runtime-requirements.txt",
    "requirements-dev.txt": BACKEND / "requirements-dev.txt",
}
RUNTIME_MANIFESTS = ("requirements.txt", "agent-runtime-requirements.txt")
WEAK_COPYLEFT = ("pikepdf", "hypothesis")
NEW_RUNTIME = {"pdfminer.six": "20260107", "pikepdf": "10.17.0", "pypdfium2": "5.14.0"}

_REQ_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[(?P<extras>[^\]]*)\])?\s*"
    r"(?P<spec>[^;]*?)\s*(?:;(?P<marker>.*))?$"
)


def norm(name: str) -> str:
    """PEP 503 normalization."""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_manifest(path: Path):
    """Return (requirements, raw_lines, includes).

    requirements: list of dicts {name, spec, raw}. Handles comments, blank
    lines, '-r' includes, extras and environment markers.
    """
    reqs, raws, includes = [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = re.sub(r"(^|\s)#.*$", "", line).strip()
        if not s:
            continue
        raws.append(s)
        if s.startswith(("-r ", "--requirement")):
            includes.append(s.split(None, 1)[1].strip())
            continue
        if s.startswith("-"):
            continue  # other pip options; checked separately via raws
        m = _REQ_RE.match(s)
        assert m, f"unparseable requirement line in {path.name}: {s!r}"
        reqs.append({"name": m["name"], "spec": m["spec"].strip(), "raw": s})
    return reqs, raws, includes


@pytest.fixture(scope="module")
def manifests():
    return {k: parse_manifest(p) for k, p in MANIFESTS.items()}


@pytest.fixture(scope="module")
def pins(manifests):
    """{manifest: {normalized name: version}} for each manifest's own entries."""
    out = {}
    for mf, (reqs, _, _) in manifests.items():
        d = {}
        for r in reqs:
            m = re.fullmatch(r"==\s*([^\s=<>!~*,]+)", r["spec"])
            assert m, f"{mf}: {r['raw']!r} is not an exact '==' pin"
            d[norm(r["name"])] = m.group(1)
        out[mf] = d
    return out


@pytest.fixture(scope="module")
def all_pins(pins):
    """Union of direct dependencies across manifests -> version."""
    merged = {}
    for d in pins.values():
        for k, v in d.items():
            assert merged.setdefault(k, v) == v, f"{k} has differing versions"
    return merged


@pytest.fixture(scope="module")
def tpl():
    return (ROOT / "THIRD-PARTY-LICENSES").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def tpl_table(text: str):
    """Rows of the section-1 direct dependency table -> (name, version, license, manifests)."""
    sec = text.split("## 1. Direct dependencies", 1)[1].split("\n## ", 1)[0]
    rows = []
    for line in sec.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] in ("Name",) or set(cells[0]) <= {"-", " "}:
            continue
        assert len(cells) == 4, line
        rows.append(tuple(cells))
    return rows


def section(text: str, start_re: str) -> str:
    m = re.search(start_re, text, re.M)
    assert m, f"section {start_re!r} not found"
    rest = text[m.end():]
    nxt = re.search(r"^#{2,3} ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


# ---------------------------------------------------------------- parser

def test_parser_handles_comments_includes_extras_markers(tmp_path):
    p = tmp_path / "r.txt"
    p.write_text(
        "# comment\n\n-r other.txt\nfoo[bar,baz]==1.0 ; python_version >= '3.9'  # tail\n"
        "Some_Pkg==2.0\nbad>=1\n",
        encoding="utf-8",
    )
    reqs, _, inc = parse_manifest(p)
    assert inc == ["other.txt"]
    assert [(r["name"], r["spec"]) for r in reqs] == [
        ("foo", "==1.0"),
        ("Some_Pkg", "==2.0"),
        ("bad", ">=1"),
    ]


# ---------------------------------------------------------------- 1.1

def test_no_legacy_library_in_manifests(manifests):
    for mf, (reqs, raws, _) in manifests.items():
        for r in reqs:
            n = norm(r["name"])
            assert not n.startswith(norm(LEGACY_PREFIX)), (mf, r["raw"])
            assert n != LEGACY_TOP, (mf, r["raw"])
        for raw in raws:
            assert LEGACY_PREFIX not in raw.lower() and LEGACY_TOP not in raw.lower()


# ---------------------------------------------------------------- 2.2

def test_every_direct_dependency_is_exact_pinned(manifests, pins):
    for mf, (reqs, _, _) in manifests.items():
        for r in reqs:
            assert re.fullmatch(r"==\s*[A-Za-z0-9][A-Za-z0-9._+!-]*", r["spec"]), (mf, r["raw"])
            assert not re.search(r"[*<>~!]|===|,", r["spec"]), (mf, r["raw"])
        assert pins[mf]  # parsed something
    assert pins["requirements.txt"]["constructs"] == "10.8.1"


def test_new_dependencies_pinned_and_consistent(pins):
    for name, ver in NEW_RUNTIME.items():
        for mf in RUNTIME_MANIFESTS:
            assert pins[mf].get(norm(name)) == ver, (mf, name)
    assert pins["requirements-dev.txt"].get("pypdf") == "6.21.4"
    assert "pypdf" not in pins["requirements.txt"]
    assert "pypdf" not in pins["agent-runtime-requirements.txt"]


def test_same_dependency_same_version_across_manifests(pins):
    seen = {}
    for mf, d in pins.items():
        for k, v in d.items():
            assert seen.setdefault(k, v) == v, f"{k} differs across manifests"


def test_dev_manifest_includes_requirements(manifests):
    assert manifests["requirements-dev.txt"][2] == ["requirements.txt"]


# ---------------------------------------------------------------- 2.5 / 2.6

def test_license_table_matches_manifests(tpl, all_pins):
    rows = tpl_table(tpl)
    table = {}
    for name, ver, _lic, _mf in rows:
        k = norm(name)
        assert k not in table, f"duplicate row {name}"
        table[k] = ver
    assert set(table) == set(all_pins)
    assert table == all_pins


def test_license_table_manifest_column_matches(tpl, pins):
    for name, _ver, _lic, mf_col in tpl_table(tpl):
        listed = {m.strip() for m in mf_col.split(",")}
        actual = {mf for mf, d in pins.items() if norm(name) in d}
        # requirements-dev.txt's '-r requirements.txt' is listed once, under requirements.txt
        assert listed == actual, (name, listed, actual)


# ---------------------------------------------------------------- 2.8

GPL_RE = re.compile(r"\b(?:AGPL|LGPL|GPL)\b(?:-[0-9.]+)?(?:-only|-or-later|\+)?", re.I)


def _gpl_family(expr: str) -> bool:
    # normalise "GPL-3.0" style: word boundary does not split on '-', so also scan tokens
    return bool(re.search(r"(?<![A-Za-z])(?:A|L)?GPL(?![A-Za-z])", expr, re.I))


def test_gpl_detector_sanity():
    for bad in ("GPL-3.0-only", "LGPL-2.1-or-later", "AGPL-3.0", "GNU Lesser General Public License v3 (LGPLv3)".replace("LGPLv3", "LGPL v3")):
        assert _gpl_family(bad), bad
    for ok in ("MPL-2.0", "MIT", "BSD-3-Clause", "Apache-2.0", "MIT-CMU"):
        assert not _gpl_family(ok), ok


def test_license_table_has_no_gpl_family(tpl):
    for name, _v, lic, _m in tpl_table(tpl):
        assert not _gpl_family(lic), (name, lic)


@pytest.mark.parametrize("dist", ["pdfminer.six", "pikepdf", "pypdfium2", "pypdf", "pillow"])
def test_installed_metadata_has_no_gpl_family(dist):
    try:
        md = metadata.metadata(dist)
    except metadata.PackageNotFoundError:
        pytest.skip(f"{dist} not installed")
    fields = []
    for key in ("License-Expression", "License"):
        v = md.get(key)
        if v:
            # a long free-text License field may mention other licenses; only
            # inspect short SPDX-like values
            if len(v) <= 200:
                fields.append(v)
    fields += [c for c in (md.get_all("Classifier") or []) if c.startswith("License ::")]
    for f in fields:
        assert not _gpl_family(f), (dist, f)
        assert "General Public License" not in f or "Mozilla" in f, (dist, f)


# ---------------------------------------------------------------- 2.9

def test_weak_copyleft_installed_as_plain_pypi_requirements(manifests):
    found = set()
    for mf, (reqs, raws, _) in manifests.items():
        for r in reqs:
            if norm(r["name"]) in WEAK_COPYLEFT:
                found.add(norm(r["name"]))
                assert re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9._]+", r["raw"]), (mf, r["raw"])
        for raw in raws:
            if any(w in raw.lower() for w in WEAK_COPYLEFT):
                low = raw.lower()
                for bad in (" @ ", "@", "git+", "http", "file:", "-e", "/", "\\", "./"):
                    assert bad not in low, (mf, raw)
    assert found == set(WEAK_COPYLEFT)


def test_no_option_lines_pointing_to_urls_or_editable(manifests):
    for mf, (_, raws, _) in manifests.items():
        for raw in raws:
            if raw.startswith("-"):
                assert raw.startswith("-r "), (mf, raw)
            assert "git+" not in raw and "://" not in raw, (mf, raw)


# ---------------------------------------------------------------- 2.10

@pytest.mark.parametrize("name", WEAK_COPYLEFT)
def test_mpl_section_has_source_links_and_full_text(tpl, all_pins, name):
    ver = all_pins[name]
    sec = section(tpl, rf"^### 3\.\d+ {re.escape(name)} {re.escape(ver)}\s*$")
    assert ver in sec
    assert re.search(
        rf"https://files\.pythonhosted\.org/packages/\S+/{re.escape(name)}-{re.escape(ver)}\.tar\.gz", sec
    )
    tag = re.search(rf"https://github\.com/\S+/{re.escape(name)}\S*", sec) or re.search(
        r"https://github\.com/\S+", sec
    )
    assert tag and ver in tag.group(0), "github tag URL must contain the version"
    assert "Mozilla Public License Version 2.0" in sec
    assert "Exhibit A" in sec
    assert "Exhibit B" in sec
    assert re.search(r"copyright", sec, re.I)
    assert "MPL-2.0" in sec


def test_mpl_section_header_versions_match_manifest(tpl, all_pins):
    heads = dict(re.findall(r"^### 3\.\d+ (\S+) (\S+)\s*$", tpl, re.M))
    for name in WEAK_COPYLEFT:
        assert heads.get(name) == all_pins[name]


# ---------------------------------------------------------------- 2.11 / 3.8

def test_readme_mpl_statement(readme):
    for name in WEAK_COPYLEFT:
        assert f"`{name}`" in readme
    assert "MPL-2.0" in readme
    para = [l for l in readme.splitlines() if "关于 MPL-2.0" in l]
    assert para, "README lacks the MPL-2.0 explanation paragraph"
    p = para[0]
    assert "pikepdf" in p and "hypothesis" in p
    assert "未修改" in p
    assert "THIRD-PARTY-LICENSES" in readme


def test_readme_lists_versions_from_manifest(readme, all_pins):
    for name in ("pdfminer.six", "pikepdf", "pypdfium2", "pypdf", "hypothesis"):
        row = [l for l in readme.splitlines() if l.startswith("|") and f"`{name}`" in l]
        assert row, f"README table row for {name} missing"
        assert all_pins[norm(name)] in row[0], (name, row[0])


def test_readme_build_instructions(readme):
    assert "scripts/build_agent_bundle.py" in readme
    assert "Linux ARM64" in readme
    assert "CPython 3.12" in readme


# ---------------------------------------------------------------- 2.14

def test_native_libraries_listed(tpl):
    s21 = section(tpl, r"^### 2\.1 pikepdf \S+\s*$")
    assert "qpdf" in s21.lower()
    assert "libjpeg" in s21
    s22 = section(tpl, r"^### 2\.2 pypdfium2 \S+\s*$")
    for comp in ("PDFium", "FreeType", "libpng", "zlib"):
        assert comp in s22, comp
    s23 = section(tpl, r"^### 2\.3 Pillow \S+\s*$")
    for comp in ("FreeType", "HarfBuzz", "libjpeg", "libtiff", "libwebp", "OpenJPEG"):
        assert comp in s23, comp


def test_native_library_section_versions_match_manifest(tpl, all_pins):
    for name, label in (("pikepdf", "2.1"), ("pypdfium2", "2.2"), ("pillow", "2.3")):
        m = re.search(rf"^### {re.escape(label)} \S+ (\S+)\s*$", tpl, re.M)
        assert m and m.group(1) == all_pins[name], (name, m and m.group(1))
