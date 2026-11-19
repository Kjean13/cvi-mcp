# ==============================================================================
#  tests/test_windows.py
#  Role     Specificites Windows: encodage des sources, chemins .prj, casse
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
from pathlib import Path

# ============================ Interne =========================================
from cvi_mcp.cparse import body_fingerprint, read_source
from cvi_mcp.indexer import Indexer
from cvi_mcp.prj import parse_prj
from cvi_mcp.queries import QueryEngine

# ----------------------------------------------------------------- encodage ----

def test_cp1252_source_is_decoded(tmp_path):
    """Un commentaire accentue en cp1252, cas courant d'un CVI sous Windows FR."""
    unit = tmp_path / "mesure.c"
    unit.write_bytes(
        "/* Récupère la température du châssis */\n"
        "int LireTemperature(void) { return 0; }\n".encode("cp1252")
    )
    text, encoding, _raw = read_source(unit)
    assert encoding == "cp1252"
    assert "température" in text
    assert "châssis" in text


def test_utf8_bom_is_handled(tmp_path):
    unit = tmp_path / "bom.c"
    unit.write_bytes(b"\xef\xbb\xbfint Alpha(void) { return 0; }\n")
    text, encoding, _raw = read_source(unit)
    assert encoding == "utf-8-sig"
    assert text.startswith("int Alpha")


def test_plain_utf8_is_preferred(tmp_path):
    unit = tmp_path / "utf8.c"
    unit.write_text("/* durée */\nint Beta(void) { return 0; }\n", encoding="utf-8")
    _text, encoding, _raw = read_source(unit)
    assert encoding == "utf-8"


def test_cp1252_comment_reaches_the_index(tmp_path):
    unit = tmp_path / "mesure.c"
    unit.write_bytes(
        "/* Récupère la température du châssis */\n"
        "int LireTemperature(void) { return 0; }\n".encode("cp1252")
    )
    indexer = Indexer(tmp_path / "index.db")
    indexer.index_file(unit)
    indexer.close()

    engine = QueryEngine(tmp_path / "index.db", tmp_path)
    data = engine.get_function("LireTemperature")
    engine.close()
    assert "température" in data["doc"]


def test_mixed_encodings_in_one_project(tmp_path):
    (tmp_path / "a.c").write_bytes("/* été */\nint A(void) { return 0; }\n".encode("cp1252"))
    (tmp_path / "b.c").write_text("/* hiver */\nint B(void) { return 1; }\n", encoding="utf-8")

    indexer = Indexer(tmp_path / "index.db")
    report = indexer.index_files([str(tmp_path / "a.c"), str(tmp_path / "b.c")])
    indexer.close()
    assert report["indexed"] == 2
    assert report["failed"] == []


# ------------------------------------------------------------------ chemins ----

def test_backslash_paths_are_resolved(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "comms.c").write_text("int C(void) { return 0; }\n", encoding="utf-8")
    project = tmp_path / "app.prj"
    project.write_text(
        '[File 0001]\nPath = "src\\comms.c"\n', encoding="utf-8"
    )
    sources = parse_prj(project)["sources"]
    assert len(sources) == 1
    assert Path(sources[0]).name == "comms.c"
    assert Path(sources[0]).is_file()


def test_absolute_windows_path_is_kept(tmp_path):
    project = tmp_path / "app.prj"
    project.write_text(
        '[File 0001]\nPath = "C:\\projets\\legacy\\acq.c"\n', encoding="utf-8"
    )
    sources = parse_prj(project)["sources"]
    # Le chemin reste absolu, il n'est pas colle derriere la racine du projet.
    assert "projets" in sources[0].replace("\\", "/")
    assert str(tmp_path) not in sources[0]


def test_unc_path_is_kept(tmp_path):
    project = tmp_path / "app.prj"
    project.write_text(
        '[File 0001]\nPath = "\\\\serveur\\partage\\acq.c"\n', encoding="utf-8"
    )
    sources = parse_prj(project)["sources"]
    assert "serveur" in sources[0].replace("\\", "/")
    assert str(tmp_path) not in sources[0]


def test_case_variants_are_deduplicated(tmp_path):
    (tmp_path / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    project = tmp_path / "app.prj"
    project.write_text(
        '[File 0001]\nPath = "main.c"\n\n[File 0002]\nPath = "MAIN.C"\n',
        encoding="utf-8",
    )
    sources = parse_prj(project)["sources"]
    # Insensible a la casse: meme fichier. Sensible: MAIN.C est retrouve par son nom.
    assert len(sources) == 1


def test_cp1252_project_file_is_read(tmp_path):
    (tmp_path / "acquisition.c").write_text("int A(void) { return 0; }\n", encoding="utf-8")
    project = tmp_path / "app.prj"
    project.write_bytes(
        '[Project Header]\nTitle = "Chaîne de mesure"\n\n'
        '[File 0001]\nPath = "acquisition.c"\n'.encode("cp1252")
    )
    assert len(parse_prj(project)["sources"]) == 1


# -------------------------------------------------------- fins de ligne --------

def test_crlf_does_not_change_the_fingerprint():
    """Un fichier passe sous un autre editeur ne doit pas paraitre modifie."""
    unix = "{\n    int a = 1;\n    return a;\n}"
    windows = unix.replace("\n", "\r\n")
    assert body_fingerprint(unix) == body_fingerprint(windows)


def test_crlf_source_indexes_identically(tmp_path):
    body = "int Gamma(void)\n{\n    return 42;\n}\n"
    (tmp_path / "unix.c").write_bytes(body.encode("utf-8"))
    (tmp_path / "win.c").write_bytes(body.replace("\n", "\r\n").encode("utf-8"))

    indexer = Indexer(tmp_path / "index.db")
    indexer.index_file(tmp_path / "unix.c")
    indexer.index_file(tmp_path / "win.c")
    hashes = {
        row[0]
        for row in indexer.db.execute(
            "SELECT body_hash FROM symbols WHERE name = 'Gamma'"
        )
    }
    indexer.close()
    assert len(hashes) == 1
