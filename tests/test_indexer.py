# ==============================================================================
#  tests/test_indexer.py
#  Role     Indexation: symboles, macros CVI, references, arguments d'appel
#  Etat     stable
#  Version  0.2.0
# ==============================================================================

# ============================ Interne =========================================
from cvi_mcp.cparse import body_fingerprint, neutralize
from cvi_mcp.indexer import Indexer
from cvi_mcp.schema import SCHEMA_VERSION

from .conftest import FIXTURES


def _rows(db, sql, params=()):
    return db.execute(sql, params).fetchall()


def test_macro_neutralisation_preserves_offsets():
    source = "int CVICALLBACK Foo(int a)\n"
    cleaned = neutralize(source)
    assert len(cleaned) == len(source)
    assert "CVICALLBACK" not in cleaned
    assert cleaned.index("Foo") == source.index("Foo")


def test_declspec_is_neutralised():
    source = '__declspec(dllexport) int Bar(void);'
    cleaned = neutralize(source)
    assert len(cleaned) == len(source)
    assert "declspec" not in cleaned


def test_functions_and_callbacks(indexed):
    indexer = Indexer(indexed["db"])
    names = {r[0] for r in _rows(indexer.db, "SELECT name FROM symbols WHERE kind='function'")}
    assert {"main", "StartCallback", "StopCallback", "TimerCallback",
            "RecordMeasurement", "CommsInit", "CommsClose"} <= names

    callbacks = {r[0] for r in _rows(indexer.db, "SELECT name FROM symbols WHERE is_callback=1")}
    assert callbacks == {"StartCallback", "StopCallback", "TimerCallback"}

    statics = {r[0] for r in _rows(indexer.db, "SELECT name FROM symbols WHERE is_static=1 AND kind='function'")}
    assert "RecordMeasurement" in statics
    indexer.close()


def test_macros_keep_their_value(indexed):
    indexer = Indexer(indexed["db"])
    rows = dict(_rows(indexer.db, "SELECT name, value FROM symbols WHERE kind='macro'"))
    assert rows["PANEL_MAIN"] == "1"
    assert rows["PANEL_MAIN_START"] == "3"
    # Les macros sous include guard doivent etre vues malgre le #ifndef.
    assert "COMMS_H" in rows
    indexer.close()


def test_prototypes_from_header(indexed):
    indexer = Indexer(indexed["db"])
    protos = {r[0] for r in _rows(indexer.db, "SELECT name FROM symbols WHERE kind='prototype'")}
    assert {"CommsInit", "CommsSendCommand", "CommsClose"} <= protos
    indexer.close()


def test_globals_and_calls(indexed):
    indexer = Indexer(indexed["db"])
    globals_ = {r[0] for r in _rows(indexer.db, "SELECT name FROM symbols WHERE kind='global'")}
    assert {"panelHandle", "gMeasurements", "gMeasCount", "gPortHandle"} <= globals_

    calls = {(r[0], r[1]) for r in _rows(indexer.db, "SELECT name, enclosing FROM refs WHERE kind='call'")}
    assert ("CommsInit", "StartCallback") in calls
    assert ("CommsClose", "StopCallback") in calls
    assert ("RecordMeasurement", "TimerCallback") in calls
    assert ("LoadPanel", "main") in calls
    indexer.close()


def test_write_and_read_are_distinguished(indexed):
    indexer = Indexer(indexed["db"])
    kinds = {
        r[0]
        for r in _rows(
            indexer.db, "SELECT kind FROM refs WHERE name='gMeasCount'"
        )
    }
    assert "write" in kinds
    assert "read" in kinds
    indexer.close()


def test_locals_do_not_shadow_globals(indexed):
    """response est une locale de StartCallback: aucune reference globale."""
    indexer = Indexer(indexed["db"])
    rows = _rows(indexer.db, "SELECT COUNT(*) FROM refs WHERE name='response'")
    assert rows[0][0] == 0
    indexer.close()


def test_call_arguments_are_recorded(indexed):
    indexer = Indexer(indexed["db"])
    rows = _rows(
        indexer.db,
        "SELECT a.pos, a.text FROM refs r JOIN call_args a ON a.ref_id = r.id "
        "WHERE r.name='InstallCtrlCallback' ORDER BY r.id, a.pos LIMIT 4",
    )
    args = [text for _pos, text in rows]
    assert "PANEL_MAIN_START" in args
    assert "StartCallback" in args
    indexer.close()


def test_body_fingerprint_ignores_formatting_and_comments():
    first = body_fingerprint("{ int a = 1; /* note */ return a; }")
    second = body_fingerprint("{\n    int a = 1;\n    // autre note\n    return a;\n}")
    third = body_fingerprint("{ int a = 2; return a; }")
    assert first == second
    assert first != third


def test_reindex_is_incremental(indexed):
    indexer = Indexer(indexed["db"])
    assert not indexer.needs_reindex(FIXTURES / "main.c")
    report = indexer.index_files(indexed["sources"])
    assert report["indexed"] == 0
    assert report["unchanged"] == len(indexed["sources"])
    indexer.close()


def test_forced_reindex_rebuilds(indexed):
    indexer = Indexer(indexed["db"])
    report = indexer.index_files(indexed["sources"], force=True)
    assert report["indexed"] == len(indexed["sources"])
    indexer.close()


def test_modified_file_is_reindexed(indexed, tmp_path):
    copy = tmp_path / "unit.c"
    copy.write_text("int Alpha(void) { return 1; }\n", encoding="utf-8")
    indexer = Indexer(indexed["db"])
    assert indexer.index_file(copy) is True
    assert indexer.index_file(copy) is False
    copy.write_text("int Alpha(void) { return 2; }\nint Beta(void) { return 0; }\n", encoding="utf-8")
    assert indexer.index_file(copy) is True
    names = {r[0] for r in _rows(indexer.db, "SELECT name FROM symbols WHERE kind='function'")}
    assert "Beta" in names
    indexer.close()


def test_schema_version_is_stamped(indexed):
    indexer = Indexer(indexed["db"])
    row = indexer.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    assert int(row[0]) == SCHEMA_VERSION
    indexer.close()
