# ==============================================================================
#  cvi_mcp/uimap.py
#  Role     Vue synthetique interface: panneau, controle, callback, appels
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import re
import sqlite3
from pathlib import Path

# ============================ Interne =========================================
from .cparse import read_source
from .queries import QueryEngine, page

# Un .uir est binaire: le lien avec le code passe uniquement par les constantes
# du .h genere. On ne devine donc rien du fichier d'interface lui meme.
_INTEGER = re.compile(r"^-?(?:0[xX][0-9a-fA-F]+|\d+)$")

# En-tete que CVI genere pour chaque .uir. Il decrit panneaux, controles,
# type de chaque controle et callback cablee dans l'editeur: c'est la source
# fiable quand elle existe.
_UIR_BANNER = "User Interface Resource (UIR) Include File"
_UIR_DEFINE = re.compile(r"^#define\s+(\w+)\s+(-?\d+)\s*(?:/\*\s*(.*?)\s*\*/)?\s*$")
_UIR_SECTION = re.compile(r"^/\*\s*([^*]+?):\s*\*/$")
_UIR_CONTROL = re.compile(r"control type:\s*([^,]+?)\s*,\s*callback function:\s*(.+)$")
# Controles d'habillage: ne pas les compter comme inutilises.
_DECORATIVE = {"deco", "textmsg", "splitter", "picture"}
_UIR_CALLBACK = re.compile(r"callback function:\s*(.+)$")


class UiMap:
    """Reconstruit le cablage de l'interface a partir des arguments d'appel.

    Aucune liste d'API CVI n'est codee en dur: un argument qui est une
    constante entiere est une cible d'interface, un argument qui est le nom
    d'une fonction du projet est une callback. La relation entre les deux
    dans un meme appel donne le cablage.
    """

    def __init__(self, engine: QueryEngine) -> None:
        self.db = engine.db
        self.engine = engine
        self.constants = self._load_constants()
        self.functions = self._load_names("kind IN ('function', 'prototype')")
        self.callbacks = self._load_names("is_callback = 1")
        self.calls = self._load_calls()
        self.uir = self._read_uir_headers()

    # ------------------------------------------------------------- chargement --

    def _load_constants(self) -> dict[str, dict]:
        rows = self.db.execute(
            "SELECT s.name, s.value, s.line_start, f.path FROM symbols s "
            "JOIN files f ON s.file_id = f.id "
            "WHERE s.kind IN ('macro', 'enumerator') AND s.value IS NOT NULL"
        ).fetchall()
        return {
            row["name"]: {
                "value": row["value"],
                "file": self.engine.rel(row["path"]),
                "line": row["line_start"],
            }
            for row in rows
            if _INTEGER.match(row["value"].strip())
        }

    def _load_names(self, condition: str) -> set[str]:
        return {
            row[0]
            for row in self.db.execute(
                f"SELECT DISTINCT name FROM symbols WHERE ({condition}) AND active = 1")
        }

    def _load_calls(self) -> list[dict]:
        rows = self.db.execute(
            "SELECT r.id, r.name, r.line, r.enclosing, f.path, a.pos, a.text "
            "FROM refs r JOIN files f ON r.file_id = f.id "
            "LEFT JOIN call_args a ON a.ref_id = r.id "
            "WHERE r.kind = 'call' AND r.active = 1 ORDER BY r.id, a.pos"
        ).fetchall()

        grouped: dict[int, dict] = {}
        for row in rows:
            entry = grouped.setdefault(
                row["id"],
                {
                    "callee": row["name"],
                    "line": row["line"],
                    "in_function": row["enclosing"],
                    "file": self.engine.rel(row["path"]),
                    "args": [],
                },
            )
            if row["text"] is not None:
                entry["args"].append(row["text"])
        return list(grouped.values())

    def _read_uir_headers(self) -> dict | None:
        """Panneaux, controles et callbacks lus dans les .h generes par CVI.
        None si le projet n'en contient aucun."""
        panels: set[str] = set()
        owners: dict[str, str] = {}
        controls: dict[str, dict] = {}
        callbacks: dict[str, list[str]] = {}
        headers: list[str] = []

        for (path,) in self.db.execute("SELECT path FROM files WHERE path LIKE '%.h'"):
            try:
                text, _encoding, _raw = read_source(Path(path))
            except OSError:
                continue
            if _UIR_BANNER not in text[:800]:
                continue
            headers.append(self.engine.rel(path))
            section, current = "", None
            for number, line in enumerate(text.splitlines(), start=1):
                stripped = line.strip()
                header = _UIR_SECTION.match(stripped)
                if header:
                    section = header.group(1).lower()
                    continue
                match = _UIR_DEFINE.match(stripped)
                if not match:
                    continue
                name, value, comment = match.group(1), match.group(2), match.group(3) or ""
                self.constants.setdefault(
                    name, {"value": value, "file": self.engine.rel(path), "line": number}
                )
                cb_match = _UIR_CALLBACK.search(comment)
                callback = cb_match.group(1).strip() if cb_match else None
                if callback == "(none)":
                    callback = None
                if callback:
                    callbacks.setdefault(callback, []).append(name)
                if "panels" not in section:
                    continue
                control = _UIR_CONTROL.search(comment)
                if control and current:
                    owners[name] = current
                    controls[name] = {"type": control.group(1), "callback": callback}
                elif not control:
                    panels.add(name)
                    current = name
                    if callback:
                        controls[name] = {"type": "panel", "callback": callback}

        if not panels:
            return None
        return {
            "headers": headers,
            "panels": panels,
            "owners": owners,
            "controls": controls,
            "callbacks": callbacks,
        }

    # ---------------------------------------------------------- classification -

    def _panels_and_controls(self) -> tuple[set[str], dict[str, str]]:
        """Separe panneaux et controles. Un panneau est une constante dont une
        autre constante derive par prefixe, ou qui est passee a une fonction
        de chargement de panneau."""
        names = set(self.constants)
        panels: set[str] = set()

        for name in names:
            prefix = name + "_"
            if any(other.startswith(prefix) for other in names):
                panels.add(name)

        for call in self.calls:
            if "panel" not in call["callee"].lower():
                continue
            for arg in call["args"]:
                if arg in names:
                    panels.add(arg)

        owners: dict[str, str] = {}
        for name in names - panels:
            candidates = [p for p in panels if name.startswith(p + "_")]
            if candidates:
                owners[name] = max(candidates, key=len)
        return panels, owners

    # -------------------------------------------------------------- resultat --

    def build(self, panel: str | None = None, summary: bool = False) -> dict:
        if self.uir:
            panels, owners = self.uir["panels"], self.uir["owners"]
        else:
            panels, owners = self._panels_and_controls()
        used = {arg for call in self.calls for arg in call["args"]}

        wiring: dict[str, list[dict]] = {}
        access: dict[str, list[dict]] = {}
        loads: dict[str, list[dict]] = {}
        floating: list[dict] = []
        wired_callbacks: set[str] = set()
        # Cibles d'interface: constantes des .h generes quand on les a, sinon
        # toute constante entiere sauf les valeurs generiques (NULL, TRUE).
        if self.uir:
            ui_names = set(self.uir["panels"]) | set(self.uir["owners"]) | set(self.uir["controls"])
        else:
            ui_names = {c for c in self.constants if c not in _GENERIC_CONSTANTS}

        for call in self.calls:
            targets = [a for a in call["args"] if a in ui_names]
            handlers = [a for a in call["args"] if a in self.functions and a != call["callee"]]
            wired_callbacks.update(handlers)

            if handlers and not targets:
                for handler in handlers:
                    floating.append(
                        {
                            "callback": handler,
                            "kind": _registration_kind(call["callee"]),
                            "via": call["callee"],
                            "in_function": call["in_function"],
                            "file": call["file"],
                            "line": call["line"],
                        }
                    )
                continue

            for target in targets:
                record = {
                    "via": call["callee"],
                    "in_function": call["in_function"],
                    "file": call["file"],
                    "line": call["line"],
                }
                if target in panels and "panel" in call["callee"].lower():
                    loads.setdefault(target, []).append(record)
                elif handlers:
                    for handler in handlers:
                        wiring.setdefault(target, []).append(
                            {**record, "callback": handler, "callback_calls": self._chain(handler)}
                        )
                else:
                    access.setdefault(target, []).append(record)

        missing: list[dict] = []
        if self.uir:
            # Cablage fait dans l'editeur d'interface, lu dans le .h genere.
            for callback, names in sorted(self.uir["callbacks"].items()):
                wired_callbacks.add(callback)
                if callback not in self.functions:
                    missing.append({"callback": callback, "used_by": sorted(names)})
                for name in names:
                    info = self.constants.get(name, {})
                    wiring.setdefault(name, []).append(
                        {
                            "via": "uir",
                            "file": info.get("file"),
                            "line": info.get("line"),
                            "callback": callback,
                            "callback_calls": self._chain(callback),
                        }
                    )

        result_panels = []
        for name in sorted(panels):
            if panel and name != panel:
                continue
            controls = sorted(c for c, owner in owners.items() if owner == name)
            result_panels.append(
                {
                    "constant": name,
                    "value": self.constants[name]["value"],
                    "defined_in": {
                        "file": self.constants[name]["file"],
                        "line": self.constants[name]["line"],
                    },
                    "loaded_by": loads.get(name, []),
                    "callback": [w["callback"] for w in wiring.get(name, [])],
                    "controls": [
                        {
                            "constant": control,
                            "type": (self.uir or {}).get("controls", {}).get(control, {}).get("type"),
                            "value": self.constants[control]["value"],
                            "line": self.constants[control]["line"],
                            "wired_to": wiring.get(control, []),
                            "accessed_by": access.get(control, []),
                            "unused_in_code": self._unused(control, used, wiring),
                        }
                        for control in controls
                    ],
                }
            )

        # Tables de pointeurs de fonctions et passages en argument a une
        # fonction du projet: installation indirecte.
        for row in self.db.execute(
            "SELECT r.name, r.kind, r.line, r.enclosing, f.path FROM refs r JOIN files f "
            "ON r.file_id = f.id WHERE r.active = 1 AND r.kind IN ('decl', 'ref')"
        ):
            name = row["name"]
            if name not in self.callbacks or name in wired_callbacks:
                continue
            wired_callbacks.add(name)
            floating.append({
                "callback": name,
                "kind": "pointer_table" if row["kind"] == "decl" else "passed_as_argument",
                "via": row["enclosing"],
                "in_function": row["enclosing"] if row["kind"] == "ref" else None,
                "file": self.engine.rel(row["path"]),
                "line": row["line"],
                "confidence": "moyenne",
            })

        reason = (
            "cablee dans aucun controle des .uir indexes ni enregistree par le code"
            if self.uir
            else "jamais installee par le code: probablement cablee dans le .uir"
        )
        excluded = {r[0] for r in self.db.execute(
            "SELECT DISTINCT name FROM refs WHERE active = 0 AND kind IN ('call', 'ref', 'decl')")}
        unwired = []
        for name in sorted(self.callbacks - wired_callbacks):
            item = {**self._locate_symbol(name), "declared_in": self._declared_in(name),
                    "reason": reason, "confidence": "haute" if self.uir else "faible"}
            if name in excluded:
                item["reason"] = "installee seulement dans du code exclu par un #if"
            unwired.append(item)

        variants, unused_by_type = [], {}
        if summary and self.uir:
            variants = _variants(result_panels)
            for p in result_panels:
                for c in p["controls"]:
                    if c["unused_in_code"]:
                        kind = c.get("type") or "?"
                        unused_by_type[kind] = unused_by_type.get(kind, 0) + 1

        if summary:
            # Resume: une ligne par panneau, le detail se demande panneau par panneau.
            # Sur un vrai projet la carte complete depasse le megaoctet.
            result_panels = [_summary(p) for p in result_panels]

        answer = {
            "source": "uir_headers" if self.uir else "heuristic",
            "uir_files": self._uir_files(),
            "panels": result_panels,
            "callbacks_reconciliation": {
                "declared_callbacks": len(self.callbacks),
                "wired_in_uir": len(self.uir["callbacks"]) if self.uir else 0,
                "registered_by_code": len({f["callback"] for f in floating}),
                "unwired": len(unwired),
                "uir_names_missing_in_code": len(missing),
            },
            "uir_callbacks_missing_in_code": missing,
            "callbacks_registered_by_code": floating,
            "callbacks_unwired": unwired,
        }
        if variants:
            answer["panel_variants"] = variants
        if unused_by_type:
            answer["unused_controls_by_type"] = dict(
                sorted(unused_by_type.items(), key=lambda kv: -kv[1]))
        if panel and not result_panels:
            answer["error"] = f"Panneau '{panel}' introuvable parmi {sorted(panels)}"
        return answer

    def _unused(self, control: str, used: set[str], wiring: dict) -> bool:
        """Ni reference dans le code, ni callback, ni simple habillage."""
        if control in used or wiring.get(control):
            return False
        kind = ((self.uir or {}).get("controls", {}).get(control) or {}).get("type") or ""
        return kind.lower() not in _DECORATIVE

    def _chain(self, callback: str) -> list[str]:
        """Fonctions appelees directement par une callback, projet d'abord."""
        rows = self.db.execute(
            "SELECT DISTINCT name FROM refs WHERE enclosing = ? AND kind = 'call' "
            "ORDER BY name",
            (callback,),
        ).fetchall()
        internal = [r[0] for r in rows if r[0] in self.functions]
        external = [r[0] for r in rows if r[0] not in self.functions]
        return internal + external

    def _declared_in(self, name: str) -> list[str]:
        """En-tetes qui declarent le prototype. Une callback declaree dans
        l'en-tete d'un .uir absent du projet se trahit ici."""
        rows = self.db.execute(
            "SELECT DISTINCT f.path FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE s.name = ? AND s.kind = 'prototype' ORDER BY f.path",
            (name,),
        )
        return [self.engine.rel(r[0]) for r in rows]

    def _locate_symbol(self, name: str) -> dict:
        row = self.db.execute(
            "SELECT s.line_start, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE s.name = ? ORDER BY CASE s.kind WHEN 'function' THEN 0 ELSE 1 END LIMIT 1",
            (name,),
        ).fetchone()
        if row is None:
            return {"name": name}
        return {"name": name, "file": self.engine.rel(row["path"]), "line": row["line_start"]}

    def _uir_files(self) -> list[str]:
        """Les .uir voisins des en-tetes indexes (CVI genere le .h a cote du
        .uir), plus ceux de la racine: couvre les dossiers hors du projet."""
        found: set[Path] = set()
        for (path,) in self.db.execute("SELECT path FROM files"):
            candidate = Path(path).with_suffix(".uir")
            if candidate.is_file():
                found.add(candidate.resolve())
        return sorted(self.engine.rel(str(p)) for p in found)


def build_ui_map(
    db_path: Path, root: Path | None, panel: str | None = None, summary: bool = False
) -> dict:
    engine = QueryEngine(db_path, root)
    try:
        return UiMap(engine).build(panel, summary)
    except sqlite3.Error as exc:
        return {"error": f"lecture de l'index impossible: {exc}"}
    finally:
        engine.close()


# Tris possibles du resume par panneau.
PANEL_SORTS = {
    "name": (lambda p: p["constant"], False),
    "controls": (lambda p: p["controls"], True),
    "unused": (lambda p: p["unused_in_code"], True),
    "callbacks": (lambda p: p["callbacks_total"], True),
    "wired": (lambda p: p["wired"], True),
    "file": (lambda p: (p["defined_in"], p["constant"]), False),
}


def paginate(answer: dict, contains: str | None, sort: str, limit: int, offset: int,
             uir: str | None = None) -> dict:
    """Filtre, trie et decoupe: les panneaux en mode resume, les controles du
    panneau demande en mode detail."""
    if "error" in answer or not answer.get("panels"):
        return answer
    needle = (contains or "").lower()
    detail = isinstance(answer["panels"][0].get("controls"), list)

    if detail:
        target = answer["panels"][0]
        controls = [c for c in target["controls"] if needle in c["constant"].lower()]
        target["controls_page"] = page(controls, offset, limit)
        target.pop("controls")
        return answer

    full = int(limit or 0) <= 0
    for p in answer["panels"]:
        every = p.pop("_all_callbacks", None)
        if full and every is not None:
            p["callbacks"] = every
    stem = (uir or "").lower().replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
    panels = [
        p for p in answer["panels"]
        if (needle in p["constant"].lower() or needle in p["defined_in"].lower())
        and (not stem or stem in p["defined_in"].lower())
    ]
    key, reverse = PANEL_SORTS.get(sort, PANEL_SORTS["name"])
    panels.sort(key=key, reverse=reverse)
    answer["totals"] = {
        "panels": len(answer["panels"]),
        "controls": sum(p["controls"] for p in answer["panels"]),
        "wired": sum(p["wired"] for p in answer["panels"]),
        "unused_in_code": sum(p["unused_in_code"] for p in answer["panels"]),
        "never_loaded": sum(1 for p in answer["panels"] if not p["loaded_by"]),
    }
    answer["panels"] = page(panels, offset, len(panels) if full else limit)
    return answer


def _registration_kind(api: str) -> str:
    """Nature d'une callback passee a une fonction sans controle d'interface,
    d'apres le nom de cette fonction: elle n'apparait pas dans un panneau."""
    lowered = api.lower()
    for word, kind in (
        ("timer", "timer"),
        ("deferred", "deferred_call"),
        ("thread", "thread"),
        ("tcp", "tcp"),
        ("udp", "udp"),
        ("server", "tcp"),
        ("list", "list_callback"),
        ("menu", "menu"),
        ("panel", "panel"),
    ):
        if word in lowered:
            return kind
    return "other"


_GENERIC_CONSTANTS = frozenset(["NULL", "TRUE", "FALSE", "EOF", "ON", "OFF", "YES", "NO"])


def _variants(panels: list[dict], limit: int = 15) -> list[dict]:
    """Panneaux qui sont des variantes l'un de l'autre (PANEL7 et PANEL12HD):
    meme jeu de noms de controles apres le prefixe du panneau. Signale ce qui
    existe dans l'un et pas dans l'autre."""
    names = {}
    for p in panels:
        prefix = p["constant"] + "_"
        names[p["constant"]] = {c["constant"].removeprefix(prefix) for c in p["controls"]}
    out = []
    keys = sorted(n for n in names if len(names[n]) >= 4)
    for i, a in enumerate(keys):
        for b in keys[i + 1:]:
            common = names[a] & names[b]
            union = names[a] | names[b]
            score = len(common) / len(union) if union else 0
            if score >= 0.6 and names[a] != names[b]:
                out.append({"panels": [a, b], "similarity": round(score, 2),
                            "only_in_first": sorted(names[a] - names[b])[:10],
                            "only_in_second": sorted(names[b] - names[a])[:10]})
            elif score == 1.0:
                out.append({"panels": [a, b], "similarity": 1.0, "identical_controls": True})
    out.sort(key=lambda v: -v["similarity"])
    return out[:limit]


def _summary(panel: dict) -> dict:
    controls = panel["controls"]
    callbacks = sorted({w["callback"] for c in controls for w in c["wired_to"]})
    return {
        "constant": panel["constant"],
        "defined_in": panel["defined_in"]["file"],
        "callback": panel["callback"],
        "loaded_by": sorted({x["in_function"] for x in panel["loaded_by"]}),
        "type_counts": _type_counts(controls),
        "controls": len(controls),
        "wired": sum(1 for c in controls if c["wired_to"]),
        "accessed": sum(1 for c in controls if c["accessed_by"]),
        "unused_in_code": sum(1 for c in controls if c["unused_in_code"]),
        "callbacks_total": len(callbacks),
        "callbacks": callbacks[:6],
        "_all_callbacks": callbacks,
    }


def _type_counts(controls: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for control in controls:
        if control.get("type"):
            counts[control["type"]] = counts.get(control["type"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1])[:4])


def ui_constants(engine: QueryEngine) -> tuple[set[str], set[str]]:
    """Panneaux et controles declares dans les .h generes, sans construire
    toute la carte. Mis en cache sur le moteur pour la duree d'un appel."""
    cached = getattr(engine, "_ui_constants", None)
    if cached is not None:
        return cached
    panels: set[str] = set()
    controls: set[str] = set()
    for (path,) in engine.db.execute("SELECT path FROM files WHERE origin = 'generated'"):
        try:
            text, _encoding, _raw = read_source(Path(path))
        except OSError:
            continue
        section = ""
        for line in text.splitlines():
            stripped = line.strip()
            header = _UIR_SECTION.match(stripped)
            if header:
                section = header.group(1).lower()
                continue
            match = _UIR_DEFINE.match(stripped)
            if not match or "panels" not in section:
                continue
            if _UIR_CONTROL.search(match.group(3) or ""):
                controls.add(match.group(1))
            else:
                panels.add(match.group(1))
    engine._ui_constants = (panels, controls)
    return panels, controls
