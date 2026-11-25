#!/usr/bin/env python3
"""Terminal dashboard for a Galmon station: local ubxtool status, upload stats, and galmon.eu's view of the station."""
import argparse
import curses
import json
import re
import subprocess  # nosec B404
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

OBSERVERS_URL = "https://galmon.eu/observers.json"
REMOTE_EVERY = 60
LOCAL_EVERY = 2
GNSS = {0: ("GPS", "G"), 1: ("SBAS", "S"), 2: ("Galileo", "E"), 3: ("BeiDou", "C"),
        5: ("QZSS", "J"), 6: ("GLONASS", "R")}
OK, WARN, BAD = 1, 2, 3


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False).stdout  # nosec B603
    except (OSError, subprocess.TimeoutExpired):
        return ""


def human_bytes(n):
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1000
    return f"{n:.1f} TB"


def human_secs(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


def service():
    """Return ubxtool's systemd properties and its command-line options."""
    props = dict(line.split("=", 1) for line in run(
        "systemctl", "show", "ubxtool",
        "-p", "ActiveState,SubState,NRestarts,MainPID,ActiveEnterTimestampMonotonic").splitlines() if "=" in line)
    opts = {}
    try:
        with open(f"/proc/{props.get('MainPID', '0')}/cmdline", encoding="utf-8") as f:
            args = f.read().split("\0")
        for flag, val in zip(args, args[1:]):
            if flag in ("--station", "--destination", "-d"):
                opts[flag.lstrip("-")[0]] = val
    except OSError:
        pass
    return props, opts


def upload():
    """Return TCP stats for ubxtool's connection to the Galmon receiver (port 29603)."""
    out = run("ss", "-tinH", "state", "established", "( dport = :29603 )")
    lines = out.splitlines()
    if not lines:
        return None
    stats = {"peer": lines[0].split()[-1]}
    info = " ".join(lines[1:])
    for key in ("bytes_sent", "bytes_acked", "bytes_retrans"):
        if m := re.search(rf"\b{key}:(\d+)", info):
            stats[key] = int(m.group(1))
    if m := re.search(r"\brtt:([\d.]+)", info):
        stats["rtt"] = float(m.group(1))
    return stats


def receiving():
    """Return (time, {gnss: signal count}) from ubxtool's latest 'currently receiving' log line."""
    for line in reversed(run("journalctl", "-u", "ubxtool", "-n", "200", "-o", "cat", "--no-pager").splitlines()):
        if "currently receiving:" in line:
            counts = defaultdict(int)
            for sig in line.split("currently receiving:", 1)[1].split():
                if m := re.match(r"(\d+),\d+@\d+", sig):
                    counts[int(m.group(1))] += 1
            return line.split(" src ", 1)[0], dict(counts)
    return None, {}


class Remote(threading.Thread):
    """Fetch galmon.eu's observer list in the background."""

    def __init__(self):
        super().__init__(daemon=True)
        self.data, self.fetched, self.error = None, 0, None

    def fetch(self):
        try:
            with urllib.request.urlopen(OBSERVERS_URL, timeout=15) as r:  # nosec B310
                self.data, self.fetched, self.error = json.load(r), time.time(), None
        except (OSError, ValueError) as e:
            self.error = str(e)

    def run(self):
        while True:
            self.fetch()
            time.sleep(REMOTE_EVERY)


def service_line(props):
    """Return the ubxtool service status line."""
    up = ""
    if props.get("ActiveState") == "active":
        mono = time.clock_gettime(time.CLOCK_MONOTONIC) - int(props.get("ActiveEnterTimestampMonotonic", 0)) / 1e6
        up = f", up {human_secs(mono)}"
    restarts = int(props.get("NRestarts", 0) or 0)
    running = props.get("SubState") == "running"
    return (f"  ubxtool    {props.get('ActiveState', '?')} ({props.get('SubState', '?')}){up}, {restarts} restarts",
            OK if running and not restarts else WARN if running else BAD)


def local_lines(props, opts, prev):
    """Return lines for ubxtool's service, upload connection and logged signals."""
    now = time.time()
    lines = [("LOCAL", 0), service_line(props)]

    tcp = upload()
    if tcp:
        sent = tcp.get("bytes_acked", tcp.get("bytes_sent", 0))
        rate = ""
        if prev.get("sent") is not None and sent >= prev["sent"]:
            rate = f", {human_bytes((sent - prev['sent']) / (now - prev['t']))}/s"
        prev.update(sent=sent, t=now)
        dest = f"{opts['d']} ({tcp['peer']})" if "d" in opts else tcp["peer"]
        lines.append((f"  upload     connected to {dest}, {human_bytes(sent)} acked{rate}, "
                      f"rtt {tcp.get('rtt', 0):.0f} ms, {human_bytes(tcp.get('bytes_retrans', 0))} retransmitted", OK))
    else:
        prev.clear()
        lines.append(("  upload     not connected to port 29603", BAD))

    when, counts = receiving()
    if counts:
        sigs = ", ".join(f"{GNSS.get(g, (str(g),))[0]} {n}" for g, n in sorted(counts.items()))
        lines.append((f"  receiving  {sigs} signals (as of {when[17:25] if when else '?'})", OK))
    else:
        lines.append(("  receiving  no 'currently receiving' log line yet", WARN))
    return lines


def sat_lines(sats):
    """Return per-constellation summaries and a per-satellite table."""
    lines = []
    svs = sorted(sats, key=lambda s: (s.get("gnss", 0), -s.get("db", 0)))
    by_gnss = defaultdict(list)
    for s in svs:
        by_gnss[s.get("gnss")].append(s)
    for g, group in sorted(by_gnss.items()):
        used = sum(1 for s in group if s.get("used"))
        avg = sum(s.get("db", 0) for s in group) / len(group)
        lines.append((f"  {GNSS.get(g, (str(g),))[0]:<10} {len(group):>2} signals, {used:>2} used, "
                      f"avg {avg:.0f} dB-Hz", OK if avg >= 35 else WARN))

    lines += [("", 0), ("  sat      sig  dB-Hz   elev    azi   prres  qi  used", 0)]
    for s in svs:
        name = f"{GNSS.get(s.get('gnss'), ('', '?'))[1]}{s.get('sv', 0):02d}"
        db = s.get("db", 0)
        lines.append((f"  {name:<8} {s.get('sigid', 0):>3}  {db:>5}  {s.get('elev', 0):>5.1f}  "
                      f"{s.get('azi', 0):>5.1f}  {s.get('prres', 0):>6.1f}  {s.get('qi', 0):>2}  "
                      f"{'yes' if s.get('used') else ''}",
                      OK if db >= 40 else WARN if db >= 30 else BAD))
    return lines


def remote_lines(station, remote):
    """Return lines for galmon.eu's view of the station."""
    lines = [("GALMON.EU", 0)]
    if not remote.data:
        msg = f"  couldn't fetch {OBSERVERS_URL}: {remote.error}" if remote.error else "  fetching..."
        return lines + [(msg, BAD if remote.error else 0)]
    obs = next((o for o in remote.data if str(o.get("id")) == str(station)), None)
    if not obs:
        return lines + [(f"  station {station} not in observers.json (yet)", BAD)]

    now = time.time()
    age = now - obs.get("last-seen", 0)
    lines.append((f"  last seen  {human_secs(age)} ago (list fetched {human_secs(now - remote.fetched)} ago), "
                  f"ubxtool up {human_secs(max(obs.get('uptime', 0), 0))}, git {obs.get('githash', '?')}",
                  OK if age < 120 else WARN if age < 600 else BAD))
    lines.append((f"  clock      offset {obs.get('clockoffsetns', 0):.0f} ns, "
                  f"drift {obs.get('clockdriftns', 0):.0f} ns/s, acc {obs.get('clockacc', 0):.0f} ns, "
                  f"position acc {obs.get('acc', 0):.1f} m", 0))

    return lines + sat_lines(obs.get("svs", {}).values())


def build(station, remote, prev):
    """Return dashboard lines as (text, color) pairs."""
    props, opts = service()
    station = station or opts.get("s")
    header = (f"galmontop · station {station or '?'} · {run('hostname').strip()} · "
              f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    return [(header, 0), ("", 0)] + local_lines(props, opts, prev) + [("", 0)] + remote_lines(station, remote)


def tui(scr, station, remote):
    # Some terminfo entries (e.g. over SSH) lack cursor visibility or colors.
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if curses.has_colors():
        curses.use_default_colors()
        for pair, color in ((OK, curses.COLOR_GREEN), (WARN, curses.COLOR_YELLOW), (BAD, curses.COLOR_RED)):
            curses.init_pair(pair, color, -1)
    scr.timeout(LOCAL_EVERY * 1000)
    prev = {}
    while True:
        lines = build(station, remote, prev)
        scr.erase()
        rows, cols = scr.getmaxyx()
        for y, (text, color) in enumerate(lines[:rows - 1]):
            attr = curses.color_pair(color) if color and curses.has_colors() else curses.A_BOLD if text.isupper() else 0
            scr.addnstr(y, 0, text, cols - 1, attr)
        scr.addnstr(rows - 1, 0, "q quit  r refresh galmon.eu", cols - 1, curses.A_DIM)
        scr.refresh()
        key = scr.getch()
        if key in (ord("q"), 27):
            return
        if key == ord("r"):
            threading.Thread(target=remote.fetch, daemon=True).start()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", help="station ID (default: from the running ubxtool)")
    parser.add_argument("--once", action="store_true", help="print once and exit")
    args = parser.parse_args()
    remote = Remote()
    if args.once:
        remote.fetch()
        for text, _ in build(args.station, remote, {}):
            print(text)
        return
    remote.start()
    curses.wrapper(tui, args.station, remote)


if __name__ == "__main__":
    main()
