# ==============================================================================
#  cvi_mcp/quality.py
#  Role     Coherence des declarations et de l'interface avec le code
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json
import re

# ============================ Interne =========================================
from .queries import QueryEngine
from .uimap import UiMap

# Fonctions d'interface: position du handle de panneau et de la constante de controle.
CONTROL_CALLS = {
    "SetCtrlVal": (0, 1), "GetCtrlVal": (0, 1), "SetCtrlAttribute": (0, 1),
    "GetCtrlAttribute": (0, 1), "SetCtrlIndex": (0, 1), "GetCtrlIndex": (0, 1),
    "InsertListItem": (0, 1), "DeleteListItem": (0, 1), "ClearListCtrl": (0, 1),
    "GetNumListItems": (0, 1), "ReplaceListItem": (0, 1), "PlotY": (0, 1), "PlotXY": (0, 1),
    "DeleteGraphPlot": (0, 1), "SetActiveCtrl": (0, 1), "DefaultCtrl": (0, 1),
    "SetTableCellVal": (0, 1), "GetTableCellVal": (0, 1), "CanvasDrawBitmap": (0, 1),
}
# Nombre de parametres attendu d'une callback CVI selon ce qui la declenche.
CALLBACK_ARITY = {"control": 6, "panel": 5, "menu": 4}

_TYPE_NOISE = re.compile(r"\b(const|volatile|register|CVICALLBACK|CVIFUNC|__cdecl|__stdcall)\b")


def _normalize_param(text: str) -> str:
    """Type d'un parametre sans son nom: 'const char *name' -> 'char*'."""
    text = _TYPE_NOISE.sub(" ", text).strip()
    if text in ("void", ""):
        return "void"
    text = re.sub(r"\[[^\]]*\]", "*", text)
    match = re.match(r"(.*?)([A-Za-z_]\w*)\s*$", text)
    if match and match.group(1).strip() and not match.group(1).strip().endswith(("struct", "enum", "union")):
        text = match.group(1)
    return re.sub(r"\s+", "", text)


def _params(raw: str | None) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except ValueError:
        return []
    normalized = [_normalize_param(v) for v in values]
    return [] if normalized == ["void"] else normalized


def consistency(engine: QueryEngine) -> list[dict]:
    db = engine.db
    out: list[dict] = []
    definitions: dict[str, list] = {}
    for r in db.execute(
        "SELECT s.name, s.params, s.line_start, s.is_static, s.is_callback, s.signature, f.path, f.origin "
        "FROM symbols s JOIN files f ON s.file_id = f.id WHERE s.kind = 'function' AND s.active = 1"
    ):
        definitions.setdefault(r["name"], []).append(r)
    prototypes: dict[str, list] = {}
    for r in db.execute(
        "SELECT s.name, s.params, s.line_start, f.path, f.origin FROM symbols s JOIN files f "
        "ON s.file_id = f.id WHERE s.kind = 'prototype' AND s.active = 1"
    ):
        prototypes.setdefault(r["name"], []).append(r)

    # Deux definitions actives: erreur d'edition de liens.
    for name, defs in definitions.items():
        if len(defs) > 1 and not all(d["is_static"] for d in defs):
            out.append({"check": "duplicate_definition", "name": name, "confidence": "haute",
                        "locations": [{"file": engine.rel(d["path"]), "line": d["line_start"]} for d in defs],
                        "why": "fonction non static definie plusieurs fois dans le code compile"})

    # Prototype different de la definition.
    for name, protos in prototypes.items():
        defs = [d for d in definitions.get(name, []) if d["origin"] == "project"]
        if not defs:
            continue
        expected = _params(defs[0]["params"])
        for proto in protos:
            got = _params(proto["params"])
            if got != expected:
                out.append({"check": "prototype_mismatch", "name": name, "confidence":
                            "haute" if len(got) != len(expected) else "moyenne",
                            "file": engine.rel(proto["path"]), "line": proto["line_start"],
                            "definition": {"file": engine.rel(defs[0]["path"]), "line": defs[0]["line_start"]},
                            "prototype_params": got, "definition_params": expected,
                            "why": "parametres du prototype differents de ceux de la definition"})

    # Prototype du projet sans definition: appel vers une fonction absente du build.
    called = {r[0] for r in db.execute("SELECT DISTINCT name FROM refs WHERE kind = 'call' AND active = 1")}
    for name, protos in prototypes.items():
        if name in definitions:
            continue
        proto = protos[0]
        if proto["origin"] != "project":
            continue
        out.append({"check": "declared_not_defined", "name": name,
                    "file": engine.rel(proto["path"]), "line": proto["line_start"],
                    "called": name in called,
                    "confidence": "moyenne" if name in called else "haute",
                    "why": ("appelee mais definie nulle part dans le projet: bibliotheque liee "
                            "ou source manquante" if name in called else "prototype orphelin")})

    # Fonction appelee seulement depuis son fichier et absente des en-tetes: static possible.
    callers_files: dict[str, set] = {}
    for r in db.execute(
        "SELECT r.name, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        "WHERE r.kind IN ('call', 'ref', 'decl') AND r.active = 1"
    ):
        callers_files.setdefault(r["name"], set()).add(r["path"])
    in_headers = {name for name, protos in prototypes.items()
                  if any(p["path"].lower().endswith(".h") for p in protos)}
    for name, defs in definitions.items():
        d = defs[0]
        if d["is_static"] or d["is_callback"] or name in in_headers or name in ("main", "WinMain", "DllMain"):
            continue
        if d["origin"] != "project" or re.search(r"DLL(EXPORT|STDCALL)|dllexport", d["signature"] or ""):
            continue
        users = callers_files.get(name, set())
        if users and users == {d["path"]}:
            out.append({"check": "could_be_static", "name": name, "file": engine.rel(d["path"]),
                        "line": d["line_start"], "confidence": "moyenne",
                        "why": "utilisee seulement dans son fichier et declaree dans aucun en-tete"})

    # extern dont le type differe de la definition.
    globals_: dict[str, list] = {}
    for r in db.execute(
        "SELECT s.name, s.signature, s.line_start, f.path FROM symbols s JOIN files f "
        "ON s.file_id = f.id WHERE s.kind = 'global' AND s.active = 1 AND f.origin = 'project'"
    ):
        globals_.setdefault(r["name"], []).append(r)
    for name, decls in globals_.items():
        types = {}
        for decl in decls:
            sig = re.sub(r"\b(extern|static)\b", " ", decl["signature"] or "")
            sig = re.sub(r"=.*$", "", sig)
            base = re.sub(r"\s+", " ", sig.split(name)[0]).strip() if name in sig else sig.strip()
            types.setdefault(base, []).append(decl)
        if len(types) > 1:
            out.append({"check": "extern_mismatch", "name": name, "confidence": "moyenne",
                        "types": {t: [f"{engine.rel(d['path'])}:{d['line_start']}" for d in ds]
                                  for t, ds in types.items()},
                        "why": "globale declaree avec des types differents"})

    out.extend(_ui_consistency(engine, definitions))
    return out


def _ui_consistency(engine: QueryEngine, definitions: dict) -> list[dict]:
    """Interface et code: arite des callbacks, controle employe avec le
    handle d'un autre panneau."""
    mapper = UiMap(engine)
    uir = mapper.uir
    if not uir:
        return []
    out = []
    for callback, owners in uir["callbacks"].items():
        defs = definitions.get(callback)
        if not defs:
            continue
        kinds = {"panel" if (uir["controls"].get(o) or {}).get("type") == "panel" else "control"
                 for o in owners}
        params = _params(defs[0]["params"])
        expected = {CALLBACK_ARITY[k] for k in kinds}
        if len(params) not in expected:
            out.append({"check": "callback_signature", "name": callback,
                        "file": engine.rel(defs[0]["path"]), "line": defs[0]["line_start"],
                        "params": len(params), "expected": sorted(expected), "wired_on": sorted(owners)[:5],
                        "confidence": "haute",
                        "why": "nombre de parametres different de celui d'une callback CVI"})

    # Handle de panneau -> panneau charge, d'apres var = LoadPanel(..., PANEL).
    handle_panel: dict[str, set[str]] = {}
    for r in engine.db.execute(
        "SELECT r.id FROM refs r WHERE r.kind = 'call' AND r.active = 1 "
        "AND r.name IN ('LoadPanel', 'LoadPanelEx')"
    ):
        panel_row = engine.db.execute(
            "SELECT text FROM call_args WHERE ref_id = ? AND pos = 2", (r["id"],)).fetchone()
        target = _assigned_to(engine, r["id"])
        if panel_row and target and panel_row[0] in uir["panels"]:
            handle_panel.setdefault(target, set()).add(panel_row[0])
    owners = uir["owners"]
    marks = ",".join("?" for _ in CONTROL_CALLS)
    for r in engine.db.execute(
        "SELECT r.id, r.name, r.line, r.enclosing, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        f"WHERE r.kind = 'call' AND r.active = 1 AND r.name IN ({marks})", list(CONTROL_CALLS),
    ):
        handle_pos, control_pos = CONTROL_CALLS[r["name"]]
        args = {a["pos"]: a["text"] for a in engine.db.execute(
            "SELECT pos, text FROM call_args WHERE ref_id = ?", (r["id"],))}
        handle, control = args.get(handle_pos, ""), args.get(control_pos, "")
        panels = handle_panel.get(handle)
        owner = owners.get(control)
        if panels and owner and len(panels) == 1 and owner not in panels:
            out.append({"check": "control_on_wrong_panel", "function": r["enclosing"],
                        "file": engine.rel(r["path"]), "line": r["line"], "call": r["name"],
                        "handle": handle, "handle_loads": sorted(panels), "control": control,
                        "control_belongs_to": owner, "confidence": "moyenne",
                        "why": f"{control} appartient a {owner} mais {handle} charge {min(panels)}"})
    return out


def _assigned_to(engine: QueryEngine, ref_id: int) -> str | None:
    """Variable qui recoit le resultat d'un appel, lue dans le texte source
    de la ligne: x = LoadPanel(...)."""
    row = engine.db.execute(
        "SELECT r.line, r.col, f.path FROM refs r JOIN files f ON r.file_id = f.id WHERE r.id = ?",
        (ref_id,)).fetchone()
    if row is None:
        return None
    cache = getattr(engine, "_line_cache", None)
    if cache is None:
        cache = engine._line_cache = {}
    if row["path"] not in cache:
        from pathlib import Path

        from .cparse import read_source

        try:
            cache[row["path"]] = read_source(Path(row["path"]))[0].split("\n")
        except OSError:
            cache[row["path"]] = []
    lines = cache[row["path"]]
    if row["line"] - 1 >= len(lines):
        return None
    before = lines[row["line"] - 1][: row["col"]]
    match = re.search(r"([A-Za-z_][\w\[\]\.>-]*)\s*=\s*(\([^)]*\)\s*)?$", before)
    return match.group(1) if match else None


CONSISTENCY_NOTES = {
    "duplicate_definition": "meme fonction non static definie deux fois: echec a l'edition de liens",
    "prototype_mismatch": "parametres du prototype et de la definition differents",
    "declared_not_defined": "prototype du projet sans definition dans les sources indexees",
    "could_be_static": "utilisee seulement dans son fichier: static limiterait sa portee",
    "extern_mismatch": "une meme globale declaree avec deux types",
    "callback_signature": "arite incompatible avec une callback de controle (6) ou de panneau (5)",
    "control_on_wrong_panel": "constante de controle d'un panneau employee avec le handle d'un autre",
}
