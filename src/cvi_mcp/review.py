# ==============================================================================
#  cvi_mcp/review.py
#  Role     Rapport de revue: les constats de plusieurs analyses, tries par gravite
#           et regroupes par ce qu'on en fait (corriger, verifier, ranger)
#  Etat     stable
#  Version  0.4.0
# ==============================================================================

# ============================ Interne =========================================
from .risks import CHECK_RANK, CONFIDENCE_RANK

# Sections du rapport, dans l'ordre ou on les lit: ce qui plante ou fuit d'abord.
# Chaque section dit quelles alertes elle reunit et quel outil donne le detail.
SECTIONS = (
    ("crashes_and_leaks", "Plantages et fuites",
     ("leak_on_return", "double_release", "leak_at_end", "unchecked_alloc", "uninitialized_use",
      "unbounded_copy", "format_mismatch"), "risks(check=...)"),
    ("threads_and_locks", "Threads et verrous",
     ("lock_gap", "inconsistent_lock", "ui_control_unlocked", "ui_from_thread",
      "blocking_call_under_lock", "shared_global_unlocked"), "threads(), lock_check()"),
    ("ignored_errors", "Erreurs ignorees",
     ("unread_return", "ignored_return"), "risks(check=...)"),
    ("consistency", "Coherence du code",
     ("duplicate_definition", "prototype_mismatch", "extern_mismatch", "callback_signature",
      "control_on_wrong_panel"), "consistency(check=...)"),
)

# Constats de consistency qui n'ont pas leur place dans une revue: ils se comptent a part.
_QUIET = frozenset({"could_be_static", "declared_not_defined"})

# Ce qui vient de lock_check et de consistency n'a pas la forme d'une alerte de risks.
_ORDER_AFTER_RISKS = len(CHECK_RANK)


def blocking_items(blocking: list[dict]) -> list[dict]:
    """Appels bloquants verrou tenu (lock_check), mis sous la forme d'une alerte."""
    return [{
        "check": "blocking_call_under_lock", "confidence": "moyenne", "function": b["function"],
        "file": b["file"], "line": b["line"],
        "why": f"{b['call']} peut attendre pendant que {', '.join(b['locks_held'])} est tenu: "
               "les autres threads attendent avec lui",
    } for b in blocking]


def consistency_items(found: list[dict]) -> list[dict]:
    """Constats de consistency gardes pour la revue, avec un champ function pour le tri."""
    out = []
    for item in found:
        if item["check"] in _QUIET:
            continue
        out.append({**item, "function": item.get("function") or item.get("name", "")})
    return out


def _rank(item: dict) -> tuple:
    return (CONFIDENCE_RANK.get(item["confidence"], 2),
            CHECK_RANK.get(item["check"], _ORDER_AFTER_RISKS),
            item.get("file", ""), item.get("line", 0))


def _brief(item: dict) -> dict:
    """L'alerte allegee pour un rapport: ce qu'il faut pour aller la lire."""
    keep = ("check", "confidence", "function", "file", "line", "lines", "occurrences", "global",
            "variable", "control", "call", "locks", "why")
    return {key: item[key] for key in keep if key in item}


def build_review(items: list[dict], limit: int = 8, min_confidence: str = "moyenne",
                 dead: dict | None = None, quiet: dict | None = None) -> dict:
    """Le rapport: par section, le nombre de constats par type et les `limit` plus graves.

    items: alertes de risks, appels bloquants et constats de coherence (forme commune:
    check, confidence, function, file, line). min_confidence ecarte le moins sur.
    dead et quiet sont des comptes a joindre tels quels (code mort, constats de style)."""
    floor = CONFIDENCE_RANK.get(min_confidence, 1)
    kept = [i for i in items if CONFIDENCE_RANK.get(i["confidence"], 2) <= floor]
    hidden = len(items) - len(kept)
    claimed: set[str] = set()
    sections = {}
    for key, title, checks, drill in SECTIONS:
        members = sorted((i for i in kept if i["check"] in checks), key=_rank)
        claimed.update(checks)
        by_check: dict[str, int] = {}
        for member in members:
            by_check[member["check"]] = by_check.get(member["check"], 0) + 1
        sections[key] = {
            "title": title, "total": len(members),
            "by_confidence": _count(members, "confidence"),
            "by_check": dict(sorted(by_check.items(), key=lambda kv: CHECK_RANK.get(kv[0], 99))),
            "items": [_brief(m) for m in members[:limit]], "details": drill,
        }
    others = [i for i in kept if i["check"] not in claimed]
    report = {"sections": sections, "hidden_below_min_confidence": hidden}
    if others:
        report["other"] = {"total": len(others), "by_check": _count(others, "check")}
    if dead:
        report["dead_code"] = dead
    if quiet:
        report["not_reviewed"] = quiet
    report["headline"] = _headline(sections)
    report["next"] = _next(sections)
    return report


def _count(items: list[dict], field: str) -> dict:
    counts: dict[str, int] = {}
    for item in items:
        counts[item[field]] = counts.get(item[field], 0) + 1
    return counts


def _headline(sections: dict) -> list[str]:
    """Quelques phrases: ce qu'il y a de plus grave, section par section."""
    out = []
    for section in sections.values():
        high = section["by_confidence"].get("haute", 0)
        if section["total"] == 0:
            continue
        out.append(f"{section['title']}: {section['total']} constat(s)"
                   + (f", dont {high} de confiance haute" if high else ""))
    return out or ["aucun constat au seuil demande"]


def _next(sections: dict) -> list[str]:
    """Par ou continuer: l'outil qui detaille la section la plus chargee en haute confiance."""
    ranked = sorted(sections.values(),
                    key=lambda s: (-s["by_confidence"].get("haute", 0), -s["total"]))
    return [f"{s['title']}: {s['details']}" for s in ranked if s["total"]][:3]
