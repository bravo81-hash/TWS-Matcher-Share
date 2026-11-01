"""
Start TWS Matcher whenever Trader Workstation starts.

Runs windowless from the Windows Startup folder (pythonw). Every few seconds it
checks for a TWS / IB Gateway process; when one appears and the dashboard is not
already serving on its port, it runs launch_dashboard.bat -- the same
one-click launcher, so the console window and browser tab behave as usual.

It launches once per TWS session, about 90 seconds after TWS starts, so TWS
has finished logging in and loading its settings first (a client connecting
earlier can be sent fill times without a timezone). If you close TWS Matcher
while TWS is still open, it is not started again until TWS itself restarts.
Activity is logged to tws_watcher.log.
"""

import ctypes
import os
import socket
import subprocess
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LAUNCHER = os.path.join(HERE, "launch_dashboard.bat")
LOG = os.path.join(HERE, "tws_watcher.log")
TWS_IMAGES = ("tws.exe", "ibgateway.exe")
DASHBOARD = ("127.0.0.1", 8787)
POLL_SECS = 5
# Give TWS time to finish logging in and loading its settings. A client that
# connects in the first seconds can receive fill times without a timezone,
# which shifts every fill by the local UTC offset.
SETTLE_SECS = 90
NO_WINDOW = 0x08000000


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


def tws_pids():
    """PIDs of running TWS / IB Gateway processes."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, creationflags=NO_WINDOW, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for line in out.splitlines():
        cells = [c.strip('"') for c in line.split('","')]
        if len(cells) > 1 and cells[0].lower() in TWS_IMAGES:
            pids.add(cells[1])
    return pids


def dashboard_running():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(DASHBOARD) == 0


def launch():
    subprocess.Popen(["cmd", "/c", "start", "TWS Matcher", "/min", LAUNCHER],
                     cwd=HERE, creationflags=NO_WINDOW)


def main():
    # one watcher per login session
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\TWSMatcherWatcher")
    if ctypes.windll.kernel32.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        return
    log("watcher started")
    handled = set()          # TWS pids we have already launched for
    first_seen = {}          # pid -> when the watcher first saw it
    while True:
        pids = tws_pids()
        now = time.time()
        for pid in pids:
            first_seen.setdefault(pid, now)
        first_seen = {pid: t for pid, t in first_seen.items() if pid in pids}
        new = {pid for pid in pids - handled
               if now - first_seen[pid] >= SETTLE_SECS}
        if new:
            if dashboard_running():
                log(f"TWS running (pid {', '.join(sorted(new))}); TWS Matcher already up")
            else:
                log(f"TWS started (pid {', '.join(sorted(new))}); launching TWS Matcher")
                try:
                    launch()
                except OSError as exc:
                    log(f"launch failed: {exc}")
        handled = (handled & pids) | new   # forget pids of TWS sessions that ended
        time.sleep(POLL_SECS)


if __name__ == "__main__":
    main()
