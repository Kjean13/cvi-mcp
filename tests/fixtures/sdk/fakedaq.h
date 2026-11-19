#ifndef FAKEDAQ_H
#define FAKEDAQ_H

#define FAKEDAQ_VAL_VOLTS 10348

/* Lit un bloc de voies analogiques */
int FakeDaqReadAnalog(int taskHandle, int samples, double timeout, double *buffer);

/* Demarre la tache d'acquisition */
int FakeDaqStartTask(int taskHandle);

int FakeDaqStopTask(int taskHandle);

#endif
