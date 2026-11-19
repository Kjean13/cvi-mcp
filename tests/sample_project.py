# ==============================================================================
#  tests/sample_project.py
#  Role     Projet CVI factice, ecrit sur disque, qui couvre preprocesseur,
#           interface, threads, ressources, verrous, INI et SQL
#  Etat     stable
#  Version  0.3.2
# ==============================================================================

# ============================ Standard ========================================
from pathlib import Path

PRJ = """[Project Header]
Version = 1900

[File 0001]
File Type = "CSource"
Path Rel Path = "../Src/main.c"

[File 0002]
File Type = "CSource"
Path Rel Path = "../Src/ui.c"

[File 0003]
File Type = "CSource"
Path Rel Path = "../Src/worker.c"

[File 0004]
File Type = "CSource"
Path Rel Path = "../Src/risky.c"

[File 0005]
File Type = "Include"
Path Rel Path = "../Inc/config.h"

[File 0006]
File Type = "Include"
Path Rel Path = "../Inc/app.h"

[File 0007]
File Type = "Include"
Path Rel Path = "../Uir/panel.h"

[File 0008]
File Type = "Library"
Path Rel Path = "../Lib/extlib.lib"

[Default Build Config Debug]
Config Name = "Debug"
Is 64-Bit = False
Is Release = False
Compiler Defines = "/DFROM_PRJ=2 /DFLAG"

[Default Build Config Release64]
Config Name = "Release64"
Is 64-Bit = True
Is Release = True
Compiler Defines = "/DFROM_PRJ=3"

[Compiler Defines]
Compiler Defines = "/DCOMMON_DEF"

[Include Paths]
Include Path 1 Is Rel = True
Include Path 1 Rel To = "Project"
Include Path 1 Rel Path = "../Lib"
"""

CONFIG_H = """#ifndef CONFIG_H
#define CONFIG_H
#define COUNT 4
//#define OPTION_B
#define DERIVED (COUNT * 2)
#define CHECK(x) if (!(x)) return -1
#define KEY_NAME "Name"
#define SECTION "General"
#define UNUSED_MACRO 12
#define ONLY_IN_IF 1
#endif
"""

APP_H = """#ifndef APP_H
#define APP_H
int Handler1(int x);
int Handler2(int x);
void Third(void);
void Ninth(int n);
void Missing(void);
void Helper(void);
int Worker(void *data);
extern char gLabel1[8], gLabel2[8], gLabel3[8], gLabel4[8];
extern int gShared;
#endif
"""

PANEL_H = """/* LabWindows/CVI User Interface Resource (UIR) Include File */
     /* Panels and Controls: */
#define  PANEL  1
#define  PANEL_OK  2  /* control type: command, callback function: OnOk */
#define  PANEL_VALUE  3  /* control type: numeric, callback function: BadArity */
#define  PANEL_TIMER  4  /* control type: timer, callback function: OnTick */
#define  OTHER  2
#define  OTHER_CTRL  2  /* control type: numeric, callback function: (none) */
#define  SPARE  3
"""

EXTLIB_H = """int ExtCall(int x);
int ExtOther(int y);
"""

MAIN_C = """#include <ansi_c.h>
#include "config.h"
#include "app.h"
#include "panel.h"
#include "extlib.h"
int gShared;
int gUnusedGlobal;
int gWriteOnly;
static int gTable[COUNT];
int (*gHandlers[])(int) = { Handler1, Handler2 };
char gLabel1[8], gLabel2[8], gLabel3[8], gLabel4[8];
int main(void)
{
    int panel = LoadPanel(0, "panel.uir", PANEL);
    int other = LoadPanel(0, "panel.uir", OTHER);
    GetCtrlVal(panel, OTHER_CTRL, &gShared);
    gWriteOnly = 3;
    strcpy(gLabel1, "a");
    strcpy(gLabel2, "b");
#if COUNT > 2
    Third();
#endif
#if COUNT > 8
    Ninth(1);
#endif
#ifdef OPTION_B
    OnlyB();
#endif
#if ONLY_IN_IF
    gTable[0] = DERIVED;
#endif
    CmtScheduleThreadPoolFunction(DEFAULT_THREAD_POOL_HANDLE, Worker, NULL, NULL);
    ExtCall(1);
    Missing();
    RunUserInterface();
    return other;
}
"""

UI_C = """#include "utility.h"
#include "config.h"
#include "app.h"
#include "panel.h"
int CVICALLBACK OnOk(int panel, int control, int event, void *cb, int e1, int e2)
{
    Helper();
    LocalOnly();
    return 0;
}
int CVICALLBACK BadArity(int panel, int control)
{
    return 0;
}
int CVICALLBACK OnTick(int panel, int control, int event, void *cb, int e1, int e2)
{
    return 0;
}
int Handler1(int x) { return x; }
int Handler2(int x) { return x + 1; }
void Third(void) { }
void Ninth(void) { }
int LocalOnly(void) { return 1; }
#ifdef OPTION_B
void OnlyB(void) { }
#endif
void Helper(void)
{
    int h = 0, n = 0;
    Ini_PutString(h, SECTION, KEY_NAME, "v");
    Ini_GetInt(h, "General", "Count", &n);
    Ini_PutInt(h, "General", "Orphan", 1);
    DBActivateSQL(h, "SELECT a FROM mesures WHERE x = 1");
    strcpy(gLabel1, "x");
    strcpy(gLabel2, "x");
    strcpy(gLabel3, "x");
    strcpy(gLabel4, "x");
}
"""

WORKER_C = """#include "config.h"
#include "app.h"
int CVICALLBACK Worker(void *data)
{
    gShared = 2;
    DisplayPanel(1);
    return 0;
}
"""

RISKY_C = """#include <stdio.h>
#include <stdlib.h>
#include "config.h"
int gLockA, gLockB;
static int Leak(int err)
{
    char *p = malloc(10);
    if (p == NULL)
        return -1;
    if (err)
        return -2;
    free(p);
    return 0;
}
static int NoLeak(void)
{
    FILE *f = fopen("a", "r");
    if (!f)
        return -1;
    fclose(f);
    return 0;
}
static char *Escapes(void)
{
    char *p = malloc(4);
    if (!p)
        return NULL;
    return p;
}
static void Twice(void)
{
    char *p = malloc(4);
    if (!p)
        return;
    free(p);
    free(p);
}
static void Copy(const char *src)
{
    char buf[8];
    strcpy(buf, src);
    strcpy(buf, "ok");
    sprintf(buf, "%s", src);
    printf("%d %d", 1);
}
static void Unchecked(void)
{
    char *q = malloc(5);
    q[0] = 1;
    free(q);
}
static void Ignored(void)
{
    OpenFile("x", 1, 2, 1);
}
static int Bitmap(void)
{
    int id;
    int s = NewBitmap(1, 2, 3, 4, 0, 0, 0, &id);
    if (s == 0) {
        DiscardBitmap(id);
    }
    return 0;
}
void Lock(int h) { CmtGetLock(h); }
void Unlock(int h) { CmtReleaseLock(h); }
void UsesWrapper(void)
{
    Lock(gLockA);
    Unlock(gLockA);
}
void AB(void)
{
    CmtGetLock(gLockA);
    CmtGetLock(gLockB);
    CmtReleaseLock(gLockB);
    CmtReleaseLock(gLockA);
}
void BA(void)
{
    CmtGetLock(gLockB);
    CmtGetLock(gLockA);
    MessagePopup("x", "y");
    CmtReleaseLock(gLockA);
    CmtReleaseLock(gLockB);
}
int MacroReturn(int x)
{
    CmtGetLock(gLockA);
    CHECK(x);
    CmtReleaseLock(gLockA);
    return 0;
}
void Split(int a)
{
    CmtGetLock(gLockA);
#ifdef OPTION_B
    if (a > 1) {
#else
    if (a > 0) {
#endif
        CmtReleaseLock(gLockA);
        return;
    }
    CmtReleaseLock(gLockA);
}
"""


def write_sample(root: Path) -> Path:
    """Ecrit le projet sous root et renvoie le chemin du .prj."""
    files = {
        "Projet/App.prj": PRJ,
        "Inc/config.h": CONFIG_H,
        "Inc/app.h": APP_H,
        "Uir/panel.h": PANEL_H,
        "Lib/extlib.h": EXTLIB_H,
        "Lib/extlib.lib": "",
        "Src/main.c": MAIN_C,
        "Src/ui.c": UI_C,
        "Src/worker.c": WORKER_C,
        "Src/risky.c": RISKY_C,
    }
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root / "Projet" / "App.prj"
