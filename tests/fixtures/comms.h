#ifndef COMMS_H
#define COMMS_H

/* Initialize the serial port for communication */
int CVIFUNC CommsInit(int port, int baudRate);

/* Send a command string and read the response */
int CVIFUNC CommsSendCommand(const char *cmd, char *response, int maxLen);

void CVIFUNC CommsClose(void);

#endif
