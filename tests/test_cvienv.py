# ==============================================================================
#  tests/test_cvienv.py
#  Role     Detection d'installation CVI, millesime quelconque
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
import platform
from pathlib import Path

# ============================ Interne =========================================
from cvi_mcp import cvienv
from cvi_mcp.config import Config


def _install(root: Path, name: str, with_bin: bool = True) -> Path:
    """Fabrique une arborescence d'installation CVI plausible."""
    base = root / name
    (base / "include").mkdir(parents=True)
    (base / "include" / "userint.h").write_text("int LoadPanel(int, const char *, int);\n", encoding="utf-8")
    (base / "sdk" / "include").mkdir(parents=True)
    if with_bin:
        (base / "bin").mkdir()
        (base / "bin" / "compile.exe").write_bytes(b"")
    return base


def test_any_year_is_detected(tmp_path):
    """Le millesime n'est pas code en dur: 2019, 2020 ou 2029 passent pareil."""
    for name in ("CVI2019", "CVI2020", "CVI2029"):
        _install(tmp_path, name)

    installs = cvienv.find_installs([tmp_path])
    assert {item["version"] for item in installs} == {"2019", "2020", "2029"}


def test_installs_are_sorted_newest_first(tmp_path):
    _install(tmp_path, "CVI2019")
    _install(tmp_path, "CVI2029")
    versions = [item["version"] for item in cvienv.find_installs([tmp_path])]
    assert versions == ["2029", "2019"]


def test_install_reports_its_layout(tmp_path):
    _install(tmp_path, "CVI2019", with_bin=False)
    install = cvienv.find_installs([tmp_path])[0]
    assert install["has_include"] is True
    assert install["has_bin"] is False
    assert install["name"] == "CVI2019"


def test_unnamed_version_does_not_break(tmp_path):
    (tmp_path / "CVI" / "include").mkdir(parents=True)
    install = cvienv.find_installs([tmp_path])[0]
    assert install["version"] is None


def test_files_are_not_mistaken_for_installs(tmp_path):
    (tmp_path / "CVI_readme.txt").write_text("rien", encoding="utf-8")
    assert cvienv.find_installs([tmp_path]) == []


def test_absent_root_is_silent():
    assert cvienv.find_installs([Path("/chemin/absent")]) == []


def test_sdk_dirs_come_from_the_install(tmp_path):
    base = _install(tmp_path, "CVI2029")
    dirs = cvienv.find_sdk_dirs([tmp_path])
    assert base / "include" in dirs
    assert base / "sdk" / "include" in dirs


def test_sdk_dirs_skip_absent_subfolders(tmp_path):
    _install(tmp_path, "CVI2029")
    assert all(path.is_dir() for path in cvienv.find_sdk_dirs([tmp_path]))


def test_sdk_dirs_are_deduplicated(tmp_path):
    _install(tmp_path, "CVI2029")
    dirs = cvienv.find_sdk_dirs([tmp_path, tmp_path])
    assert len(dirs) == len({str(p) for p in dirs})


def test_build_command_found_in_bin(tmp_path):
    base = _install(tmp_path, "CVI2029")
    commands = cvienv.find_build_commands([tmp_path])
    assert base / "bin" / "compile.exe" in commands


def test_build_command_absent_when_not_installed(tmp_path):
    _install(tmp_path, "CVI2019", with_bin=False)
    detected = [p for p in cvienv.find_build_commands([tmp_path]) if tmp_path in p.parents]
    assert detected == []


def test_describe_reports_platform_and_settings(env):
    report = cvienv.describe(Config(), roots=[])
    assert report["platform"]["system"] == platform.system()
    assert report["platform"]["python_bits"] in (32, 64)
    assert report["settings"]["project"].endswith("fixtures")
    assert report["settings"]["allow_body"] is False


def test_describe_warns_outside_windows(env):
    report = cvienv.describe(Config(), roots=[])
    if platform.system() != "Windows":
        assert any("Windows" in warning for warning in report["warnings"])


def test_describe_warns_on_missing_project(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CVI_MCP_PROJECT", str(tmp_path / "nulle-part"))
    report = cvienv.describe(Config(), roots=[])
    assert any("CVI_MCP_PROJECT" in warning for warning in report["warnings"])


def test_describe_warns_when_no_project_file(env, monkeypatch, tmp_path):
    monkeypatch.setenv("CVI_MCP_PROJECT", str(tmp_path))
    report = cvienv.describe(Config(), roots=[])
    assert any("balayage" in warning for warning in report["warnings"])


def test_describe_lists_detected_install(env, tmp_path):
    _install(tmp_path, "CVI2029")
    report = cvienv.describe(Config(), roots=[tmp_path])
    assert report["cvi_installs"][0]["version"] == "2029"
    assert report["build_commands_detected"]
