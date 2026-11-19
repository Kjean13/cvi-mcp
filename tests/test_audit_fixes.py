# ==============================================================================
#  tests/test_audit_fixes.py
#  Role     Non-regression des defauts trouves a l'audit de la 0.3.0
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import os
import time

# ============================ Interne =========================================
from cvi_mcp import diffs
from cvi_mcp.indexer import Indexer
from cvi_mcp.prj import parse_prj
from cvi_mcp.queries import QueryEngine

# ------------------------------------------- priorite des operateurs .prj ------

def test_includes_only_collects_include_keys(tmp_path):
    """Un 'A and B or C' faisait tomber dans includes toute valeur contenant
    un antislash, quelle que soit la cle."""
    project = tmp_path / "app.prj"
    project.write_text(
        '[Project Header]\n'
        'Title = "C:\\projets\\mon appli"\n'
        'Target = "dist\\appli.exe"\n'
        '\n[Compiler Options]\n'
        'Include Paths = "..\\commun\\include"\n',
        encoding="utf-8",
    )
    includes = parse_prj(project)["includes"]
    assert includes == ["..\\commun\\include"]


def test_target_and_title_never_land_in_includes(tmp_path):
    project = tmp_path / "app.prj"
    project.write_text('[Linker]\nOutput = "C:\\out\\bin\\app.exe"\n', encoding="utf-8")
    assert parse_prj(project)["includes"] == []


def test_includes_are_deduplicated(tmp_path):
    project = tmp_path / "app.prj"
    project.write_text(
        '[Compiler Options]\n'
        'Include Paths = "..\\commun"\n'
        'Extra Include Paths = "..\\commun"\n',
        encoding="utf-8",
    )
    assert parse_prj(project)["includes"] == ["..\\commun"]


# ------------------------------------------------ chemin rapide d'indexation ---

def test_unchanged_file_is_not_reread(tmp_path, monkeypatch):
    """Taille et date inchangees: aucun octet ne doit etre relu."""
    unit = tmp_path / "unit.c"
    unit.write_text("int Alpha(void) { return 0; }\n", encoding="utf-8")

    indexer = Indexer(tmp_path / "index.db")
    indexer.index_file(unit)

    reads: list[str] = []
    original = type(unit).read_bytes

    def spy(self, *args, **kwargs):
        reads.append(str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(unit), "read_bytes", spy)
    assert indexer.index_file(unit) is False
    assert indexer.needs_reindex(unit) is False
    assert reads == []
    indexer.close()


def test_touched_but_identical_file_falls_back_to_hash(tmp_path):
    """Date modifiee sans changement de contenu: le hash tranche et evite
    une reindexation inutile."""
    unit = tmp_path / "unit.c"
    unit.write_text("int Alpha(void) { return 0; }\n", encoding="utf-8")

    indexer = Indexer(tmp_path / "index.db")
    indexer.index_file(unit)

    future = time.time() + 120
    os.utime(unit, (future, future))
    assert indexer.index_file(unit) is False
    indexer.close()


def test_real_change_is_still_detected(tmp_path):
    unit = tmp_path / "unit.c"
    unit.write_text("int Alpha(void) { return 0; }\n", encoding="utf-8")

    indexer = Indexer(tmp_path / "index.db")
    indexer.index_file(unit)
    unit.write_text("int Alpha(void) { return 1; }\nint Beta(void) { return 0; }\n", encoding="utf-8")
    assert indexer.index_file(unit) is True

    names = {r[0] for r in indexer.db.execute("SELECT name FROM symbols WHERE kind='function'")}
    indexer.close()
    assert names == {"Alpha", "Beta"}


def test_size_and_mtime_are_stored(tmp_path):
    unit = tmp_path / "unit.c"
    unit.write_text("int Alpha(void) { return 0; }\n", encoding="utf-8")

    indexer = Indexer(tmp_path / "index.db")
    indexer.index_file(unit)
    row = indexer.db.execute("SELECT size, mtime FROM files").fetchone()
    indexer.close()
    assert row[0] == unit.stat().st_size
    assert row[1] > 0


# --------------------------------------------- valeurs de macro dans le diff ---

def test_macro_value_change_is_detected(tmp_path):
    """Un identifiant de controle qui change de valeur cassait l'interface
    sans apparaitre dans le diff."""
    before_dir = tmp_path / "v1"
    after_dir = tmp_path / "v2"
    for folder, value in ((before_dir, "3"), (after_dir, "4")):
        folder.mkdir()
        (folder / "ui.h").write_text(
            f"#define PANEL_MAIN 1\n#define PANEL_MAIN_START {value}\n", encoding="utf-8"
        )

    before = diffs.state_of_path(before_dir)
    after = diffs.state_of_path(after_dir)
    report = diffs.compare(before, after, "v1", "v2")

    changed = {item["name"]: item for item in report["value_changed"]}
    assert "PANEL_MAIN_START" in changed
    assert (changed["PANEL_MAIN_START"]["before"], changed["PANEL_MAIN_START"]["after"]) == ("3", "4")
    assert changed["PANEL_MAIN_START"]["kind"] == "macro"
    assert report["summary"]["value_changed"] == 1
    assert report["identical"] is False


def test_unchanged_macro_is_quiet(tmp_path):
    for name in ("v1", "v2"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "ui.h").write_text("#define PANEL_MAIN 1\n", encoding="utf-8")

    report = diffs.compare(
        diffs.state_of_path(tmp_path / "v1"),
        diffs.state_of_path(tmp_path / "v2"),
        "v1",
        "v2",
    )
    assert report["identical"] is True


def test_enumerator_value_change_is_detected(tmp_path):
    for name, value in (("v1", "0"), ("v2", "10")):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "modes.c").write_text(
            f"enum Mode {{ MODE_IDLE = {value}, MODE_RUN }};\n", encoding="utf-8"
        )

    report = diffs.compare(
        diffs.state_of_path(tmp_path / "v1"),
        diffs.state_of_path(tmp_path / "v2"),
        "v1",
        "v2",
    )
    assert {item["name"] for item in report["value_changed"]} == {"MODE_IDLE"}


# ------------------------------------------------ lacunes de couverture --------

def test_removed_file_is_pruned_from_the_index(tmp_path):
    """Un fichier retire du projet ne doit plus repondre dans les requetes."""
    first = tmp_path / "a.c"
    second = tmp_path / "b.c"
    first.write_text("int Alpha(void) { return 0; }\n", encoding="utf-8")
    second.write_text("int Beta(void) { return 0; }\n", encoding="utf-8")

    indexer = Indexer(tmp_path / "index.db")
    indexer.index_files([str(first), str(second)])
    indexer.index_files([str(first)])
    indexer.close()

    engine = QueryEngine(tmp_path / "index.db", tmp_path)
    assert {f["path"] for f in engine.files()} == {"a.c"}
    assert engine.find_symbol("Beta", exact=True) == []
    engine.close()


def test_missing_file_is_reported_not_raised(tmp_path):
    indexer = Indexer(tmp_path / "index.db")
    report = indexer.index_files([str(tmp_path / "absent.c")])
    indexer.close()
    assert report["indexed"] == 0
    assert report["failed"][0]["reason"] == "introuvable"


def test_schema_change_rebuilds_the_index(tmp_path):
    """Un index d'une version anterieure doit etre reconstruit, pas lu de
    travers."""
    import sqlite3

    from cvi_mcp.schema import ensure_schema

    db = tmp_path / "index.db"
    connection = sqlite3.connect(str(db))
    ensure_schema(connection)
    connection.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    indexer = Indexer(db)
    assert indexer.rebuilt is True
    indexer.close()


def test_project_env_accepts_prj_file(tmp_path, monkeypatch):
    """CVI_MCP_PROJECT peut designer le .prj lui-meme, pas seulement son dossier."""
    from cvi_mcp.config import Config
    from cvi_mcp.prj import collect_sources

    (tmp_path / "a.c").write_text("int A(void){return 0;}\n")
    (tmp_path / "b.c").write_text("int B(void){return 0;}\n")
    prj = tmp_path / "one.prj"
    prj.write_text('[File 0001]\nPath = "a.c"\n')
    (tmp_path / "two.prj").write_text('[File 0001]\nPath = "b.c"\n')

    monkeypatch.setenv("CVI_MCP_PROJECT", str(prj))
    cfg = Config()
    assert cfg.project_path == tmp_path.resolve()
    assert cfg.project_files() == [prj.resolve()]
    found = collect_sources(cfg.project_path, None, cfg.project_file)
    assert [p.rsplit("/", 1)[-1] for p in found] == ["a.c"]


def test_prj_entry_resolution_real_layout(tmp_path):
    """Format reel: Path absolu d'une autre machine, Path Rel Path relatif.
    Le relatif prime, puis le nom retrouve dans le dossier, sinon signale."""
    from cvi_mcp.prj import parse_prj

    (tmp_path / "a.c").write_text("int A(void){return 0;}\n")
    (tmp_path / "gen.h").write_text("#define PANEL 1\n")
    prj = tmp_path / "x.prj"
    prj.write_text(
        '[File 0001]\nPath Rel Path = "a.c"\nPath = "/c/Users/autre/a.c"\n\n'
        '[File 0002]\nPath Rel Path = "../../Desktop/p/gen.h"\nPath = "/c/Users/autre/p/gen.h"\n\n'
        '[File 0003]\nPath Rel Path = "absent.c"\nPath = "/c/Users/autre/absent.c"\n'
    )
    info = parse_prj(prj)
    names = [p.rsplit("/", 1)[-1] for p in info["sources"]]
    assert "a.c" in names and "gen.h" in names
    assert [r["listed"].rsplit("/", 1)[-1] for r in info["relocated"]] == ["gen.h"]
    assert info["unresolved"] == ["absent.c"]


def test_prj_long_paths_split_over_lines(tmp_path):
    """CVI coupe les chemins de plus de 80 caracteres en Path Line0001, Line0002."""
    from cvi_mcp.prj import parse_prj

    (tmp_path / "Sources").mkdir()
    (tmp_path / "Sources" / "Calculs.c").write_text("int C(void){return 0;}\n")
    prj = tmp_path / "x.prj"
    prj.write_text(
        '[File 0001]\nFile Type = "CSource"\n'
        'Path Line0001 = "/c/Users/autre/Desktop/v6 - Copy/D"\n'
        'Path Line0002 = "ev/Commun/Sources/Calculs.c"\n\n'
        '[File 0002]\nFile Type = "Library"\nPath Rel Path = "../Commun/Ressources/MBT.lib"\n'
        'Path Line0001 = "/c/Users/autre/Desktop/v6 - Copy/D"\n'
        'Path Line0002 = "ev/Commun/Ressources/MBT.lib"\n\n'
        '[File 0003]\nFile Type = "Function Panel"\nPath Rel Path = "../Commun/x.fp"\n'
    )
    info = parse_prj(prj)
    assert [r["listed"] for r in info["relocated"]] == [
        "/c/Users/autre/Desktop/v6 - Copy/Dev/Commun/Sources/Calculs.c"
    ]
    assert info["libs"] == ["../Commun/Ressources/MBT.lib"]
    assert not any("ev/" in p for p in info["sources"] + info["libs"])


def test_project_files_follow_designated_cws(tmp_path, monkeypatch):
    """Un .cws designe impose ses .prj, meme si d'autres sont dans le dossier."""
    from cvi_mcp.config import Config

    (tmp_path / "A.prj").write_text("[Project Header]\n")
    (tmp_path / "B.prj").write_text("[Project Header]\n")
    cws = tmp_path / "B.cws"
    cws.write_text('[Workspace Header]\nProject 0001 = "B.prj"\n')
    monkeypatch.setenv("CVI_MCP_PROJECT", str(cws))
    assert [p.name for p in Config().project_files()] == ["B.prj"]
