# ==============================================================================
#  tests/test_preproc.py
#  Role     Preprocesseur simule: expressions, directives, includes, unites
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp.preproc import (
    ACTIVE,
    INACTIVE,
    UNKNOWN,
    BuildContext,
    Evaluator,
    Macro,
    Preprocessor,
    Unknown,
    evaluate_value,
    scan,
    strip_comments,
)
from cvi_mcp.prj import build_settings, parse_defines
from tests.sample_project import write_sample


def _env(**values):
    return {k: Macro(k, v) for k, v in values.items()}


# ------------------------------------------------------------- expressions -----

@pytest.mark.parametrize(("expr", "expected"), [
    ("1 + 2 * 3", 7),
    ("(1 + 2) * 3", 9),
    ("N > 3 && N < 10", 1),
    ("defined(N) || defined OTHER", 1),
    ("!defined(OTHER)", 1),
    ("N == 4 ? 10 : 20", 10),
    ("0x10 >> 2", 4),
    ("'A' == 65", 1),
    ("010 == 8", 1),
    ("DOUBLE == 8", 1),
    ("-1 < 0", 1),
    ("4UL == 4", 1),
])
def test_evaluator(expr, expected):
    assert Evaluator(_env(N="4", DOUBLE="(N * 2)")).evaluate(expr) == expected


def test_undefined_identifier_is_zero_and_reported():
    evaluator = Evaluator({})
    assert evaluator.evaluate("MISSING > 2") == 0
    assert evaluator.undefined == {"MISSING"}


def test_function_like_macro_is_unknown():
    env = {"F": Macro("F", "x", ["x"])}
    with pytest.raises(Unknown):
        Evaluator(env).evaluate("F(2) > 1")
    with pytest.raises(Unknown):
        Evaluator({}).evaluate("G(2)")


def test_evaluate_value_rejects_text():
    assert evaluate_value('"text"', {}) is None
    assert evaluate_value("(4 * 3)", {}) == 12


# --------------------------------------------------------------- directives ----

def test_scan_joins_continuations_and_ignores_comments():
    text = "#define A \\\n  12\n/* #define HIDDEN 1 */\n// #if 0\n  #  ifdef X\n#endif\n"
    found = [(d.line, d.kind, d.arg) for d in scan(text)]
    assert found == [(1, "define", "A    12"), (5, "ifdef", "X"), (6, "endif", "")]


def test_strip_comments_keeps_lines_and_strings():
    text = 'a = "/* not */"; /* x\ny */ b // c\n'
    out = strip_comments(text)
    assert out.count("\n") == text.count("\n") and '"/* not */"' in out and "x" not in out


# ----------------------------------------------------------------- unites ------

def _files(tmp_path, files: dict) -> list[str]:
    paths = []
    for rel, text in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        paths.append(str(target))
    return paths


def test_header_active_in_one_unit_is_active(tmp_path):
    paths = _files(tmp_path, {
        "h.h": "#ifdef WANT\nint shown;\n#endif\n",
        "a.c": '#define WANT\n#include "h.h"\n',
        "b.c": '#include "h.h"\n',
    })
    pp = Preprocessor(paths, BuildContext())
    pp.run()
    header = pp.results[str((tmp_path / "h.h").resolve())]
    assert header.branches[1].state == ACTIVE and header.branches[1].varies
    assert header.in_build


def test_guard_elif_chain_undef_and_pragma_once(tmp_path):
    paths = _files(tmp_path, {
        "g.h": "#ifndef G_H\n#define G_H\n#define V 2\n#endif\n",
        "o.h": "#pragma once\n#define ONCE 1\n",
        "m.c": ('#include "g.h"\n#include "g.h"\n#include "o.h"\n'
                "#if V == 1\nint one;\n#elif V == 2\nint two;\n#else\nint other;\n#endif\n"
                "#undef V\n#ifdef V\nint gone;\n#endif\n"),
    })
    pp = Preprocessor(paths, BuildContext())
    pp.run()
    guard = pp.results[str((tmp_path / "g.h").resolve())]
    assert guard.guard == "G_H"
    unit = pp.results[str((tmp_path / "m.c").resolve())]
    states = {line: b.state for line, b in unit.branches.items()}
    assert states == {4: INACTIVE, 6: ACTIVE, 8: INACTIVE, 12: INACTIVE}
    assert 7 not in unit.inactive_lines() and 5 in unit.inactive_lines()


def test_forced_value_wins_over_define(tmp_path):
    paths = _files(tmp_path, {"m.c": "#define N 4\n#if N > 8\nint big;\n#endif\n"})
    pp = Preprocessor(paths, BuildContext(forced={"N": "12"}))
    pp.run()
    assert pp.results[str((tmp_path / "m.c").resolve())].branches[2].state == ACTIVE


def test_unknown_branch_keeps_both_variants(tmp_path):
    paths = _files(tmp_path, {"m.c": "#if F(1)\nint a;\n#else\nint b;\n#endif\n"})
    pp = Preprocessor(paths, BuildContext())
    pp.run()
    unit = pp.results[str((tmp_path / "m.c").resolve())]
    assert {b.state for b in unit.branches.values()} == {UNKNOWN}
    assert unit.blank_lines() == set()


def test_include_resolution_is_case_insensitive_and_uses_include_paths(tmp_path):
    paths = _files(tmp_path, {"src/m.c": '#include "LIB.H"\n#include "Other.h"\n',
                              "inc/lib.h": "#define FROM_LIB 1\n"})
    (tmp_path / "third").mkdir()
    (tmp_path / "third" / "other.h").write_text("#define THIRD 1\n")
    pp = Preprocessor(paths, BuildContext(include_paths=[str(tmp_path / "third")]))
    pp.run()
    includes = pp.results[str((tmp_path / "src/m.c").resolve())].includes
    assert includes[1].how == "dossier_du_projet" and includes[2].how == "include_path"
    assert str((tmp_path / "third" / "other.h").resolve()) in pp.reached


def test_predefined_macros_follow_configuration(tmp_path):
    paths = _files(tmp_path, {"m.c": "#ifdef _WIN64\nint w64;\n#endif\n#ifdef _CVI_DEBUG_\nint dbg;\n#endif\n"})
    for is_64, release, expected in ((False, False, (INACTIVE, ACTIVE)), (True, True, (ACTIVE, INACTIVE))):
        pp = Preprocessor(paths, BuildContext(is_64=is_64, is_release=release))
        pp.run()
        unit = pp.results[str((tmp_path / "m.c").resolve())]
        assert (unit.branches[1].state, unit.branches[4].state) == expected


# ---------------------------------------------------------------------- prj ----

def test_parse_defines():
    assert parse_defines('/DA /DB=2 /D"C=x y"') == {"A": "1", "B": "2", "C": "x y"}


def test_build_settings_reads_paths_defines_and_configs(tmp_path):
    prj = write_sample(tmp_path)
    debug = build_settings(prj)
    assert debug["config"] == "Debug" and not debug["is_64"]
    assert debug["defines"] == {"COMMON_DEF": "1", "FROM_PRJ": "2", "FLAG": "1"}
    assert debug["include_paths"][0]["found"].endswith("Lib")
    assert debug["cvi_version"] == "1900"
    release = build_settings(prj, "release64")
    assert release["is_64"] and release["is_release"] and release["defines"]["FROM_PRJ"] == "3"
