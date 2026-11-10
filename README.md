Please view the Jupyter Notebook [here](https://nbviewer.jupyter.org/github/mobilinkd/KenwoodTK-x90/blob/master/Kenwood_TK-790.ipynb).

## AllStarLink remote base (audio + CAT interface)

The [`asl-remote-base`](asl-remote-base/README.md) directory has a design for running a
TK-690/790/890 as a frequency-agile AllStarLink 3 remote base. It covers a DB-25 audio and CAT
interface board, Raspberry Pi Pico firmware, a CAT daemon (it emulates app_rpt's `remote = kenwood`
rig and serves the Hamlib rigctld protocol), ASL3 and Hamlib configuration, and a browser-based
virtual control head ([`hardware/VIRTUAL_HEAD.md`](asl-remote-base/hardware/VIRTUAL_HEAD.md)).
