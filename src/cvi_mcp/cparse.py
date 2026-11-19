# ==============================================================================
#  cvi_mcp/cparse.py
#  Role     Primitives tree-sitter C partagees par l'indexeur projet et le SDK
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import hashlib
import re
from pathlib import Path

# ============================ Externe =========================================
import tree_sitter_c as tsc
from tree_sitter import Language, Parser

C_LANGUAGE = Language(tsc.language())

# Les macros de calling convention CVI cassent l'analyse C standard. On les
# remplace par des espaces de longueur identique pour que les positions
# ligne/colonne restent exactes.
_CVI_NOISE = re.compile(
    r"\b(?:CVICALLBACK|CVIFUNC|CVIFUNC_C|CVIFUNCC|DLLEXPORT|DLLIMPORT"
    r"|__cdecl|__stdcall|CVIDLLENTRY)\b"
    r"|__declspec\s*\([^)]*\)"
)

_WHITESPACE = re.compile(r"\s+")
_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


# Les sources CVI sont editees sous Windows: elles sont rarement en UTF-8.
# Un commentaire accentue en cp1252 ne doit pas arriver mutile dans l'index.
# La marque d'ordre des octets est testee a part: utf-8-sig accepte aussi un
# fichier sans marque, il masquerait donc l'encodage reellement utilise.
_BOM = b"\xef\xbb\xbf"
_ENCODINGS = ("utf-8", "cp1252", "latin-1")


def new_parser() -> Parser:
    return Parser(C_LANGUAGE)


def read_source(path: Path) -> tuple[str, str, bytes]:
    """Texte, encodage retenu et octets bruts d'un fichier source.

    latin-1 accepte n'importe quel octet, l'appel ne leve donc jamais.
    """
    raw = Path(path).read_bytes()
    if raw.startswith(_BOM):
        return raw.decode("utf-8-sig", errors="replace"), "utf-8-sig", raw
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding), encoding, raw
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1", raw


def neutralize(source: str) -> str:
    """Neutralise les macros CVI sans decaler un seul caractere."""
    return _CVI_NOISE.sub(lambda m: " " * len(m.group(0)), source)


def text_of(source: bytes, node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def walk(node):
    """Parcours en profondeur de tous les descendants."""
    stack = list(reversed(node.children))
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def declarator_info(node, source: bytes) -> tuple[str | None, list[str]]:
    """Nom et parametres d'un function_declarator, pointeurs traverses."""
    target = node
    while target.type == "pointer_declarator" and target.children:
        target = target.children[-1]
    if target.type != "function_declarator":
        inner = next(
            (c for c in walk(node) if c.type == "function_declarator"), None
        )
        if inner is None:
            return None, []
        target = inner

    name_node = target.child_by_field_name("declarator")
    name = text_of(source, name_node).strip("*& ") if name_node else None

    params: list[str] = []
    params_node = target.child_by_field_name("parameters")
    if params_node:
        for child in params_node.children:
            if child.type == "parameter_declaration":
                params.append(_WHITESPACE.sub(" ", text_of(source, child)).strip())
    return name, params


def leading_comment(lines: list[str], decl_line: int) -> str | None:
    """Commentaire colle juste au-dessus d'une declaration."""
    if decl_line <= 0:
        return None
    previous = lines[decl_line - 1].strip()
    if previous.endswith("*/"):
        block: list[str] = []
        for index in range(decl_line - 1, -1, -1):
            block.append(lines[index].strip())
            if "/*" in lines[index]:
                break
        return " ".join(reversed(block))
    if previous.startswith("//"):
        return previous
    return None


def body_fingerprint(body: str) -> str:
    """Empreinte stable d'un corps de fonction: commentaires et mise en forme
    retires, pour que le diff signale un vrai changement de logique."""
    stripped = _BLOCK_COMMENT.sub(" ", body)
    stripped = _LINE_COMMENT.sub(" ", stripped)
    stripped = _WHITESPACE.sub(" ", stripped).strip()
    return hashlib.sha256(stripped.encode("utf-8")).hexdigest()[:16]


def file_fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]
