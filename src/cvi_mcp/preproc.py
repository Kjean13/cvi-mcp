# ==============================================================================
#  cvi_mcp/preproc.py
#  Role     Preprocesseur simule: directives, #if evalues, includes resolus,
#           code actif ou exclu par unite de compilation
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ============================ Interne =========================================
from .cparse import read_source

# Ordre des etats: un bloc vu dans plusieurs unites garde le plus "vivant".
INACTIVE, UNKNOWN, ACTIVE = 0, 1, 2
STATE_NAMES = {INACTIVE: "inactive", UNKNOWN: "unknown", ACTIVE: "active"}

# Macros que les compilateurs C courants definissent mais pas CVI: les voir
# indefinies est la reponse exacte, pas une incertitude.
KNOWN_UNDEFINED = frozenset(
    ["__cplusplus", "_MSC_VER", "__GNUC__", "__clang__", "__APPLE__", "__linux__", "__unix__", "_MAC", "__MWERKS__", "__BORLANDC__", "__WATCOMC__", "_NI_unix_", "_NI_linux_", "_NI_sparc_"]
)

_DIRECTIVE = re.compile(r"^\s*#\s*([A-Za-z_]+)\b(.*)$")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DEFINE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\([^)]*\))?\s*(.*)$", re.DOTALL)
_MAX_DEPTH = 40

# Directives deja lues, par chemin: le serveur vit longtemps, on ne relit un
# fichier que si sa taille ou sa date change.
_SCAN_CACHE: dict[str, tuple[int, float, list]] = {}


# ------------------------------------------------------------ lexique ----------

def strip_comments(text: str) -> str:
    """Commentaires remplaces par des espaces, sauts de ligne gardes, chaines
    et caracteres intacts: les numeros de ligne restent exacts."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:end]))
            i = end
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(text[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


@dataclass
class Directive:
    line: int          # premiere ligne, base 1
    end: int           # derniere ligne (continuations par antislash)
    kind: str
    arg: str


def scan(text: str) -> list[Directive]:
    """Directives d'un fichier, commentaires retires, continuations jointes."""
    lines = strip_comments(text).split("\n")
    found: list[Directive] = []
    index = 0
    while index < len(lines):
        start = index
        logical = lines[index]
        while logical.rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            logical = logical.rstrip()[:-1] + " " + lines[index]
        match = _DIRECTIVE.match(logical)
        if match:
            found.append(Directive(start + 1, index + 1, match.group(1), match.group(2).strip()))
        index += 1
    return found


def parse_define(arg: str) -> tuple[str, list[str] | None, str] | None:
    match = _DEFINE.match(arg)
    if not match:
        return None
    params = None
    if match.group(2):
        params = [p.strip() for p in match.group(2)[1:-1].split(",") if p.strip()]
    return match.group(1), params, " ".join(match.group(3).split())


# ---------------------------------------------------------- expressions --------

class Unknown(Exception):
    """Expression non evaluable: macro fonction, valeur non numerique."""


_TOKEN = re.compile(
    r"\s*(?:(0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)[uUlL]*"
    r"|('(?:\\.|[^'])')"
    r"|([A-Za-z_][A-Za-z0-9_]*)"
    r"|(<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%<>!~&|^?:()]))"
)

_BINARY = {
    "||": 1, "&&": 2, "|": 3, "^": 4, "&": 5, "==": 6, "!=": 6,
    "<": 7, "<=": 7, ">": 7, ">=": 7, "<<": 8, ">>": 8,
    "+": 9, "-": 9, "*": 10, "/": 10, "%": 10,
}


def _char_value(literal: str) -> int:
    body = literal[1:-1]
    escapes = {"n": 10, "t": 9, "r": 13, "0": 0, "\\": 92, "'": 39, '"': 34}
    if body.startswith("\\") and len(body) > 1:
        return escapes.get(body[1], ord(body[1]))
    return ord(body[0]) if body else 0


class Evaluator:
    """Evalue une expression #if avec un environnement de macros.

    Un identifiant non defini vaut 0, comme en C; il est note pour que la
    reponse dise sur quoi repose le verdict."""

    def __init__(self, env: dict[str, "Macro"]) -> None:
        self.env = env
        self.names: set[str] = set()
        self.undefined: set[str] = set()

    def evaluate(self, expr: str, depth: int = 0) -> int:
        if depth > 32:
            raise Unknown("expansion trop profonde")
        tokens = self._tokens(expr)
        self._toks, self._pos, self._depth = tokens, 0, depth
        value = self._ternary()
        if self._pos != len(tokens):
            raise Unknown(f"syntaxe non reconnue: {expr}")
        return value

    def _tokens(self, expr: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        pos = 0
        expr = expr.strip()
        while pos < len(expr):
            match = _TOKEN.match(expr, pos)
            if not match or match.end() == pos:
                if expr[pos:].strip() == "":
                    break
                raise Unknown(f"jeton non reconnu dans: {expr}")
            pos = match.end()
            number, char, ident, op = match.groups()
            if number is not None:
                out.append(("num", number))
            elif char is not None:
                out.append(("num", str(_char_value(char))))
            elif ident is not None:
                out.append(("id", ident))
            else:
                out.append(("op", op))
        return out

    def _peek(self) -> tuple[str, str] | None:
        return self._toks[self._pos] if self._pos < len(self._toks) else None

    def _take(self) -> tuple[str, str]:
        token = self._peek()
        if token is None:
            raise Unknown("expression incomplete")
        self._pos += 1
        return token

    def _ternary(self) -> int:
        cond = self._binary(1)
        token = self._peek()
        if token == ("op", "?"):
            self._take()
            left = self._ternary()
            if self._take() != ("op", ":"):
                raise Unknown("ternaire incomplet")
            right = self._ternary()
            return left if cond else right
        return cond

    def _binary(self, level: int) -> int:
        left = self._unary()
        while True:
            token = self._peek()
            if not token or token[0] != "op" or token[1] not in _BINARY:
                return left
            prec = _BINARY[token[1]]
            if prec < level:
                return left
            self._take()
            right = self._binary(prec + 1)
            left = self._apply(token[1], left, right)

    @staticmethod
    def _apply(op: str, a: int, b: int) -> int:
        if op in ("/", "%") and b == 0:
            raise Unknown("division par zero")
        table = {
            "||": lambda: int(bool(a) or bool(b)), "&&": lambda: int(bool(a) and bool(b)),
            "|": lambda: a | b, "^": lambda: a ^ b, "&": lambda: a & b,
            "==": lambda: int(a == b), "!=": lambda: int(a != b),
            "<": lambda: int(a < b), "<=": lambda: int(a <= b),
            ">": lambda: int(a > b), ">=": lambda: int(a >= b),
            "<<": lambda: a << b, ">>": lambda: a >> b,
            "+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
            "/": lambda: int(a / b), "%": lambda: a % b,
        }
        return table[op]()

    def _unary(self) -> int:
        kind, text = self._take()
        if kind == "op":
            if text == "(":
                value = self._ternary()
                if self._take() != ("op", ")"):
                    raise Unknown("parenthese non fermee")
                return value
            if text in ("!", "~", "-", "+"):
                value = self._unary()
                return {"!": int(not value), "~": ~value, "-": -value, "+": value}[text]
            raise Unknown(f"operateur inattendu: {text}")
        if kind == "num":
            return int(text, 0) if not re.fullmatch(r"0\d+", text) else int(text, 8)
        if text == "defined":
            paren = self._peek() == ("op", "(")
            if paren:
                self._take()
            name_kind, name = self._take()
            if name_kind != "id":
                raise Unknown("defined sans nom")
            if paren and self._take() != ("op", ")"):
                raise Unknown("defined mal ferme")
            self.names.add(name)
            if name not in self.env:
                self.undefined.add(name)
            return int(name in self.env)
        return self._identifier(text)

    def _identifier(self, name: str) -> int:
        self.names.add(name)
        macro = self.env.get(name)
        if macro is None:
            if self._peek() == ("op", "("):
                raise Unknown(f"appel de {name} non evaluable")
            self.undefined.add(name)
            return 0
        if macro.params is not None:
            raise Unknown(f"macro fonction {name} non evaluee")
        if macro.value.strip() == "":
            raise Unknown(f"{name} definie sans valeur")
        nested = Evaluator(self.env)
        try:
            value = nested.evaluate(macro.value, self._depth + 1)
        finally:
            self.names |= nested.names
            self.undefined |= nested.undefined
        return value


def evaluate_value(value: str, env: dict) -> int | None:
    """Valeur entiere d'une macro, ou None si elle n'est pas numerique."""
    try:
        return Evaluator(env).evaluate(value)
    except (Unknown, ValueError, RecursionError):
        return None


# -------------------------------------------------------------- modele ---------

@dataclass
class Macro:
    name: str
    value: str
    params: list[str] | None = None
    origin: str = ""        # fichier:ligne, "prj", "predefini" ou "force"


@dataclass
class Branch:
    line: int               # ligne de la directive (#if, #elif, #else)
    end: int                # derniere ligne du contenu
    kind: str
    expr: str
    chain: int              # ligne du #if qui ouvre la chaine
    state: int = INACTIVE
    evaluated: bool = False
    varies: bool = False
    guard: bool = False
    names: set[str] = field(default_factory=set)
    undefined: set[str] = field(default_factory=set)
    reason: str = ""


@dataclass
class Include:
    line: int
    text: str
    system: bool
    resolved: str | None = None
    how: str = "unresolved"
    state: int = INACTIVE


@dataclass
class FileResult:
    path: str
    listed: bool = True
    branches: dict[int, Branch] = field(default_factory=dict)
    includes: dict[int, Include] = field(default_factory=dict)
    defines: dict[int, dict] = field(default_factory=dict)
    endif_lines: set[int] = field(default_factory=set)
    reached_by: str | None = None
    in_build: bool = False
    guard: str | None = None
    directives: int = 0

    def inactive_lines(self) -> set[int]:
        """Lignes du contenu des branches inactives."""
        out: set[int] = set()
        for branch in self.branches.values():
            if branch.state == INACTIVE and not branch.guard:
                out.update(range(branch.line + 1, branch.end + 1))
        return out

    def blank_lines(self) -> set[int]:
        """Lignes a effacer avant l'analyse syntaxique: contenu inactif et
        directives des chaines entierement tranchees. Une chaine dont une
        branche reste incertaine est laissee intacte."""
        out = self.inactive_lines()
        chains: dict[int, list[Branch]] = {}
        for branch in self.branches.values():
            chains.setdefault(branch.chain, []).append(branch)
        for members in chains.values():
            if all(b.state != UNKNOWN for b in members):
                for member in members:
                    out.add(member.line)
                last = max(members, key=lambda b: b.line)
                out.add(last.end + 1)
        return out

    def signature(self) -> str:
        """Empreinte de ce qui change l'indexation de ce fichier."""
        parts = sorted(self.blank_lines())
        states = sorted((b.line, b.state) for b in self.branches.values())
        raw = f"{parts}|{states}|{self.in_build}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass
class BuildContext:
    """Ce que le .prj et la configuration apportent au preprocesseur."""

    include_paths: list[str] = field(default_factory=list)
    defines: dict[str, str] = field(default_factory=dict)
    forced: dict[str, str] = field(default_factory=dict)
    is_64: bool = False
    is_release: bool = False
    cvi_version: str | None = None
    config_name: str = "Debug"


def predefined(ctx: BuildContext) -> dict[str, Macro]:
    """Macros que le compilateur CVI fournit sans en-tete."""
    values = {"_CVI_": ctx.cvi_version or "1", "_NI_mswin_": "1", "_WIN32": "1",
              "WIN32": "1", "_NI_mswin32_": "1", "__STDC__": "1"}
    if ctx.is_64:
        values.update({"_WIN64": "1", "_NI_mswin64_": "1", "_M_X64": "1", "_M_AMD64": "1"})
    else:
        values.update({"_NI_i386_": "1", "_M_IX86": "1"})
    if not ctx.is_release:
        values["_CVI_DEBUG_"] = "1"
    return {k: Macro(k, v, None, "predefini") for k, v in values.items()}


# ---------------------------------------------------------- simulation ---------

class Preprocessor:
    """Rejoue le preprocesseur sur chaque unite de compilation (.c du build).

    Un en-tete inclus par plusieurs unites garde, pour chaque branche, l'etat
    le plus vivant vu: actif dans une unite suffit a le compter comme compile.
    Seules les directives sont relues, jamais le code: c'est rapide."""

    def __init__(self, files: list[str], ctx: BuildContext, units: list[str] | None = None):
        self.ctx = ctx
        self.files = [str(Path(f).resolve()) for f in files]
        self.indexed = set(self.files)
        chosen = self.files if units is None else [str(Path(u).resolve()) for u in units]
        self.units = [f for f in chosen if f.lower().endswith(".c")]
        self._cache: dict[str, list[Directive]] = {}
        self._dir_cache: dict[str, dict[str, str]] = {}
        self.results: dict[str, FileResult] = {}
        self.reached: dict[str, str] = {}          # en-tete non indexe -> unite
        self.env_values: dict[str, set[str]] = {}
        self.defined_anywhere: set[str] = set()
        project_dirs = sorted({str(Path(f).parent) for f in self.files})
        self.search_dirs = [str(Path(p)) for p in ctx.include_paths] + project_dirs

    # ------------------------------------------------------------ lecture ---

    def directives(self, path: str) -> list[Directive]:
        if path not in self._cache:
            try:
                stat = os.stat(path)
                key = (stat.st_size, stat.st_mtime)
            except OSError:
                key = (0, 0.0)
            cached = _SCAN_CACHE.get(path)
            if cached and cached[:2] == key:
                self._cache[path] = cached[2]
            else:
                try:
                    text, _enc, _raw = read_source(Path(path))
                except OSError:
                    text = ""
                found = scan(text)
                _SCAN_CACHE[path] = (*key, found)
                self._cache[path] = found
        return self._cache[path]

    def _listing(self, folder: str) -> dict[str, str]:
        """Nom en minuscules vers nom reel: Windows ignore la casse."""
        if folder not in self._dir_cache:
            try:
                self._dir_cache[folder] = {n.lower(): n for n in os.listdir(folder)}
            except OSError:
                self._dir_cache[folder] = {}
        return self._dir_cache[folder]

    def _find(self, folder: str, name: str) -> str | None:
        target = Path(folder)
        parts = name.replace("\\", "/").split("/")
        for part in parts[:-1]:
            if part in ("", "."):
                continue
            if part == "..":
                target = target.parent
                continue
            real = self._listing(str(target)).get(part.lower())
            if real is None:
                return None
            target = target / real
        real = self._listing(str(target)).get(parts[-1].lower())
        if real is None:
            return None
        candidate = target / real
        return str(candidate.resolve()) if candidate.is_file() else None

    def resolve(self, name: str, system: bool, current: str, stack: list[str]) -> tuple[str | None, str]:
        """Ordre de recherche du compilateur: dossier du fichier, dossiers des
        fichiers qui l'incluent, chemins d'inclusion du .prj, dossiers des
        sources du projet."""
        if not system:
            for holder in [current, *reversed(stack)]:
                found = self._find(str(Path(holder).parent), name)
                if found:
                    return found, "dossier_du_fichier" if holder == current else "dossier_parent"
        for folder in self.ctx.include_paths:
            found = self._find(folder, name)
            if found:
                return found, "include_path"
        for folder in self.search_dirs[len(self.ctx.include_paths):]:
            found = self._find(folder, name)
            if found:
                return found, "dossier_du_projet"
        return None, "systeme" if system else "introuvable"

    # --------------------------------------------------------- simulation ---

    def run(self) -> dict[str, FileResult]:
        for path in self.files:
            self.results[path] = FileResult(path)
            for d in self.directives(path):
                if d.kind == "define":
                    parsed = parse_define(d.arg)
                    if parsed:
                        self.defined_anywhere.add(parsed[0])
        for unit in self.units:
            env = {**predefined(self.ctx)}
            for name, value in self.ctx.defines.items():
                env[name] = Macro(name, value, None, "prj")
            for name, value in self.ctx.forced.items():
                env[name] = Macro(name, value, None, "force")
            self._process(unit, env, unit, [], set())
            for name, macro in env.items():
                if macro.params is None:
                    self.env_values.setdefault(name, set()).add(macro.value)
            self.results[unit].in_build = True
            self.results[unit].reached_by = unit
        # En-tetes atteints par aucune unite: evalues seuls, pour la lecture.
        for path in self.files:
            result = self.results[path]
            if result.reached_by is None:
                env = {**predefined(self.ctx)}
                for name, value in {**self.ctx.defines, **self.ctx.forced}.items():
                    env[name] = Macro(name, value)
                self._process(path, env, None, [], set(), standalone=True)
        self._final_env = env_from_values(self.env_values)
        return self.results

    def _result(self, path: str) -> FileResult | None:
        return self.results.get(path)

    def _process(self, path: str, env: dict, unit: str | None, stack: list[str],
                 once: set[str], standalone: bool = False) -> None:
        if len(stack) > _MAX_DEPTH or path in stack:
            return
        result = self._result(path)
        if result is not None and unit and not standalone:
            if result.reached_by is None:
                result.reached_by = unit
            result.in_build = True
        elif result is None and unit:
            self.reached.setdefault(path, unit)
        if path in once:
            return
        directives = self.directives(path)
        if result is not None:
            result.directives = len(directives)
            self._mark_guard(result, directives)

        # Pile de chaines: (etat parent, une branche deja prise, branche courante)
        frames: list[dict] = []

        def live() -> int:
            return frames[-1]["state"] if frames else ACTIVE

        for index, d in enumerate(directives):
            kind = d.kind
            if kind in ("if", "ifdef", "ifndef"):
                parent = live()
                state, info = self._condition(kind, d.arg, env, parent)
                frames.append({"parent": parent, "taken": state == ACTIVE,
                               "maybe": state == UNKNOWN, "state": state, "chain": d.line})
                self._record(result, d, directives, index, kind, state, info, d.line, parent)
            elif kind in ("elif", "else") and frames:
                frame = frames[-1]
                parent = frame["parent"]
                if kind == "else":
                    if parent == INACTIVE or frame["taken"]:
                        state, info = INACTIVE, {"reason": "branche precedente prise" if frame["taken"] else "parent inactif"}
                    elif frame["maybe"]:
                        state, info = UNKNOWN, {"reason": "depend d'une branche precedente incertaine"}
                    else:
                        state, info = parent, {}
                else:
                    if parent == INACTIVE or frame["taken"]:
                        state, info = INACTIVE, {"reason": "branche precedente prise" if frame["taken"] else "parent inactif"}
                    else:
                        state, info = self._condition("if", d.arg, env, parent)
                        if frame["maybe"] and state == ACTIVE:
                            state = UNKNOWN
                frame["taken"] = frame["taken"] or state == ACTIVE
                frame["maybe"] = frame["maybe"] or state == UNKNOWN
                frame["state"] = state
                self._record(result, d, directives, index, kind, state, info, frame["chain"], parent)
            elif kind == "endif":
                if frames:
                    frames.pop()
                if result is not None:
                    result.endif_lines.add(d.line)
            elif live() == INACTIVE:
                continue
            elif kind == "define":
                parsed = parse_define(d.arg)
                if parsed:
                    name, params, value = parsed
                    if result is not None:
                        self._note_define(result, d.line, name, params, value, live())
                    if name not in self.ctx.forced:
                        env[name] = Macro(name, value, params, f"{path}:{d.line}")
            elif kind == "undef":
                name = d.arg.split()[0] if d.arg.split() else ""
                if name and name not in self.ctx.forced:
                    env.pop(name, None)
            elif kind == "pragma" and d.arg.split()[:1] == ["once"]:
                once.add(path)
            elif kind == "include":
                self._include(result, d, env, path, unit, stack, once, live(), standalone)

    def _mark_guard(self, result: FileResult, directives: list[Directive]) -> None:
        """#ifndef X / #define X en tete et #endif final: garde d'inclusion."""
        if len(directives) < 3 or result.guard:
            return
        first, second, last = directives[0], directives[1], directives[-1]
        if first.kind == "ifndef" and second.kind == "define" and last.kind == "endif":
            name = first.arg.split()[0] if first.arg.split() else ""
            parsed = parse_define(second.arg)
            if name and parsed and parsed[0] == name:
                depth = 0
                for d in directives:
                    if d.kind in ("if", "ifdef", "ifndef"):
                        depth += 1
                    elif d.kind == "endif":
                        depth -= 1
                        if depth == 0 and d is not last:
                            return
                result.guard = name

    def _condition(self, kind: str, arg: str, env: dict, parent: int) -> tuple[int, dict]:
        if parent == INACTIVE:
            return INACTIVE, {"reason": "parent inactif"}
        name = arg.split()[0] if arg.split() else ""
        if kind in ("ifdef", "ifndef"):
            info = {"names": {name}}
            present = name in env
            if not present:
                info["undefined"] = {name}
            value = present if kind == "ifdef" else not present
            state = ACTIVE if value else INACTIVE
        else:
            evaluator = Evaluator(env)
            try:
                value = evaluator.evaluate(arg)
                state = ACTIVE if value else INACTIVE
                info = {"names": evaluator.names, "undefined": evaluator.undefined}
            except (Unknown, ValueError, RecursionError) as exc:
                return min(parent, UNKNOWN), {"names": evaluator.names,
                                               "reason": f"non evaluable: {exc}"}
        if parent == UNKNOWN and state == ACTIVE:
            state = UNKNOWN
        return state, info

    def _record(self, result, d: Directive, directives, index, kind, state, info, chain, parent):
        if result is None:
            return
        depth, end = 0, d.end
        for later in directives[index + 1:]:
            if later.kind in ("if", "ifdef", "ifndef"):
                depth += 1
            elif later.kind == "endif":
                if depth == 0:
                    end = later.line - 1
                    break
                depth -= 1
            elif later.kind in ("elif", "else") and depth == 0:
                end = later.line - 1
                break
        branch = result.branches.get(d.line)
        guard = bool(result.guard and kind == "ifndef" and d.arg.split()[:1] == [result.guard]
                      and chain == d.line and index == 0)
        if branch is None:
            branch = Branch(d.line, end, kind, d.arg, chain, state, guard=guard)
            result.branches[d.line] = branch
        else:
            if branch.evaluated and branch.state != state and parent != INACTIVE:
                branch.varies = True
            branch.state = max(branch.state, state)
        if parent != INACTIVE:
            branch.evaluated = True
        # Les noms sont lus dans le texte, meme quand la branche n'est pas
        # evaluee (parent inactif, branche precedente prise).
        branch.names |= set(_IDENT.findall(d.arg)) - {"defined"}
        branch.names |= set(info.get("names", ()))
        branch.undefined |= set(info.get("undefined", ()))
        if info.get("reason") and (not branch.reason or state >= branch.state):
            branch.reason = info["reason"]

    def _note_define(self, result, line, name, params, value, state):
        entry = result.defines.setdefault(
            line, {"name": name, "params": params, "value": value, "state": state})
        entry["state"] = max(entry["state"], state)

    def _include(self, result, d, env, path, unit, stack, once, state, standalone):
        raw = d.arg.strip()
        if raw.startswith("<"):
            name, system = raw[1:raw.find(">")] if ">" in raw else raw[1:], True
        elif raw.startswith('"'):
            name, system = raw[1:].split('"', 1)[0], False
        else:
            macro = env.get(raw.split()[0]) if raw.split() else None
            if macro is None:
                return
            value = macro.value.strip()
            system = value.startswith("<")
            name = value.strip('<>"')
        found, how = self.resolve(name, system, path, stack)
        if result is not None:
            entry = result.includes.get(d.line)
            if entry is None:
                result.includes[d.line] = Include(d.line, name, system, found, how, state)
            else:
                entry.state = max(entry.state, state)
        if found and not standalone:
            self._process(found, env, unit, [*stack, path], once)
        elif found and standalone and found not in self.indexed:
            self._process(found, env, None, [*stack, path], once, standalone=True)

    # ------------------------------------------------------------- export ---

    @property
    def env(self) -> dict[str, Macro]:
        return self._final_env


def env_from_values(values: dict[str, set[str]]) -> dict[str, Macro]:
    """Environnement fusionne des unites: une valeur unique par macro."""
    env: dict[str, Macro] = {}
    for name, seen in values.items():
        env[name] = Macro(name, min(seen))
    return env


def confidence(branch: Branch, defined_anywhere: set[str], forced: dict) -> tuple[str, str]:
    """Confiance du verdict d'une branche et sa raison."""
    if branch.guard:
        return "haute", "garde d'inclusion"
    if branch.state == UNKNOWN:
        return "faible", branch.reason or "expression non evaluable"
    if not branch.evaluated:
        return "haute", "parent inactif"
    doubtful = {n for n in branch.undefined if n not in KNOWN_UNDEFINED and n not in forced}
    if not doubtful:
        if branch.varies:
            return "moyenne", "etat different selon l'unite de compilation"
        return "haute", branch.reason or "evaluee avec les #define du projet"
    elsewhere = sorted(doubtful & defined_anywhere)
    if elsewhere:
        return "moyenne", (f"{', '.join(elsewhere)} defini dans le projet mais pas avant "
                           "cette ligne dans l'unite: verifier l'ordre des includes")
    return "moyenne", (f"{', '.join(sorted(doubtful))} defini nulle part dans le projet ni le "
                       ".prj: vaut 0, sauf s'il vient d'un en-tete systeme ou de la ligne de "
                       "commande")
