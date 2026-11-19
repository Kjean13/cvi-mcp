#include "comms.h"
#include <ansi_c.h>

static int gPortHandle = -1;

/* Initialize serial port */
int CVIFUNC CommsInit(int port, int baudRate)
{
    gPortHandle = OpenComConfig(port, "", baudRate, 0, 8, 1, 512, 512);
    if (gPortHandle < 0)
        return -1;
    return 0;
}

/* Send command and read response */
int CVIFUNC CommsSendCommand(const char *cmd, char *response, int maxLen)
{
    int bytes;
    ComWrt(gPortHandle, cmd, (int)strlen(cmd));
    bytes = ComRd(gPortHandle, response, maxLen);
    return bytes;
}

void CVIFUNC CommsClose(void)
{
    if (gPortHandle >= 0)
    {
        CloseCom(gPortHandle);
        gPortHandle = -1;
    }
}
