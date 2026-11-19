# ==============================================================================
#  tests/test_server.py
#  Role     Outils MCP de bout en bout: sorties, protection du code, audit
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json
import shutil
import sys

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp import server

from .conftest import FIXTURES, SDK_FIXTURES


def call(tool, **kwargs):
    """Appelle un outil MCP et renvoie sa charge utile deserialisee."""
    function = getattr(tool, "fn", tool)
    return json.loads(function(**kwargs))


@pytest.fixture
def variant(tmp_path):
    target = tmp_path / "v2"
    shutil.copytree(FIXTURES, target, ignore=shutil.ignore_patterns("sdk"))
    return target


# ---------------------------------------------------- environnement ------------

def test_cvi_environment_reports_version_and_settings(env):
    report = call(server.cvi_environment)
    assert report["cvi_mcp_version"] == server.__version__
    assert report["settings"]["project"].endswith("fixtures")
    assert "system" in report["platform"]
    assert isinstance(report["warnings"], list)


def test_cvi_environment_before_any_indexing(env):
    report = call(server.cvi_environment)
    assert "index" in report


def test_cvi_environment_after_indexing(env):
    call(server.project_overview)
    report = call(server.cvi_environment)
    assert report["index"]["files"] == 4
    assert report["index"]["by_origin"]["project"]["files"] >= 3
    assert report["index"]["snapshots"] == []


def test_cvi_environment_sees_snapshots(env):
    call(server.snapshot_save, label="recette")
    report = call(server.cvi_environment)
    assert report["index"]["snapshots"] == ["recette"]


# --------------------------------------------------------------- lecture -------

def test_overview_indexes_on_first_call(env):
    data = call(server.project_overview)
    assert data["project_file"] == "sample.prj"
    assert data["total_lines"] > 0
    assert "NiDAQmx.lib" in data["linked_libs"]


def test_sdk_folder_is_excluded_from_project(env):
    listing = call(server.project_overview, section="files", limit=100)
    files = {item["path"] for item in listing["items"]}
    assert not any(path.startswith("sdk/") for path in files)


def test_list_and_find(env):
    listed = call(server.list_symbols, kind="function", limit=50)
    assert listed["total"] >= 7

    found = call(server.find_symbol, name="CommsInit", exact=True)
    assert found["results"][0]["file"] == "comms.c"


def test_get_function_without_body_by_default(env):
    data = call(server.get_function, name="StartCallback", include_body=True)
    assert "body" not in data
    assert "ALLOW_BODY" in data["body_note"]


def test_get_function_with_body_when_allowed(env, monkeypatch):
    monkeypatch.setenv("CVI_MCP_ALLOW_BODY", "1")
    data = call(server.get_function, name="StartCallback", include_body=True)
    assert "CommsInit" in data["body"]
    assert "body_note" not in data


def test_body_is_capped(env, monkeypatch):
    monkeypatch.setenv("CVI_MCP_ALLOW_BODY", "1")
    monkeypatch.setenv("CVI_MCP_MAX_SNIPPET_LINES", "3")
    data = call(server.get_function, name="StartCallback", include_body=True)
    assert len(data["body"].splitlines()) == 3
    assert "tronque a 3" in data["body_note"]


def test_structure_is_served_without_body(env):
    data = call(server.get_function, name="StartCallback")
    assert data["signature"].startswith("int")
    assert data["params"]
    assert [c["function"] for c in data["callers"]] == ["main"]
    assert data["callers"][0]["via"] == "pointer"
    assert any(c["name"] == "CommsInit" for c in data["callees"])


def test_unknown_function_is_explicit(env):
    assert "introuvable" in call(server.get_function, name="Nope")["error"]


def test_usages_and_graph(env):
    usages = call(server.find_usages, name="gMeasCount", kind="write")
    assert usages["total"] >= 1 and usages["usages"]["items"]

    graph = call(server.call_graph, function="StopCallback")["graph"]
    assert {e["name"] for e in graph["StopCallback"]} == {"CommsSendCommand", "CommsClose"}


def test_file_outline(env):
    outline = call(server.file_outline, file="comms.c")
    assert outline["file"] == "comms.c"
    assert "error" in call(server.file_outline, file="absent.c")


def test_reindex_reports(env):
    first = call(server.reindex)
    assert first["sources_resolved"] == 4
    forced = call(server.reindex, force=True)
    assert forced["indexed"] == 4


# ------------------------------------------------------------- interface -------

def test_ui_map_tool(env):
    data = call(server.ui_map)
    panels = {p["constant"] for p in data["panels"]["items"]}
    assert panels == {"PANEL_MAIN", "PANEL_CONFIG"}
    assert data["totals"]["panels"] == 2
    assert [c["name"] for c in data["callbacks_unwired"]] == ["TimerCallback"]
    main = next(p for p in data["panels"]["items"] if p["constant"] == "PANEL_MAIN")
    assert "StartCallback" in main["callbacks"] and isinstance(main["controls"], int)
    assert main["callbacks_total"] >= len(main["callbacks"])

    detail = call(server.ui_map, panel="PANEL_MAIN")
    controls = detail["panels"][0]["controls_page"]["items"]
    assert controls[0]["constant"].startswith("PANEL_MAIN_")


def test_ui_map_filter_sort_and_page(env):
    one = call(server.ui_map, contains="config", limit=5)
    assert [p["constant"] for p in one["panels"]["items"]] == ["PANEL_CONFIG"]
    paged = call(server.ui_map, sort="controls", limit=1)
    assert paged["panels"]["total"] == 2 and paged["panels"]["next_offset"] == 1
    second = call(server.ui_map, sort="controls", limit=1, offset=1)
    assert second["panels"]["items"][0]["constant"] != paged["panels"]["items"][0]["constant"]


# ------------------------------------------------------------------- SDK -------

def test_sdk_index_and_lookup(env):
    report = call(server.sdk_index, paths=str(SDK_FIXTURES))
    assert report["prototypes"] == 3

    found = call(server.sdk_lookup, name="FakeDaqReadAnalog")
    assert found["results"][0]["header"] == "fakedaq.h"


def test_sdk_lookup_guides_when_empty(env):
    answer = call(server.sdk_lookup, name="FakeDaqReadAnalog")
    assert answer["count"] == 0
    assert "sdk_index" in answer["hint"]


def test_sdk_index_without_paths_explains(env, monkeypatch):
    monkeypatch.setenv("CVI_MCP_SDK_PATHS", "")
    answer = call(server.sdk_index)
    if "error" in answer:
        assert "CVI_MCP_SDK_PATHS" in answer["hint"]
    else:
        assert "roots" in answer


def test_external_callee_gets_sdk_signature(env, monkeypatch, tmp_path):
    """Un appel externe resolu par le SDK remonte avec sa signature."""
    unit = tmp_path / "acq"
    unit.mkdir()
    (unit / "acq.c").write_text(
        '#include "fakedaq.h"\n'
        "int Acquire(int task)\n"
        "{\n"
        "    double buffer[8];\n"
        "    FakeDaqStartTask(task);\n"
        "    return FakeDaqReadAnalog(task, 8, 1.0, buffer);\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CVI_MCP_PROJECT", str(unit))
    call(server.sdk_index, paths=str(SDK_FIXTURES))

    data = call(server.get_function, name="Acquire")
    signatures = {c["name"]: c.get("sdk_signature") for c in data["callees"]}
    assert "double *buffer" in signatures["FakeDaqReadAnalog"]


# ----------------------------------------------------------------- build -------

def test_build_check_dry_run(env):
    report = call(server.build_check, dry_run=True)
    assert report["mode"] == "dry_run"
    assert report["project_file"] == "sample.prj"
    assert report["args_template"] == "{project}"


def test_build_check_without_cvi_is_explicit(env):
    report = call(server.build_check)
    assert report["mode"] == "indisponible"
    assert report["command"]["available"] is False


def test_build_check_runs_configured_command(env, monkeypatch, tmp_path):
    script = tmp_path / "fakecc.py"
    script.write_text(
        "print('main.c(5,2) : error: undefined identifier Zig.')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CVI_MCP_BUILD_CMD", sys.executable)
    monkeypatch.setenv("CVI_MCP_BUILD_ARGS", f"{script} {{project}}")

    report = call(server.build_check)
    assert report["errors"] == 1
    assert report["diagnostics"]["items"][0]["line"] == 5


def test_build_check_needs_a_project_file(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CVI_MCP_PROJECT", str(tmp_path))
    assert "aucun .prj" in call(server.build_check)["error"]


# ------------------------------------------------------------------ diff -------

def test_snapshot_tools(env):
    assert call(server.snapshot_save, label="release-1")["symbols"] > 0
    assert [s["label"] for s in call(server.snapshot_list)["snapshots"]] == ["release-1"]
    assert call(server.snapshot_delete, label="release-1")["deleted"] == "release-1"


def test_diff_against_snapshot_is_stable(env):
    call(server.snapshot_save, label="base")
    report = call(server.diff_versions, before="base")
    assert report["identical"] is True
    assert report["to"] == "current"


def test_diff_unknown_snapshot_lists_available(env):
    call(server.snapshot_save, label="base")
    report = call(server.diff_versions, before="absent")
    assert "introuvable" in report["error"]
    assert report["available"] == ["base"]


def test_diff_against_path(env, variant):
    source = variant / "comms.c"
    source.write_text(
        source.read_text(encoding="utf-8").replace("return -1;", "return -9;"),
        encoding="utf-8",
    )
    report = call(server.diff_versions, before="current", path=str(variant))
    assert {i["name"] for i in report["body_changed"]} == {"CommsInit"}


def test_diff_against_missing_path(env):
    report = call(server.diff_versions, before="current", path="/absent")
    assert "introuvable" in report["error"]


# ----------------------------------------------------------------- audit -------

def test_every_call_is_logged(env):
    call(server.project_overview)
    call(server.find_symbol, name="CommsInit")

    lines = env["audit"].read_text(encoding="utf-8").strip().splitlines()
    tools = [json.loads(line)["tool"] for line in lines]
    assert tools == ["project_overview", "find_symbol"]
    assert all(json.loads(line)["result_bytes"] > 0 for line in lines)


def test_audit_counts_lines_of_code(env, monkeypatch):
    monkeypatch.setenv("CVI_MCP_ALLOW_BODY", "1")
    call(server.get_function, name="StartCallback", include_body=True)
    entry = json.loads(env["audit"].read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["code_lines"] > 0


def test_audit_records_zero_without_body(env):
    call(server.get_function, name="StartCallback", include_body=True)
    entry = json.loads(env["audit"].read_text(encoding="utf-8").strip().splitlines()[-1])
    assert entry["code_lines"] == 0


def test_cli_version(capsys):
    with pytest.raises(SystemExit) as exit_info:
        server.main(["--version"])
    assert exit_info.value.code == 0
    assert server.__version__ in capsys.readouterr().out


def test_cli_check_prints_diagnostic(env, capsys):
    code = server.main(["--check"])
    report = json.loads(capsys.readouterr().out)
    assert report["cvi_mcp_version"] == server.__version__
    # Code de sortie non nul des qu'un avertissement doit etre vu.
    assert code == (1 if report["warnings"] else 0)


def test_tools_are_registered():
    expected = {
        "cvi_environment",
        "project_overview",
        "list_symbols",
        "find_symbol",
        "get_function",
        "find_usages",
        "call_graph",
        "file_outline",
        "reindex",
        "ui_map",
        "sdk_index",
        "sdk_lookup",
        "build_check",
        "snapshot_save",
        "snapshot_list",
        "snapshot_delete",
        "diff_versions",
    }
    for name in expected:
        assert hasattr(server, name)
