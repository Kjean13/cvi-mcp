#include <ansi_c.h>
#include <cvirte.h>
#include <userint.h>
#include "comms.h"
#include "ui_constants.h"

static int panelHandle;
static int configHandle;
static double gMeasurements[1024];
static int gMeasCount = 0;

/* Callback du bouton Demarrer */
int CVICALLBACK StartCallback(int panel, int control, int event,
                              void *callbackData, int eventData1, int eventData2)
{
    char response[256];
    if (event != EVENT_COMMIT)
        return 0;
    CommsInit(1, 9600);
    CommsSendCommand("START\n", response, sizeof(response));
    SetCtrlVal(panelHandle, PANEL_MAIN_STATUS, "Running");
    return 0;
}

/* Callback du bouton Arreter */
int CVICALLBACK StopCallback(int panel, int control, int event,
                             void *callbackData, int eventData1, int eventData2)
{
    if (event != EVENT_COMMIT)
        return 0;
    CommsSendCommand("STOP\n", NULL, 0);
    CommsClose();
    SetCtrlVal(panelHandle, PANEL_MAIN_STATUS, "Stopped");
    return 0;
}

/* Cablee dans le .uir, jamais installee depuis le code */
int CVICALLBACK TimerCallback(int panel, int control, int event,
                              void *callbackData, int eventData1, int eventData2)
{
    double sample;
    if (event != EVENT_TIMER_TICK)
        return 0;
    GetCtrlVal(panelHandle, PANEL_MAIN_GRAPH, &sample);
    RecordMeasurement(sample);
    return 0;
}

/* Enregistre une mesure */
static void RecordMeasurement(double value)
{
    if (gMeasCount < 1024)
    {
        gMeasurements[gMeasCount] = value;
        gMeasCount++;
    }
}

int main(int argc, char *argv[])
{
    if (InitCVIRTE(0, argv, 0) == 0)
        return -1;
    panelHandle = LoadPanel(0, "panel.uir", PANEL_MAIN);
    configHandle = LoadPanel(0, "panel.uir", PANEL_CONFIG);
    DisplayPanel(panelHandle);
    InstallCtrlCallback(panelHandle, PANEL_MAIN_START, StartCallback, 0);
    InstallCtrlCallback(panelHandle, PANEL_MAIN_STOP, StopCallback, 0);
    SetCtrlVal(configHandle, PANEL_CONFIG_BAUD, 9600);
    RunUserInterface();
    DiscardPanel(panelHandle);
    return 0;
}
