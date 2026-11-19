# ==============================================================================
#  tests/test_features.py
#  Role     Outils de la 0.3.2 sur un projet factice complet: configuration,
#           recherche, impact, risques, threads, coherence, persistance,
#           diff, budget de reponse, multi-projets, diagnostic
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp import server
from tests.sample_project import write_sample


def call(tool, **kwargs):
    fn = getattr(tool, "fn", tool)
    return json.loads(fn(**kwargs))


@pytest.fixture
def sample(tmp_path, monkeypatch):
    for key in ("CVI_MCP_EXCLUDE", "CVI_MCP_SDK_PATHS", "CVI_MCP_BUILD_CMD"):
        monkeypatch.delenv(key, raising=False)
    prj = write_sample(tmp_path / "Dev")
    monkeypatch.setenv("CVI_MCP_PROJECT", str(prj))
    monkeypatch.setenv("CVI_MCP_DB", str(tmp_path / "index.db"))
    monkeypatch.setenv("CVI_MCP_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    return {"root": tmp_path / "Dev", "prj": prj, "tmp": tmp_path}


def _items(payload, key):
    value = payload[key]
    return value["items"] if isinstance(value, dict) and "items" in value else value


# ------------------------------------------------------------ preprocesseur ----

def test_reached_header_is_indexed_as_library(sample):
    files = {f["path"]: f for f in _items(call(server.project_overview, section="files", limit=50), "items")}
    assert files["Lib/extlib.h"]["origin"] == "library"
    outline = call(server.file_outline, file="main.c")
    assert outline["in_build"] and outline["preprocessor_branches"]["inactive"] >= 2
    resolved = {i["name"]: i["how"] for i in outline["includes"]}
    assert resolved["config.h"] == "dossier_du_projet" and resolved["extlib.h"] == "include_path"


def test_inactive_code_is_hidden_and_explained(sample):
    assert call(server.find_symbol, name="OnlyB", exact=True)["count"] == 0
    why = call(server.find_symbol, name="OnlyB", exact=True)
    assert "exclu" in why["reason"]
    assert call(server.find_symbol, name="OnlyB", exact=True, include_inactive=True)["count"] == 1
    switch = call(server.find_symbol, name="OPTION_B", exact=True)
    assert "#if" in switch["reason"]
    usages = call(server.find_usages, name="Ninth")
    assert usages["total"] == 0 and usages["inactive_excluded"] == 1


def test_macro_usages_and_config(sample):
    usage = call(server.macro_usages, name="COUNT")
    assert usage["value"] == "4"
    assert usage["summary"]["conditions_by_state"] == {"active": 1, "inactive": 1}
    assert usage["summary"]["arrays_dimensioned"] >= 1 and usage["summary"]["used_in_macros"] == 1
    config = call(server.config_map)
    switches = {s["name"]: s for s in _items(config, "switches")}
    assert switches["OPTION_B"]["commented_definition"]["file"] == "Inc/config.h"
    assert not switches["OPTION_B"]["defined"] and config["configuration"] == "Debug"
    assert "COUNT" in switches and "CONFIG_H" not in switches


def test_config_diff_what_if(sample):
    diff = call(server.config_diff, defines={"COUNT": "12"})
    assert diff["summary"]["branches_switched"] == 1
    assert {m["name"] for m in _items(diff, "macros_changing")} >= {"COUNT", "DERIVED"}
    assert any(f["function"] == "main" for f in _items(diff, "functions_affected"))
    flipped = call(server.config_diff, defines="OPTION_B")
    assert any(s["name"] == "OnlyB" for s in _items(flipped, "symbols_appearing"))
    assert "error" in call(server.config_diff, defines="")


def test_configuration_from_env(sample, monkeypatch):
    monkeypatch.setenv("CVI_MCP_DEFINES", "OPTION_B")
    assert call(server.find_symbol, name="OnlyB", exact=True)["count"] == 1
    monkeypatch.delenv("CVI_MCP_DEFINES")
    monkeypatch.setenv("CVI_MCP_BUILD_CONFIG", "Release64")
    assert call(server.config_map)["is_64"] is True


# ------------------------------------------------------------------ recherche --

def test_tolerant_search_and_filters(sample):
    hits = call(server.find_symbol, name="count derived")
    assert hits["count"] == 0
    nb = call(server.find_symbol, name="nb label")
    assert nb["count"] == 0
    suggestions = call(server.find_symbol, name="Handlr1")["did_you_mean"]
    assert suggestions[0]["name"] == "Handler1"
    folder = call(server.list_symbols, file="Src", kind="function", limit=100)
    assert folder["total"] > 5 and all(i["file"].startswith("Src/") for i in folder["items"])
    pattern = call(server.list_symbols, file="Inc/*.h", kind="macro", limit=100)
    assert pattern["total"] >= 5
    assert "aucun fichier" in call(server.list_symbols, file="Nope/")["reason"]


def test_search_text_zones(sample):
    strings = call(server.search_text, pattern="General", scope="strings")
    assert strings["total"] >= 2 and all(h["zone"] == "string" for h in strings["hits"]["items"])
    assert strings["hits"]["items"][0]["match"].startswith('"')
    comments = call(server.search_text, pattern="OPTION_B", scope="comments", origin="all")
    assert comments["total"] == 1
    assert "error" in call(server.search_text, pattern="(", regex=True)


def test_topic_series_related_impact(sample):
    topic = call(server.find_by_topic, topic="handlers")
    assert topic["totals"]["functions"] == 2
    series = _items(call(server.series), "families")
    labels = {f["series"]: f for f in series}
    assert labels["gLabel<n>"]["to"] == 4
    assert any(f["file"] == "Src/main.c" and f["stops_at"] == 2
               for f in labels["gLabel<n>"]["files_stopping_early"])
    related = call(server.related, name="gLabel1")
    assert related["same_series"] and related["same_series"][0]["series"] == "gLabel<n>"
    impact = call(server.impact, name="COUNT")
    steps = {s["step"] for s in impact["checklist"]["items"]}
    assert {"definition", "directive #if", "tableau dimensionne", "macro derivee"} <= steps
    assert impact["nature"] == "macro"


def test_function_summary_skeleton_and_peek(sample, monkeypatch):
    helper = call(server.get_function, name="Helper", skeleton=True)
    assert "General/Name" in helper["touches"]["ini_written"]
    assert "General/Count" in helper["touches"]["ini_read"]
    assert any("call Ini_PutString" in line for line in helper["skeleton"])
    assert "body" not in helper
    assert "error" in call(server.peek, file="config.h", line=3)
    monkeypatch.setenv("CVI_MCP_ALLOW_PEEK", "1")
    peek = call(server.peek, file="config.h", line=3, before=0, after=0)
    assert peek["text"] == "#define COUNT 4"
    monkeypatch.setenv("CVI_MCP_ALLOW_BODY", "1")
    ranged = call(server.get_function, name="Helper", include_body=True, lines="3-4")
    assert len(ranged["body"].splitlines()) == 2


# ------------------------------------------------------------------- risques ---

def _risks(**kwargs):
    data = call(server.risks, min_confidence="faible", limit=200, **kwargs)
    return data, data["findings"]["items"]


def test_c_risks_and_leaks(sample):
    _data, items = _risks(file="risky.c")
    found = {(i["check"], i["function"]) for i in items}
    assert ("leak_on_return", "Leak") in found
    assert ("double_release", "Twice") in found
    assert ("unbounded_copy", "Copy") in found and ("format_mismatch", "Copy") in found
    assert ("unchecked_alloc", "Unchecked") in found and ("ignored_return", "Ignored") in found
    for clean in ("NoLeak", "Escapes", "Bitmap"):
        assert not any(i["function"] == clean for i in items), clean
    copies = [i for i in items if i["check"] == "unbounded_copy"]
    assert len(copies) == 2  # le litteral court est sans danger


def test_thread_risks(sample):
    _data, items = _risks()
    assert any(i["check"] == "ui_from_thread" and i["function"] == "Worker" for i in items)
    assert any(i["check"] == "shared_global_unlocked" and i["global"] == "gShared" for i in items)
    threads = call(server.threads)
    assert threads["entry_points"][0]["function"] == "Worker"
    assert threads["ui_timers"][0]["callback"] == "OnTick"
    flow = call(server.threads, global_name="gShared")
    assert {w["function"] for w in flow["writers"]} == {"main", "Worker"}


def test_lock_wrappers_order_blocking_and_macro_return(sample):
    report = call(server.lock_check, limit=50)
    assert {w["function"] for w in report["wrappers"]} == {"Lock", "Unlock"}
    assert report["lock_order_inversions"]
    assert report["blocking_calls_while_held"][0]["call"] == "MessagePopup"
    findings = {(f["function"], f["kind"]) for f in report["findings"]["items"]}
    assert ("MacroReturn", "return_while_held") in findings
    assert not any(f[0] in ("UsesWrapper", "Split") for f in findings)


# ---------------------------------------------------------------- coherence ----

def test_consistency(sample):
    items = call(server.consistency, limit=100)["findings"]["items"]
    found = {(i["check"], i.get("name") or i.get("function")) for i in items}
    assert ("prototype_mismatch", "Ninth") in found
    assert ("declared_not_defined", "Missing") in found
    assert ("could_be_static", "LocalOnly") in found
    assert ("callback_signature", "BadArity") in found
    assert ("control_on_wrong_panel", "main") in found


def test_dead_code_categories(sample):
    dead = call(server.dead_code, limit=50)
    functions = {f["name"]: f for f in dead["functions"]["top"]}
    assert functions["Ninth"]["status"] == "only_used_in_excluded_code"
    assert "Handler1" not in functions  # adresse prise dans une table
    globals_ = {g["name"] for g in dead["globals"]["top"]}
    assert "gUnusedGlobal" in globals_
    assert "gWriteOnly" in {g["name"] for g in dead["globals_write_only"]["top"]}
    macros = {m["name"] for m in dead["macros"]["top"]}
    assert "UNUSED_MACRO" in macros and "CONFIG_H" not in macros and "ONLY_IN_IF" not in macros
    panels = {p["panel"]: p["status"] for p in dead["panels"]["top"]}
    assert panels == {"SPARE": "never_loaded"}


def test_ui_map_tables_variants_and_filter(sample):
    ui = call(server.ui_map)
    assert ui["callbacks_reconciliation"]["unwired"] == 0
    kinds = {c["callback"]: c["kind"] for c in ui["callbacks_registered_by_code"]}
    assert kinds.get("Worker") == "thread"
    assert call(server.ui_map, uir="nothing.uir")["panels"]["total"] == 0


# -------------------------------------------------------------- cartographie ---

def test_domains_libraries_call_sites_persistence(sample, tmp_path):
    (sample["root"] / "Projet" / ".cvi-mcp.json").write_text(json.dumps({"domains": {"ihm": ["ui*"]}}))
    domains = {d["domain"]: d for d in _items(call(server.project_overview, section="domains"), "items")}
    assert domains["ihm"]["decided_by"] == "regle .cvi-mcp.json"
    libs = _items(call(server.project_overview, section="libraries"), "items")
    assert libs[0]["found"] and libs[0]["top"] == ["ExtCall"]
    sites = call(server.call_sites, pattern="Ini_Put*")
    assert {f["function"] for f in sites["functions"]} == {"Ini_PutString", "Ini_PutInt"}
    ini = call(server.persistence)
    assert {k["key"] for k in ini["written_never_read"]["items"]} == {"Name", "Orphan"}
    assert {k["key"] for k in ini["read_never_written"]["items"]} == {"Count"}
    sql = call(server.persistence, kind="sql")
    assert sql["tables"][0]["table"] == "mesures"


def test_module_graph_cycles_and_export(sample):
    graph = call(server.module_graph)
    assert graph["edges_total"] >= 1
    mermaid = call(server.module_graph, format="mermaid")
    assert mermaid["graph"].startswith("graph LR")
    assert call(server.module_graph, format="dot")["graph"].startswith("digraph")


def test_complexity_sections(sample):
    rows = call(server.complexity, limit=100)["items"]
    assert all({"cognitive", "params", "frame_bytes"} <= r.keys() for r in rows)
    assert call(server.complexity, section="by_file")["items"]


# ---------------------------------------------------------------------- diff ---

def test_diff_headline_ini_and_activation(sample, monkeypatch):
    call(server.snapshot_save, label="v1")
    (sample["root"] / "Inc" / "config.h").write_text(
        (sample["root"] / "Inc" / "config.h").read_text().replace("#define COUNT 4", "#define COUNT 9"))
    diff = call(server.diff_versions, before="v1")
    assert "COUNT: 4 -> 9" in diff["headline"]
    assert diff["summary"]["activation_changed"] >= 0
    path_diff = call(server.diff_versions, before="v1", after="current", files="Inc/*")
    assert all(i["file"].startswith("Inc/") for i in path_diff["value_changed"])


# ---------------------------------------------------------------- ergonomie ----

def test_budget_compact_audit(sample, monkeypatch):
    monkeypatch.setenv("CVI_MCP_MAX_CHARS", "2500")
    big = call(server.list_symbols, limit=500)
    assert big["truncated"]["lists"] and big["next_offset"] == big["limit"]
    monkeypatch.delenv("CVI_MCP_MAX_CHARS")
    compact = call(server.list_symbols, limit=20, format="compact")
    assert "columns" in compact["items"] and compact["items"]["rows"]
    monkeypatch.setenv("CVI_MCP_AUDIT_PATHS", "0")
    call(server.file_outline, file="main.c")
    last = json.loads((sample["tmp"] / "audit.jsonl").read_text().splitlines()[-1])
    assert last["args"]["file"].startswith("sha256:")


def test_use_project_and_self_check(sample, tmp_path):
    other = write_sample(tmp_path / "Other")
    listing = call(server.use_project)
    assert listing["current"].endswith("App.prj")
    switched = call(server.use_project, name_or_path=str(other))
    assert switched["current"] == str(other) and switched["files"] > 0
    assert "error" in call(server.use_project, name_or_path=str(tmp_path / "nope"))
    check = call(server.self_check)
    assert check["project"]["file"] == "App.prj"
    assert check["unresolved_includes"][0]["likely"] == "sdk_ou_systeme"
    assert check["index_info"]["build_config"] == "Debug"


def test_build_helpers():
    from cvi_mcp import build

    assert build.config_flag("Debug64") == "-debug64" and build.config_flag("Release") == ""
    assert build.config_flag("Custom") == "-config=Custom"
    assert build.cvi_year("1900") == "2019" and build.cvi_year("x") is None
    before = {"diagnostics": [{"file": "a.c", "message": "m", "severity": "error"}], "errors": 1}
    after = {"diagnostics": [{"file": "b.c", "message": "n", "severity": "warning"}]}
    change = build.compare_builds(before, after)
    assert change["fixed"] == 1 and change["new"][0]["file"] == "b.c"
