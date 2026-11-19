# ==============================================================================
#  tests/test_build.py
#  Role     Diagnostic de compilation: resolution de commande et parseur
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import sys
from pathlib import Path

# ============================ Interne =========================================
from cvi_mcp.build import parse_output, resolve_command, run_build


def test_parse_cvi_format():
    report = parse_output(
        "main.c(42,13) : error: undefined identifier 'foo'.\n"
        "comms.c(7,1) : warning: unreferenced variable 'tmp'.\n"
    )
    assert report["errors"] == 1
    assert report["warnings"] == 1

    first = report["diagnostics"][0]
    assert first["file"] == "main.c"
    assert first["line"] == 42
    assert first["col"] == 13
    assert "undefined identifier" in first["message"]


def test_parse_gcc_format():
    report = parse_output("src/comms.c:18:5: error: expected ';'\n")
    item = report["diagnostics"][0]
    assert item["file"] == "src/comms.c"
    assert item["line"] == 18
    assert item["col"] == 5
    assert item["severity"] == "error"


def test_parse_quoted_format():
    report = parse_output('"main.c", line 90: warning: missing return\n')
    item = report["diagnostics"][0]
    assert (item["file"], item["line"], item["severity"]) == ("main.c", 90, "warning")


def test_parse_fatal_is_an_error():
    report = parse_output("main.c(1,1) : fatal error: cannot open 'comms.h'.\n")
    assert report["errors"] == 1


def test_parse_link_stage():
    report = parse_output("LINK : error: unresolved external symbol CommsInit\n")
    item = report["diagnostics"][0]
    assert item["stage"] == "link"
    assert "file" not in item


def test_parse_counts_by_file():
    report = parse_output(
        "main.c(1,1) : warning: a\n"
        "main.c(2,1) : warning: b\n"
        "comms.c(3,1) : error: c\n"
    )
    assert list(report["by_file"]) == ["main.c", "comms.c"]
    assert report["by_file"]["main.c"] == 2


def test_parse_ignores_noise():
    report = parse_output("Building project...\n\nDone.\n")
    assert report["diagnostics"] == []
    assert report["errors"] == 0


def test_resolve_reports_absence():
    resolved = resolve_command("", [])
    assert resolved["available"] is False
    assert "CVI_MCP_BUILD_CMD" in resolved["hint"]


def test_resolve_honours_configuration():
    resolved = resolve_command(sys.executable, [])
    assert resolved["available"] is True
    assert resolved["source"] == "CVI_MCP_BUILD_CMD"


def test_configuration_wins_over_detection(tmp_path):
    """Un millesime detecte ne doit jamais supplanter un choix explicite."""
    detected = tmp_path / "compile.exe"
    detected.write_bytes(b"")
    resolved = resolve_command(sys.executable, [detected])
    assert resolved["command"] == sys.executable


def test_resolve_uses_first_detected_candidate(tmp_path):
    first = tmp_path / "CVI2029" / "compile.exe"
    second = tmp_path / "CVI2019" / "compile.exe"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")

    resolved = resolve_command("", [first, second])
    assert resolved["available"] is True
    assert resolved["source"] == "installation CVI detectee"
    assert Path(resolved["command"]) == first
    assert resolved["other_candidates"] == [str(second)]


def test_resolve_configured_but_missing_is_flagged(tmp_path):
    resolved = resolve_command(str(tmp_path / "compile.exe"), [])
    assert resolved["available"] is False
    assert resolved["source"] == "CVI_MCP_BUILD_CMD"


def test_run_build_parses_a_real_process(tmp_path):
    """Un faux compilateur qui ecrit des diagnostics sur la sortie standard."""
    script = tmp_path / "fakecc.py"
    script.write_text(
        "import sys\n"
        "print('main.c(12,4) : error: undefined identifier Foo.')\n"
        "print('main.c(30,1) : warning: unreferenced variable n.')\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    project = tmp_path / "sample.prj"
    project.write_text("[Project Header]\n", encoding="utf-8")

    report = run_build(project, sys.executable, f"{script} {{project}}", timeout=30)
    assert report["exit_code"] == 1
    assert report["status"] == "erreurs"
    assert (report["errors"], report["warnings"]) == (1, 1)
    assert "main.c" in report["by_file"]
    assert str(project) in report["command_line"]


def test_run_build_reports_success(tmp_path):
    script = tmp_path / "ok.py"
    script.write_text("print('Build succeeded')\n", encoding="utf-8")
    project = tmp_path / "sample.prj"
    project.write_text("[Project Header]\n", encoding="utf-8")

    report = run_build(project, sys.executable, f"{script} {{project}}", timeout=30)
    assert report["status"] == "ok"
    assert report["errors"] == 0


def test_run_build_handles_missing_executable(tmp_path):
    project = tmp_path / "sample.prj"
    project.write_text("[Project Header]\n", encoding="utf-8")
    report = run_build(project, str(tmp_path / "nexistepas"), "{project}", timeout=5)
    assert report["status"] == "echec"


def test_run_build_honours_timeout(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    project = tmp_path / "sample.prj"
    project.write_text("[Project Header]\n", encoding="utf-8")

    report = run_build(project, sys.executable, f"{script} {{project}}", timeout=1)
    assert report["status"] == "timeout"
    assert report["timeout_seconds"] == 1


def test_parse_keeps_directory_and_normalizes_separators(tmp_path):
    root = tmp_path
    text = f"{root.as_posix()}/src/acq.c(17) : error: boom\nsrc\\ui\\a.c(3) : warning: w\n"
    files = [d["file"] for d in parse_output(text, root)["diagnostics"]]
    assert files == ["src/acq.c", "src/ui/a.c"]
