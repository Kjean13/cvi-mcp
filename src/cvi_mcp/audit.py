# ==============================================================================
#  cvi_mcp/audit.py
#  Role     Journal local JSONL de chaque appel et de ce qui en est sorti
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import hashlib
import json
import time
from pathlib import Path

# Arguments qui portent un chemin ou un nom de fichier.
_PATH_ARGS = ("file", "path", "before", "after", "files", "uir", "target", "name_or_path")


class AuditLog:
    """Trace append-only. Un seul objet par processus, jamais de rotation
    automatique: le fichier appartient a l'utilisateur."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, tool: str, args: dict, result_bytes: int, code_lines: int = 0,
               hide_paths: bool = False) -> None:
        kept = {k: v for k, v in args.items() if v is not None}
        if hide_paths:
            # Empreinte stable: deux appels sur le meme fichier restent reconnaissables.
            for key in _PATH_ARGS:
                if isinstance(kept.get(key), str):
                    kept[key] = "sha256:" + hashlib.sha256(kept[key].encode()).hexdigest()[:12]
        entry = {
            "ts": round(time.time(), 3),
            "tool": tool,
            "args": kept,
            "result_bytes": result_bytes,
            "code_lines": code_lines,
        }
        try:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            # Un journal indisponible ne doit jamais empecher une lecture.
            pass
