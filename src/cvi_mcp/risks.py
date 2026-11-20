# ==============================================================================
#  cvi_mcp/risks.py
#  Role     Suivi chemin par chemin des verrous et ressources, risques C
#           classiques. Positions et explications seulement, jamais de code.
#  Etat     stable
#  Version  0.4.0
# ==============================================================================

# ============================ Standard ========================================
import re
from pathlib import Path

# ============================ Interne =========================================
from .cparse import declarator_info, neutralize, new_parser, read_source, text_of, walk
from .preproc import evaluate_value
from .queries import QueryEngine

# Rang d'une confiance: plus petit = plus sur. Partage avec le serveur et avec
# threads.py pour que le tri et le seuil min_confidence parlent la meme langue.
CONFIDENCE_RANK = {"haute": 0, "moyenne": 1, "faible": 2}

# Les alertes de risks, de la plus grave a la moins grave. A confiance egale c'est cet
# ordre qui decide de ce qui reste quand la reponse est coupee par le budget de taille:
# une fuite ou un plantage ne doit pas disparaitre derriere deux cents copies de chaine.
CHECKS = (
    "leak_on_return", "double_release", "leak_at_end", "unchecked_alloc", "uninitialized_use",
    "unread_return", "ignored_return", "lock_gap", "inconsistent_lock", "ui_control_unlocked",
    "ui_from_thread", "unbounded_copy", "format_mismatch", "shared_global_unlocked",
)
CHECK_RANK = {name: rank for rank, name in enumerate(CHECKS)}

# Verrous: fonction de prise -> fonction de liberation. Le verrou est nomme
# par sa fonction de liberation et son premier argument. CmtTryToGetLock est
# exclu: sans lire son code retour on ne sait pas si le verrou est tenu.
LOCK_PAIRS = {
    "CmtGetLock": "CmtReleaseLock",
    "CmtGetLockEx": "CmtReleaseLock",
    "CmtGetTSVPtr": "CmtReleaseTSVPtr",
    "CmtGetTSQReadPtr": "CmtReleaseTSQReadPtr",
    "CmtGetTSQWritePtr": "CmtReleaseTSQWritePtr",
    "EnterCriticalSection": "LeaveCriticalSection",
    "WaitForSingleObject": "ReleaseMutex",
}

# Ressources: (prise, liberation, ou se trouve le handle). return: valeur de
# retour affectee a une variable; argN: N-ieme argument (& retire).
RESOURCE_PAIRS = [
    ("fopen", "fclose", "return"), ("_fdopen", "fclose", "return"),
    ("malloc", "free", "return"), ("calloc", "free", "return"), ("strdup", "free", "return"),
    ("_strdup", "free", "return"),
    ("OpenFile", "CloseFile", "return"),
    ("LoadPanel", "DiscardPanel", "return"), ("LoadPanelEx", "DiscardPanel", "return"),
    ("NewPanel", "DiscardPanel", "return"), ("DuplicatePanel", "DiscardPanel", "return"),
    ("LoadMenuBar", "DiscardMenuBar", "return"), ("NewMenuBar", "DiscardMenuBar", "return"),
    ("NewBitmap", "DiscardBitmap", "arg7"), ("GetBitmapFromFile", "DiscardBitmap", "arg1"),
    ("GetCtrlBitmap", "DiscardBitmap", "arg3"), ("GetPanelDisplayBitmap", "DiscardBitmap", "arg3"),
    ("GetCtrlDisplayBitmap", "DiscardBitmap", "arg3"),
    ("imaqCreateImage", "imaqDispose", "return"),
    ("CmtNewLock", "CmtDiscardLock", "arg2"), ("CmtNewTSV", "CmtDiscardTSV", "arg1"),
    ("CmtNewTSQ", "CmtDiscardTSQ", "arg4"),
    ("NewAsyncTimer", "DiscardAsyncTimer", "return"),
    ("Ini_New", "Ini_Dispose", "return"),
    ("ListCreate", "ListDispose", "return"),
    ("DBConnect", "DBDisconnect", "return"), ("DBActivateSQL", "DBDeactivateSQL", "return"),
    ("CreateFileA", "CloseHandle", "return"), ("CreateEventA", "CloseHandle", "return"),
]

# Appels qui peuvent attendre longtemps: dangereux verrou tenu.
BLOCKING_CALLS = frozenset(
    ["ProcessSystemEvents", "ProcessDrawEvents", "Delay", "Sleep", "SyncWait", "RunUserInterface", "ClientTCPRead", "ServerTCPRead", "ClientTCPWrite", "ServerTCPWrite", "ConnectToTCPServer", "TCPRead", "TCPWrite", "ComRd", "ComWrt", "ComRdTerm", "MessagePopup", "ConfirmPopup", "GenericMessagePopup", "PromptPopup", "FileSelectPopup", "FileSelectPopupEx", "DirSelectPopup", "CmtWaitForThreadPoolFunctionCompletion", "CmtWaitForThreadPoolFunctionCompletionEx", "WaitForSingleObject", "WaitForMultipleObjects"]
)

# Fonctions dont le code de retour signale un echec qu'on ne devrait pas ignorer.
CHECKED_CALLS = frozenset(
    ["LoadPanel", "LoadPanelEx", "OpenFile", "ConnectToTCPServer", "RegisterTCPServer", "ClientTCPRead", "ClientTCPWrite", "ServerTCPRead", "ServerTCPWrite", "Ini_ReadFromFile", "Ini_WriteToFile", "CmtScheduleThreadPoolFunction", "CmtScheduleThreadPoolFunctionAdv", "NewAsyncTimer", "DBConnect", "DBActivateSQL", "DBImmediateSQL", "DBFetchNext", "OpenComConfig", "fopen", "malloc", "calloc", "ReadFile", "WriteFile", "CopyFile", "DeleteFile", "MakeDir"]
)

# Famille printf: position de la chaine de format.
FORMAT_ARG = {"printf": 0, "sprintf": 1, "fprintf": 1, "snprintf": 2, "scanf": 0, "sscanf": 1,
              "fscanf": 1, "_snprintf": 2, "DebugPrintf": 0}

UNBOUNDED = {"strcpy": (0, 1), "strcat": (0, 1), "sprintf": (0, None), "vsprintf": (0, None),
             "gets": (0, None), "wcscpy": (0, 1), "lstrcpy": (0, 1)}

_FORMAT_SPEC = re.compile(r"%(%|[-+ #0]*(\*|\d+)?(\.(\*|\d+))?(hh|h|ll|l|L|z|j|t|I64|I32)?[diouxXeEfgGaAcspn])")
# Un echappement C vaut un caractere: \n, \033 (octal), \x1b (hexadecimal).
_C_ESCAPE = re.compile(r"\\(?:[0-7]{1,3}|x[0-9a-fA-F]+|.)")
_TYPE_SIZES = {"char": 1, "short": 2, "int": 4, "long": 4, "float": 4, "double": 8}


# ------------------------------------------------------------- sources ----------

def active_source(engine: QueryEngine, path: str) -> bytes:
    """Source tel que compile: lignes exclues par le preprocesseur effacees,
    positions conservees. Repli sur le source brut sans etat."""
    cache = getattr(engine, "_active_sources", None)
    if cache is None:
        cache = engine._active_sources = {}
    if path in cache:
        return cache[path]
    text, _enc, _raw = read_source(Path(path))
    rows = neutralize(text).split("\n")
    project = getattr(engine, "project", None)
    result = project.pp.results.get(str(Path(path).resolve())) if project and project.pp else None
    if result is not None:
        for number in result.blank_lines():
            if 0 < number <= len(rows):
                rows[number - 1] = " " * len(rows[number - 1])
    cache[path] = "\n".join(rows).encode("utf-8")
    return cache[path]


def function_nodes(engine: QueryEngine, path: str, wanted: set[str] | None = None):
    """(nom, noeud, source) des fonctions d'un fichier, code compile seulement."""
    source = active_source(engine, path)
    tree = new_parser().parse(source)
    for node in walk(tree.root_node):
        if node.type != "function_definition":
            continue
        declarator = node.child_by_field_name("declarator")
        name = declarator_info(declarator, source)[0] if declarator is not None else None
        if name and (wanted is None or name in wanted):
            yield name, node, source


def _args(call, source: bytes) -> list[str]:
    arguments = call.child_by_field_name("arguments")
    if arguments is None:
        return []
    return [" ".join(text_of(source, c).split()) for c in arguments.children if c.is_named]


def _callee(call, source: bytes) -> str:
    target = call.child_by_field_name("function")
    return text_of(source, target) if target is not None else ""


def _locals(node, source: bytes, env: dict) -> dict[str, dict]:
    """Variables locales et parametres: tableau ou non, taille en octets."""
    out: dict[str, dict] = {}
    declarator = node.child_by_field_name("declarator")
    for param in walk(declarator) if declarator is not None else []:
        if param.type == "parameter_declaration":
            inner = param.child_by_field_name("declarator")
            if inner is not None:
                name = re.sub(r"[\s*&\[\]0-9]", "", text_of(source, inner).split("[")[0])
                if name:
                    out[name] = {"param": True, "array": False, "pointer": "*" in text_of(source, param)}
    body = node.child_by_field_name("body")
    for decl in walk(body) if body is not None else []:
        if decl.type != "declaration":
            continue
        type_node = decl.child_by_field_name("type")
        base = text_of(source, type_node) if type_node is not None else "int"
        unit = next((v for k, v in _TYPE_SIZES.items() if k in base.split()), 4)
        for child in decl.children:
            if child.type not in ("identifier", "init_declarator", "array_declarator",
                                  "pointer_declarator"):
                continue
            target = child.child_by_field_name("declarator") if child.type == "init_declarator" else child
            if target is None:
                continue
            raw = text_of(source, target)
            name = re.match(r"[\s*]*([A-Za-z_]\w*)", raw)
            if not name:
                continue
            info = {"param": False, "array": target.type == "array_declarator",
                    "pointer": raw.lstrip().startswith("*"), "line": decl.start_point[0] + 1}
            if info["array"]:
                size = target.child_by_field_name("size")
                value = evaluate_value(text_of(source, size), env) if size is not None else None
                if value:
                    info["bytes"] = value * unit
                    info["size_text"] = text_of(source, size)
            out[name.group(1)] = info
    return out


# ------------------------------------------------------------ parcours ----------

class PathWalker:
    """Suit, chemin par chemin, ce qui est tenu (verrou, ressource).

    Les branches sont parcourues separement puis fusionnees au pire cas.
    Un goto coupe le suivi (fonction marquee verifiee en partie). Un return
    cache dans une macro est reconnu. Etat: cle -> [tenu, libere partout]."""

    def __init__(self, source: bytes, track, return_macros: set[str]) -> None:
        self.source = source
        self.track = track            # callable(call, state, walker) -> None
        self.return_macros = return_macros
        self.findings: list[dict] = []
        self.breaks: list[list] = []
        self.has_goto = False
        self.taken_at: dict[str, int] = {}
        self.order: list[tuple[str, str, int]] = []
        self.blocking: list[dict] = []

    @staticmethod
    def merge(*states):
        alive = [s for s in states if s is not None]
        if not alive:
            return None
        merged: dict[str, list] = {}
        keys = {k for s in alive for k in s}
        for key in keys:
            held = max(s.get(key, [0, False])[0] for s in alive)
            released = all(s.get(key, [0, False])[1] for s in alive)
            merged[key] = [held, released]
        return merged

    @staticmethod
    def copy(state):
        return None if state is None else {k: list(v) for k, v in state.items()}

    def calls(self, node, state):
        if state is None or node is None:
            return state
        nodes = [node] if node.type == "call_expression" else []
        nodes += [n for n in walk(node) if n.type == "call_expression"]
        for call in sorted(nodes, key=lambda n: n.start_byte):
            self.track(call, state, self)
        return state

    def exit(self, node, state, kind: str) -> None:
        for key, (held, _released) in state.items():
            if held > 0:
                self.findings.append({"kind": kind, "line": node.start_point[0] + 1, "key": key,
                                      "acquired_line": self.taken_at.get(key)})

    def run(self, body) -> None:
        end = self.stmt(body, {})
        if end is not None:
            for key, (held, _released) in end.items():
                if held > 0:
                    self.findings.append({"kind": "held_at_end", "line": body.end_point[0] + 1,
                                          "key": key, "acquired_line": self.taken_at.get(key)})

    def _macro_return(self, node) -> bool:
        if node.type != "expression_statement" or not self.return_macros:
            return False
        first = node.named_children[0] if node.named_children else None
        if first is None:
            return False
        name = _callee(first, self.source) if first.type == "call_expression" else text_of(self.source, first)
        return name.strip() in self.return_macros

    def stmt(self, node, state):
        if state is None or node is None:
            return state
        kind = node.type
        field = node.child_by_field_name

        if kind == "compound_statement":
            for child in node.named_children:
                state = self.stmt(child, state)
            return state
        if kind == "if_statement":
            condition = field("condition")
            state = self.calls(condition, state)
            then_state, else_state = self.copy(state), self.copy(state)
            self.on_condition(condition, then_state, else_state)
            then = self.stmt(field("consequence"), then_state)
            other = field("alternative")
            otherwise = self.stmt(other, else_state) if other is not None else else_state
            return self.merge(then, otherwise)
        if kind == "else_clause":
            for child in node.named_children:
                state = self.stmt(child, state)
            return state
        if kind in ("while_statement", "for_statement"):
            for name in ("initializer", "condition", "update"):
                state = self.calls(field(name), state)
            self.breaks.append([])
            looped = self.stmt(field("body"), self.copy(state))
            return self.merge(state, looped, *self.breaks.pop())
        if kind == "do_statement":
            self.breaks.append([])
            looped = self.stmt(field("body"), self.copy(state))
            looped = self.calls(field("condition"), looped)
            return self.merge(state, looped, *self.breaks.pop())
        if kind == "switch_statement":
            state = self.calls(field("condition"), state)
            self.breaks.append([])
            current, has_default = None, False
            body = field("body")
            for case in body.named_children if body is not None else []:
                if case.type != "case_statement":
                    current = self.stmt(case, current)
                    continue
                has_default = has_default or case.child_by_field_name("value") is None
                current = self.merge(self.copy(state), current)
                value = case.child_by_field_name("value")
                for child in case.named_children:
                    if child != value:
                        current = self.stmt(child, current)
            exits = self.breaks.pop()
            return self.merge(current, *exits, None if has_default else state)
        if kind in ("break_statement", "continue_statement"):
            if self.breaks:
                self.breaks[-1].append(self.copy(state))
            return None
        if kind == "return_statement" or self._macro_return(node):
            state = self.calls(node, state)
            self.on_return(node, state)
            self.exit(node, state, "return_while_held")
            return None
        if kind == "goto_statement":
            self.has_goto = True
            return None
        if kind == "labeled_statement":
            if state is None:
                state = {}
            for child in node.named_children:
                if child.type != "statement_identifier":
                    state = self.stmt(child, state)
            return state
        if kind in ("preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else"):
            # Branche restee incertaine apres simulation: deux variantes.
            skip = {field("condition"), field("name"), field("alternative")}
            main = self.copy(state)
            for child in node.named_children:
                if child not in skip:
                    main = self.stmt(child, main)
            other = field("alternative")
            if other is None:
                return main if kind == "preproc_else" else self.merge(main, state)
            return self.merge(main, self.stmt(other, self.copy(state)))
        state = self.calls(node, state)
        self.on_statement(node, state)
        return state

    # Points d'extension pour le suivi des ressources.
    def on_condition(self, condition, then_state, else_state) -> None:
        pass

    def on_return(self, node, state) -> None:
        pass

    def on_statement(self, node, state) -> None:
        pass


def _return_macros(engine: QueryEngine) -> set[str]:
    return {r[0] for r in engine.db.execute(
        "SELECT name FROM symbols WHERE kind = 'macro' AND value LIKE '%return%'")}


def _project_paths(engine: QueryEngine, names: list[str]) -> dict[str, set[str]]:
    """Fichiers et fonctions du projet qui appellent au moins un de ces noms."""
    marks = ",".join("?" for _ in names)
    out: dict[str, set[str]] = {}
    for row in engine.db.execute(
        "SELECT DISTINCT r.enclosing, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        f"WHERE r.kind = 'call' AND r.active = 1 AND f.origin = 'project' AND r.name IN ({marks})",
        names,
    ):
        out.setdefault(row["path"], set()).add(row["enclosing"])
    return out


def _handle(args: list[str], index: int) -> str:
    return args[index].lstrip("&").strip() if index < len(args) else ""


# ---------------------------------------------------------------- verrous -------

def _lock_wrappers(engine: QueryEngine, pairs: dict, blocking: frozenset, returns: set,
                   names: list[str]) -> tuple[dict, dict]:
    """Enveloppes de verrou et fichiers a suivre.

    Une enveloppe est une fonction qui prend le verrou recu en parametre et le
    garde, ou qui le rend sans l'avoir pris. Deux passes: une enveloppe peut
    en appeler une autre. Renvoie (enveloppes, fichier -> fonctions a suivre)."""
    wrappers: dict[str, tuple[str, int, str]] = {}
    targets = _project_paths(engine, names)
    for _round in range(2):
        for path, functions in targets.items():
            for fname, node, source in function_nodes(engine, path, functions):
                params = _param_names(node, source)
                walker = _lock_walker(source, pairs, wrappers, blocking, returns)
                walker.run(node.child_by_field_name("body"))
                for finding in walker.findings:
                    family, handle = _split_key(finding["key"])
                    if handle in params:
                        mode = "release" if finding["kind"] == "release_without_acquire" else "acquire"
                        if finding["kind"] in ("held_at_end", "return_while_held", "release_without_acquire"):
                            wrappers[fname] = (family, params.index(handle), mode)
        targets = _project_paths(engine, names + list(wrappers))
    return wrappers, targets



def lock_check(engine: QueryEngine, pairs: dict[str, str] | None = None,
               blocking: frozenset | None = None) -> dict:
    """Verrous suivis chemin par chemin sur le code compile. Fonctions qui
    prennent ou rendent un verrou recu en parametre reconnues comme
    enveloppes; ordre de prise de plusieurs verrous (risque d'interblocage);
    appels bloquants verrou tenu."""
    pairs = {**LOCK_PAIRS, **(pairs or {})}
    blocking = blocking or BLOCKING_CALLS
    release = set(pairs.values())
    returns = _return_macros(engine)
    names = list(pairs) + list(release)
    totals = {r["name"]: r["total"] for r in engine.db.execute(
        f"SELECT name, COUNT(*) AS total FROM refs WHERE kind = 'call' AND active = 1 "
        f"AND name IN ({','.join('?' for _ in names)}) GROUP BY name", names)}
    wrappers, targets = _lock_wrappers(engine, pairs, blocking, returns, names)

    findings, partial, order_edges, blocked = [], [], {}, []
    checked = 0
    for path, functions in targets.items():
        for fname, node, source in function_nodes(engine, path, functions):
            if fname in wrappers:
                continue
            checked += 1
            walker = _lock_walker(source, pairs, wrappers, blocking, returns)
            walker.run(node.child_by_field_name("body"))
            reasons = []
            if walker.has_goto:
                reasons.append("goto: chemins apres le saut non suivis")
            if node.has_error:
                reasons.append("syntaxe non reconnue malgre le preprocesseur: arbre approximatif")
            if reasons:
                partial.append({"function": fname, "file": engine.rel(path), "reasons": reasons})
            for item in walker.findings:
                entry = {"function": fname, "file": engine.rel(path), "kind": item["kind"],
                         "line": item["line"], "lock": item["key"]}
                if item.get("acquired_line"):
                    entry["acquired_line"] = item["acquired_line"]
                entry["confidence"] = "faible" if node.has_error else (
                    "moyenne" if walker.has_goto else "haute")
                findings.append(entry)
            for first, second, line in walker.order:
                order_edges.setdefault((first, second), []).append(
                    {"function": fname, "file": engine.rel(path), "line": line})
            for item in walker.blocking:
                blocked.append({"function": fname, "file": engine.rel(path), **item})

    inversions = []
    for (a, b), where in order_edges.items():
        if (b, a) in order_edges and a < b:
            inversions.append({"locks": [a, b], "a_then_b": where[:3],
                               "b_then_a": order_edges[(b, a)][:3]})

    rank = {"return_while_held": 0, "held_at_end": 1, "release_without_acquire": 2}
    findings.sort(key=lambda f: (f["confidence"] != "haute", rank.get(f["kind"], 3), f["file"], f["line"]))
    counts: dict[str, int] = {}
    for item in findings:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1

    balance = []
    for rel in dict.fromkeys(pairs.values()):
        acquires = [a for a, r in pairs.items() if r == rel and totals.get(a)]
        if not acquires and not totals.get(rel):
            continue
        balance.append({"acquire": " + ".join(acquires) or "-",
                        "calls": sum(totals.get(a, 0) for a in acquires),
                        "release": rel, "release_calls": totals.get(rel, 0)})
    return {
        "balance": balance,
        "functions_checked": checked,
        "wrappers": [{"function": k, "family": v[0], "handle_param": v[1], "role": v[2]}
                     for k, v in sorted(wrappers.items())],
        "counts": counts,
        "lock_order_inversions": inversions,
        "blocking_calls_while_held": blocked,
        "findings": findings,
        "partially_checked": sorted(partial, key=lambda p: (p["file"], p["function"])),
    }


def _param_names(node, source: bytes) -> list[str]:
    declarator = node.child_by_field_name("declarator")
    _name, params = declarator_info(declarator, source) if declarator is not None else (None, [])
    out = []
    for param in params:
        match = re.search(r"([A-Za-z_]\w*)\s*(\[[^\]]*\])?\s*$", param)
        out.append(match.group(1) if match else "")
    return out


def _split_key(key: str) -> tuple[str, str]:
    family, _sep, handle = key.partition("(")
    return family, handle.rstrip(")")


def _lock_walker(source, pairs, wrappers, blocking, returns, walker_class=None) -> PathWalker:
    release = set(pairs.values())

    def track(call, state, walker: PathWalker) -> None:
        name = _callee(call, source)
        line = call.start_point[0] + 1
        args = _args(call, source)
        mode = None
        if name in pairs:
            family, handle, mode = pairs[name], _handle(args, 0), "acquire"
        elif name in release:
            family, handle, mode = name, _handle(args, 0), "release"
        elif name in wrappers:
            family, index, mode = wrappers[name]
            handle = _handle(args, index)
        elif name in blocking:
            # Le verrou s'affiche par son nom (giVerrou...), pas par la cle interne
            # "<famille de liberation>(<verrou>)".
            held = [_split_key(k)[1] for k, (count, _r) in state.items() if count > 0]
            if held:
                walker.blocking.append({"call": name, "line": line, "locks_held": held})
            return
        if mode is None:
            return
        key = f"{family}({handle})"
        if mode == "acquire":
            for other, (count, _r) in state.items():
                if count > 0 and other != key:
                    walker.order.append((other, key, line))
            entry = state.setdefault(key, [0, False])
            entry[0] += 1
            entry[1] = False
            walker.taken_at[key] = line
        else:
            entry = state.get(key)
            if entry and entry[0] > 0:
                entry[0] -= 1
                entry[1] = entry[0] == 0
            else:
                walker.findings.append({"kind": "release_without_acquire", "line": line, "key": key})

    return (walker_class or PathWalker)(source, track, returns)


# ---------------------------------------------- verrous tenus, ligne par ligne -----

class HeldLines:
    """Verrous tenus a chaque position d'une fonction.

    Une instruction occupe un intervalle (ligne, colonne): trois instructions
    sur une meme ligne (CmtGetLock(l); x = 1; CmtReleaseLock(l);) ne se
    confondent pas. Une instruction vue par plusieurs chemins garde
    l'intersection: protegee seulement si elle l'est sur tous les chemins."""

    def __init__(self) -> None:
        self._by_line: dict[int, list[tuple[tuple[int, int], tuple[int, int], frozenset]]] = {}

    def add(self, start: tuple[int, int], end: tuple[int, int], held: frozenset) -> None:
        for line in range(start[0], end[0] + 1):
            self._by_line.setdefault(line, []).append((start, end, held))

    def at(self, line: int, col: int | None = None) -> frozenset:
        """Verrous tenus en (ligne, colonne). Sans colonne, ceux qui le sont a
        toutes les instructions de la ligne."""
        spans = self._by_line.get(line, [])
        if col is not None:
            inside = [held for start, end, held in spans if start <= (line, col) <= end]
            if inside:
                return frozenset.intersection(*inside)
        return frozenset.intersection(*(held for _s, _e, held in spans)) if spans else frozenset()


class _HeldRecorder(PathWalker):
    """Suit les verrous comme lock_check et note, pour chaque instruction, ceux
    qui sont tenus apres elle. Les branches sont suivies chacune pour
    elle-meme: apres un retour anticipe qui rend le verrou, la suite du code
    reste sous verrou."""

    def __init__(self, source, track, return_macros) -> None:
        super().__init__(source, track, return_macros)
        self.held = HeldLines()

    def calls(self, node, state):
        state = super().calls(node, state)
        if node is not None and state is not None:
            held = frozenset(_split_key(key)[1] for key, (count, _r) in state.items() if count > 0)
            self.held.add((node.start_point[0] + 1, node.start_point[1]),
                          (node.end_point[0] + 1, node.end_point[1]), held)
        return state


def lock_map(engine: QueryEngine, pairs: dict[str, str] | None = None) -> dict[str, HeldLines]:
    """Pour chaque fonction qui touche a un verrou: les verrous tenus par position.

    Une fonction absente n'en prend aucun. Deux fonctions static de meme nom
    dans deux fichiers sont confondues (le nom seul identifie la fonction
    dans l'index). Sert a threads.py pour dire si un acces est protege."""
    # Reanalyser toutes les fonctions couteuses coute plusieurs secondes: avec les
    # paires par defaut, le resultat est garde avec le projet (project.memo).
    project = getattr(engine, "project", None)
    memo = project.memo if project is not None and pairs is None else None
    if memo is not None and "lock_map" in memo:
        return memo["lock_map"]
    pairs = {**LOCK_PAIRS, **(pairs or {})}
    returns = _return_macros(engine)
    names = list(pairs) + list(set(pairs.values()))
    wrappers, targets = _lock_wrappers(engine, pairs, BLOCKING_CALLS, returns, names)
    out: dict[str, HeldLines] = {}
    for path, functions in targets.items():
        for fname, node, source in function_nodes(engine, path, functions):
            if fname in wrappers:
                continue   # une enveloppe prend le verrou pour son appelant: pas de zone propre
            walker = _lock_walker(source, pairs, wrappers, BLOCKING_CALLS, returns, _HeldRecorder)
            walker.run(node.child_by_field_name("body"))
            out[fname] = walker.held
    if memo is not None:
        memo["lock_map"] = out
    return out


LOCK_NOTES = {
    "balance": "un ecart entre prises et liberations n'est pas une anomalie en soi: "
    "une prise rendue sur plusieurs branches compte plusieurs liberations",
    "return_while_held": "return atteint avec le verrou pris sur au moins un chemin",
    "held_at_end": "fin de fonction avec le verrou pris: voulu si la fonction sert a "
    "prendre le verrou (elle apparait alors dans wrappers), sinon oubli",
    "release_without_acquire": "liberation sans prise visible: normal si l'appelant a pris le verrou",
    "wrappers": "fonctions qui prennent ou rendent le verrou recu en parametre: leurs "
    "appels comptent comme prise ou liberation chez l'appelant",
    "lock_order_inversions": "deux verrous pris dans un ordre puis dans l'autre: risque "
    "d'interblocage si deux threads se croisent",
    "blocking_calls_while_held": "appel qui peut attendre (popup, TCP, ProcessSystemEvents) "
    "pendant qu'un verrou est tenu",
    "analysis": "fait sur le code compile: les #if sont resolus avant l'analyse",
}


# -------------------------------------------------------------- ressources -----

def _empty_summary() -> dict:
    """Ce qu'une fonction fait des ressources qu'elle recoit ou produit.

    releases: indice de parametre -> familles (nom de la prise) qu'elle libere.
    keeps: indices de parametres qu'elle garde (ranges hors de la fonction ou renvoyes).
    returns: familles qu'elle renvoie, prises dans la fonction et non liberees."""
    return {"releases": {}, "keeps": set(), "returns": set()}


class _ResourceWalker(PathWalker):
    """Ressource affectee a une variable locale: fichier, memoire, panneau,
    bitmap, handle. Un test d'invalidite (x == NULL, x < 0, !x) annule la
    ressource dans la branche ou elle n'existe pas; un return ou une copie
    hors de la fonction la fait sortir du suivi.

    Avec des resumes de fonctions (summaries), un handle passe a une fonction du
    projet n'echappe plus par principe: si la fonction le libere, il est libere;
    si elle le garde ou si on ne sait rien d'elle, il echappe; si elle ne fait que
    l'utiliser, il reste suivi et la fuite apres l'appel est vue. Une fonction qui
    renvoie une ressource prise (un fopen enrobe) en est une prise a son tour.
    Le walker note aussi le resume de la fonction qu'il parcourt (self.summary)."""

    def __init__(self, source, specs, locals_, project_functions, returns,
                 summaries=None, params=None) -> None:
        super().__init__(source, self._track, returns)
        self.specs = specs
        self.release = {}
        for acquire, release, where in specs:
            self.release.setdefault(release, []).append((acquire, where))
        self.locals = locals_
        self.project_functions = project_functions
        self.status: dict[str, str] = {}
        self.summaries = summaries or {}
        self.params = params or {}              # nom du parametre -> indice
        self.summary = _empty_summary()

    def _target(self, call, args, where) -> str | None:
        if where == "return":
            parent = call.parent
            while parent is not None and parent.type in ("parenthesized_expression", "cast_expression"):
                parent = parent.parent
            if parent is None:
                return None
            if parent.type == "assignment_expression":
                left = parent.child_by_field_name("left")
                return text_of(self.source, left).strip() if left is not None else None
            if parent.type == "init_declarator":
                left = parent.child_by_field_name("declarator")
                return re.sub(r"[\s*]", "", text_of(self.source, left)) if left is not None else None
            return None
        index = int(where[3:])
        return _handle(args, index) or None

    def _returned_directly(self, call) -> bool:
        """L'appel est la valeur d'un return: la ressource passe a l'appelant."""
        parent = call.parent
        while parent is not None and parent.type in ("parenthesized_expression", "cast_expression"):
            parent = parent.parent
        return parent is not None and parent.type == "return_statement"

    def _track(self, call, state, walker) -> None:
        name = _callee(call, self.source)
        args = _args(call, self.source)
        line = call.start_point[0] + 1
        for acquire, release, where in self.specs:
            if name == acquire:
                if where == "return" and self._returned_directly(call):
                    self.summary["returns"].add(acquire)
                target = self._target(call, args, where)
                if target and target in self.locals and not self.locals[target].get("param"):
                    key = f"{acquire}->{target}"
                    state[key] = [1, False]
                    self.taken_at[key] = line
                    if where != "return":
                        status = self._target(call, args, "return")
                        if status:
                            self.status[status] = key
                return
        if name in self.release and args:
            handle = _handle(args, 0)
            self._release(handle, [acquire for acquire, _w in self.release[name]], state, line)
            return
        if name in self.project_functions:
            self._call_project_function(call, name, args, state, line)

    def _release(self, handle: str, families: list[str], state, line: int) -> None:
        """Libere handle pour chaque famille: double liberation si c'est deja fait.
        Un parametre libere par la fonction est note dans son resume."""
        for family in families:
            key = f"{family}->{handle}"
            entry = state.get(key)
            if entry is None:
                if handle in self.params:
                    self.summary["releases"].setdefault(self.params[handle], set()).add(family)
                continue
            if entry[0] == 0 and entry[1]:
                self.findings.append({"kind": "double_release", "line": line, "key": key,
                                      "acquired_line": self.taken_at.get(key)})
            entry[0], entry[1] = 0, True

    def _call_project_function(self, call, name: str, args: list[str], state, line: int) -> None:
        summary = self.summaries.get(name)
        for index, arg in enumerate(args):
            handle = arg.lstrip("&")
            if summary is None:
                self._escape(handle, state)      # fonction inconnue: elle a pu garder le handle
                continue
            families = summary["releases"].get(index)
            if families:
                self._release(handle, sorted(families), state, line)
            if index in summary["keeps"]:
                self._escape(handle, state)
        if summary is None or not summary["returns"]:
            return
        # Fonction qui renvoie une ressource prise: c'est une prise pour l'appelant.
        if self._returned_directly(call):
            self.summary["returns"].update(summary["returns"])
            return
        target = self._target(call, args, "return")
        if target and target in self.locals and not self.locals[target].get("param"):
            for family in sorted(summary["returns"]):
                key = f"{family}->{target}"
                state[key] = [1, False]
                self.taken_at[key] = line

    def _escape(self, text: str, state, returned: bool = False) -> None:
        """Le handle sort du suivi: renvoye, range ailleurs ou confie a une fonction
        inconnue. On le note dans le resume (famille renvoyee, parametre garde)."""
        for key in list(state):
            family, _sep, handle = key.partition("->")
            if handle == text:
                if returned:
                    self.summary["returns"].add(family)
                del state[key]
        if text in self.params:
            self.summary["keeps"].add(self.params[text])

    def on_return(self, node, state) -> None:
        value = node.named_children[0] if node.named_children else None
        if value is not None:
            for ident in [value, *walk(value)]:
                if ident.type == "identifier":
                    self._escape(text_of(self.source, ident), state, returned=True)

    def on_statement(self, node, state) -> None:
        for item in [node, *walk(node)]:
            if item.type != "assignment_expression":
                continue
            right = item.child_by_field_name("right")
            left = item.child_by_field_name("left")
            if right is None or left is None or right.type != "identifier":
                continue
            target = text_of(self.source, left).strip()
            # Copie dans une globale, un champ, un tableau ou via un pointeur:
            # la ressource vit au-dela de la fonction.
            if not (target in self.locals and re.fullmatch(r"[A-Za-z_]\w*", target)):
                self._escape(text_of(self.source, right), state)

    def on_condition(self, condition, then_state, else_state) -> None:
        """x == NULL, !x, x < 0: ressource absente dans la branche alors.
        s == 0 ou s >= 0 sur le code retour s de la prise: ressource creee."""
        for var, then_valid in _condition_facts(condition, self.source):
            for key in list(then_state):
                acquired_var = key.split("->", 1)[1]
                status_of = self.status.get(var)
                if var == acquired_var:
                    valid = then_valid["handle"]
                elif status_of == key:
                    valid = then_valid["status"]
                else:
                    continue
                if valid is True:
                    else_state.pop(key, None)
                elif valid is False:
                    then_state.pop(key, None)


def _strip(node):
    while node is not None and node.type == "parenthesized_expression" and node.named_children:
        node = node.named_children[0]
    return node


def _var(node, source: bytes) -> str | None:
    node = _strip(node)
    if node is None:
        return None
    if node.type == "identifier":
        return text_of(source, node)
    if node.type == "assignment_expression":
        return _var(node.child_by_field_name("left"), source)
    return None


def _condition_facts(condition, source: bytes) -> list[tuple[str, dict]]:
    """Faits vrais dans la branche alors: pour chaque variable testee, la
    ressource est-elle valide si la variable est le handle, ou si c'est le
    code retour de la prise (0 ou positif: succes en CVI)."""
    node = _strip(condition)
    if node is None:
        return []
    if node.type == "binary_expression":
        op_node = node.child_by_field_name("operator")
        op = text_of(source, op_node) if op_node is not None else ""
        left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
        if op == "&&":
            return _condition_facts(left, source) + _condition_facts(right, source)
        zero = {"0", "NULL", "0L", "(void*)0", "FALSE"}
        lt, rt = _strip(left), _strip(right)
        if lt is None or rt is None:
            return []
        if " ".join(text_of(source, rt).split()) in zero:
            var = _var(lt, source)
        elif " ".join(text_of(source, lt).split()) in zero:
            var = _var(rt, source)
            op = {"<": ">", ">": "<", "<=": ">=", ">=": "<="}.get(op, op)
        else:
            return []
        if var is None:
            return []
        table = {"==": (False, True), "!=": (True, False), "<": (False, False),
                 "<=": (False, None), ">": (True, None), ">=": (None, True)}
        if op not in table:
            return []
        handle, status = table[op]
        return [(var, {"handle": handle, "status": status})]
    if node.type == "unary_expression" and text_of(source, node).lstrip().startswith("!"):
        var = _var(node.child_by_field_name("argument"), source)
        return [(var, {"handle": False, "status": True})] if var else []
    var = _var(node, source)
    return [(var, {"handle": True, "status": False})] if var else []


def _resource_summaries(nodes: list, specs: list, project_functions: set, returns: set,
                        env: dict) -> dict[str, dict]:
    """Resume de chaque fonction (ce qu'elle libere, garde ou renvoie).

    Une fonction depend de ceux qu'elle appelle: on rejoue le calcul jusqu'a ce que les
    resumes ne bougent plus (quatre tours au plus: la chaine d'appels qui transmet un
    handle est courte). Une fonction absente du resultat est inconnue et ses appelants
    la traitent comme prenant la propriete du handle."""
    summaries: dict[str, dict] = {}
    for _round in range(4):
        fresh: dict[str, dict] = {}
        for fname, node, source in nodes:
            params = {name: index for index, name in enumerate(_param_names(node, source)) if name}
            walker = _ResourceWalker(source, specs, _locals(node, source, env), project_functions,
                                     returns, summaries, params)
            walker.run(node.child_by_field_name("body"))
            fresh[fname] = walker.summary
        if fresh == summaries:
            break
        summaries = fresh
    return summaries


def resource_check(engine: QueryEngine, extra: list | None = None) -> dict:
    specs = RESOURCE_PAIRS + [tuple(x) for x in (extra or [])]
    names = sorted({s[0] for s in specs} | {s[1] for s in specs})
    project_functions = {r[0] for r in engine.db.execute(
        "SELECT name FROM symbols WHERE kind = 'function' AND active = 1")}
    returns = _return_macros(engine)
    env = engine.project.pp.env if getattr(engine, "project", None) else {}
    release_of = {spec[0]: spec[1] for spec in specs}
    # Fermetures de connexion par (fichier, fonction): un statement encore actif a
    # ce moment est la fuite la plus courante de la bibliotheque de base de donnees.
    disconnects: dict[tuple[str, str], list[int]] = {}
    for r in engine.db.execute(
        "SELECT r.enclosing, r.line, f.path FROM refs r JOIN files f ON r.file_id = f.id "
        "WHERE r.kind = 'call' AND r.active = 1 AND r.name = 'DBDisconnect' "
        "AND r.enclosing IS NOT NULL"
    ):
        disconnects.setdefault((r["path"], r["enclosing"]), []).append(r["line"])

    # Toutes les fonctions du projet: une fonction qui n'appelle que fprintf est une
    # fonction qui emprunte son parametre, et il faut le savoir pour voir la fuite apres elle.
    by_file: dict[str, set[str]] = {}
    for r in engine.db.execute(
        "SELECT s.name, f.path FROM symbols s JOIN files f ON s.file_id = f.id "
        "WHERE s.kind = 'function' AND s.active = 1 AND f.origin = 'project'"
    ):
        by_file.setdefault(r["path"], set()).add(r["name"])
    nodes = []
    for path, functions in by_file.items():
        nodes.extend((fname, node, source, path) for fname, node, source in
                     function_nodes(engine, path, functions))
    project = getattr(engine, "project", None)
    memo = project.memo if project is not None else {}
    if "resource_summaries" not in memo:
        memo["resource_summaries"] = _resource_summaries(
            [n[:3] for n in nodes], specs, project_functions, returns, env)
    summaries = memo["resource_summaries"]

    findings, checked = [], 0
    for fname, node, source, path in nodes:
        if not _touches_resources(node, source, names, summaries):
            continue
        checked += 1
        params = {name: index for index, name in enumerate(_param_names(node, source)) if name}
        walker = _ResourceWalker(source, specs, _locals(node, source, env), project_functions,
                                 returns, summaries, params)
        walker.run(node.child_by_field_name("body"))
        for item in walker.findings:
            acquire, _sep, var = item["key"].partition("->")
            kind = {"return_while_held": "leak_on_return", "held_at_end": "leak_at_end"}.get(
                item["kind"], item["kind"])
            findings.append({
                "check": kind, "function": fname, "file": engine.rel(path), "line": item["line"],
                "resource": acquire, "variable": var, "acquired_line": item.get("acquired_line"),
                "confidence": "faible" if node.has_error else (
                    "moyenne" if walker.has_goto else "haute"),
                "why": _resource_why(kind, acquire, var, release_of.get(acquire), item,
                                     disconnects.get((path, fname), [])),
            })
    return {"functions_checked": checked, "findings": findings}


def _touches_resources(node, source: bytes, names: list[str], summaries: dict) -> bool:
    """La fonction appelle-t-elle une prise ou une fonction qui en renvoie une? Les
    autres n'ont rien a rapporter (une liberation seule se note dans le resume)."""
    producers = {name for name, summary in summaries.items() if summary["returns"]}
    acquires = {spec for spec in names} | producers
    body = node.child_by_field_name("body")
    return any(c.type == "call_expression" and _callee(c, source) in acquires for c in walk(body))


def _resource_why(kind: str, acquire: str, variable: str, release: str | None, item: dict,
                  disconnects: list[int]) -> str:
    """Phrase qui nomme la liberation manquante et, pour un statement de base de
    donnees, la fermeture de connexion qui l'a laisse actif."""
    release = release or "la liberation"
    taken = item.get("acquired_line")
    where = f" ligne {taken}" if taken else ""
    if kind == "double_release":
        return f"{variable} ({acquire}{where}) est libere deux fois par {release}"
    text = f"{variable} ({acquire}{where}) "
    text += ("n'est jamais libere avant la fin de la fonction" if kind == "leak_at_end"
             else "est perdu sur ce return")
    text += f": {release} manquant"
    closing = [line for line in disconnects if (taken or 0) <= line <= item["line"]]
    if acquire == "DBActivateSQL" and closing:
        text += (f"; la connexion est fermee ligne {closing[0]} (DBDisconnect) alors que le "
                 "statement est encore actif")
    return text


# ---------------------------------------------------------------- risques C -----

def c_risks(engine: QueryEngine, checked_calls: frozenset | None = None) -> list[dict]:
    """Copies non bornees vers un tampon local, allocations et ouvertures non
    testees, codes de retour ignores, formats printf incoherents."""
    checked_calls = checked_calls or CHECKED_CALLS
    env = engine.project.pp.env if getattr(engine, "project", None) else {}
    char_globals = _char_globals(engine, env)
    names = sorted(set(UNBOUNDED) | set(FORMAT_ARG) | set(checked_calls))
    out: list[dict] = []
    for path, functions in _project_paths(engine, names).items():
        for fname, node, source in function_nodes(engine, path, functions):
            local = _locals(node, source, env)
            body = node.child_by_field_name("body")
            where = {"function": fname, "file": engine.rel(path)}
            calls = [n for n in walk(body) if n.type == "call_expression"]
            for call in calls:
                name = _callee(call, source)
                args = _args(call, source)
                line = call.start_point[0] + 1
                if name in UNBOUNDED and args:
                    out.extend(_unbounded(name, args, local, where, line, char_globals))
                if name in FORMAT_ARG:
                    item = _format(name, args, where, line)
                    if item:
                        out.append(item)
                if name in checked_calls and call.parent is not None \
                        and call.parent.type == "expression_statement":
                    out.append({**where, "check": "ignored_return", "line": line, "call": name,
                                "confidence": "haute",
                                "why": f"code de retour de {name} ignore: un echec passe inapercu"})
            out.extend(_unchecked_alloc(body, source, local, where))
            out.extend(_unread_return(body, source, local, where, checked_calls))
            out.extend(_uninitialized_use(body, source, local, where))
    return out


def _unbounded(name, args, local, where, line, char_globals=None) -> list[dict]:
    """Une copie sans borne vers un tableau local, ou rien quand on peut
    prouver qu'elle tient: litteral court, format borne, ou (strcpy) source
    qui est elle-meme un tableau char plus petit que le tampon."""
    dest_index, src_index = UNBOUNDED[name]
    dest = args[dest_index].lstrip("&").split("[")[0].strip()
    info = local.get(dest)
    if not info or not info.get("array") or not info.get("bytes"):
        return []
    confidence = "moyenne"
    if src_index is not None and src_index < len(args):
        source = args[src_index]
        if source.startswith('"') and len(source) - 2 < info["bytes"]:
            return []  # litteral plus court que le tampon
        # strcpy ecrase le tampon: une source de taille connue <= au tampon tient
        # toujours. strcat ajoute a un contenu inconnu: rien a prouver.
        if name in ("strcpy", "lstrcpy"):
            known = _source_bytes(source, local, char_globals or {})
            if known is not None and known <= info["bytes"]:
                return []
    elif name in ("sprintf", "vsprintf") and len(args) > 1:
        bound = _format_bound(args[1], args[2:])
        if bound is not None and bound <= info["bytes"]:
            return []  # format borne qui tient dans le tampon (NUL compris dans bound)
        floats = bool(re.search(r"%[^%a-zA-Z]*l?[fFeEgG]", args[1]))
        confidence = "faible" if bound is None else ("moyenne" if floats else "haute")
        nominal = _format_bound(args[1], args[2:], nominal=True)
        if bound is not None and nominal is not None and nominal <= info["bytes"]:
            # Ne deborde que si un entier depasse la largeur prevue par le format
            # (date, heure): possible en theorie seulement.
            return [{**where, "check": "unbounded_copy", "line": line, "call": name,
                     "buffer": dest, "buffer_bytes": info["bytes"], "confidence": "faible",
                     "why": f"{name} dans {dest}[{info.get('size_text')}]: tient avec les largeurs "
                            "du format, deborde seulement si un entier est plus large"}]
    return [{**where, "check": "unbounded_copy", "line": line, "call": name, "buffer": dest,
             "buffer_bytes": info["bytes"], "confidence": confidence,
             "why": f"{name} ecrit dans {dest}[{info.get('size_text')}] sans borne: "
                    "debordement si la source est plus longue"}]


def _format_bound(fmt: str, args: list[str], nominal: bool = False) -> int | None:
    """Taille maximale produite par un format litteral, NUL final compris,
    None si une chaine %s sans precision rend la taille imprevisible.
    nominal=True: un entier avec largeur fixe (%02d, %04i) compte sa largeur,
    pas l'etendue complete d'un int."""
    if not (fmt.startswith('"') and fmt.endswith('"')):
        return None
    body = fmt[1:-1]
    total = len(_C_ESCAPE.sub("x", _FORMAT_SPEC.sub("", body)))
    index = 0
    for match in _FORMAT_SPEC.finditer(body):
        spec = match.group(0)
        if match.group(1) == "%":
            total += 1
            continue
        conv = spec[-1]
        width = int(match.group(2)) if match.group(2) and match.group(2).isdigit() else 0
        precision = int(match.group(4)) if match.group(4) and match.group(4).isdigit() else None
        if conv == "s":
            if precision is None:
                literal = args[index] if index < len(args) else ""
                if not (literal.startswith('"') and literal.endswith('"')):
                    return None
                size = len(literal) - 2
            else:
                size = precision
        elif conv == "c":
            size = 1
        elif conv in "diouxX":
            big = (match.group(5) or "") in ("l", "ll", "I64")
            if conv in "di":
                size = 21 if big else 11          # signe compris
            elif conv == "u":
                size = 20 if big else 10
            elif conv in "xX":
                size = 16 if big else 8
            else:
                size = 22 if big else 11
            if nominal and width:
                size = 0                          # la largeur du format fait foi
        elif conv in "eEgGaA":
            size = 24 + (precision or 0)
        elif conv == "f":
            size = 48 + (precision if precision is not None else 6)
        else:
            size = 24
        total += max(size, width)
        index += 1 + (match.group(2) == "*") + (match.group(4) == "*")
    return total + 1


# ------------------------------------------------- tableaux de caracteres -------

_DIMENSION = re.compile(r"\[([^\]]*)\]")


def _char_globals(engine: QueryEngine, env: dict) -> dict[str, list[int]]:
    """Globales char[] du projet: nom -> taille de chaque dimension (en octets,
    un char vaut un octet). Sert a prouver qu'une copie tient dans son tampon.
    Les tableaux de pointeurs et les dimensions non numeriques sont ignores."""
    out: dict[str, list[int]] = {}
    for row in engine.db.execute(
        "SELECT s.name, s.signature FROM symbols s JOIN files f ON f.id = s.file_id "
        "WHERE s.kind = 'global' AND s.active = 1 AND f.origin = 'project'"
    ):
        signature = row["signature"] or ""
        head = signature.split("[", 1)[0]
        if "[" not in signature or "char" not in head.split() or "*" in head:
            continue
        dims = [evaluate_value(d, env) for d in _DIMENSION.findall(signature)]
        if dims and all(dims):
            out[row["name"]] = dims
    return out


def _source_bytes(expr: str, local: dict, char_globals: dict) -> int | None:
    """Taille garantie, NUL compris, de la chaine designee par expr: un tableau
    char local, ou une globale char[] (avec ses indices: g[i] est la ligne i
    d'un char g[N][M]). None quand on ne sait pas."""
    match = re.fullmatch(r"([A-Za-z_]\w*)((?:\s*\[[^\]]*\])*)", expr.strip())
    if not match:
        return None
    name, subscripts = match.groups()
    used = subscripts.count("[")
    info = local.get(name)
    if info is not None:
        # Un local masque la globale de meme nom. Ses dimensions multiples ne
        # sont pas suivies: seul un tableau a une dimension, sans indice, compte.
        return info["bytes"] if info.get("array") and info.get("bytes") and used == 0 else None
    dims = char_globals.get(name)
    return dims[-1] if dims and len(dims) - used == 1 else None


# -------------------------------------------------------------- regroupement -----

def group_findings(items: list[dict]) -> list[dict]:
    """Une alerte par fonction pour ce qui se repete.

    Vingt strcat vers un meme tampon de requete SQL sont un seul probleme (la
    somme peut deborder), pas vingt; huit codes de retour ranges dans le meme
    resCode et jamais lus, un seul oubli d'habitude. Le groupe garde la meilleure
    confiance de ses membres, la premiere ligne et la liste des lignes (douze
    au plus). Un unbounded_copy se groupe par tampon, un unread_return par variable."""
    groups: dict[tuple, list[dict]] = {}
    rest: list[dict] = []
    for item in items:
        if item["check"] == "unbounded_copy":
            groups.setdefault(("unbounded_copy", item["file"], item["function"], item["buffer"]),
                              []).append(item)
        elif item["check"] == "unread_return":
            groups.setdefault(("unread_return", item["file"], item["function"], item["variable"]),
                              []).append(item)
        else:
            rest.append(item)
    for key, members in groups.items():
        if len(members) == 1:
            rest.append(members[0])
            continue
        members.sort(key=lambda m: m["line"])
        calls: dict[str, int] = {}
        for member in members:
            calls[member["call"]] = calls.get(member["call"], 0) + 1
        best = min(members, key=lambda m: CONFIDENCE_RANK.get(m["confidence"], 2))
        head = {**members[0], "confidence": best["confidence"], "occurrences": len(members),
                "lines": [m["line"] for m in members][:12], "calls": calls}
        detail = ", ".join(f"{call} x{count}" for call, count in calls.items())
        if key[0] == "unbounded_copy":
            head["why"] = (f"{len(members)} ecritures ({detail}) "
                           f"dans {head['buffer']}[{head.get('buffer_bytes')}] sans borne: "
                           "leur total peut depasser le tampon")
        else:
            head["why"] = (f"{len(members)} codes de retour ({detail}) ranges dans "
                           f"{head['variable']} puis jamais lus: les echecs passent inapercus")
        rest.append(head)
    return rest


def _format(name, args, where, line) -> dict | None:
    index = FORMAT_ARG[name]
    if index >= len(args) or not args[index].startswith('"') or not args[index].endswith('"'):
        return None
    fmt = args[index][1:-1]
    expected = 0
    for match in _FORMAT_SPEC.finditer(fmt):
        if match.group(1) == "%":
            continue
        expected += 1 + (match.group(2) == "*") + (match.group(4) == "*")
    given = len(args) - index - 1
    if expected == given:
        return None
    return {**where, "check": "format_mismatch", "line": line, "call": name,
            "expected_args": expected, "given_args": given, "confidence": "haute",
            "why": "nombre d'arguments different du nombre de conversions du format"}


_RELEASES = {"free", "fclose", "CloseFile", "DiscardBitmap", "DiscardPanel"}
_ALLOC = {"malloc", "calloc", "realloc", "strdup", "_strdup", "fopen", "OpenFile"}


def _unchecked_alloc(body, source, local, where) -> list[dict]:
    """Variable recue d'une allocation ou ouverture puis utilisee sans test."""
    out = []
    for call in [n for n in walk(body) if n.type == "call_expression"]:
        name = _callee(call, source)
        if name not in _ALLOC:
            continue
        parent = call.parent
        while parent is not None and parent.type in ("cast_expression", "parenthesized_expression"):
            parent = parent.parent
        if parent is None:
            continue
        if parent.type == "assignment_expression":
            target = text_of(source, parent.child_by_field_name("left")).strip()
        elif parent.type == "init_declarator":
            target = re.sub(r"[\s*]", "", text_of(source, parent.child_by_field_name("declarator")))
        else:
            continue
        if target not in local:
            continue
        line = call.start_point[0] + 1
        statement = parent
        while statement is not None and not statement.type.endswith("statement") \
                and statement.type != "declaration":
            statement = statement.parent
        if statement is not None and statement.parent is not None and \
                statement.parent.type in ("if_statement", "while_statement"):
            continue  # if ((x = fopen(...)) == NULL)
        first_use, tested = None, False
        for ident in walk(body):
            if ident.type != "identifier" or text_of(source, ident) != target:
                continue
            at = ident.start_point[0] + 1
            if ident.start_byte <= call.end_byte or at < line:
                continue
            holder = ident.parent
            in_test = False
            while holder is not None and holder != body:
                if holder.type in ("if_statement", "while_statement", "conditional_expression",
                                   "for_statement") or (holder.type == "call_expression"
                                                         and _callee(holder, source) == "assert"):
                    cond = holder.child_by_field_name("condition")
                    if holder.type == "call_expression" or (
                            cond is not None and cond.start_byte <= ident.start_byte <= cond.end_byte):
                        in_test = True
                        break
                holder = holder.parent
            if in_test:
                tested = True
                break
            caller = ident.parent.parent if ident.parent is not None else None
            if caller is not None and caller.type == "call_expression" and \
                    _callee(caller, source) in _RELEASES:
                continue  # free(NULL) et fclose apres test sont sans danger ici
            first_use = at
            break
        if first_use and not tested:
            out.append({**where, "check": "unchecked_alloc", "line": line, "call": name,
                        "variable": target, "first_use": first_use, "confidence": "moyenne",
                        "why": f"resultat de {name} utilise ligne {first_use} sans test d'echec"})
    return out


# ------------------------------------------- usages d'une variable locale ---------

_LOOPS = ("for_statement", "while_statement", "do_statement")
# Noeuds qu'on traverse pour retrouver la variable derriere x.champ ou (x).
_TRANSPARENT = ("field_expression", "parenthesized_expression")


def _is_assigned_name(node, source: bytes) -> bool:
    """Le noeud est-il la cible d'un '=' simple, directement ou par un champ
    (x = 1, x.champ = 1)? Un champ par '->' lit le pointeur: ce n'est pas une ecriture."""
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        if node.parent.type == "field_expression":
            argument = node.parent.child_by_field_name("argument")
            if argument != node or b"->" in source[node.end_byte : node.parent.end_byte][:3]:
                return False
        node = node.parent
    holder = node.parent
    if holder is None or holder.type != "assignment_expression":
        return False
    left, right = holder.child_by_field_name("left"), holder.child_by_field_name("right")
    return left == node and right is not None and \
        source[left.end_byte : right.start_byte].strip() == b"="


def _name_uses(body, source: bytes, name: str) -> list[tuple]:
    """Chaque occurrence d'un identifiant local, dans l'ordre du source:
    (noeud, nature). Natures: decl (sa declaration), write (affectation simple),
    addr (&x: peut etre rempli par l'appele), sizeof (n'evalue rien), read."""
    uses = []
    for ident in walk(body):
        if ident.type != "identifier" or text_of(source, ident) != name:
            continue
        parent = ident.parent
        if parent is None:
            continue
        declared_here = parent.type == "init_declarator" and \
            parent.child_by_field_name("declarator") == ident
        if declared_here or parent.type in ("declaration", "pointer_declarator",
                                            "array_declarator"):
            kind = "decl"
        elif _address_taken(ident, source):
            kind = "addr"
        elif _is_va_init(ident, source):
            kind = "write"   # va_start(ap, dernier): la macro remplit ap
        elif any(a.type == "sizeof_expression" for a in _ancestors(ident)):
            kind = "sizeof"
        elif _is_assigned_name(ident, source):
            kind = "write"
        else:
            kind = "read"
        uses.append((ident, kind))
    return uses


def _address_taken(node, source: bytes) -> bool:
    """&x, &x.champ ou &x.a.b: l'appele peut remplir la variable."""
    while node.parent is not None and node.parent.type in _TRANSPARENT:
        if node.parent.type == "field_expression" and \
                (node.parent.child_by_field_name("argument") != node
                 or b"->" in source[node.end_byte : node.parent.end_byte][:3]):
            return False
        node = node.parent
    holder = node.parent
    return holder is not None and holder.type == "pointer_expression" and \
        text_of(source, holder).lstrip().startswith("&")


def _is_va_init(ident, source: bytes) -> bool:
    """Premier argument de va_start ou va_copy: ces macros initialisent la liste."""
    arguments = ident.parent
    if arguments is None or arguments.type != "argument_list":
        return False
    call = arguments.parent
    return call is not None and call.type == "call_expression" and \
        _callee(call, source) in ("va_start", "va_copy") and arguments.named_children[:1] == [ident]


def _ancestors(node):
    node = node.parent
    while node is not None:
        yield node
        node = node.parent


def _enclosing_loop(node):
    return next((a for a in _ancestors(node) if a.type in _LOOPS), None)


def _unread_return(body, source, local, where, checked_calls) -> list[dict]:
    """Code de retour range dans une variable puis jamais lu: l'echec passe inapercu
    comme si l'appel etait ignore (resCode = DBFetchNext(h); puis rien).

    Il y a deux cas. La variable n'est lue nulle part dans la fonction (haute), ou
    elle est reaffectee avant d'etre lue (moyenne). Une affectation dans une condition
    ou un argument est utilisee sur place: elle n'entre pas ici. Dans une boucle, une
    lecture ailleurs dans la meme boucle suffit: elle sert a l'iteration suivante."""
    out = []
    for call in [n for n in walk(body) if n.type == "call_expression"]:
        name = _callee(call, source)
        if name not in checked_calls:
            continue
        holder = call.parent
        while holder is not None and holder.type in ("cast_expression", "parenthesized_expression"):
            holder = holder.parent
        if holder is None:
            continue
        if holder.type == "assignment_expression" and holder.parent is not None \
                and holder.parent.type == "expression_statement" \
                and holder.child_by_field_name("left").type == "identifier":
            target = text_of(source, holder.child_by_field_name("left")).strip()
        elif holder.type == "init_declarator" and holder.parent is not None \
                and holder.parent.type == "declaration":
            target = _declared_identifier(holder.child_by_field_name("declarator"), source)
        else:
            continue
        info = local.get(target)
        if not info or info.get("param") or info.get("array"):
            continue
        uses = [u for u in _name_uses(body, source, target) if u[1] not in ("decl", "sizeof")]
        later = [u for u in uses if u[0].start_byte >= call.end_byte]
        loop = _enclosing_loop(call)
        if any(kind in ("read", "addr") for _n, kind in uses if kind != "write" and loop is not None
               and loop.start_byte <= _n.start_byte <= loop.end_byte):
            continue   # relue a l'iteration suivante
        following = next(iter(later), None)
        if following is not None and following[1] in ("read", "addr"):
            continue
        never_read = not any(kind in ("read", "addr") for _n, kind in uses)
        why = (f"code de retour de {name} range dans {target} mais {target} n'est jamais lu "
               "dans la fonction: l'echec passe inapercu" if never_read else
               f"code de retour de {name} range dans {target} puis ecrase avant d'avoir ete lu")
        out.append({**where, "check": "unread_return", "line": call.start_point[0] + 1,
                    "call": name, "variable": target,
                    "confidence": "haute" if never_read else "moyenne", "why": why})
    return out


def _declared_identifier(node, source: bytes) -> str:
    """Nom d'un declarateur (x, *x, x = 1 sans sa valeur)."""
    return re.sub(r"[\s*&]", "", text_of(source, node).split("=")[0].split("[")[0])


def _uninitialized_use(body, source, local, where) -> list[dict]:
    """Variable locale declaree sans valeur et lue avant toute ecriture.

    On suit l'ordre du source. Une premiere occurrence qui ecrit (x = ..., x.champ = ...)
    ou prend l'adresse (&x: l'appele la remplit) met la variable hors de cause, y compris
    quand l'initialisation ne couvre que certaines branches: ce cas n'est pas vu.
    Un tableau, une variable static et un parametre sont ecartes. Une lecture dans une
    boucle qui ecrit aussi la variable plus loin est ecartee (drapeau de premiere passe)."""
    declared: dict[str, int] = {}
    for decl in walk(body):
        if decl.type != "declaration":
            continue
        if any(c.type == "storage_class_specifier" and text_of(source, c) in ("static", "extern")
               for c in decl.children):
            continue
        for child in decl.children:
            is_name = child.type in ("identifier", "pointer_declarator")
            if is_name and "[" not in text_of(source, child):
                declared[_declared_identifier(child, source)] = decl.start_point[0] + 1
    out = []
    for name, line in declared.items():
        info = local.get(name)
        if not info or info.get("param") or info.get("array"):
            continue
        uses = [u for u in _name_uses(body, source, name) if u[1] not in ("decl", "sizeof")]
        if not uses or uses[0][1] != "read":
            continue
        first = uses[0][0]
        loop = _enclosing_loop(first)
        if loop is not None and any(
                kind in ("write", "addr") and loop.start_byte <= n.start_byte <= loop.end_byte
                for n, kind in uses):
            continue
        out.append({**where, "check": "uninitialized_use", "line": first.start_point[0] + 1,
                    "variable": name, "declared_line": line, "confidence": "moyenne",
                    "why": f"{name} declaree ligne {line} sans valeur et lue ici avant toute "
                           "ecriture"})
    return out


RISK_NOTES = {
    "unbounded_copy": "copie sans borne vers un tableau local de taille fixe; les copies d'un meme "
    "tampon dans une fonction sont regroupees (occurrences, lines), group=false pour les separer",
    "format_mismatch": "arguments en nombre different des conversions du format",
    "ignored_return": "code d'erreur d'une fonction qui peut echouer jamais lu",
    "unchecked_alloc": "allocation ou ouverture utilisee avant tout test d'echec",
    "unread_return": "code de retour range dans une variable jamais lue (ou ecrasee avant lecture)",
    "uninitialized_use": "variable locale lue avant toute ecriture (les initialisations par "
    "branche ne sont pas suivies)",
    "leak_on_return": "ressource acquise dans la fonction, perdue sur un return",
    "leak_at_end": "ressource acquise dans la fonction, jamais liberee ni transmise",
    "double_release": "liberation d'une ressource deja liberee sur tous les chemins",
}
