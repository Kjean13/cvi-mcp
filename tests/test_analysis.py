# ==============================================================================
#  tests/test_analysis.py
#  Role     Code mort, verrous, graphe de modules, complexite, origine, pagination
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp import analysis, server
from cvi_mcp.indexer import Indexer
from cvi_mcp.prj import display_root
from cvi_mcp.queries import QueryEngine, page

LOCKS = """
#include "lib.h"
int gLock;

void Clean(void)
{
    CmtGetLock(gLock);
    Work();
    CmtReleaseLock(gLock);
}

int EarlyExit(int err)
{
    CmtGetLock(gLock);
    if (err)
        return -1;
    CmtReleaseLock(gLock);
    return 0;
}

int ReleasedBeforeExit(int err)
{
    CmtGetLock(gLock);
    if (err) {
        CmtReleaseLock(gLock);
        return -1;
    }
    CmtReleaseLock(gLock);
    return 0;
}

void LoopBreak(int n)
{
    int i;
    for (i = 0; i < n; i++) {
        CmtGetLock(gLock);
        if (i == 3)
            break;
        CmtReleaseLock(gLock);
    }
}

void Switched(int mode)
{
    CmtGetLock(gLock);
    switch (mode) {
    case 1:
        CmtReleaseLock(gLock);
        break;
    default:
        CmtReleaseLock(gLock);
        break;
    }
}

void TakesOnly(void)
{
    CmtGetLock(gLock);
}

void GivesOnly(void)
{
    CmtReleaseLock(gLock);
}

void UnderIfdef(int n)
{
#ifdef OPTION
    CmtGetLock(gLock);
    if (n == 0) {
        CmtReleaseLock(gLock);
    } else {
        CmtReleaseLock(gLock);
    }
#endif
}

void WithGoto(int err)
{
    CmtGetLock(gLock);
    if (err)
        goto Done;
    Work();
Done:
    CmtReleaseLock(gLock);
}
"""


@pytest.fixture()
def project(tmp_path):
    root = tmp_path / "Dev"
    (root / "Projet").mkdir(parents=True)
    (root / "Commun").mkdir()
    (root / "Commun" / "lib.h").write_text(
        "int CmtGetLock(int);\nint CmtReleaseLock(int);\nint ImaqThing(int a);\n"
        "#define LIB_A 1\n#define LIB_B 2\n"
    )
    (root / "Commun" / "own.h").write_text("int Work(void);\n#define OWN_USED 3\n#define OWN_DEAD 4\n#define OWN_GATE 1\n#define OWN_ALIAS OWN_USED\n")
    (root / "Commun" / "locks.c").write_text(LOCKS)
    (root / "Commun" / "work.c").write_text(
        '#include "own.h"\n'
        "static int Unused(void) { return 0; }\n"
        "#if OWN_GATE\nstatic int gate = 1;\n#endif\n"
        "int Exported(void) { return OWN_USED; }\n"
        "int Work(void) {\n  int a = 0;\n  if (a && 1) { while (a) { a--; } }\n"
        "  return ImaqThing(a);\n}\n"
    )
    (root / "Commun" / "ui.h").write_text(
        "/* LabWindows/CVI User Interface Resource (UIR) Include File */\n"
        "     /* Panels and Controls: */\n"
        "#define  MAINP  1\n#define  MAINP_GO  2  /* control type: command, callback function: OnGo */\n"
        "#define  SPARE  2\n"
    )
    (root / "Commun" / "ui.c").write_text(
        '#include "ui.h"\n'
        "int CVICALLBACK OnGo(int panel, int control, int event, void *d, int a, int b)\n"
        "{ Clean(); return 0; }\n"
        "int CVICALLBACK Orphan(int panel, int control, int event, void *d, int a, int b)\n"
        "{ return 0; }\n"
        'int main(void) { LoadPanel(0, "ui.uir", MAINP); EarlyExit(0); ReleasedBeforeExit(0);'
        " LoopBreak(1); Switched(1); UnderIfdef(0); TakesOnly(); GivesOnly(); WithGoto(0); return 0; }\n"
    )
    files = [str(p) for p in sorted((root / "Commun").iterdir())]
    db = tmp_path / "i.db"
    indexer = Indexer(db)
    indexer.index_files(files)
    indexer.close()
    engine = QueryEngine(db, display_root(root / "Projet", files))
    yield engine
    engine.close()


# ---------------------------------------------------------------- origine -------

def test_origins_separate_team_code_from_generated_and_library(project):
    origins = {f["path"]: f["origin"] for f in project.files()}
    assert origins["Commun/ui.h"] == "generated"
    assert origins["Commun/lib.h"] == "library"
    assert origins["Commun/own.h"] == "project"
    assert origins["Commun/work.c"] == "project"


def test_paths_are_relative_to_common_root(project):
    assert all(f["path"].startswith("Commun/") for f in project.files())


def test_library_prototypes_count_as_external(project):
    externals = {e["name"] for e in project.external_calls()}
    assert "ImaqThing" in externals and "CmtGetLock" in externals
    assert "Work" not in externals


def test_project_only_totals(project):
    by_origin = project.overview()["by_origin"]
    assert set(by_origin) == {"project", "generated", "library"}
    assert "macro" not in by_origin["project"]["symbols"] or (
        by_origin["project"]["symbols"]["macro"] == 4
    )


# ---------------------------------------------------------------- verrous -------

def _findings(project, function):
    report = analysis.lock_check(project)
    return [(f["kind"], f["line"]) for f in report["findings"] if f["function"] == function], report


def test_clean_lock_has_no_finding(project):
    assert _findings(project, "Clean")[0] == []


def test_return_while_held_is_found(project):
    found, _ = _findings(project, "EarlyExit")
    assert found and found[0][0] == "return_while_held"


def test_release_before_return_is_not_a_finding(project):
    assert _findings(project, "ReleasedBeforeExit")[0] == []


def test_break_carries_the_held_lock_out_of_the_loop(project):
    kinds = {k for k, _ in _findings(project, "LoopBreak")[0]}
    assert "held_at_end" in kinds


def test_every_switch_branch_releases(project):
    assert _findings(project, "Switched")[0] == []


def test_split_acquire_and_release(project):
    assert {k for k, _ in _findings(project, "TakesOnly")[0]} == {"held_at_end"}
    assert {k for k, _ in _findings(project, "GivesOnly")[0]} == {"release_without_acquire"}


def test_preprocessor_block_keeps_branches(project):
    assert _findings(project, "UnderIfdef")[0] == []


def test_goto_is_flagged_as_partial(project):
    _found, report = _findings(project, "WithGoto")
    assert "WithGoto" in [p["function"] for p in report["partially_checked"]]


def test_split_if_header_lowers_confidence(tmp_path):
    src = tmp_path / "split.c"
    src.write_text(
        "int g;\nvoid Split(int a)\n{\n    CmtGetLock(g);\n#ifndef X\n    if (a)\n#else\n"
        "    if (1)\n#endif\n    {\n        a++;\n    }\n    CmtReleaseLock(g);\n    return;\n}\n"
    )
    db = tmp_path / "s.db"
    indexer = Indexer(db)
    indexer.index_files([str(src)])
    indexer.close()
    engine = QueryEngine(db, tmp_path)
    try:
        report = analysis.lock_check(engine)
    finally:
        engine.close()
    assert [p["function"] for p in report["partially_checked"]] == ["Split"]
    assert all(f["confidence"] == "faible" for f in report["findings"])


def test_lock_balance_counts_calls(project):
    balance = analysis.lock_check(project)["balance"][0]
    # UnderIfdef est exclue (OPTION non defini): sa prise ne compte pas.
    assert balance["acquire"] == "CmtGetLock" and balance["calls"] == 7
    assert balance["release_calls"] == 9


# -------------------------------------------------------------- code mort -------

def test_dead_code_categories(project):
    from cvi_mcp.uimap import UiMap

    mapper = UiMap(project)
    found = analysis.dead_code(project, mapper.uir, sorted(mapper.uir["panels"]))
    functions = {f["name"]: f["confidence"] for f in found["functions"]}
    assert functions == {"Unused": "haute", "Exported": "moyenne"}
    assert all(f["status"] == "never_called" for f in found["functions"])
    assert [c["name"] for c in found["callbacks"]] == ["Orphan"]
    assert "OnGo" not in functions and "main" not in functions
    assert {m["name"] for m in found["macros"]} == {"OWN_DEAD", "OWN_ALIAS"}
    assert [(p["panel"], p["status"]) for p in found["panels"]] == [("SPARE", "never_loaded")]


# ------------------------------------------------- modules et complexite -------

def test_module_graph_edges(project):
    graph = analysis.module_graph(project)
    edges = {(e["from"], e["to"]) for e in graph["edges"]}
    assert ("Commun/ui.c", "Commun/locks.c") in edges
    assert ("Commun/locks.c", "Commun/work.c") in edges


def test_complexity_metrics(project):
    rows = {r["name"]: r for r in analysis.complexity(project)}
    assert rows["Work"]["complexity"] == 4  # if, &&, while
    assert rows["Work"]["depth"] == 2
    assert rows["Work"]["risk"] == "faible"


# -------------------------------------------------------------- pagination -----

def test_page_reports_next_offset():
    chunk = page(list(range(5)), 0, 2)
    assert chunk["items"] == [0, 1] and chunk["next_offset"] == 2 and chunk["total"] == 5
    assert page(list(range(5)), 4, 2)["next_offset"] is None


# -------------------------------------------------------------- serveur --------

def _call(tool, **kwargs) -> dict:
    fn = getattr(tool, "fn", tool)
    return json.loads(fn(**kwargs))


def test_overview_summary_and_sections(env):
    summary = _call(server.project_overview, limit=2)
    assert "by_origin" in summary and summary["externals"]["total"] >= len(summary["externals"]["top"])
    assert len(summary["externals"]["top"]) <= 2
    section = _call(server.project_overview, section="externals", limit=1, offset=1)
    assert section["offset"] == 1 and len(section["items"]) == 1
    bad = _call(server.project_overview, section="nope")
    assert "sections" in bad


def test_every_answer_carries_index_info(env):
    info = _call(server.project_overview)["index_info"]
    assert info["server_version"] and info["indexed_at"] and info["files"] == 4


def test_get_function_head_lines(env, monkeypatch):
    monkeypatch.setenv("CVI_MCP_ALLOW_BODY", "1")
    monkeypatch.setenv("CVI_MCP_MAX_SNIPPET_LINES", "10")
    data = _call(server.get_function, name="StartCallback", include_body=True, lines=2)
    assert len(data["body"].splitlines()) == 2
    assert data["complexity"] >= 1 and data["lines"] > 2
    capped = _call(server.get_function, name="StartCallback", include_body=True, lines=99)
    assert len(capped["body"].splitlines()) <= 10


def test_new_tools_answer(env):
    assert "functions" in _call(server.dead_code)
    assert "balance" in _call(server.lock_check)
    assert "modules" in _call(server.module_graph)
    assert "risk_distribution" in _call(server.complexity)


def test_macro_used_only_in_preprocessor_or_macro_is_not_dead(project, tmp_path):
    names = {m["name"] for m in analysis.dead_code(project, None, [])["macros"]}
    assert "OWN_DEAD" in names
    assert "OWN_GATE" not in names and "OWN_ALIAS" in names


def test_externals_are_categorized(env):
    items = _call(server.project_overview, section="externals", limit=50)["items"]
    assert items and all(i["category"] in {"libc", "sdk", "unknown"} for i in items)
    summary = _call(server.project_overview)["externals"]
    assert sum(summary["by_category"].values()) == summary["total"]


def test_modules_group_files_by_shared_word(project):
    found = analysis.modules(project)
    assert found and all({"module", "files", "lines", "functions", "stems"} <= m.keys() for m in found)
    assert sum(m["files"] for m in found) >= 1


def test_ui_map_reconciliation_and_full_mode(env):
    summary = _call(server.ui_map)
    rec = summary["callbacks_reconciliation"]
    assert rec["unwired"] == len(summary["callbacks_unwired"])
    full = _call(server.ui_map, limit=0)
    assert full["panels"]["next_offset"] is None
    assert all("_all_callbacks" not in p for p in full["panels"]["items"])
