# ==============================================================================
#  tests/test_sdk.py
#  Role     Index SDK: en-tetes fiables, panneaux .fp heuristiques
#  Etat     stable
#  Version  0.2.0
# ==============================================================================

# ============================ Standard ========================================
from pathlib import Path

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp.sdk import SdkIndexer, SdkLookup, resolve_roots

from .conftest import SDK_FIXTURES


@pytest.fixture
def sdk(env):
    indexer = SdkIndexer(env["db"])
    report = indexer.index([SDK_FIXTURES])
    indexer.close()
    return {**env, "report": report}


def test_headers_are_indexed(sdk):
    assert sdk["report"]["headers_indexed"] == 1
    assert sdk["report"]["prototypes"] == 3


def test_prototype_lookup(sdk):
    lookup = SdkLookup(sdk["db"])
    results = lookup.lookup("FakeDaqReadAnalog")
    lookup.close()

    entry = next(r for r in results if r["kind"] == "prototype")
    assert entry["header"] == "fakedaq.h"
    assert "double *buffer" in entry["signature"]
    assert len(entry["params"]) == 4
    assert "analogiques" in entry["doc"]


def test_macro_lookup(sdk):
    lookup = SdkLookup(sdk["db"])
    results = lookup.lookup("FAKEDAQ_VAL_VOLTS")
    lookup.close()
    assert results[0]["kind"] == "macro"


def test_partial_lookup(sdk):
    lookup = SdkLookup(sdk["db"])
    results = lookup.lookup("FakeDaq", exact=False)
    lookup.close()
    assert {r["name"] for r in results} >= {"FakeDaqStartTask", "FakeDaqStopTask"}


def test_fp_entries_require_header_confirmation(sdk):
    report = sdk["report"]
    assert report["panel_files"] == 1
    assert report["panel_entries_confirmed"] == 2
    assert report["panel_candidates_discarded"] >= 1
    assert "pas documente" in report["panel_note"]


def test_fp_marks_public_api(sdk):
    lookup = SdkLookup(sdk["db"])
    kinds = {r["kind"] for r in lookup.lookup("FakeDaqStartTask")}
    missing = {r["kind"] for r in lookup.lookup("FakeDaqStopTask")}
    lookup.close()
    assert "panel_entry" in kinds
    assert "panel_entry" not in missing


def test_unconfirmed_name_never_surfaces(sdk):
    lookup = SdkLookup(sdk["db"])
    assert lookup.lookup("ZzNotInHeader") == []
    lookup.close()


def test_signatures_for_externals(sdk):
    lookup = SdkLookup(sdk["db"])
    found = lookup.signatures_for(["FakeDaqStartTask", "SetCtrlVal"])
    lookup.close()
    assert "FakeDaqStartTask" in found
    assert "SetCtrlVal" not in found


def test_sdk_index_is_incremental(sdk):
    indexer = SdkIndexer(sdk["db"])
    again = indexer.index([SDK_FIXTURES])
    indexer.close()
    assert again["headers_indexed"] == 0
    assert again["prototypes"] == 3


def test_missing_path_is_reported(env):
    indexer = SdkIndexer(env["db"])
    report = indexer.index([Path("/chemin/absent")])
    indexer.close()
    assert report["missing_paths"] == ["/chemin/absent"]
    assert report["prototypes"] == 0


def test_sdk_index_does_not_touch_project_tables(indexed):
    before = indexed["report"]["indexed"]
    indexer = SdkIndexer(indexed["db"])
    indexer.index([SDK_FIXTURES])
    count = indexer.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    indexer.close()
    assert count == before


def test_resolve_roots_prefers_configuration(tmp_path):
    explicit = tmp_path / "inc"
    explicit.mkdir()
    assert resolve_roots([explicit], [tmp_path]) == [explicit]
    assert resolve_roots([], [tmp_path, Path("/absent")]) == [tmp_path]
