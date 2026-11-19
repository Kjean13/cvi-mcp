# ==============================================================================
#  tests/test_prj.py
#  Role     Lecture des fichiers projet et resolution des sources
#  Etat     stable
#  Version  0.2.0
# ==============================================================================

# ============================ Standard ========================================
from pathlib import Path

# ============================ Interne =========================================
from cvi_mcp.prj import collect_sources, discover_sources, parse_cws, parse_prj

from .conftest import FIXTURES


def _names(paths):
    return {Path(p).name for p in paths}


def test_prj_sources_and_libs():
    info = parse_prj(FIXTURES / "sample.prj")
    assert {"main.c", "comms.c", "comms.h", "ui_constants.h"} <= _names(info["sources"])
    assert any("NiDAQmx" in lib for lib in info["libs"])
    assert any("visa32" in lib for lib in info["libs"])
    assert info["sections"], "les sections doivent remonter pour le diagnostic"


def test_cws_lists_projects():
    projects = parse_cws(FIXTURES / "sample.cws")
    assert _names(projects) == {"sample.prj"}


def test_prj_tolerates_garbage(tmp_path):
    broken = tmp_path / "broken.prj"
    broken.write_text("pas du tout un ini\n[[[\n", encoding="utf-8")
    info = parse_prj(broken)
    assert info["sources"] == []


def test_collect_sources_prefers_project_file():
    sources = collect_sources(FIXTURES)
    assert "main.c" in _names(sources)
    # Le .prj ne liste pas le SDK, il ne doit donc pas remonter.
    assert "fakedaq.h" not in _names(sources)


def test_collect_sources_applies_exclusion():
    sources = collect_sources(FIXTURES, lambda p: Path(p).name == "comms.h")
    assert "comms.h" not in _names(sources)
    assert "main.c" in _names(sources)


def test_discover_sources_is_the_fallback(tmp_path):
    (tmp_path / "a.c").write_text("int a(void) { return 0; }\n", encoding="utf-8")
    (tmp_path / "b.h").write_text("int a(void);\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignore\n", encoding="utf-8")
    assert _names(discover_sources(tmp_path)) == {"a.c", "b.h"}
    assert _names(collect_sources(tmp_path)) == {"a.c", "b.h"}
