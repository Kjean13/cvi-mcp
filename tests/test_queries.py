# ==============================================================================
#  tests/test_queries.py
#  Role     Lectures: vue d'ensemble, symboles, usages, graphe, plan de fichier
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp.queries import QueryEngine


@pytest.fixture
def engine(indexed):
    qe = QueryEngine(indexed["db"], indexed["root"])
    yield qe
    qe.close()


def test_overview(engine):
    data = engine.overview()
    assert data["total_lines"] > 0
    assert {"main.c", "comms.c", "comms.h", "ui_constants.h"} <= {
        f["path"] for f in engine.files()
    }
    assert [e["name"] for e in data["entry_points"]] == ["main"]
    assert {c["name"] for c in engine.callbacks()} == {
        "StartCallback",
        "StopCallback",
        "TimerCallback",
    }
    assert data["by_origin"]["project"]["symbols"]["function"] >= 7


def test_paths_are_relative(engine):
    for item in engine.files():
        assert not item["path"].startswith("/")


def test_externals_are_detected(engine):
    externals = {item["name"] for item in engine.external_calls()}
    assert "LoadPanel" in externals
    assert "SetCtrlVal" in externals
    assert "CommsInit" not in externals


def test_list_symbols_filters(engine):
    macros = engine.list_symbols(kind="macro", limit=50)
    assert all(m["kind"] == "macro" for m in macros)

    in_comms = engine.list_symbols(file="comms.c")
    assert all(s["file"] == "comms.c" for s in in_comms)

    partial = engine.list_symbols(name_contains="Comms")
    assert {s["name"] for s in partial} >= {"CommsInit", "CommsClose"}

    assert len(engine.list_symbols(limit=3)) == 3


def test_find_symbol_exact_and_partial(engine):
    exact = engine.find_symbol("CommsInit", exact=True)
    assert exact and all(s["name"] == "CommsInit" for s in exact)
    assert len(engine.find_symbol("Comms")) >= 3
    assert engine.find_symbol("NExistePas", exact=True) == []


def test_get_function_relations(engine):
    data = engine.get_function("StartCallback")
    assert data["is_callback"] is True
    assert data["doc"] and "Demarrer" in data["doc"]
    assert len(data["params"]) == 6

    callees = {c["name"]: c for c in data["callees"]}
    assert callees["CommsInit"]["external"] is False
    assert callees["SetCtrlVal"]["external"] is True

    callers = {c["function"] for c in engine.get_function("CommsInit")["callers"]}
    assert callers == {"StartCallback"}


def test_get_function_prefers_definition_over_prototype(engine):
    assert engine.get_function("CommsInit")["kind"] == "function"


def test_get_function_unknown(engine):
    assert engine.get_function("Inconnue") is None


def test_find_usages(engine):
    calls = engine.find_usages("CommsSendCommand", kind="call")["items"]
    assert {u["in_function"] for u in calls} == {"StartCallback", "StopCallback"}

    writes = engine.find_usages("gMeasCount", kind="write")["items"]
    assert writes and all(u["kind"] == "write" for u in writes)
    assert all("line" in u and "file" in u for u in writes)


def test_call_graph_callees(engine):
    graph = engine.call_graph("StartCallback", depth=2)
    assert {e["name"] for e in graph["StartCallback"]} == {"CommsInit", "CommsSendCommand"}


def test_call_graph_callers(engine):
    graph = engine.call_graph("CommsClose", direction="callers", depth=2)
    assert {e["name"] for e in graph["CommsClose"]} == {"StopCallback"}


def test_call_graph_external_opt_in(engine):
    without = engine.call_graph("main", depth=1)
    with_ext = engine.call_graph("main", depth=1, include_external=True)
    assert len(with_ext["main"]) > len(without["main"])
    assert any(e["external"] for e in with_ext["main"])


def test_call_graph_handles_recursion(indexed, tmp_path):
    from cvi_mcp.indexer import Indexer

    unit = tmp_path / "rec.c"
    unit.write_text(
        "int Ping(int n) { return n > 0 ? Pong(n - 1) : 0; }\n"
        "int Pong(int n) { return Ping(n); }\n",
        encoding="utf-8",
    )
    indexer = Indexer(indexed["db"])
    indexer.index_file(unit)
    indexer.close()

    qe = QueryEngine(indexed["db"], indexed["root"])
    graph = qe.call_graph("Ping", depth=5)
    qe.close()
    assert "Ping" in graph and "Pong" in graph


def test_file_outline(engine):
    outline = engine.file_outline("comms.h")
    assert outline["file"] == "comms.h"
    assert next(s["name"] for s in outline["symbols"]) == "COMMS_H"
    assert engine.file_outline("absent.c") is None


def test_file_outline_lists_includes(engine):
    outline = engine.file_outline("main.c")
    included = {i["name"]: i["system"] for i in outline["includes"]}
    assert included["ansi_c.h"] is True
    assert included["comms.h"] is False


def test_call_graph_includes_callbacks_installed_by_pointer(indexed):
    """Un callback installe depuis main apparait dans son graphe, marque via pointer."""
    from cvi_mcp.queries import QueryEngine

    engine = QueryEngine(indexed["db"], indexed["root"])
    try:
        edges = engine.call_graph("main", "callees", 1)["main"]
    finally:
        engine.close()
    pointer = [e["name"] for e in edges if e.get("via") == "pointer"]
    assert "StartCallback" in pointer
