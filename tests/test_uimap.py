# ==============================================================================
#  tests/test_uimap.py
#  Role     Cablage interface: panneaux, controles, callbacks, orphelins
#  Etat     stable
#  Version  0.2.0
# ==============================================================================

# ============================ Externe =========================================
import pytest

# ============================ Interne =========================================
from cvi_mcp.uimap import build_ui_map


@pytest.fixture
def ui(indexed):
    return build_ui_map(indexed["db"], indexed["root"])


def _panel(ui, name):
    return next(p for p in ui["panels"] if p["constant"] == name)


def _control(panel, name):
    return next(c for c in panel["controls"] if c["constant"] == name)


def test_panels_are_separated_from_controls(ui):
    assert {p["constant"] for p in ui["panels"]} == {"PANEL_MAIN", "PANEL_CONFIG"}


def test_panel_definition_is_located(ui):
    panel = _panel(ui, "PANEL_MAIN")
    assert panel["value"] == "1"
    assert panel["defined_in"]["file"] == "ui_constants.h"
    assert panel["defined_in"]["line"] > 0


def test_panel_load_sites(ui):
    panel = _panel(ui, "PANEL_MAIN")
    assert [load["via"] for load in panel["loaded_by"]] == ["LoadPanel"]
    assert panel["loaded_by"][0]["in_function"] == "main"


def test_controls_are_attached_to_their_panel(ui):
    main = {c["constant"] for c in _panel(ui, "PANEL_MAIN")["controls"]}
    config = {c["constant"] for c in _panel(ui, "PANEL_CONFIG")["controls"]}
    assert main == {
        "PANEL_MAIN_GRAPH",
        "PANEL_MAIN_START",
        "PANEL_MAIN_STOP",
        "PANEL_MAIN_STATUS",
    }
    assert config == {"PANEL_CONFIG_PORT", "PANEL_CONFIG_BAUD"}


def test_button_to_callback_to_calls(ui):
    """Le resultat attendu: ce bouton declenche cette callback qui appelle ca."""
    start = _control(_panel(ui, "PANEL_MAIN"), "PANEL_MAIN_START")
    assert len(start["wired_to"]) == 1

    wiring = start["wired_to"][0]
    assert wiring["callback"] == "StartCallback"
    assert wiring["via"] == "InstallCtrlCallback"
    assert wiring["in_function"] == "main"
    assert wiring["file"] == "main.c"
    assert {"CommsInit", "CommsSendCommand"} <= set(wiring["callback_calls"])


def test_project_calls_come_before_sdk_calls(ui):
    wiring = _control(_panel(ui, "PANEL_MAIN"), "PANEL_MAIN_START")["wired_to"][0]
    chain = wiring["callback_calls"]
    assert chain.index("CommsInit") < chain.index("SetCtrlVal")


def test_control_access_sites(ui):
    status = _control(_panel(ui, "PANEL_MAIN"), "PANEL_MAIN_STATUS")
    assert not status["wired_to"]
    assert {a["in_function"] for a in status["accessed_by"]} == {
        "StartCallback",
        "StopCallback",
    }
    assert {a["via"] for a in status["accessed_by"]} == {"SetCtrlVal"}


def test_unused_control_is_flagged(ui):
    port = _control(_panel(ui, "PANEL_CONFIG"), "PANEL_CONFIG_PORT")
    assert port["unused_in_code"] is True
    baud = _control(_panel(ui, "PANEL_CONFIG"), "PANEL_CONFIG_BAUD")
    assert baud["unused_in_code"] is False


def test_callback_absent_from_code_is_reported(ui):
    orphans = {c["name"] for c in ui["callbacks_unwired"]}
    assert orphans == {"TimerCallback"}
    assert ui["callbacks_unwired"][0]["file"] == "main.c"
    assert ui["callbacks_unwired"][0]["reason"]
    assert "uir" in ui["callbacks_unwired"][0]["reason"]


def test_wired_callbacks_are_not_reported_as_orphans(ui):
    orphans = {c["name"] for c in ui["callbacks_unwired"]}
    assert "StartCallback" not in orphans
    assert "StopCallback" not in orphans


def test_panel_filter(indexed):
    ui = build_ui_map(indexed["db"], indexed["root"], panel="PANEL_CONFIG")
    assert [p["constant"] for p in ui["panels"]] == ["PANEL_CONFIG"]


def test_unknown_panel_filter_explains(indexed):
    ui = build_ui_map(indexed["db"], indexed["root"], panel="PANEL_ABSENT")
    assert "error" in ui
    assert "PANEL_MAIN" in ui["error"]


def test_generated_uir_header_is_authoritative(tmp_path):
    """Format reel du .h genere par CVI: panneaux, types et callbacks cablees
    dans l'editeur sont lus tels quels, sans heuristique de prefixe."""
    from cvi_mcp.indexer import Indexer
    from cvi_mcp.queries import QueryEngine
    from cvi_mcp.uimap import UiMap

    (tmp_path / "cam.h").write_text(
        "/* LabWindows/CVI User Interface Resource (UIR) Include File */\n"
        "     /* Panels and Controls: */\n"
        "#define  CAM                 1\n"
        "#define  CAM_GAIN            2  /* control type: numeric, callback function: OnGain */\n"
        "#define  CAM_LABEL           3  /* control type: textMsg, callback function: (none) */\n"
        "#define  CAM_SPARE           4  /* control type: numeric, callback function: (none) */\n"
        "#define  CAM_EXIT            5  /* control type: command, callback function: OnExit */\n"
        "     /* Callback Prototypes: */\n"
    )
    (tmp_path / "cam.c").write_text(
        '#include "cam.h"\n'
        "int CVICALLBACK OnGain(int panel, int control, int event, void *d, int a, int b)\n"
        "{ return 0; }\n"
        "int CVICALLBACK Dead(int panel, int control, int event, void *d, int a, int b)\n"
        "{ return 0; }\n"
        'int Open(void) { return LoadPanel(0, "cam.uir", CAM); }\n'
    )
    db = tmp_path / "i.db"
    indexer = Indexer(db)
    indexer.index_files([str(tmp_path / "cam.h"), str(tmp_path / "cam.c")])
    indexer.close()

    engine = QueryEngine(db, tmp_path)
    try:
        ui = UiMap(engine).build()
    finally:
        engine.close()

    assert ui["source"] == "uir_headers"
    assert [p["constant"] for p in ui["panels"]] == ["CAM"]
    controls = {c["constant"]: c for c in ui["panels"][0]["controls"]}
    assert controls["CAM_GAIN"]["type"] == "numeric"
    assert controls["CAM_GAIN"]["wired_to"][0]["callback"] == "OnGain"
    assert controls["CAM_LABEL"]["unused_in_code"] is False
    assert controls["CAM_SPARE"]["unused_in_code"] is True
    assert ui["uir_callbacks_missing_in_code"] == [{"callback": "OnExit", "used_by": ["CAM_EXIT"]}]
    assert [o["name"] for o in ui["callbacks_unwired"]] == ["Dead"]
