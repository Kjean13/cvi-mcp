# ==============================================================================
#  scripts/acceptance.py
#  Role     Recette de l'archive livree: installation neuve, tests, appels reels
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

# Recette de l'artefact livre, pas de l'arbre de travail: on extrait l'archive,
# on installe dans un environnement vierge et on parle MCP pour de vrai.
#   python scripts/acceptance.py [chemin/vers/cvi-mcp.tar.gz]
ARCHIVE = Path(sys.argv[1] if len(sys.argv) > 1 else "cvi-mcp.tar.gz").resolve()
ARENA = Path(tempfile.gettempdir()) / "cvi-mcp-acceptance"
results: list[tuple[str, bool, str]] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append((label, bool(ok), detail))
    return bool(ok)


class Server:
    """Client MCP minimal sur stdio, pour appeler les outils pour de vrai."""

    def __init__(self, python: Path, env: dict):
        self.proc = subprocess.Popen(
            [str(python), "-m", "cvi_mcp.server"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        )
        self.id = 0
        self._send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "recette", "version": "0"}}})
        self.info = json.loads(self.proc.stdout.readline())["result"]
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def _send(self, payload):
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def tools(self) -> set[str]:
        self.id += 1
        self._send({"jsonrpc": "2.0", "id": self.id, "method": "tools/list", "params": {}})
        return {t["name"] for t in json.loads(self.proc.stdout.readline())["result"]["tools"]}

    def call(self, tool: str, /, **arguments) -> dict:
        """tool est positionnel strict: les outils ont eux-memes un argument
        nomme 'name', il ne doit pas entrer en collision."""
        self.id += 1
        self._send({"jsonrpc": "2.0", "id": self.id, "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments}})
        answer = json.loads(self.proc.stdout.readline())
        return json.loads(answer["result"]["content"][0]["text"])

    def close(self):
        self.proc.terminate()


def main() -> int:
    # ---------------------------------------------- 1. archive et extraction --
    check("archive presente", ARCHIVE.is_file(), f"{ARCHIVE.stat().st_size // 1024} Ko")
    if ARENA.exists():
        shutil.rmtree(ARENA)
    ARENA.mkdir(parents=True)
    with tarfile.open(ARCHIVE) as tar:
        names = tar.getnames()
        unsafe = [n for n in names if n.startswith("/") or ".." in Path(n).parts]
        check("archive sans chemin dangereux", not unsafe, str(unsafe[:3]))
        tar.extractall(ARENA, filter="data")
    root = ARENA / "cvi-mcp"
    check("arborescence extraite", root.is_dir(), str(len(names)) + " entrees")

    expected = {"pyproject.toml", "README.md", ".gitignore"}
    check("fichiers de tete presents", expected <= {p.name for p in root.iterdir()})
    check("pas de residu de build",
          not any(root.rglob("__pycache__")) and not any(root.rglob("*.egg-info")))

    # ------------------------------------------------ 2. installation neuve ---
    venv = ARENA / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True)
    python = venv / "bin" / "python"
    scripts = venv / "bin"
    done = subprocess.run([str(python), "-m", "pip", "install", "-e", f"{root}[dev]", "-q"],
                          capture_output=True, text=True, check=False)
    check("installation dans un venv vierge", done.returncode == 0,
          done.stderr.strip().splitlines()[-1] if done.returncode else "")

    # ------------------------------------------------------- 3. suite tests ---
    done = subprocess.run([str(python), "-m", "pytest", "-q"], cwd=str(root),
                          capture_output=True, text=True, check=False)
    tail = done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""
    check("suite de tests depuis l'archive", done.returncode == 0, tail)

    # ----------------------------------------------------------- 4. la CLI ---
    done = subprocess.run([str(scripts / "cvi-mcp"), "--version"], capture_output=True, text=True, check=False)
    version = done.stdout.strip()
    check("cvi-mcp --version", done.returncode == 0 and version.startswith("cvi-mcp "), version)

    fixtures = root / "tests" / "fixtures"
    env = dict(os.environ,
               PATH=f"{scripts}:{os.environ['PATH']}",
               CVI_MCP_PROJECT=str(fixtures),
               CVI_MCP_DB=str(ARENA / "index.db"),
               CVI_MCP_AUDIT_LOG=str(ARENA / "audit.jsonl"),
               CVI_MCP_EXCLUDE="sdk/*")
    env.pop("CVI_MCP_ALLOW_BODY", None)
    done = subprocess.run([str(scripts / "cvi-mcp"), "--check"], capture_output=True,
                          text=True, env=env, check=False)
    report = json.loads(done.stdout)
    check("cvi-mcp --check rend un diagnostic",
          report["cvi_mcp_version"] in version and "warnings" in report,
          f"{len(report['warnings'])} avertissement(s)")
    check("code de sortie aligne sur les avertissements",
          done.returncode == (1 if report["warnings"] else 0))

    # ------------------------------------------------- 5. protocole et outils --
    srv = Server(python, env)
    check("handshake MCP", srv.info["serverInfo"]["name"] == "cvi-mcp",
          srv.info["serverInfo"].get("version", ""))
    tools = srv.tools()
    check("38 outils exposes", len(tools) == 38, f"{len(tools)} trouves")

    overview = srv.call("project_overview")
    files = srv.call("project_overview", section="files", limit=100)["items"]
    check("project_overview indexe au premier appel",
          overview["total_lines"] > 0 and overview["total_files"] == 4 and len(files) == 4,
          f"{overview['total_files']} fichiers, {overview['total_lines']} lignes")
    check("totaux par origine", "project" in overview["by_origin"])
    check("exclusion appliquee", not any(f["path"].startswith("sdk/") for f in files))
    check("chemins relatifs", all(not f["path"].startswith(("/", "..")) for f in files))
    info = overview["index_info"]
    check("index_info dans chaque reponse",
          version.endswith(info["server_version"]) and info["files"] == 4 and info.get("indexed_at"),
          f"{info.get('indexed_at')}")

    # ------------------------------------------- 6. le coeur metier: ui_map ---
    ui = srv.call("ui_map")
    check("ui_map separe panneaux et controles",
          {p["constant"] for p in ui["panels"]["items"]} == {"PANEL_MAIN", "PANEL_CONFIG"})
    panels = {name: srv.call("ui_map", panel=name, limit=100)["panels"][0]
              for name in ("PANEL_MAIN", "PANEL_CONFIG")}
    for detail in panels.values():
        detail["controls"] = detail["controls_page"]["items"]
    start = next(c for c in panels["PANEL_MAIN"]["controls"]
                 if c["constant"] == "PANEL_MAIN_START")
    wiring = start["wired_to"][0]
    check("bouton vers callback vers appels",
          wiring["callback"] == "StartCallback"
          and {"CommsInit", "CommsSendCommand"} <= set(wiring["callback_calls"]),
          f"{wiring['callback']} -> {wiring['callback_calls'][:2]}")
    check("callback cablee dans le .uir signalee",
          [c["name"] for c in ui["callbacks_unwired"]] == ["TimerCallback"])
    check("controle inutilise signale",
          next(c for c in panels["PANEL_CONFIG"]["controls"]
               if c["constant"] == "PANEL_CONFIG_PORT")["unused_in_code"] is True)

    # ------------------------------------------------ 6 bis. analyses --------
    dead = srv.call("dead_code")
    check("dead_code repond par categorie",
          set(dead) >= {"functions", "callbacks", "globals", "macros", "panels"})
    locks = srv.call("lock_check")
    check("lock_check repond sans code", "balance" in locks and "findings" in locks)
    graph = srv.call("module_graph")
    check("module_graph relie les fichiers",
          any(e["from"] == "main.c" and e["to"] == "comms.c" for e in graph["heaviest_edges"]))
    risk = srv.call("complexity", limit=2)
    check("complexity pagine", len(risk["items"]) == 2 and risk["total"] >= 7,
          f"{risk['total']} fonctions")
    config = srv.call("config_map")
    check("config_map lit la configuration", "switches" in config and "configuration" in config)
    risks = srv.call("risks")
    check("risks repond par categorie", "counts" in risks and "findings" in risks)
    found = srv.call("find_symbol", name="StratCallback")
    check("find_symbol propose des noms proches",
          any(s["name"] == "StartCallback" for s in found.get("did_you_mean", [])))
    check("self_check diagnostique", "unresolved_includes" in srv.call("self_check"))

    # ------------------------------------------------ 7. garde de sortie ------
    fn = srv.call("get_function", name="StartCallback", include_body=True)
    check("aucun corps par defaut", "body" not in fn and "body_note" in fn,
          fn.get("body_note", ""))
    check("structure quand meme rendue",
          fn["signature"].startswith("int") and len(fn["params"]) == 6
          and any(c["name"] == "CommsInit" for c in fn["callees"]))

    usages = srv.call("find_usages", name="gMeasCount", kind="write")
    check("find_usages distingue l'ecriture", usages["total"] >= 1)
    graph = srv.call("call_graph", function="StopCallback")["graph"]
    check("call_graph remonte les appeles",
          {e["name"] for e in graph["StopCallback"]} == {"CommsSendCommand", "CommsClose"})

    # ------------------------------------------------------- 8. SDK et diff ---
    indexed = srv.call("sdk_index", paths=str(fixtures / "sdk"))
    check("sdk_index lit les en-tetes", indexed["prototypes"] == 3,
          f"{indexed['prototypes']} prototypes, {indexed['panel_entries_confirmed']} entrees .fp")
    found = srv.call("sdk_lookup", name="FakeDaqReadAnalog")
    check("sdk_lookup rend un prototype",
          any("double *buffer" in r.get("signature", "") for r in found["results"]))

    check("snapshot enregistre", srv.call("snapshot_save", label="recette")["symbols"] > 0)
    check("diff stable sans changement",
          srv.call("diff_versions", before="recette")["identical"] is True)

    variant = ARENA / "v2"
    shutil.copytree(fixtures, variant, ignore=shutil.ignore_patterns("sdk"))
    header = variant / "ui_constants.h"
    header.write_text(header.read_text(encoding="utf-8")
                      .replace("#define PANEL_MAIN_START    3", "#define PANEL_MAIN_START    4"),
                      encoding="utf-8")
    diff = srv.call("diff_versions", before="current", path=str(variant))
    check("changement de valeur de macro detecte",
          {i["name"] for i in diff["value_changed"]} == {"PANEL_MAIN_START"},
          str(diff["summary"]))

    build = srv.call("build_check", dry_run=True)
    check("build_check en diagnostic sans CVI",
          build["project_file"] == "sample.prj" and "command" in build,
          build["mode"])
    srv.close()

    # ------------------------------------------- 9. corps autorise, plafonne --
    env_body = dict(env, CVI_MCP_ALLOW_BODY="1", CVI_MCP_MAX_SNIPPET_LINES="5")
    srv = Server(python, env_body)
    fn = srv.call("get_function", name="StartCallback", include_body=True)
    check("corps rendu quand autorise", "body" in fn)
    check("corps plafonne",
          len(fn["body"].splitlines()) == 5 and "tronque a 5" in fn.get("body_note", ""))
    srv.close()

    # ------------------------------------------------------- 10. journal ------
    entries = [json.loads(line) for line in
               Path(env["CVI_MCP_AUDIT_LOG"]).read_text(encoding="utf-8").splitlines()]
    # La trace doit refleter exactement la recette, --check y compris: ni appel
    # manquant, ni appel en trop.
    expected_trace = [
        "cvi_environment", "project_overview", "project_overview", "ui_map", "ui_map", "ui_map",
        "dead_code", "lock_check", "module_graph", "complexity", "config_map", "risks",
        "find_symbol", "self_check", "get_function",
        "find_usages", "call_graph", "sdk_index", "sdk_lookup", "snapshot_save",
        "diff_versions", "diff_versions", "build_check", "get_function",
    ]
    check("journal fidele a la sequence d'appels",
          [e["tool"] for e in entries] == expected_trace, f"{len(entries)} entrees")
    check("lignes de code comptees",
          any(e["code_lines"] == 5 for e in entries)
          and all(e["code_lines"] == 0 for e in entries if e["tool"] != "get_function"),
          "le journal distingue bien ce qui est sorti")

    # ------------------------------------------------- 11. documentation -----
    readme = (root / "README.md").read_text(encoding="utf-8")
    section = readme.split("Tools\n===")[1].split("How it works")[0]
    table = {name for row in re.findall(r"^\|.*\|$", section, re.MULTILINE)
             for name in re.findall(r"`(\w+)`", row)}
    check("README liste les 38 outils", table == tools, str(tools ^ table))

    pages = {p.stem for p in (root / "wiki").glob("*.md")}
    check("wiki synthetique", len(pages) == 7, f"{len(pages)} fichiers: {sorted(pages)}")
    for page in ("Tools", "Tools-fr"):
        doc = set(re.findall(r"`(\w+)\(",
                  (root / "wiki" / f"{page}.md").read_text(encoding="utf-8")))
        check(f"wiki/{page} couvre les 38 outils", doc == tools, str(tools ^ doc))

    used = set(re.findall(r"\"(CVI_MCP_\w+)\"",
               (root / "src/cvi_mcp/config.py").read_text(encoding="utf-8")))
    for page in ("Home", "Home-fr"):
        cited = set(re.findall(r"\| `(CVI_MCP_\w+)` \|",
                    (root / "wiki" / f"{page}.md").read_text(encoding="utf-8")))
        check(f"wiki/{page} documente toutes les variables", cited == used, str(used ^ cited))

    dead = []
    for page in (root / "wiki").glob("*.md"):
        for target in re.findall(r"\]\((?!https?:)([^)#]+)\)", page.read_text(encoding="utf-8")):
            if target not in pages:
                dead.append(f"{page.name} -> {target}")
    check("liens internes du wiki valides", not dead, str(dead[:3]))

    dead = [t for t in re.findall(r"\]\(\.\./\.\./wiki/([\w-]+)\)", readme) if t not in pages]
    check("liens du README vers le wiki valides", not dead, str(dead[:3]))

    # ------------------------------------------------------ 12. visuels ------
    import xml.etree.ElementTree as ET
    for name in ("banner.svg", "flow.svg"):
        svg = (root / "docs" / name).read_text(encoding="utf-8")
        try:
            ET.fromstring(svg)
            valid = True
        except ET.ParseError:
            valid = False
        check(f"docs/{name} valide", valid)
    flow = (root / "docs" / "flow.svg").read_text(encoding="utf-8")
    check("schema anime", flow.count("<animate") == 6, f"{flow.count('<animate')} animations")
    check("schema a angles droits", "rx=" not in flow)
    check("cartouche aligne sur la version du paquet",
          f">{version.split()[1]}<" in flow, version)
    for name in ("banner.svg", "flow.svg"):
        # rsvg-convert refuse /dev/null, il lui faut un fichier ordinaire.
        out = ARENA / f"{name}.png"
        done = subprocess.run(["rsvg-convert", "-w", "800",
                               str(root / "docs" / name), "-o", str(out)],
                              capture_output=True, text=True, check=False)
        check(f"docs/{name} se rend sans erreur",
              done.returncode == 0 and out.is_file() and out.stat().st_size > 2000,
              done.stderr.strip()[:60] if done.returncode else f"{out.stat().st_size // 1024} Ko")

    # --------------------------------------------------------- 13. marqueurs --
    markers = (chr(0x2014), chr(0x1F916))      # tiret cadratin, emoji robot
    suspects = []
    for path in list(root.rglob("*.py")) + list(root.rglob("*.md")) + list(root.rglob("*.svg")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in markers):
            suspects.append(path.name)
    check("aucun marqueur de generation", not suspects, str(suspects[:3]))

    # -------------------------------------------------------------- bilan -----
    width = max(len(label) for label, _, _ in results)
    print()
    for label, ok, detail in results:
        print(f"  [{'OK ' if ok else 'ECHEC'}] {label.ljust(width)}  {detail}")
    failed = [label for label, ok, _ in results if not ok]
    print(f"\n  {len(results) - len(failed)}/{len(results)} controles passes")
    if failed:
        print("  echecs: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
