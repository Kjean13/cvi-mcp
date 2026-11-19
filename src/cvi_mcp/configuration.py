# ==============================================================================
#  cvi_mcp/configuration.py
#  Role     Options de compilation: usages d'une macro, interrupteurs du
#           projet, effet d'un changement de valeur (what-if)
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json
import re
from pathlib import Path

# ============================ Interne =========================================
from .config import Config
from .cparse import read_source
from .preproc import INACTIVE, KNOWN_UNDEFINED, STATE_NAMES, evaluate_value
from .project import Project, load
from .queries import QueryEngine, page

_LINE_DEFINE = re.compile(r"^\s*//+\s*#\s*define\s+([A-Za-z_]\w*)\s*(.*)$")
_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)
_BLOCK_DEFINE = re.compile(r"^\s*\**\s*#\s*define\s+([A-Za-z_]\w*)\s*(.*)$", re.MULTILINE)


def commented_defines(paths: list[str]) -> dict[str, list[dict]]:
    """#define mis en commentaire: une option desactivee a la main, souvent
    l'alternative a la valeur courante (//#define AVEC_OPTION)."""
    found: dict[str, list[dict]] = {}
    for path in paths:
        try:
            text, _enc, _raw = read_source(Path(path))
        except OSError:
            continue
        for number, line in enumerate(text.split("\n"), 1):
            match = _LINE_DEFINE.match(line)
            if match:
                found.setdefault(match.group(1), []).append(
                    {"path": path, "line": number, "value": " ".join(match.group(2).split()) or None})
        for block in _BLOCK.finditer(text):
            first = text.count("\n", 0, block.start()) + 1
            for match in _BLOCK_DEFINE.finditer(block.group(0)):
                line = first + block.group(0).count("\n", 0, match.start())
                found.setdefault(match.group(1), []).append(
                    {"path": path, "line": line, "value": " ".join(match.group(2).split()) or None})
    return found


def _state_map(proj: Project) -> dict[str, dict[int, int]]:
    """Etat de chaque branche, par fichier et ligne de directive."""
    return {
        path: {line: branch.state for line, branch in result.branches.items()}
        for path, result in proj.pp.results.items()
    }


def _inactive(proj: Project) -> dict[str, set[int]]:
    return {path: result.inactive_lines() for path, result in proj.pp.results.items()}


def _value(name: str, proj: Project) -> dict:
    """Valeur d'une macro dans l'environnement fusionne des unites."""
    seen = proj.pp.env_values.get(name)
    if not seen:
        return {"defined": False}
    raw = sorted(seen)
    out: dict = {"defined": True, "value": raw[0]}
    number = evaluate_value(raw[0], proj.pp.env)
    if number is not None and str(number) != raw[0]:
        out["evaluated"] = number
    if len(raw) > 1:
        out["values_by_unit"] = raw
    return out


# ----------------------------------------------------------- macro_usages -------

def macro_usages(engine: QueryEngine, cfg: Config, name: str,
                 forced: dict | None = None, limit: int = 30) -> dict:
    proj = load(cfg, forced) if forced else engine.project
    states = _state_map(proj)
    inactive = _inactive(proj)
    db = engine.db

    definitions = []
    for r in db.execute(
        "SELECT s.line_start, s.value, s.active, s.params, f.path FROM symbols s "
        "JOIN files f ON s.file_id = f.id WHERE s.name = ? AND s.kind = 'macro' "
        "ORDER BY f.path, s.line_start", (name,),
    ):
        live = r["line_start"] not in inactive.get(r["path"], set())
        entry = {"file": engine.rel(r["path"]), "line": r["line_start"], "value": r["value"],
                 "active": live}
        number = evaluate_value(r["value"] or "", proj.pp.env) if r["value"] else None
        if number is not None and str(number) != (r["value"] or ""):
            entry["evaluated"] = number
        definitions.append(entry)

    conditions = []
    for r in db.execute(
        "SELECT b.line_start, b.expr, b.kind, b.confidence, f.path FROM pp_blocks b "
        "JOIN files f ON b.file_id = f.id WHERE b.names LIKE ? AND b.guard = 0 "
        "ORDER BY f.path, b.line_start", (f'%"{name}"%',),
    ):
        state = states.get(r["path"], {}).get(r["line_start"], INACTIVE)
        conditions.append({"file": engine.rel(r["path"]), "line": r["line_start"],
                           "directive": f"#{r['kind']} {r['expr']}".strip(),
                           "state": STATE_NAMES[state], "confidence": r["confidence"]})

    code: dict[tuple, dict] = {}
    arrays, in_macros, initializers = [], [], []
    for r in db.execute(
        "SELECT r.line, r.kind, r.enclosing, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        "WHERE r.name = ? AND r.kind NOT IN ('cond', 'return') ORDER BY f.path, r.line", (name,),
    ):
        live = r["line"] not in inactive.get(r["path"], set())
        where = {"file": engine.rel(r["path"]), "line": r["line"]}
        if r["kind"] == "dim":
            arrays.append({**where, "array_or_function": r["enclosing"], "active": live})
        elif r["kind"] == "macro":
            value = _value(r["enclosing"], proj)
            in_macros.append({**where, "macro": r["enclosing"], "active": live,
                              **({"evaluated": value.get("evaluated", value.get("value"))}
                                 if value.get("defined") else {})})
        elif r["kind"] == "decl":
            initializers.append({**where, "global": r["enclosing"], "active": live})
        else:
            key = (r["enclosing"], r["path"])
            group = code.setdefault(key, {"function": r["enclosing"], "file": engine.rel(r["path"]),
                                          "uses": 0, "first_line": r["line"], "active": live})
            group["uses"] += 1
            group["active"] = group["active"] or live

    by_state: dict[str, int] = {}
    for c in conditions:
        by_state[c["state"]] = by_state.get(c["state"], 0) + 1
    by_file: dict[str, int] = {}
    for c in conditions:
        by_file[c["file"]] = by_file.get(c["file"], 0) + 1

    commented = [
        {"file": engine.rel(c["path"]), "line": c["line"], "value": c["value"]}
        for c in commented_defines([d for d in proj.files if Path(d).suffix.lower() == ".h"]).get(name, [])
    ]
    found = definitions or conditions or code or arrays or in_macros or initializers
    payload = {
        "name": name,
        "configuration": proj.ctx.config_name,
        "forced": forced or {},
        **_value(name, proj),
        "summary": {
            "definitions": len(definitions),
            "conditions": len(conditions),
            "conditions_by_state": by_state,
            "conditions_by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])),
            "functions_using": len(code),
            "arrays_dimensioned": len(arrays),
            "used_in_macros": len(in_macros),
            "used_in_initializers": len(initializers),
        },
        "definitions": definitions,
        "commented_definitions": commented,
        "conditions": page(conditions, 0, limit),
        "code": page(sorted(code.values(), key=lambda g: -g["uses"]), 0, limit),
        "arrays": page(arrays, 0, limit),
        "in_macros": page(in_macros, 0, limit),
        "initializers": page(initializers, 0, limit),
    }
    if not found:
        payload["reason"] = f"'{name}' n'apparait ni dans une definition, ni dans un #if, ni dans le code"
        payload["did_you_mean"] = engine.suggest(name)
    return payload


# ------------------------------------------------------------- config_map -------

def config_map(engine: QueryEngine, cfg: Config, scope: str = "project",
               limit: int = 40, offset: int = 0) -> dict:
    """Interrupteurs: macros testees par des #if du code du projet, avec leur
    valeur courante et le nombre de blocs qu'elles pilotent."""
    proj = engine.project
    states = _state_map(proj)
    origin_clause = "" if scope == "all" else " AND f.origin = 'project'"
    switches: dict[str, dict] = {}
    for r in engine.db.execute(
        "SELECT b.names, b.line_start, b.guard, f.path FROM pp_blocks b JOIN files f "
        f"ON b.file_id = f.id WHERE b.guard = 0 AND b.kind != 'else'{origin_clause}"
    ):
        state = states.get(r["path"], {}).get(r["line_start"], INACTIVE)
        for name in _names(r["names"]):
            if name in KNOWN_UNDEFINED:
                continue
            entry = switches.setdefault(name, {"name": name, "blocks": 0, "files": set(),
                                               "active": 0, "inactive": 0, "unknown": 0})
            entry["blocks"] += 1
            entry["files"].add(engine.rel(r["path"]))
            entry[STATE_NAMES[state]] += 1

    commented = commented_defines([p for p in proj.files if p.lower().endswith(".h")])
    defined_at = {}
    for r in engine.db.execute(
        "SELECT s.name, s.line_start, s.active, f.path FROM symbols s JOIN files f "
        "ON s.file_id = f.id WHERE s.kind = 'macro' ORDER BY s.active DESC"
    ):
        defined_at.setdefault(r["name"], {"file": engine.rel(r["path"]), "line": r["line_start"]})

    items = []
    for entry in switches.values():
        name = entry["name"]
        value = _value(name, proj)
        origin = "prj" if name in proj.ctx.defines else (
            "predefini" if name in _PREDEFINED else None)
        item = {
            "name": name,
            **value,
            "blocks": entry["blocks"],
            "active": entry["active"],
            "inactive": entry["inactive"],
            "files": len(entry["files"]),
        }
        if entry["unknown"]:
            item["unknown"] = entry["unknown"]
        if origin:
            item["origin"] = origin
        elif name in defined_at:
            item["defined_in"] = defined_at[name]
        if name in commented:
            c = commented[name][0]
            item["commented_definition"] = {"file": engine.rel(c["path"]), "line": c["line"],
                                             "value": c["value"]}
        items.append(item)
    items.sort(key=lambda i: (-i["blocks"], i["name"]))

    return {
        "configuration": proj.ctx.config_name,
        "available_configurations": [c["name"] for c in proj.settings.get("configs", [])],
        "is_64": proj.ctx.is_64,
        "cvi_version": proj.ctx.cvi_version,
        "prj_defines": proj.ctx.defines,
        "include_paths": [engine.rel(p) for p in proj.ctx.include_paths],
        "switches_total": len(items),
        "switches_off": sum(1 for i in items if not i["defined"]),
        "switches": page(items, offset, limit),
        "note": "switch non defini = option desactivee (vaut 0). commented_definition montre "
                "la ligne a decommenter pour l'activer; config_diff simule le changement",
    }


_PREDEFINED = frozenset(["_CVI_", "_NI_mswin_", "_WIN32", "WIN32", "_NI_mswin32_", "__STDC__", "_WIN64", "_NI_mswin64_", "_M_X64", "_M_AMD64", "_NI_i386_", "_M_IX86", "_CVI_DEBUG_"])


def _names(raw: str | None) -> list[str]:
    try:
        return [n for n in json.loads(raw or "[]") if n != "defined"]
    except ValueError:
        return []


# ------------------------------------------------------------- config_diff ------

def config_diff(engine: QueryEngine, cfg: Config, forced: dict, limit: int = 40) -> dict:
    """Ce que change une autre valeur de macros, sans toucher aux fichiers:
    branches #if qui basculent, fonctions et globales qui apparaissent ou
    disparaissent, macros derivees et tableaux qui changent de taille."""
    base = engine.project
    other = load(cfg, forced)
    before, after = _state_map(base), _state_map(other)
    lines_before, lines_after = _inactive(base), _inactive(other)

    blocks = []
    for path, branches in after.items():
        for line, state in sorted(branches.items()):
            old = before.get(path, {}).get(line, INACTIVE)
            if old != state:
                branch = other.pp.results[path].branches[line]
                blocks.append({"file": engine.rel(path), "line": line,
                               "directive": f"#{branch.kind} {branch.expr}".strip(),
                               "before": STATE_NAMES[old], "after": STATE_NAMES[state]})

    appear, vanish = [], []
    used_on: dict[str, int] = {}
    used_off: dict[str, int] = {}
    touched: dict[tuple, dict] = {}
    ids = {r["path"]: r["id"] for r in engine.db.execute("SELECT id, path FROM files")}
    for path in sorted(set(lines_before) | set(lines_after)):
        on = lines_before.get(path, set()) - lines_after.get(path, set())
        off = lines_after.get(path, set()) - lines_before.get(path, set())
        if not (on or off) or path not in ids:
            continue
        for r in engine.db.execute(
            "SELECT name, kind, line_start FROM symbols WHERE file_id = ? "
            "AND kind IN ('function', 'global', 'macro', 'prototype', 'enumerator') "
            "ORDER BY line_start", (ids[path],),
        ):
            item = {"name": r["name"], "kind": r["kind"], "file": engine.rel(path), "line": r["line_start"]}
            if r["line_start"] in on:
                appear.append(item)
            elif r["line_start"] in off:
                vanish.append(item)
        for r in engine.db.execute(
            "SELECT name, kind, line, enclosing FROM refs WHERE file_id = ? "
            "AND kind NOT IN ('cond', 'return')", (ids[path],),
        ):
            side = "on" if r["line"] in on else "off" if r["line"] in off else None
            if side is None:
                continue
            counter = used_on if side == "on" else used_off
            counter[r["name"]] = counter.get(r["name"], 0) + 1
            if r["enclosing"]:
                key = (r["enclosing"], path)
                entry = touched.setdefault(key, {"function": r["enclosing"], "file": engine.rel(path),
                                                 "lines_compiled_in": set(), "lines_compiled_out": set()})
                entry["lines_compiled_in" if side == "on" else "lines_compiled_out"].add(r["line"])

    derived = []
    for name in sorted(set(base.pp.env) | set(other.pp.env)):
        a, b = base.pp.env.get(name), other.pp.env.get(name)
        if a is not None and b is not None and a.params is None and b.params is None:
            va, vb = evaluate_value(a.value, base.pp.env), evaluate_value(b.value, other.pp.env)
            if va != vb and (va is not None or vb is not None):
                derived.append({"name": name, "before": va, "after": vb})
        elif (a is None) != (b is None):
            derived.append({"name": name, "before": a.value if a else "non defini",
                            "after": b.value if b else "non defini"})

    changed = {d["name"] for d in derived}
    arrays = []
    if changed:
        holders = ",".join("?" for _ in changed)
        for r in engine.db.execute(
            "SELECT r.name, r.line, r.enclosing, f.path FROM refs r JOIN files f ON r.file_id = f.id "
            f"WHERE r.kind = 'dim' AND r.name IN ({holders}) ORDER BY f.path, r.line",
            sorted(changed),
        ):
            arrays.append({"array_or_function": r["enclosing"], "dimension": r["name"],
                           "file": engine.rel(r["path"]), "line": r["line"]})

    functions = []
    for entry in touched.values():
        functions.append({"function": entry["function"], "file": entry["file"],
                          "lines_compiled_in": len(entry["lines_compiled_in"]),
                          "lines_compiled_out": len(entry["lines_compiled_out"])})
    functions.sort(key=lambda f: -(f["lines_compiled_in"] + f["lines_compiled_out"]))
    names_on = [{"name": n, "uses": c} for n, c in sorted(used_on.items(), key=lambda kv: (-kv[1], kv[0]))]
    names_off = [{"name": n, "uses": c} for n, c in sorted(used_off.items(), key=lambda kv: (-kv[1], kv[0]))]
    flips: dict[str, int] = {}
    for b in blocks:
        flips[b["file"]] = flips.get(b["file"], 0) + 1
    return {
        "forced": forced,
        "configuration": base.ctx.config_name,
        "summary": {
            "branches_switched": len(blocks),
            "by_file": dict(sorted(flips.items(), key=lambda kv: -kv[1])),
            "symbols_appearing": len(appear),
            "symbols_disappearing": len(vanish),
            "macros_changing": len(derived),
            "arrays_resized": len(arrays),
            "functions_affected": len(functions),
            "names_newly_used": len(names_on),
            "names_no_longer_used": len(names_off),
        },
        "functions_affected": page(functions, 0, limit),
        "names_newly_used": page(names_on, 0, limit),
        "names_no_longer_used": page(names_off, 0, limit),
        "branches": page(blocks, 0, limit),
        "symbols_appearing": page(appear, 0, limit),
        "symbols_disappearing": page(vanish, 0, limit),
        "macros_changing": page(derived, 0, limit),
        "arrays_resized": page(arrays, 0, limit),
        "note": "simulation sur les directives: aucun fichier modifie. Les numeros de "
                "ligne sont ceux des fichiers actuels",
    }


