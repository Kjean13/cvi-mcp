# ==============================================================================
#  tests/test_diffs.py
#  Role     Instantanes et comparaison entre deux versions d'un projet
#  Etat     stable
#  Version  0.2.0
# ==============================================================================

# ============================ Standard ========================================
import shutil

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp import diffs
from cvi_mcp.queries import QueryEngine

from .conftest import FIXTURES


@pytest.fixture
def db(indexed):
    engine = QueryEngine(indexed["db"], indexed["root"])
    yield engine
    engine.close()


@pytest.fixture
def variant(tmp_path):
    """Copie des fixtures ou l'on fait evoluer le code."""
    target = tmp_path / "v2"
    shutil.copytree(FIXTURES, target, ignore=shutil.ignore_patterns("sdk"))
    return target


def _only(path):
    return lambda p: "/sdk/" in str(p).replace("\\", "/")


def declare(root, filename):
    """Ajoute un fichier au .prj: c'est lui qui pilote la liste des sources."""
    project = root / "sample.prj"
    text = project.read_text(encoding="utf-8")
    project.write_text(
        text + f'\n[File 0099]\nPath = "{filename}"\n', encoding="utf-8"
    )


def test_snapshot_round_trip(db, indexed):
    saved = diffs.save_snapshot(db.db, "v1", indexed["root"])
    assert saved["symbols"] > 0
    assert saved["call_edges"] > 0

    listed = diffs.list_snapshots(db.db)
    assert [s["label"] for s in listed] == ["v1"]
    assert listed[0]["files"] == len(indexed["sources"])


def test_snapshot_label_current_is_reserved(db, indexed):
    assert "error" in diffs.save_snapshot(db.db, diffs.CURRENT, indexed["root"])


def test_snapshot_refuses_silent_overwrite(db, indexed):
    diffs.save_snapshot(db.db, "v1", indexed["root"])
    assert "error" in diffs.save_snapshot(db.db, "v1", indexed["root"])
    assert "label" in diffs.save_snapshot(db.db, "v1", indexed["root"], overwrite=True)


def test_snapshot_delete(db, indexed):
    diffs.save_snapshot(db.db, "v1", indexed["root"])
    assert diffs.delete_snapshot(db.db, "v1") == {"deleted": "v1"}
    assert diffs.list_snapshots(db.db) == []
    assert "error" in diffs.delete_snapshot(db.db, "v1")


def test_identical_states_report_no_change(db, indexed):
    diffs.save_snapshot(db.db, "v1", indexed["root"])
    before = diffs.load_snapshot(db.db, "v1")
    after = diffs.capture_state(db.db, indexed["root"])
    report = diffs.compare(before, after, "v1", "current")
    assert report["identical"] is True
    assert report["summary"]["body_changed"] == 0


def test_detects_added_and_removed_function(db, indexed, variant):
    before = diffs.capture_state(db.db, indexed["root"])

    source = variant / "comms.c"
    text = source.read_text(encoding="utf-8")
    text = text.replace(
        "void CVIFUNC CommsClose(void)",
        "int CVIFUNC CommsFlush(void) { return 0; }\n\nvoid CVIFUNC CommsClose(void)",
    )
    source.write_text(text, encoding="utf-8")

    after = diffs.state_of_path(variant, _only(variant))
    report = diffs.compare(before, after, "v1", "v2")

    assert {item["name"] for item in report["added"]} == {"CommsFlush"}
    assert report["removed"] == []
    assert report["summary"]["added"] == 1


def test_detects_body_change_without_storing_code(db, indexed, variant):
    before = diffs.capture_state(db.db, indexed["root"])

    source = variant / "comms.c"
    text = source.read_text(encoding="utf-8").replace("9600", "19200")
    text = text.replace("return -1;", "return -2;")
    source.write_text(text, encoding="utf-8")

    after = diffs.state_of_path(variant, _only(variant))
    report = diffs.compare(before, after, "v1", "v2")

    changed = {item["name"] for item in report["body_changed"]}
    assert "CommsInit" in changed
    assert report["summary"]["signature_changed"] == 0
    # Les empreintes ne doivent jamais exposer le code lui meme.
    assert all("body" not in item for item in report["body_changed"])


def test_comment_only_change_is_not_a_change(db, indexed, variant):
    before = diffs.capture_state(db.db, indexed["root"])

    source = variant / "comms.c"
    text = source.read_text(encoding="utf-8").replace(
        "/* Initialize serial port */", "/* Ouvre le port serie, revu le 19/11 */"
    )
    source.write_text(text, encoding="utf-8")

    after = diffs.state_of_path(variant, _only(variant))
    report = diffs.compare(before, after, "v1", "v2")
    assert report["summary"]["body_changed"] == 0


def test_detects_signature_change(db, indexed, variant):
    before = diffs.capture_state(db.db, indexed["root"])

    source = variant / "comms.c"
    text = source.read_text(encoding="utf-8").replace(
        "int CVIFUNC CommsInit(int port, int baudRate)",
        "int CVIFUNC CommsInit(int port, int baudRate, int parity)",
    )
    source.write_text(text, encoding="utf-8")

    after = diffs.state_of_path(variant, _only(variant))
    report = diffs.compare(before, after, "v1", "v2")

    names = {item["name"] for item in report["signature_changed"]}
    assert "CommsInit" in names
    entry = next(i for i in report["signature_changed"] if i["name"] == "CommsInit")
    assert "parity" in entry["after"]
    assert "parity" not in entry["before"]


def test_detects_moved_function(db, indexed, variant):
    before = diffs.capture_state(db.db, indexed["root"])

    source = variant / "comms.c"
    text = source.read_text(encoding="utf-8")
    start = text.index("void CVIFUNC CommsClose(void)")
    moved, kept = text[start:], text[:start]
    source.write_text(kept, encoding="utf-8")
    (variant / "extra.c").write_text(
        '#include "comms.h"\n#include <ansi_c.h>\n\nstatic int gPortHandle = -1;\n\n' + moved,
        encoding="utf-8",
    )
    declare(variant, "extra.c")

    after = diffs.state_of_path(variant, _only(variant))
    report = diffs.compare(before, after, "v1", "v2")

    moved_names = {item["name"]: item for item in report["moved"]}
    assert "CommsClose" in moved_names
    assert moved_names["CommsClose"]["from"] == "comms.c"
    assert moved_names["CommsClose"]["to"] == "extra.c"


def test_detects_call_changes(db, indexed, variant):
    before = diffs.capture_state(db.db, indexed["root"])

    source = variant / "main.c"
    text = source.read_text(encoding="utf-8").replace(
        "    CommsInit(1, 9600);", "    CommsClose();"
    )
    source.write_text(text, encoding="utf-8")

    after = diffs.state_of_path(variant, _only(variant))
    report = diffs.compare(before, after, "v1", "v2")

    assert {"caller": "StartCallback", "callee": "CommsInit"} in report["calls_removed"]
    assert {"caller": "StartCallback", "callee": "CommsClose"} in report["calls_added"]


def test_detects_file_addition(db, indexed, variant):
    before = diffs.capture_state(db.db, indexed["root"])
    (variant / "logger.c").write_text(
        "int LogWrite(const char *line) { return 0; }\n", encoding="utf-8"
    )
    declare(variant, "logger.c")
    after = diffs.state_of_path(variant, _only(variant))
    report = diffs.compare(before, after, "v1", "v2")
    assert "logger.c" in report["files_added"]
    assert report["files_removed"] == []


def test_state_of_path_leaves_main_index_alone(db, indexed, variant):
    before = db.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    diffs.state_of_path(variant, _only(variant))
    after = db.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    assert before == after
