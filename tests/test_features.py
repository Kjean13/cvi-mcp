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


# ---------------------------------------------------------------- cache projet --

def test_project_cache_reused_when_nothing_changed(sample):
    call(server.find_symbol, name="Handler1")
    first = server._LAST["project"]
    call(server.find_symbol, name="Handler1")
    assert server._LAST["project"] is first
    assert call(server.find_symbol, name="Handler1")["index_info"].get("reindexed_this_call") in (0, None)


def test_project_cache_sees_edited_file(sample):
    assert call(server.find_symbol, name="BrandNewFunc", exact=True)["count"] == 0
    first = server._LAST["project"]
    main = sample["root"] / "Src" / "main.c"
    main.write_text(main.read_text(encoding="utf-8") + "\nint BrandNewFunc(void) { return 1; }\n",
                    encoding="utf-8")
    assert call(server.find_symbol, name="BrandNewFunc", exact=True)["count"] == 1
    assert server._LAST["project"] is not first


def test_project_cache_sees_changed_defines(sample, monkeypatch):
    call(server.config_map)
    first = server._LAST["project"]
    monkeypatch.setenv("CVI_MCP_DEFINES", "OPTION_B")
    assert call(server.find_symbol, name="OnlyB", exact=True)["count"] == 1
    assert server._LAST["project"] is not first


def test_project_cache_sees_header_change(sample):
    """Un en-tete modifie change le code actif: le cache doit s'en apercevoir."""
    header = sample["root"] / "Inc" / "app.h"
    before = call(server.find_symbol, name="HeaderAddedMacro", exact=True)["count"]
    header.write_text(header.read_text(encoding="utf-8") + "\n#define HeaderAddedMacro 7\n",
                      encoding="utf-8")
    assert before == 0
    assert call(server.find_symbol, name="HeaderAddedMacro", exact=True)["count"] == 1


# ---------------------------------------------------------------- recherche --

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


def test_unused_symbol_is_explained(sample):
    """Un symbole defini mais jamais reference ne doit pas etre presente comme inconnu."""
    names = [s["name"] for s in call(server.list_symbols, kind="global", limit=200)["items"]]
    unused = [n for n in names if call(server.find_usages, name=n)["total"] == 0]
    if not unused:  # aucune globale inutilisee dans le projet d'exemple
        pytest.skip("pas de symbole inutilise dans l'exemple")
    reason = call(server.find_usages, name=unused[0])["reason"]
    assert "defini" in reason and "aucun symbole indexe" not in reason
    assert "aucun symbole indexe" in call(server.find_usages, name="NoSuchThing99")["reason"]


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


def test_topic_does_not_match_short_prefix_of_word(sample):
    """'coloration' ne doit pas remonter ChangeColorRealTime (couleur graphique)."""
    main = sample["root"] / "Src" / "main.c"
    main.write_text(main.read_text(encoding="utf-8")
                    + "\nint ChangeColorRealTime(void) { return 0; }\n"
                    + "int AfficheColoration(void) { return 0; }\n"
                    + "int ColorationMoyenne(void) { return 0; }\n", encoding="utf-8")
    names = {f["name"] for f in call(server.find_by_topic, topic="coloration")["functions"]}
    assert {"AfficheColoration", "ColorationMoyenne"} <= names
    assert "ChangeColorRealTime" not in names


def test_series_name_filter_is_strict(sample):
    """Un nom inconnu ne doit pas renvoyer toutes les suites."""
    everything = _items(call(server.series), "families")
    assert len(everything) >= 1
    assert [f["series"] for f in _items(call(server.series, name="gLabel1"), "families")] == ["gLabel<n>"]
    assert _items(call(server.series, name="gLabel"), "families")      # sans numero: prefixe
    assert _items(call(server.series, name="ZZZnope"), "families") == []
    assert _items(call(server.series, name="ZZZnope7"), "families") == []


def test_persistence_rejects_unknown_kind(sample):
    assert "error" in call(server.persistence, kind="bin")
    assert "keys" in call(server.persistence, kind="ini")
    assert "tables" in call(server.persistence, kind="sql")


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
    # Par defaut les copies d'un meme tampon forment une alerte (le litteral court est sans danger).
    copies = [i for i in items if i["check"] == "unbounded_copy"]
    assert len(copies) == 1
    assert copies[0]["occurrences"] == 2 and len(copies[0]["lines"]) == 2
    assert set(copies[0]["calls"]) == {"strcpy", "sprintf"}
    # group=false rend le detail, une alerte par appel.
    _data, raw = _risks(file="risky.c", group=False)
    detail = [i for i in raw if i["check"] == "unbounded_copy"]
    assert len(detail) == 2 and not any("occurrences" in i for i in detail)


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
    blocked = report["blocking_calls_while_held"][0]
    assert blocked["call"] == "MessagePopup"
    # Le verrou s'affiche par son nom, pas par la cle interne "Famille(verrou)".
    assert blocked["locks_held"] and all("(" not in name for name in blocked["locks_held"])
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


# ------------------------------------------------------- tailles de formats --

@pytest.mark.parametrize("fmt, args, expected", [
    ('"%c%c%c%c"', ["a", "b", "c", "d"], 5),                 # 4 caracteres + NUL
    ('"\\033bg%06X\\033fg%06X"', ["a", "b"], 23),             # 6 + 2 * 8 hex + NUL
    ('"%d"', ["n"], 12),                                      # int signe: 11 + NUL
    ('"%u"', ["n"], 11),
    ('"%lld"', ["n"], 22),
    ('"%s"', ["name"], None),                                 # taille imprevisible
    ('"id=%s"', ['"abc"'], 7),
])
def test_format_bound_values(fmt, args, expected):
    from cvi_mcp import risks
    assert risks._format_bound(fmt, args) == expected


def test_format_bound_nominal_uses_field_width():
    from cvi_mcp import risks
    fmt = '"%04i-%02i-%02i"'
    assert risks._format_bound(fmt, ["a", "b", "c"]) == 3 * 11 + 2 + 1
    assert risks._format_bound(fmt, ["a", "b", "c"], nominal=True) == 4 + 2 + 2 + 2 + 1


def _unbounded_item(fmt, size, args=("x",)):
    from cvi_mcp import risks
    local = {"buf": {"array": True, "bytes": size, "size_text": str(size)}}
    return risks._unbounded("sprintf", ["buf", fmt, *args], local, {"function": "f", "file": "a.c"}, 1)


def test_unbounded_sprintf_exact_fit_is_not_reported():
    assert _unbounded_item('"%c%c%c%c"', 5, ("a", "b", "c", "d")) == []        # 5 octets: tient pile
    assert _unbounded_item('"\\033bg%06X\\033fg%06X"', 24, ("a", "b")) == []   # 23 <= 24


def test_unbounded_sprintf_tiers():
    real = _unbounded_item('"%d-%d-%d-%d"', 10, ("a", "b", "c", "d"))
    assert real and real[0]["confidence"] == "haute"
    theory = _unbounded_item('"%04i-%02i-%02i"', 12, ("a", "b", "c"))           # 11 nominal <= 12
    assert theory and theory[0]["confidence"] == "faible"
    assert _unbounded_item('"%04i-%02i-%02i"', 8, ("a", "b", "c"))[0]["confidence"] == "haute"


def test_call_graph_accepts_name_alias_and_validates(sample):
    by_function = call(server.call_graph, function="main")
    by_name = call(server.call_graph, name="main")
    assert by_name["graph"] == by_function["graph"] and by_function["graph"].get("main") is not None
    assert "error" in call(server.call_graph)
    assert "error" in call(server.call_graph, function="main", direction="sideways")


def test_empty_patterns_are_rejected(sample):
    assert "error" in call(server.search_text, pattern="")
    assert "error" in call(server.search_text, pattern="   ")
    assert "error" in call(server.call_sites, pattern="")


# ------------------------------------------------ copies de taille prouvee --

def _append_c(sample, text):
    """Ajoute du code C a un fichier du projet d'exemple (le cache voit la modification)."""
    main = sample["root"] / "Src" / "main.c"
    main.write_text(main.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_strcpy_from_smaller_array_is_proven_safe(sample):
    _append_c(sample, """
char gNames[4][16];
char gTitle[40];
static void CopySmall(int i)
{
    char dest[20];
    char tiny[10];
    strcpy(dest, gNames[i]);      /* 16 <= 20: tient */
    strcpy(dest, tiny);           /* 10 <= 20: tient */
    strcpy(tiny, gTitle);         /* 40 > 10: peut deborder */
}
""")
    _data, items = _risks(file="main.c")
    copies = [i for i in items if i["check"] == "unbounded_copy" and i["function"] == "CopySmall"]
    assert [(c["buffer"], c["line"] - copies[0]["line"]) for c in copies] == [("tiny", 0)]


def test_group_findings_keeps_best_confidence_and_lines():
    from cvi_mcp import risks
    base = {"check": "unbounded_copy", "function": "F", "file": "a.c", "buffer": "sql",
            "buffer_bytes": 2000}
    items = [{**base, "line": 30, "call": "strcat", "confidence": "moyenne"},
             {**base, "line": 10, "call": "strcpy", "confidence": "haute"},
             {**base, "line": 20, "call": "strcat", "confidence": "faible"},
             {**base, "function": "G", "line": 5, "call": "strcat", "confidence": "moyenne"},
             {"check": "ignored_return", "function": "F", "file": "a.c", "line": 1}]
    grouped = risks.group_findings(items)
    assert len(grouped) == 3          # F/sql groupe, G/sql seul, l'autre check
    head = next(g for g in grouped if g.get("occurrences"))
    assert (head["line"], head["lines"], head["confidence"]) == (10, [10, 20, 30], "haute")
    assert head["calls"] == {"strcpy": 1, "strcat": 2}
    assert not any("occurrences" in g for g in grouped if g["function"] == "G")


# ------------------------------------------------ verrous suivis par ligne --

LOCKED_C = """
int gGuarded, gSplit, gOpen;
int lockA, lockB, lockC;
static void Bump(void)                   /* appelee seulement sous lockA */
{
    gGuarded++;
    gSplit = 1;
}
static void Guarded(void)
{
    CmtGetLock(lockA);
    Bump();
    CmtReleaseLock(lockA);
}
static int Early(int err)                /* retour anticipe: la suite reste sous lockA */
{
    CmtGetLock(lockA);
    if (err) {
        CmtReleaseLock(lockA);
        return -1;
    }
    gGuarded = 5;
    CmtReleaseLock(lockA);
    return 0;
}
void UiSide(void)
{
    CmtGetLock(lockB);
    gSplit = 2;                          /* lockB ici, lockA dans Bump */
    CmtReleaseLock(lockB);
    gOpen = 1;                           /* aucun verrou */
    CmtGetLock(lockA);
    gGuarded = 0;
    CmtReleaseLock(lockA);
}
void DrawBare(void)  { SetCtrlAttribute(1, MY_CANVAS, 3, 3); }
"""
# Dessins sur une seule ligne: la prise, l'acces et la liberation partagent une ligne,
# la carte des verrous doit les distinguer par leur colonne.
_ONE_LINE = "void {name}(void) {{ CmtGetLock({lock}); SetCtrlAttribute(1, MY_CANVAS, {n}, {n}); " \
            "CmtReleaseLock({lock}); }}\n"
LOCKED_C += "".join(_ONE_LINE.format(name=name, lock=lock, n=n) for n, (name, lock) in enumerate(
    [("DrawSafe", "lockC"), ("DrawSafe2", "lockC"), ("DrawWrong", "lockA")]))


@pytest.fixture
def locked(sample):
    """Le projet d'exemple, avec du code verrouille appele depuis le thread Worker."""
    path = sample["root"] / "Src" / "worker.c"
    text = path.read_text(encoding="utf-8")
    head = "int CVICALLBACK Worker(void *data)\n{"
    assert head in text
    path.write_text(text.replace(head, LOCKED_C + head + "\n    Guarded();\n    Early(1);\n"
                                 "    DrawBare();\n    gOpen = 3;"), encoding="utf-8")
    return sample


def _checks(items, check):
    return [i for i in items if i["check"] == check]


def test_lock_inherited_from_callers_and_early_return(locked):
    _data, items = _risks()
    unlocked = {i["global"] for i in _checks(items, "shared_global_unlocked")}
    assert "gOpen" in unlocked                       # jamais sous verrou
    assert "gGuarded" not in unlocked   # Bump herite de lockA, Early garde lockA apres return
    flow = call(server.threads, global_name="gGuarded")
    bump = next(f for f in flow["writers"] + flow["readers_only"] if f["function"] == "Bump")
    assert bump["protected_by_callers"] and bump["unlocked_lines"] == []
    early = next(f for f in flow["writers"] if f["function"] == "Early")
    assert early["unlocked_lines"] == []


def test_inconsistent_lock_between_contexts(locked):
    _data, items = _risks()
    conflict = [i for i in _checks(items, "inconsistent_lock") if i["global"] == "gSplit"]
    assert len(conflict) == 1
    assert conflict[0]["locks"] == ["lockA", "lockB"]
    assert {conflict[0]["function"], conflict[0]["other"]["function"]} == {"Bump", "UiSide"}
    assert not any(i["global"] == "gGuarded" for i in _checks(items, "inconsistent_lock"))


def test_ui_control_touched_without_its_usual_lock(locked):
    _data, items = _risks()
    found = {i["function"]: i for i in _checks(items, "ui_control_unlocked")}
    assert set(found) == {"DrawWrong", "DrawBare"}   # DrawSafe et DrawSafe2 tiennent lockC
    assert found["DrawBare"]["locks"] == ["lockC"] and "aucun verrou" in found["DrawBare"]["why"]
    assert "seul lockA est tenu" in found["DrawWrong"]["why"]
    assert found["DrawBare"]["confidence"] == "moyenne"     # dessine depuis un thread


def test_lock_map_unit(locked):
    from cvi_mcp import threads
    engine = server._engine(server._cfg())
    try:
        where, entries = threads.contexts(engine)
        locks = threads.LockMap(engine, entries)
        assert "Bump" in locks.covered and locks.inherited["Bump"] == frozenset({"lockA"})
        assert "Worker" not in locks.covered      # lancee dans un thread: appelable de l'exterieur
        assert "UiSide" not in locks.covered             # rien ne l'appelle
    finally:
        engine.close()


# ------------------------------------------ declarations a plusieurs noms --

def test_every_name_of_a_multi_declaration_is_indexed(sample):
    _append_c(sample, """
int gMultiA, gMultiB[4], *gMultiC;
static int gMultiD = 7, gMultiE = 8;
""")
    expected = {"gMultiA": "int gMultiA", "gMultiB": "int gMultiB[4]", "gMultiC": "int *gMultiC",
                "gMultiD": "static int gMultiD = 7", "gMultiE": "static int gMultiE = 8"}
    for name, signature in expected.items():
        found = call(server.find_symbol, name=name, exact=True)["results"]
        assert [(r["kind"], r["signature"]) for r in found] == [("global", signature)], name


def test_multi_declaration_reads_and_writes_are_attributed(sample):
    _append_c(sample, """
int gPairA, gPairB;
static void UsePair(void) { gPairB = gPairA + 1; }
""")
    assert call(server.find_usages, name="gPairB")["by_kind"] == {"write": 1}
    assert call(server.find_usages, name="gPairA")["by_kind"] == {"read": 1}
    assert call(server.dead_code, section="globals", limit=200)["total"] >= 0


def test_series_separates_definition_and_usage(sample):
    """main.c declare gLabel1..4 mais n'utilise que gLabel1 et gLabel2."""
    labels = {f["series"]: f for f in _items(call(server.series), "families")}
    early = labels["gLabel<n>"]["files_stopping_early"]
    assert {"file": "Src/main.c", "stops_at": 2, "in": "usage"} in early
    assert not any(e["file"] == "Src/main.c" and e["in"] == "definition" for e in early)


# ------------------------------------------------- reponses et suggestions --

def test_suggestions_never_propose_the_searched_name(sample):
    engine = server._engine(server._cfg())
    try:
        names = [s["name"] for s in engine.suggest("Handler1")]
        assert "Handler1" not in names and "Handler2" in names
    finally:
        engine.close()


def test_find_usages_gives_by_file_overview(sample):
    usages = call(server.find_usages, name="gLabel1")
    assert sum(usages["by_file"].values()) == usages["total"]
    assert list(usages["by_file"].values()) == sorted(usages["by_file"].values(), reverse=True)


def test_truncated_list_keeps_a_by_file_summary(sample, monkeypatch):
    monkeypatch.setenv("CVI_MCP_MAX_CHARS", "700")
    out = call(server.find_usages, name="gLabel1")
    note = out["truncated"]["lists"][0]
    assert note["kept"] < note["had"]
    assert sum(note["by_file"].values()) == note["had"]    # la liste entiere, pas la partie gardee


# ----------------------------------------------------------------- history --

def _git_commit(root, message):
    import subprocess
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", *env, "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", *env, "commit", "-qm", message], cwd=root, check=True,
                   capture_output=True)


@pytest.fixture
def repo(sample):
    import shutil
    import subprocess
    if shutil.which("git") is None:
        pytest.skip("git absent")
    subprocess.run(["git", "init", "-q"], cwd=sample["root"], check=True, capture_output=True)
    _git_commit(sample["root"], "premier")
    return sample


def test_history_without_target_caps_files_per_commit(repo):
    from cvi_mcp import gitinfo
    extra = repo["root"] / "Many"
    extra.mkdir()
    for i in range(gitinfo.MAX_FILES_PER_COMMIT + 5):
        (extra / f"f{i}.txt").write_text("x", encoding="utf-8")
    _git_commit(repo["root"], "beaucoup de fichiers")
    commits = call(server.history)["commits"]
    assert commits[0]["subject"] == "beaucoup de fichiers"
    assert len(commits[0]["files"]) == gitinfo.MAX_FILES_PER_COMMIT
    assert commits[0]["files_total"] == gitinfo.MAX_FILES_PER_COMMIT + 5
    first = commits[-1]
    assert "files_total" not in first or first["files_total"] >= len(first["files"])


def test_history_of_function_file_and_unknown_target(repo):
    main = repo["root"] / "Src" / "main.c"
    main.write_text(main.read_text(encoding="utf-8") + "\nint Fresh(void) { return 1; }\n",
                    encoding="utf-8")
    _git_commit(repo["root"], "ajoute Fresh")
    by_function = call(server.history, target="Fresh")
    assert by_function["function"] == "Fresh"
    assert by_function["commits"][0]["subject"] == "ajoute Fresh"
    by_file = call(server.history, target="main.c")
    assert by_file["file"].endswith("main.c") and len(by_file["commits"]) >= 2
    assert "reason" in call(server.history, target="NoSuchThing")


def test_history_without_repository(sample):
    assert call(server.history)["available"] is False


def test_lock_map_is_kept_with_the_project_and_dropped_with_it(locked):
    from cvi_mcp import risks
    engine = server._engine(server._cfg())
    try:
        first = risks.lock_map(engine)
        assert risks.lock_map(engine) is first                    # meme projet: reutilise
        own_pairs = {"MyLock": "MyUnlock"}                        # paires propres: pas de memo
        assert risks.lock_map(engine, own_pairs) is not first
    finally:
        engine.close()
    worker = locked["root"] / "Src" / "worker.c"
    worker.write_text(worker.read_text(encoding="utf-8") + "\nint gLater;\n", encoding="utf-8")
    engine = server._engine(server._cfg())
    try:
        assert risks.lock_map(engine) is not first                # fichier modifie: carte neuve
    finally:
        engine.close()
