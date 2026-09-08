#!/usr/bin/env python3
"""
chrony_dashboard.py - log chronyc tracking stats to SQLite and serve a
small web dashboard (default port 80) with history charts.

No dependencies beyond the Python 3 stdlib. Chart.js is loaded from a CDN
by the *browser*, not by the Pi.

Environment variables (all optional):
  DASH_PORT        HTTP port                     (default: 80)
  DASH_DB          SQLite path                   (default: $STATE_DIRECTORY/chrony.db
                                                  or ./chrony.db)
  DASH_POLL        poll interval, seconds        (default: 30)
  DASH_RETENTION   days of history to keep       (default: 180)
  DASH_IFACE       network interface(s) to count for the throughput
                   chart, comma-separated (default: all except lo)

NTP server stats (req/s, drops/s, unique clients) come from `chronyc
serverstats` and `chronyc clients`, which need chronyd's admin socket —
run as root or a member of the chrony group (`clients` also requires
client logging to be enabled in chronyd, which it is by default).
Events (reboot, chrony restart, PPS lost/fix, reference change) are
detected by the poller and drawn as dashed vertical lines on all charts.
Network throughput (rx/tx bytes per second from /proc/net/dev) is logged
alongside so NTP traffic surges can be correlated with link load.

CSV export: GET /api/export.csv?hours=N (rows in a window) or
/api/export.csv (everything). To reset the database, stop the service and
delete the .db file (plus its -wal/-shm siblings); see README/notes.
"""

import html
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- config ----

PORT = int(os.environ.get("DASH_PORT", "80"))
POLL_INTERVAL = int(os.environ.get("DASH_POLL", "30"))
RETENTION_DAYS = int(os.environ.get("DASH_RETENTION", "180"))
NET_IFACES = [i.strip() for i in os.environ.get("DASH_IFACE", "").split(",")
              if i.strip()]  # empty = every interface except lo

# Clock display formats for the dashboard page.
LOCAL_CLOCK_12_HOUR = True    # local time as 12-hour with AM/PM (False = 24-hour)
UTC_CLOCK_12_HOUR = False     # UTC is conventionally 24-hour; change if you like

_state_dir = os.environ.get("STATE_DIRECTORY")  # set by systemd StateDirectory=
DB_PATH = os.environ.get(
    "DASH_DB",
    os.path.join(_state_dir, "chrony.db") if _state_dir else "./chrony.db",
)

HOSTNAME = os.uname().nodename
START_TIME = time.time()

# Chart.js is served locally so the dashboard works with no internet at all.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CHARTJS_PATH = os.path.join(SCRIPT_DIR, "chart.umd.min.js")
try:
    with open(CHARTJS_PATH, "rb") as f:
        CHARTJS = f.read()
except OSError:
    CHARTJS = None
    print(f"WARNING: {CHARTJS_PATH} not found - charts will not render. "
          "Place chart.umd.min.js next to this script.", file=sys.stderr)

# ------------------------------------------------------------- database ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracking (
    ts            INTEGER PRIMARY KEY,   -- unix seconds
    stratum       INTEGER,
    sys_offset    REAL,   -- seconds, system time vs NTP
    last_offset   REAL,   -- seconds
    rms_offset    REAL,   -- seconds
    freq_ppm      REAL,
    resid_ppm     REAL,
    skew_ppm      REAL,
    root_delay    REAL,   -- seconds
    root_disp     REAL,   -- seconds
    leap          TEXT,
    ref_name      TEXT,   -- selected reference (e.g. PPS)
    src_reach     INTEGER,-- reach register of selected source (decimal of octal 377 = 255)
    src_err       REAL,   -- estimated error of selected source, seconds
    selected      INTEGER,-- 1 = a source is selected (*), 0 = holdover/none
    combined      INTEGER,-- number of '+' sources diluting the solution
    nmea_offset   REAL,   -- NMEA offset vs system clock (sourcestats), seconds
    nmea_sd       REAL,   -- NMEA std dev (sourcestats), seconds
    temp_c        REAL,   -- SoC temperature, °C
    ntp_rate      REAL,   -- NTP requests answered per second (from serverstats)
    ntp_drop_rate REAL,   -- NTP requests dropped per second
    clients       INTEGER,-- distinct client addresses in chronyd's client log
    clients_act   INTEGER,-- of those, active within CLIENT_ACTIVE_SECS
    net_rx_rate   REAL,   -- bytes/s received (all non-lo or DASH_IFACE)
    net_tx_rate   REAL    -- bytes/s transmitted
);
CREATE INDEX IF NOT EXISTS idx_tracking_ts ON tracking (ts);

CREATE TABLE IF NOT EXISTS events (
    ts     INTEGER,
    kind   TEXT,    -- reboot | chrony restart | pps lost | pps fix | ref change
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts);

-- tiny key/value store so event detection survives restarts of this service
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def ensure_columns(conn):
    """Add columns introduced after the first release to an existing DB."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tracking)")}
    for name, decl in (("selected", "INTEGER"), ("combined", "INTEGER"),
                       ("nmea_offset", "REAL"), ("nmea_sd", "REAL"),
                       ("temp_c", "REAL"), ("ntp_rate", "REAL"),
                       ("ntp_drop_rate", "REAL"), ("clients", "INTEGER"),
                       ("clients_act", "INTEGER"),
                       ("net_rx_rate", "REAL"), ("net_tx_rate", "REAL")):
        if name not in cols:
            conn.execute(f"ALTER TABLE tracking ADD COLUMN {name} {decl}")
    conn.commit()


# --------------------------------------------------------------- polling ----

def chronyc_csv(*args):
    """Run `chronyc -c <args>` and return list of CSV rows (lists of str)."""
    out = subprocess.run(
        ["chronyc", "-c", *args],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    return [line.split(",") for line in out.strip().splitlines() if line]


def read_temp():
    """SoC temperature in °C from sysfs, or None if unavailable."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None


def read_serverstats():
    """Cumulative (ntp_rx, ntp_dropped) counters, or (None, None).

    Field order of `chronyc -c serverstats` starts with NTP packets
    received / dropped in every chrony version; later fields vary.
    Needs the admin socket (root or chrony group).
    """
    try:
        row = chronyc_csv("serverstats")[0]
        return int(row[0]), int(row[1])
    except Exception:
        return None, None


CLIENT_ACTIVE_SECS = 3600  # "active" = NTP packet within the last hour


def read_client_count():
    """(total, active) distinct non-loopback client addresses, or
    (None, None). Total is everyone in chronyd's client log (since its
    last start, LRU-bounded by clientloglimit); active is the subset
    heard from within CLIENT_ACTIVE_SECS. Needs the admin socket and
    client logging (chronyd default)."""
    try:
        rows = chronyc_csv("-n", "clients")
        total = active = 0
        for r in rows:
            if len(r) < 6 or r[0] in ("127.0.0.1", "::1", "localhost"):
                continue
            try:
                if int(r[1]) > 0:  # has sent at least one NTP packet
                    total += 1
                    if float(r[5]) <= CLIENT_ACTIVE_SECS:  # Last column
                        active += 1
            except ValueError:
                continue  # Last is "-" when never seen
        return total, active
    except Exception:
        return None, None


def read_net_counters():
    """Cumulative (rx_bytes, tx_bytes) summed over NET_IFACES (or every
    interface except lo), from /proc/net/dev. (None, None) on failure."""
    try:
        rx = tx = 0
        with open("/proc/net/dev") as f:
            for line in f.readlines()[2:]:
                name, _, rest = line.partition(":")
                name = name.strip()
                if not rest:
                    continue
                if NET_IFACES:
                    if name not in NET_IFACES:
                        continue
                elif name == "lo":
                    continue
                fields = rest.split()
                rx += int(fields[0])
                tx += int(fields[8])
        return rx, tx
    except (OSError, ValueError, IndexError):
        return None, None


def read_boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return None


def boot_time():
    """Unix timestamp of the last boot."""
    with open("/proc/uptime") as f:
        return time.time() - float(f.read().split()[0])


def chronyd_pid():
    try:
        out = subprocess.run(["pidof", "chronyd"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        return int(out.split()[0]) if out else None
    except Exception:
        return None


# ---------------------------------------------------------------- events ----

def meta_get(conn, key):
    row = conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(conn, key, val):
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (key, val))


def log_event(conn, ts, kind, detail=""):
    conn.execute("INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)",
                 (int(ts), kind, detail))
    print(f"event: {kind} {detail}".rstrip(), file=sys.stderr)


def poll_once():
    """Return a dict of one sample, or None on failure."""
    try:
        t = chronyc_csv("tracking")[0]
        # chronyc -c tracking fields:
        # 0 refid  1 refname  2 stratum  3 reftime  4 sys_offset  5 last_offset
        # 6 rms_offset  7 freq  8 resid_freq  9 skew  10 root_delay
        # 11 root_disp  12 update_interval  13 leap
        sample = {
            "ts": int(time.time()),
            "stratum": int(t[2]),
            "sys_offset": float(t[4]),
            "last_offset": float(t[5]),
            "rms_offset": float(t[6]),
            "freq_ppm": float(t[7]),
            "resid_ppm": float(t[8]),
            "skew_ppm": float(t[9]),
            "root_delay": float(t[10]),
            "root_disp": float(t[11]),
            "leap": t[13],
            "ref_name": t[1],
            "src_reach": None,
            "src_err": None,
            "selected": 0,
            "combined": 0,
            "temp_c": read_temp(),
        }
        # Scan all sources: find the selected (*) row, count combined (+) rows.
        for row in chronyc_csv("sources"):
            if len(row) < 10:
                continue
            if row[1] == "*":
                sample["selected"] = 1
                sample["ref_name"] = row[2]
                sample["src_reach"] = int(row[5], 8)  # chronyc prints reach in octal
                sample["src_err"] = float(row[9])
            elif row[1] == "+":
                sample["combined"] += 1
        # NMEA health from sourcestats.
        # chronyc -c sourcestats fields:
        # 0 name  1 NP  2 NR  3 span  4 freq  5 freq_skew  6 offset  7 stddev
        sample["nmea_offset"] = None
        sample["nmea_sd"] = None
        for row in chronyc_csv("sourcestats"):
            if len(row) >= 8 and row[0] == "NMEA":
                sample["nmea_offset"] = float(row[6])
                sample["nmea_sd"] = float(row[7])
                break
        # NTP server load. Raw cumulative counters ride along under private
        # keys; the poller turns them into rates and strips them.
        sample["_ntp_rx"], sample["_ntp_drop"] = read_serverstats()
        sample["ntp_rate"] = None
        sample["ntp_drop_rate"] = None
        sample["clients"], sample["clients_act"] = read_client_count()
        sample["_net_rx"], sample["_net_tx"] = read_net_counters()
        sample["net_rx_rate"] = None
        sample["net_tx_rate"] = None
        return sample
    except Exception as e:  # chronyd down, parse change, etc.
        print(f"poll failed: {e}", file=sys.stderr)
        return None


def poller(stop_event):
    conn = db_connect()
    conn.executescript(SCHEMA)
    ensure_columns(conn)

    # -- startup detection: things that happened while we weren't running --
    bid = read_boot_id()
    prev_bid = meta_get(conn, "boot_id") if bid else None
    rebooted = bool(bid and prev_bid and prev_bid != bid)
    if rebooted:
        log_event(conn, boot_time(), "reboot")
    if bid:
        meta_set(conn, "boot_id", bid)
    pid = chronyd_pid()
    prev_pid = meta_get(conn, "chronyd_pid")
    if pid and prev_pid and int(prev_pid) != pid and not rebooted:
        # chronyd PID changed while we were down (a reboot changes it too,
        # but the reboot event above already covers that case)
        log_event(conn, time.time(), "chrony restart")
    if pid:
        meta_set(conn, "chronyd_pid", str(pid))
    conn.commit()

    prev = None            # previous sample, for state-change detection
    prev_counters = None   # (ts, ntp_rx, ntp_drop) for rate computation
    prev_net = None        # (ts, rx_bytes, tx_bytes) for throughput
    last_prune = 0.0
    while not stop_event.is_set():
        sample = poll_once()
        if sample:
            now_ts = sample["ts"]

            # Cumulative counters -> per-second rates. A counter that goes
            # backwards means chronyd restarted; skip that interval.
            rx, drop = sample.pop("_ntp_rx"), sample.pop("_ntp_drop")
            if rx is not None and prev_counters:
                pts, prx, pdrop = prev_counters
                dt = now_ts - pts
                if dt > 0 and rx >= prx:
                    sample["ntp_rate"] = (rx - prx) / dt
                    sample["ntp_drop_rate"] = max(0, drop - pdrop) / dt
            if rx is not None:
                prev_counters = (now_ts, rx, drop)

            # Same treatment for interface byte counters (they wrap or
            # reset on reboot / interface bounce; skip that interval).
            nrx, ntx = sample.pop("_net_rx"), sample.pop("_net_tx")
            if nrx is not None and prev_net:
                pts, prx, ptx = prev_net
                dt = now_ts - pts
                if dt > 0 and nrx >= prx and ntx >= ptx:
                    sample["net_rx_rate"] = (nrx - prx) / dt
                    sample["net_tx_rate"] = (ntx - ptx) / dt
            if nrx is not None:
                prev_net = (now_ts, nrx, ntx)

            # Event detection: chronyd restart (PID change) and PPS state.
            cur_pid = chronyd_pid()
            if cur_pid and pid and cur_pid != pid:
                log_event(conn, now_ts, "chrony restart")
                meta_set(conn, "chronyd_pid", str(cur_pid))
            if cur_pid:
                pid = cur_pid
            if prev:
                if prev["selected"] and not sample["selected"]:
                    log_event(conn, now_ts, "pps lost", prev.get("ref_name") or "")
                elif sample["selected"] and not prev["selected"]:
                    log_event(conn, now_ts, "pps fix", sample.get("ref_name") or "")
                elif (sample["selected"] and prev["selected"]
                      and sample["ref_name"] != prev["ref_name"]):
                    log_event(conn, now_ts, "ref change",
                              f"{prev['ref_name']} -> {sample['ref_name']}")
            prev = sample

            conn.execute(
                """INSERT OR REPLACE INTO tracking
                   (ts, stratum, sys_offset, last_offset, rms_offset,
                    freq_ppm, resid_ppm, skew_ppm, root_delay, root_disp,
                    leap, ref_name, src_reach, src_err, selected, combined,
                    nmea_offset, nmea_sd, temp_c, ntp_rate, ntp_drop_rate,
                    clients, clients_act, net_rx_rate, net_tx_rate)
                   VALUES (:ts,:stratum,:sys_offset,:last_offset,:rms_offset,
                           :freq_ppm,:resid_ppm,:skew_ppm,:root_delay,
                           :root_disp,:leap,:ref_name,:src_reach,:src_err,
                           :selected,:combined,:nmea_offset,:nmea_sd,:temp_c,
                           :ntp_rate,:ntp_drop_rate,:clients,:clients_act,
                           :net_rx_rate,:net_tx_rate)""",
                sample,
            )
            conn.commit()
        now = time.time()
        if now - last_prune > 6 * 3600:
            cutoff = int(now - RETENTION_DAYS * 86400)
            conn.execute("DELETE FROM tracking WHERE ts < ?", (cutoff,))
            conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            conn.commit()
            last_prune = now
        stop_event.wait(POLL_INTERVAL)
    conn.close()


# ------------------------------------------------------------------ api ----

MAX_POINTS = 600  # downsample history to at most this many buckets


def api_current():
    conn = db_connect()
    row = conn.execute(
        """SELECT ts, stratum, sys_offset, last_offset, rms_offset, freq_ppm,
                  resid_ppm, skew_ppm, root_delay, root_disp, leap, ref_name,
                  src_reach, src_err, selected, combined, nmea_offset, nmea_sd,
                  temp_c, ntp_rate, ntp_drop_rate, clients, clients_act,
                  net_rx_rate, net_tx_rate
           FROM tracking ORDER BY ts DESC LIMIT 1"""
    ).fetchone()
    first = conn.execute("SELECT MIN(ts), COUNT(*) FROM tracking").fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "no samples yet"}
    keys = ["ts", "stratum", "sys_offset", "last_offset", "rms_offset",
            "freq_ppm", "resid_ppm", "skew_ppm", "root_delay", "root_disp",
            "leap", "ref_name", "src_reach", "src_err", "selected", "combined",
            "nmea_offset", "nmea_sd", "temp_c", "ntp_rate", "ntp_drop_rate",
            "clients", "clients_act", "net_rx_rate", "net_tx_rate"]
    d = dict(zip(keys, row))
    d.update(ok=True, hostname=HOSTNAME, poll=POLL_INTERVAL,
             first_ts=first[0], samples=first[1],
             server_now=time.time(),
             service_uptime=int(time.time() - START_TIME))
    return d


def api_history(hours):
    span = int(hours * 3600)
    now = int(time.time())
    since = now - span
    step = max(POLL_INTERVAL, span // MAX_POINTS)
    conn = db_connect()
    rows = conn.execute(
        """SELECT (ts/:step)*:step AS bucket,
                  AVG(last_offset), AVG(rms_offset), AVG(freq_ppm),
                  AVG(skew_ppm), AVG(root_disp), AVG(src_err),
                  MIN(last_offset), MAX(last_offset),
                  AVG(selected), AVG(combined),
                  AVG(nmea_offset), AVG(nmea_sd), AVG(temp_c),
                  AVG(ntp_rate), AVG(ntp_drop_rate), AVG(clients),
                  AVG(clients_act), AVG(net_rx_rate), AVG(net_tx_rate)
           FROM tracking WHERE ts >= :since
           GROUP BY bucket ORDER BY bucket""",
        {"step": step, "since": since},
    ).fetchall()
    conn.close()
    # Pad the full window: one entry per bucket from `since` to now, with
    # nulls where no samples exist, so short histories show against the
    # complete time axis instead of stretching to fill it.
    by_bucket = {r[0]: r for r in rows}
    empty = (None,) * 19
    buckets = range((since // step) * step, now + step, step)
    padded = [(b,) + tuple(by_bucket.get(b, (b,) + empty)[1:]) for b in buckets]
    return {
        "ok": True,
        "step": step,
        "t":        [r[0] for r in padded],
        "offset":   [r[1] for r in padded],
        "rms":      [r[2] for r in padded],
        "freq":     [r[3] for r in padded],
        "skew":     [r[4] for r in padded],
        "rootdisp": [r[5] for r in padded],
        "srcerr":   [r[6] for r in padded],
        "off_min":  [r[7] for r in padded],
        "off_max":  [r[8] for r in padded],
        "lock":     [r[9] for r in padded],
        "combined": [r[10] for r in padded],
        "nmea_off": [r[11] for r in padded],
        "nmea_sd":  [r[12] for r in padded],
        "temp":     [r[13] for r in padded],
        "ntp_rate": [r[14] for r in padded],
        "ntp_drop": [r[15] for r in padded],
        "clients":  [r[16] for r in padded],
        "clients_act": [r[17] for r in padded],
        "net_rx":   [r[18] for r in padded],
        "net_tx":   [r[19] for r in padded],
    }


def api_events(hours):
    since = int(time.time() - hours * 3600)
    conn = db_connect()
    rows = conn.execute(
        "SELECT ts, kind, detail FROM events WHERE ts >= ? ORDER BY ts",
        (since,),
    ).fetchall()
    conn.close()
    return {"ok": True, "events": [
        {"ts": r[0], "kind": r[1], "detail": r[2]} for r in rows
    ]}


EXPORT_COLS = ["ts", "stratum", "sys_offset", "last_offset", "rms_offset",
               "freq_ppm", "resid_ppm", "skew_ppm", "root_delay", "root_disp",
               "leap", "ref_name", "src_reach", "src_err", "selected",
               "combined", "nmea_offset", "nmea_sd", "temp_c", "ntp_rate",
               "ntp_drop_rate", "clients", "clients_act", "net_rx_rate",
               "net_tx_rate"]


def api_export_csv(hours=None):
    """Raw (not downsampled) tracking rows as CSV bytes. hours=None dumps
    the whole table; otherwise rows within the last `hours`. A human-
    readable UTC timestamp is prepended to the raw unix `ts` column."""
    import csv
    import io
    from datetime import datetime, timezone
    conn = db_connect()
    cols = ", ".join(EXPORT_COLS)
    if hours is None:
        cur = conn.execute(f"SELECT {cols} FROM tracking ORDER BY ts")
    else:
        since = int(time.time() - hours * 3600)
        cur = conn.execute(f"SELECT {cols} FROM tracking WHERE ts >= ? ORDER BY ts",
                           (since,))
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["utc"] + EXPORT_COLS)
    for row in cur:
        utc = datetime.fromtimestamp(row[0], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        w.writerow([utc] + list(row))
    conn.close()
    return buf.getvalue().encode()


def api_sources():
    """Live `chronyc -c sources` for the debug section."""
    try:
        rows = chronyc_csv("sources")
        return {"ok": True, "ts": time.time(), "sources": [
            {"mode": r[0], "state": r[1], "name": r[2], "stratum": int(r[3]),
             "poll": int(r[4]), "reach": int(r[5], 8), "lastrx": r[6],
             "adj_offset": float(r[7]), "meas_offset": float(r[8]),
             "est_err": float(r[9])}
            for r in rows if len(r) >= 10
        ]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------------ page ----

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__HOSTNAME__ · chrony</title>
<script src="/chart.umd.min.js"></script>
<style>
/* 7-segment LED font for the TrueTime theme clock. Loads from the internet
   with monospace fallback — everything else on this page stays local. */
@font-face{
  font-family:"DSEG7";
  src:url("https://cdn.jsdelivr.net/npm/dseg@0.46.0/fonts/DSEG7-Classic/DSEG7Classic-Regular.woff2") format("woff2");
  font-display:swap;
}
:root{
  --bg:#0d1210;        /* instrument chassis */
  --panel:#131a17;     /* recessed panel */
  --line:#243029;      /* hairline rules */
  --ink:#9db4aa;       /* engraved label gray-green */
  --dim:#5c6f66;
  --bright:#cfe3da;    /* high-emphasis values */
  --tipbg:#1b2420;
  --vfd:#7ce8b4;       /* vacuum-fluorescent green */
  --amber:#f0b45a;     /* frequency channel */
  --cyan:#6fd3e0;      /* dispersion channel */
  --bad:#e07a6f;
}
:root[data-theme="light"]{
  --bg:#eef1ee;
  --panel:#f9faf9;
  --line:#d3dbd5;
  --ink:#3f4f47;
  --dim:#7d8b83;
  --bright:#1c2a24;
  --tipbg:#ffffff;
  --vfd:#0c7a4b;
  --amber:#a3660c;
  --cyan:#0c7181;
  --bad:#b3372c;
}
:root[data-theme="truetime"]{
  --bg:#e9e4d3;        /* chassis beige (bright 90s putty) */
  --panel:#b4c74d;     /* backlit STN LCD green */
  --line:#7f8747;      /* darker LCD/engraving lines */
  --ink:#33401a;
  --dim:#5f683a;
  --bright:#1a2408;    /* LCD segment near-black-green */
  --tipbg:#c6d573;
  --vfd:#243610;
  --amber:#7d4a10;
  --cyan:#1c5a52;
  --bad:#a3231a;
  /* front-panel silkscreen: light geometric caps, wide tracking
     (matches the XL-AK's "STATUS" / "GPS TIME & FREQUENCY" lettering) */
  --ttfont:"Century Gothic","CenturyGothic","Avenir Next",Avenir,
    Futura,"Futura PT","URW Gothic","Avant Garde",Questrial,Verdana,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{
  background:var(--bg);color:var(--ink);
  font:400 14px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,"DejaVu Sans Mono",monospace;
  padding:20px clamp(12px,3vw,40px) 48px;
}
a{color:inherit}
.mast{display:flex;align-items:baseline;justify-content:space-between;
  flex-wrap:wrap;gap:8px;border-bottom:1px solid var(--line);padding-bottom:14px}
.mast h1{font-size:15px;font-weight:600;letter-spacing:.18em;text-transform:uppercase}
.mast h1 b{color:var(--vfd);font-weight:600}
.mast .meta{font-size:11px;color:var(--dim);letter-spacing:.08em}
.leap-bad{color:var(--bad)}

/* signature: the VFD error readout */
.readout{margin:26px auto 30px;text-align:center}
.readout .val{
  font-size:clamp(44px,9vw,84px);font-weight:300;letter-spacing:.02em;
  color:var(--vfd);line-height:1;
  font-variant-numeric:tabular-nums;
  transition:opacity .25s ease;
}
.readout .val.swap{opacity:0}
.readout .val.hold{color:var(--bad)}
.val .odo,.val .st{display:inline-block;height:1em;line-height:1;vertical-align:top}
.val .odo{overflow:hidden}
.val .reel{display:block;transition:transform 2s cubic-bezier(.25,.9,.3,1)}
.val .reel span{display:block;height:1em;line-height:1}
@media (prefers-reduced-motion: reduce){
  .val .reel{transition:none}
  .readout .val{transition:none}
}
.readout .lbl{font-size:11px;letter-spacing:.32em;text-transform:uppercase;
  color:var(--dim);margin-top:6px}
.clock{margin-top:18px;font-variant-numeric:tabular-nums;
  display:inline-flex;align-items:center;gap:22px;text-align:left;
  background:var(--panel);border:1px solid var(--line);border-radius:4px;
  padding:12px 22px}
.clock .utc{font-size:26px;color:var(--bright);letter-spacing:.06em}
.clock .utc small,.clock .loc small{font-size:11px;color:var(--dim);
  letter-spacing:.22em;text-transform:uppercase;margin-left:8px}
.clock .loc{font-size:15px;color:var(--ink);margin-top:2px;letter-spacing:.06em}
.pps{display:flex;flex-direction:column;align-items:center;gap:6px}
.led{width:10px;height:10px;border-radius:50%;background:#5a2019}
.led.on{background:#ff2f00;box-shadow:0 0 8px rgba(255,47,0,.55)}
.ledlbl{font-size:9px;letter-spacing:.22em;color:var(--dim)}

.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  border:1px solid var(--line);border-radius:4px;overflow:hidden;margin-bottom:30px}
.cell{padding:12px 14px;border-right:1px solid var(--line);
  border-bottom:1px solid var(--line);background:var(--panel)}
.cell .k{font-size:10px;letter-spacing:.22em;text-transform:uppercase;color:var(--dim)}
.cell .v{font-size:17px;margin-top:4px;color:var(--bright);font-variant-numeric:tabular-nums}
.cell .v small{font-size:11px;color:var(--dim)}

.ranges{display:flex;gap:6px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.ranges span{font-size:10px;letter-spacing:.22em;text-transform:uppercase;
  color:var(--dim);margin-right:8px}
.ranges button{
  font:600 11px/1 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.1em;
  color:var(--ink);background:var(--panel);border:1px solid var(--line);
  border-radius:3px;padding:7px 12px;cursor:pointer}
.ranges button:hover{border-color:var(--vfd)}
.ranges button:focus-visible{outline:2px solid var(--vfd);outline-offset:2px}
.ranges button.on{color:var(--bg);background:var(--vfd);border-color:var(--vfd)}

.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:4px;
  padding:14px;position:relative}
.chart h2{font-size:10px;font-weight:600;letter-spacing:.24em;
  text-transform:uppercase;color:var(--vfd);margin-bottom:10px;
  display:inline-flex;align-items:center;gap:7px}
.chart h2 small{font-size:10px;font-weight:600;color:var(--dim);text-transform:none}
.info{width:15px;height:15px;flex:none;border-radius:50%;
  border:1px solid var(--dim);background:none;color:var(--dim);
  font:600 9px/1 ui-monospace,Menlo,Consolas,monospace;
  cursor:pointer;padding:0}
.info:hover{border-color:var(--vfd);color:var(--vfd)}
.info:focus-visible{outline:2px solid var(--vfd);outline-offset:2px}
.tip{display:none;position:absolute;top:34px;left:14px;right:14px;z-index:5;
  background:var(--tipbg);border:1px solid var(--line);border-radius:4px;
  padding:10px 12px;font-size:11px;line-height:1.5;color:var(--ink);
  letter-spacing:0;text-transform:none;box-shadow:0 4px 14px rgba(0,0,0,.4)}
.tip.show{display:block}
.chart .wrap{position:relative;height:190px}
footer{margin-top:34px;font-size:10px;color:var(--dim);letter-spacing:.08em}

.settings{margin-top:30px;border:1px solid var(--line);border-radius:4px;
  background:var(--panel)}
.settings summary{font-size:10px;font-weight:600;letter-spacing:.24em;
  text-transform:uppercase;color:var(--vfd);padding:12px 14px;cursor:pointer;
  list-style:none;user-select:none}
.settings summary::-webkit-details-marker{display:none}
.settings summary::before{content:"+";display:inline-block;width:14px;color:var(--dim)}
.settings[open] summary::before{content:"−"}
.settings summary:focus-visible{outline:2px solid var(--vfd);outline-offset:-2px}
.setbody{padding:4px 14px 16px;border-top:1px solid var(--line)}
.setrow{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:14px}
.setlbl{font-size:10px;letter-spacing:.22em;text-transform:uppercase;
  color:var(--dim);min-width:60px}
.seg{display:flex;gap:6px}
.seg button,.seg a{
  font:600 11px/1 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.1em;
  color:var(--ink);background:var(--bg);border:1px solid var(--line);
  border-radius:3px;padding:7px 12px;cursor:pointer}
.seg button:hover{border-color:var(--vfd)}
.seg button.on{color:var(--bg);background:var(--vfd);border-color:var(--vfd)}
.seg button:focus-visible,.seg a:focus-visible{outline:2px solid var(--vfd);outline-offset:2px}
.seg a{text-decoration:none;display:inline-block}
.seg a:hover{border-color:var(--vfd)}
.sethint{font-size:10px;color:var(--dim);letter-spacing:.04em}

/* ---- TrueTime theme component styling -------------------------------- */
#ttlogo,#ttcap{display:none}
[data-theme="truetime"] #ttlogo{display:block}
[data-theme="truetime"] #ttcap{display:inline;font-family:var(--ttfont);
  font-weight:400;font-size:11px;letter-spacing:.34em;color:#1e2f8f;
  text-transform:uppercase;white-space:nowrap;align-self:center}
[data-theme="truetime"] .mast h1{display:none}
/* silkscreen lettering for engraved labels (values stay monospace/DSEG7) */
[data-theme="truetime"] .lbl,
[data-theme="truetime"] .cell .k,
[data-theme="truetime"] .ledlbl,
[data-theme="truetime"] .chart h2,
[data-theme="truetime"] .debug h2,
[data-theme="truetime"] .settings summary,
[data-theme="truetime"] .setlbl,
[data-theme="truetime"] .ranges span,
[data-theme="truetime"] .mast .meta,
[data-theme="truetime"] #srctbl th,
[data-theme="truetime"] .ranges button,
[data-theme="truetime"] .seg button,
[data-theme="truetime"] .seg a{font-family:var(--ttfont)}
[data-theme="truetime"] .readout .val{
  display:inline-block;background:var(--panel);color:var(--bright);
  border:2px solid #8a8574;border-radius:4px;padding:8px 26px;
  box-shadow:inset 0 3px 10px rgba(0,0,0,.35);
  font-size:clamp(30px,6vw,56px);font-weight:600}
[data-theme="truetime"] .clock{
  background:#170b07;border:2px solid #8a8574;
  border-radius:4px;padding:12px 24px;
  box-shadow:inset 0 3px 12px rgba(0,0,0,.75)}
[data-theme="truetime"] .ledlbl{color:#8a5c42}
[data-theme="truetime"] .clock .utc,
[data-theme="truetime"] .clock .loc{
  font-family:"DSEG7",ui-monospace,Menlo,monospace;color:#ff2f00}
[data-theme="truetime"] .clock .loc{color:#d94a17;font-size:14px;margin-top:8px;
  text-align:center}
[data-theme="truetime"] .clock .utc small,
[data-theme="truetime"] .clock .loc small{
  font-family:var(--ttfont);color:#8a5c42}
[data-theme="truetime"] .clock .ap{
  font-family:var(--ttfont);
  font-size:12px;font-weight:400;color:#d94a17;letter-spacing:.08em}
[data-theme="truetime"] .ranges button,
[data-theme="truetime"] .seg button,
[data-theme="truetime"] .seg a{
  background:#1d1d1b;color:#eae6d8;border-color:#000}
[data-theme="truetime"] .ranges button.on,
[data-theme="truetime"] .seg button.on{
  background:var(--panel);color:var(--bright);border-color:#000}

.debug{margin-top:30px}
.debug h2{font-size:10px;font-weight:600;letter-spacing:.24em;
  text-transform:uppercase;color:var(--vfd);margin-bottom:10px}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:4px;
  background:var(--panel)}
#srctbl{width:100%;border-collapse:collapse;font-size:12px;
  font-variant-numeric:tabular-nums;white-space:nowrap}
#srctbl th{font-size:10px;font-weight:600;letter-spacing:.16em;
  text-transform:uppercase;color:var(--dim);text-align:left;
  padding:9px 12px;border-bottom:1px solid var(--line)}
#srctbl td{padding:7px 12px;border-bottom:1px solid var(--line);color:var(--ink)}
#srctbl tr:last-child td{border-bottom:none}
#srctbl tr.sel td{color:var(--bright)}
#srctbl tr.sel td:first-child{color:var(--vfd)}
</style>
</head>
<body>
<div class="mast">
  <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <svg id="ttlogo" viewBox="25 115 1140 205" width="150" role="img" aria-label="TrueTime Pi logo">
      <!-- "TrueTime" traced from the XL-AK front panel silkscreen; "Pi" drawn to match -->
      <path fill="#17246e" d="M72 126c-6 1-9 5-15 14c-11 21-12 21 25 21c21 0 22 0 24 2c2 3 1 6-3 14c-1 1-3 4-4 5c0 2-2 5-4 7c-1 3-3 6-4 8c-3 4-3 6-7 11c-1 2-4 7-5 10c-2 3-5 8-6 10c-1 2-4 6-5 9c-4 6-10 16-12 19c-2 3-4 6-7 12c-3 6-7 13-10 19c-7 10-5 12 10 12c13 1 16 0 21-8c0-1 3-5 4-7c2-3 4-6 5-8c1-1 3-4 4-6c1-1 3-4 4-6c1-2 3-5 4-7c4-5 6-9 8-14c2-3 4-6 5-8c2-2 4-6 5-9c2-3 4-6 4-7c1-2 2-4 3-5c1-2 2-5 4-6c3-5 4-7 6-11c1-2 4-6 5-8c1-3 4-6 5-9c8-15 10-17 18-18c3-1 13-1 19-1c3 1 7 1 16 1l12 0l1-1c4-1 7-4 11-11c1-2 3-6 4-8c4-8 4-8 4-10l1-1l-1-1c-2-2-1-2-32-2c-21 0-29 0-33 0c-4-1-14-1-33-1c-17 0-27 0-28-1c-2 0-21 0-23 0z M461 130c-5 1-7 3-12 12c-1 3-3 6-4 8c-2 2-3 4-3 5l-1 2l0 2l0 2l0 1c1 0 2 1 3 2c1 0 8 0 20 0c29 0 31 0 32 4l1 2l0 1c0 2-1 4-4 9c-2 2-3 5-4 7c-2 4-3 6-8 13c-2 4-5 9-6 10c-1 2-3 5-4 7c-1 2-3 6-5 9c-1 2-3 5-4 7c-2 3-3 5-5 9c-1 1-3 4-4 6c-1 2-3 5-4 7c-1 2-3 5-4 8c-2 2-4 5-5 7c-1 2-3 6-6 10c-3 5-5 8-6 12l-3 6l0 1c0 5 5 6 19 5c10 0 12-1 18-12c1-2 3-4 4-6c3-3 4-5 8-11c3-7 7-13 11-19c1-2 3-6 4-8c2-3 4-7 6-10c2-4 4-7 5-9c1-2 3-5 4-7c2-3 4-6 4-7c1-1 3-5 5-8c4-5 10-17 14-23c2-3 4-6 9-12c2-3 4-5 9-5l2-1l22 0l22 0l2-1c4-1 7-4 14-17c1-2 3-5 4-7c3-5 3-7 0-9c-1 0-2 0-10 0l-5 0l-6 0l-5-1l-24 0l-24 0l-4 0l-4-1l-10 0c-7 1-11 1-14 0c-6 0-42 0-44 0z M197 176c-1 1-6 1-9 1c-18 1-31 9-40 23c-1 2-3 5-4 6c0 1-2 5-4 7c-1 3-3 6-4 8c-3 5-4 6-6 10c-2 2-3 5-4 7c-1 2-3 4-4 6c-1 1-2 4-3 6c-5 8-7 12-10 18c-5 7-5 8-8 12c-8 14-9 17-6 19l1 0l2 0c1 0 3 0 5 0c2 0 4 0 6 0c13 3 16 1 25-15c1-3 3-6 4-7c3-4 4-6 7-12c2-3 4-7 6-10c3-4 4-6 7-12c1-1 2-4 4-6c1-2 3-5 4-7c1-2 3-5 4-6c0-2 2-5 3-6c3-6 5-9 9-12c4-2 6-3 13-3c12 0 14-1 20-15l3-5l0-2c0-2-1-3-4-4c-4-1-13-1-17-1z M247 177c-5 1-7 4-12 12c-1 2-3 5-5 8c-1 2-4 5-5 8c-1 2-2 5-3 6c0 1-2 4-3 6c-2 3-4 6-5 9c-3 3-4 5-7 12c-1 2-2 3-4 6c-1 2-3 4-3 5c-3 6-8 15-11 19c-3 5-4 6-5 9l-1 2l0 4l0 3l1 2c2 7 6 11 12 13l1 0l7 0c4 0 8 0 9 1c1 0 4 0 6 0l4 0l3-1c4-1 4-1 7 0c7 1 12 2 16 1c13-3 14-3 22-9c9-7 12-9 17-20c1-2 3-4 4-6c2-3 3-4 5-7c2-5 7-13 10-17c5-7 8-12 10-16c3-6 5-10 12-20c1-3 4-7 5-9c2-2 4-6 5-8c3-4 3-6 3-8l0-1l-1-1c0-1-1-1-2-1l-1-1l-11 0l-11 0l-2 1c-5 1-7 3-11 11c-3 4-3 4-6 9c-3 4-5 8-7 13c-2 2-3 5-5 7c-2 4-3 5-6 9c-1 2-3 7-6 11c-2 4-5 9-6 11c-7 11-12 16-18 17l-1 1l-5 0c-7 0-10-1-12-3l-1-2l0-1c0-3 1-5 6-13c2-3 4-7 5-9c2-4 8-14 11-17c1-2 2-5 4-8c1-2 3-4 4-6c4-6 13-23 14-25c1-4 0-6-4-7c-2 0-21 0-23 0z M393 179c-8 1-18 4-22 7c-10 6-16 14-22 24c-1 2-3 4-3 5c-1 1-3 4-4 7c-2 3-5 7-6 10c-1 2-4 5-5 8c-1 2-3 6-5 8c-3 5-6 9-9 16c-2 3-4 6-5 7c-5 8-6 16-2 22c6 10 7 10 39 10c21 0 21 0 25-1c4-2 4-2 9-2c8-1 9-2 17-7c7-6 10-11 17-24c3-6 0-8-16-8c-11 0-12 0-17 3c-9 5-21 7-26 2c-4-4 2-16 10-18c3 0 11-1 15 0c5 0 41 0 43 0c5-1 10-7 21-28c1-2 3-6 5-9c6-10 7-14 5-19c-4-9-10-12-24-13c-13 0-18 0-23 1c-3 1-4 1-8 0c-3-1-7-1-9-1zM410 207c8 4 0 17-11 18c-12 2-17 1-17-5c0-9 8-14 22-14l4 0l2 1z M559 181c-8 1-13 5-21 22c-1 3-3 6-6 10c-5 8-6 10-10 18c-2 5-4 8-6 11c-5 7-8 11-10 15c-1 2-3 5-3 6c-2 2-2 3-4 8c-1 2-3 5-4 7c-2 2-3 5-4 7c-1 1-2 4-4 6c-3 5-3 6-3 8l0 2l0 1c1 1 1 1 2 2l1 0l5 1c16 0 19 0 22-4c4-4 10-12 13-17c2-5 7-12 10-16c1-3 3-6 5-9c1-2 3-6 4-8c5-7 9-14 11-18c1-3 3-6 4-7c1-2 4-5 5-8c2-2 4-6 5-8c14-22 15-27 10-28c-2-1-18-2-22-1z M643 182c-2 0-5 0-8 0c-10 0-14 1-24 8c-4 2-6 4-10 8c-5 6-5 6-12 18c-2 3-4 7-5 9c-1 1-3 4-4 5c-3 5-7 13-8 16c-3 5-6 10-9 15l-4 5l-4 8l-4 8l-3 6c-10 15-8 20 6 17c1 0 5-1 8-1c9-1 11-2 14-5c2-3 11-18 14-23c1-3 3-6 4-7c2-4 4-6 6-9c1-2 2-4 3-6c1-1 3-4 4-5c1-2 2-6 4-8c19-33 19-33 33-33l6 0l1 0c5 2 6 7 2 13c-1 2-4 6-6 10c-2 5-5 9-7 11c-2 3-3 6-5 10c-1 3-2 4-5 8c-2 4-4 8-6 11c-1 2-2 5-3 6c-1 2-3 4-4 6c-1 2-2 4-3 6c-6 9-7 14-4 16c4 1 23 1 26 0c3-2 11-12 18-24c1-2 3-6 4-7c2-2 3-5 4-7c2-3 3-6 5-8c3-5 8-12 10-16c1-1 2-4 3-6c3-4 4-6 6-9c7-14 13-19 22-19c17 0 18 3 9 18c-1 2-3 6-5 9c-2 4-2 5-6 10c-1 1-3 5-5 9c-2 4-5 8-6 10c-3 5-6 9-8 14c-3 5-5 9-9 15c-7 11-5 13 16 13l7 0l1-1c3-1 4-2 9-8c1-3 3-6 4-7c1-2 3-5 4-7c2-2 3-5 4-6c1-2 2-4 3-5c1-2 2-4 3-6c1-1 3-4 4-5c1-2 3-5 5-8c1-2 3-5 4-7c1-2 4-6 5-9c4-6 5-7 7-12c2-1 3-4 4-6c6-8 8-13 9-16c3-11-4-21-16-22c-3 0-104 0-108 0z M825 184c-14 2-25 8-31 17c-2 2-4 5-6 8c-5 7-6 8-9 13c-1 3-4 7-6 10c-2 3-4 7-5 9c-2 3-2 4-5 8c-1 1-2 3-4 6c-1 2-2 5-4 7c-4 6-9 16-12 22l-1 2l0 3l0 2l2 3c3 9 5 11 11 12l1 1l13 0c7 0 16 0 20 1c12 0 18 0 25-2c9-1 20-8 25-14c4-5 5-7 8-11c1-2 2-5 3-6c6-8 3-9-17-10c-10 0-11 0-17 4c-5 3-8 4-15 4c-11 0-14-4-7-14c4-7 10-9 23-7c6 0 15 0 30 0l12 0l2-1c6-2 10-6 17-21c1-2 3-6 5-8c8-13 9-17 8-23c-1-9-6-14-15-15c-3 0-48 0-51 0zM844 210c2 1 4 3 4 6l0 1l-1 2c-4 8-11 12-21 12c-7 0-10-2-10-6c0-9 11-16 24-16l3 0l1 1z"/>
      <path fill="#a3231a" d="M1115 181c-8 1-12 5-21 22c-1 3-3 6-6 10c-5 8-6 10-10 18c-2 5-4 8-6 11c-5 7-8 11-10 15c-1 2-3 5-3 6c-2 2-2 3-4 8c-1 2-3 5-4 7c-1 2-3 5-4 7c-1 1-2 4-3 6c-3 5-4 6-4 8l0 2l0 1c1 1 1 1 2 2l1 0l5 1c16 0 19 0 22-4c4-4 10-12 13-17c3-5 7-12 10-16c1-3 3-6 5-9c1-2 3-6 5-8c4-7 8-14 10-18c2-3 3-6 4-7c2-2 4-5 5-8c2-2 4-6 5-8c14-22 15-27 10-28c-2-1-18-2-22-1z M915 307.8L892 307.8Q885 307.8 889.1 300.8L986.2 136Q992.1 126 1002.1 126L1083.1 126Q1117.1 126 1097 160L1075.2 197Q1055.2 231 1021.2 231L967.2 231L926.1 300.8Q922 307.8 915 307.8zM1004.3 168L991.9 189Q985.5 200 996.5 200L1028 200Q1039 200 1045.4 189L1057.8 168Q1064.3 157 1053.3 157L1021.8 157Q1010.8 157 1004.3 168z"/>
    </svg>
    <span id="ttcap">GPS&nbsp;TIME&nbsp;&amp;&nbsp;FREQUENCY</span>
    <h1><b id="host">·</b>&nbsp; chrony tracking</h1>
  </div>
  <div class="meta" id="meta">connecting…</div>
</div>

<div class="readout">
  <div class="val" id="bigerr">— —</div>
  <div class="lbl">Estimated error · <span id="refname">?</span> reference</div>
  <div class="clock">
    <div>
      <div class="utc" id="clk_utc">--:--:--<small>UTC</small></div>
      <div class="loc" id="clk_loc">--:--:--<small>Local</small></div>
    </div>
    <div class="pps"><span class="led" id="ppsled"></span><span class="ledlbl">PPS</span></div>
  </div>
</div>

<div class="ranges">
  <span>Window</span>
  <button data-h="0.167">10m</button>
  <button data-h="0.5">30m</button>
  <button data-h="1">1h</button>
  <button data-h="6">6h</button>
  <button data-h="24" class="on">24h</button>
  <button data-h="168">7d</button>
  <button data-h="720">30d</button>
</div>

<div class="charts">
  <div class="chart"><h2 data-tip="The most recent measured offset between the system clock and the selected reference at each clock update. On a healthy PPS lock this hovers within a few microseconds of zero; sustained excursions mean the discipline is being disturbed. Averaged per time bucket in this view.">Last offset <small>· µs</small></h2><div class="wrap"><canvas id="c_off"></canvas></div></div>
  <div class="chart"><h2 data-tip="The rate the system clock would drift without correction, in parts per million. This is the crystal's natural error — it breathes with temperature, and chrony compensates continuously. The absolute value doesn't matter; slow smooth movement is normal, sudden jumps are not.">Frequency <small>· ppm</small></h2><div class="wrap"><canvas id="c_frq"></canvas></div></div>
  <div class="chart"><h2 data-tip="SoC temperature from the thermal zone. The crystal's frequency error tracks temperature, so this curve should mirror the frequency chart — a diurnal swing here explains a diurnal ppm swing there. Flat temperature with a moving frequency points at something other than thermals.">Temperature <small>· °C</small></h2><div class="wrap"><canvas id="c_tmp"></canvas></div></div>
  <div class="chart"><h2 data-tip="Long-term root-mean-square average of measured offsets — the overall jitter of the clock discipline. On a Pi with GPIO PPS, tens of microseconds is typical; most of it is interrupt latency variation.">RMS offset <small>· µs</small></h2><div class="wrap"><canvas id="c_rms"></canvas></div></div>
  <div class="chart"><h2 data-tip="Chrony's estimated uncertainty in its own frequency measurement. Lower means the discipline is converged and the oscillator is stable. Rises briefly after restarts or reference dropouts, then settles.">Skew <small>· ppm</small></h2><div class="wrap"><canvas id="c_skw"></canvas></div></div>
  <div class="chart"><h2 data-tip="The accumulated worst-case error bound toward the reference clock. NTP clients add this (plus root delay and network path) to their own error budget, so it bounds the accuracy this server can pass downstream.">Root dispersion <small>· µs</small></h2><div class="wrap"><canvas id="c_dsp"></canvas></div></div>
  <div class="chart"><h2 data-tip="The estimated error bound of the currently selected source — the big readout at the top of the page, over time. Gaps mean no source was selected (holdover). A sawtooth pattern means the reference is dropping out and recovering repeatedly.">Source est. error <small>· ns</small></h2><div class="wrap"><canvas id="c_err"></canvas></div></div>
  <div class="chart"><h2 data-tip="Lock %: the fraction of samples in each bucket where a source was selected (*) — dips below 100 mean holdover. Combined: how many additional (+) sources were averaged into the solution; with PPS trusted this should stay at zero, and anything above it means other sources were diluting the clock.">Selection <small>· lock % / combined</small></h2><div class="wrap"><canvas id="c_sel"></canvas></div></div>
  <div class="chart"><h2 data-tip="The NMEA refclock's offset and standard deviation measured against the PPS-disciplined system clock (from sourcestats). NMEA only numbers the PPS pulses, so its precision doesn't affect the clock — but if the offset drifts toward the lock window edge (±delay/2), pulse pairing starts failing.">NMEA vs system <small>· µs</small></h2><div class="wrap"><canvas id="c_nmea"></canvas></div></div>
  <div class="chart"><h2 data-tip="NTP requests answered and dropped per second, from serverstats counters differenced between samples. Drops should stay at zero — anything above means rate limiting or overload. Reading serverstats needs chronyd's admin socket; a blank chart means chronyc was refused.">NTP requests <small>· req/s</small></h2><div class="wrap"><canvas id="c_ntp"></canvas></div></div>
  <div class="chart"><h2 data-tip="Distinct client addresses in chronyd's client log. Seen: everyone since chronyd last started (LRU-bounded by clientloglimit, so it creeps upward — IPv6 privacy addresses rotate daily and count repeatedly). Active: the subset heard from within the last hour — the live gauge of who is actually using this server.">NTP clients</h2><div class="wrap"><canvas id="c_ncl"></canvas></div></div>
  <div class="chart"><h2 data-tip="Bytes per second in and out of this host's network interfaces (from /proc/net/dev, all interfaces except lo unless DASH_IFACE is set), averaged per bucket. Compare against the NTP request chart: a surge of clients shows up here as a matching bump — each NTP exchange is roughly 90 bytes each way, so 1,000 req/s is about 90 kB/s per direction.">Network throughput <small>· kB/s</small></h2><div class="wrap"><canvas id="c_net"></canvas></div></div>
</div>

<div class="ranges" id="evlegend" style="display:none;margin-top:14px;margin-bottom:0"></div>

<div class="grid" id="cells" style="margin-top:30px;margin-bottom:0"></div>

<div class="debug">
  <h2>Debug · chronyc sources</h2>
  <div class="tblwrap">
  <table id="srctbl">
    <thead><tr>
      <th>MS</th><th>Name/IP</th><th>St</th><th>Poll</th><th>Reach</th>
      <th>LastRx</th><th>Adj offset</th><th>Meas offset</th><th>Est error</th>
    </tr></thead>
    <tbody><tr><td colspan="9">loading…</td></tr></tbody>
  </table>
  </div>
</div>

<details class="settings">
  <summary>Settings</summary>
  <div class="setbody">
    <div class="setrow">
      <span class="setlbl">Theme</span>
      <div class="seg" id="themeseg">
        <button type="button" data-t="light">Light</button>
        <button type="button" data-t="dark">Dark</button>
        <button type="button" data-t="system">System</button>
        <button type="button" data-t="truetime">TrueTime</button>
      </div>
    </div>
    <div class="setrow">
      <span class="setlbl">Export</span>
      <div class="seg">
        <a id="exportwin" href="/api/export.csv?hours=24" download>Export data in selected window</a>
        <a id="exportall" href="/api/export.csv" download>Export all data</a>
      </div>
      <span class="sethint">CSV of raw samples (not downsampled). Window export follows the selector above.</span>
    </div>
  </div>
</details>

<footer id="foot"></footer>

<script>
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fmt = (v,d=3) => v==null ? "—" : Number(v).toFixed(d);
function hexToRgba(hex, a){
  const n = parseInt(hex.slice(1), 16);
  return "rgba("+(n>>16&255)+","+(n>>8&255)+","+(n&255)+","+a+")";
}

function fmtErr(sec){
  if (sec==null) return "— —";
  const ns = Math.abs(sec)*1e9;
  if (ns < 1000)    return "±" + ns.toFixed(0) + " ns";
  if (ns < 1e6)     return "±" + (ns/1e3).toFixed(2) + " µs";
  return "±" + (ns/1e6).toFixed(2) + " ms";
}
function fmtOffset(sec){
  const us = sec*1e6, a = Math.abs(us);
  if (a < 1)   return (us*1000).toFixed(1) + " ns";
  if (a < 1e3) return us.toFixed(2) + " µs";
  return (us/1e3).toFixed(3) + " ms";
}
function reachOct(r){ return r==null ? "—" : r.toString(8).padStart(3,"0"); }
function fmtDur(s){
  const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60);
  return (d?d+"d ":"")+(h?h+"h ":"")+m+"m";
}

const CELLS = [
  ["Stratum",        c => c.stratum],
  ["Reference",      c => c.ref_name],
  ["Selection",      c => c.selected ? "locked" : '<span style="color:var(--bad)">HOLDOVER</span>'],
  ["Combined srcs",  c => c.combined ?? "—"],
  ["Reach",          c => reachOct(c.src_reach)],
  ["Last offset",    c => fmtOffset(c.last_offset)],
  ["RMS offset",     c => fmtOffset(c.rms_offset)],
  ["Frequency",      c => fmt(c.freq_ppm,3)+" <small>ppm</small>"],
  ["Temperature",    c => c.temp_c==null ? "—" : fmt(c.temp_c,1)+" <small>°C</small>"],
  ["Skew",           c => fmt(c.skew_ppm,3)+" <small>ppm</small>"],
  ["Root disp",      c => fmtOffset(c.root_disp)],
  ["NMEA offset",    c => c.nmea_offset==null ? "—" : fmtOffset(c.nmea_offset)],
  ["NMEA std dev",   c => c.nmea_sd==null ? "—" : fmtOffset(c.nmea_sd)],
  ["NTP rate",       c => c.ntp_rate==null ? "—" : fmt(c.ntp_rate,1)+" <small>req/s</small>"],
  ["NTP clients",    c => c.clients_act==null ? "—" :
                          c.clients_act+" <small>past hour</small>"],
  ["Network",        c => c.net_rx_rate==null ? "—" :
                          "↓"+fmt(c.net_rx_rate/1e3,1)+" ↑"+fmt(c.net_tx_rate/1e3,1)+" <small>kB/s</small>"],
  ["Leap status",    c => c.leap],
];

let charts = {}, hours = 24;

// ---- Event annotations: dashed vertical lines on every chart ----------
// EVENTS is refreshed with each history load; the window geometry
// (t0/step/n) maps a timestamp onto the shared category x-axis.
let EVENTS = [], EVT0 = 0, EVSTEP = 1, EVN = 0;
const EV_COLORS = {
  "reboot": "--bad",
  "chrony restart": "--amber",
  "pps lost": "--bad",
  "pps fix": "--vfd",
  "ref change": "--cyan",
};
const eventLines = {
  id: "eventLines",
  afterDraw(chart){
    if (!EVENTS.length || EVN < 2) return;
    const {ctx, chartArea:a} = chart;
    for (const ev of EVENTS){
      const idx = (ev.ts - EVT0) / EVSTEP;
      if (idx < 0 || idx > EVN - 1) continue;
      const px = a.left + idx / (EVN - 1) * (a.right - a.left);
      ctx.save();
      ctx.strokeStyle = hexToRgba(css(EV_COLORS[ev.kind] || "--dim"), .6);
      ctx.setLineDash([3,3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(px, a.top);
      ctx.lineTo(px, a.bottom);
      ctx.stroke();
      ctx.restore();
    }
  }
};
Chart.register(eventLines);

function updateEvLegend(){
  const el = document.getElementById("evlegend");
  const kinds = [...new Set(EVENTS.map(e => e.kind))];
  if (!kinds.length){ el.style.display = "none"; return; }
  el.style.display = "flex";
  el.innerHTML = "<span>Events</span>" + kinds.map(k =>
    '<span style="font-size:10px;letter-spacing:.12em;text-transform:uppercase;'+
    'color:'+css(EV_COLORS[k] || "--dim")+'">╎ '+k+
    ' × '+EVENTS.filter(e => e.kind === k).length+'</span>'
  ).join("");
}

function mkChart(id, color, fill){
  const tick = {color: css("--dim"), font:{family:"ui-monospace, Menlo, Consolas, monospace",size:10}, maxTicksLimit:6};
  return new Chart(document.getElementById(id), {
    type:"line",
    data:{labels:[],datasets:[{data:[],borderColor:color,borderWidth:1.4,
      pointRadius:0,tension:.25,fill:!!fill,
      backgroundColor:fill? hexToRgba(color,.08) : undefined}]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:false},tooltip:{intersect:false,mode:"index"}},
      scales:{
        x:{ticks:tick,grid:{color:css("--line")}},
        y:{ticks:tick,grid:{color:css("--line")}}
      }
    }
  });
}

function initCharts(){
  const g=css("--vfd"), a=css("--amber"), cy=css("--cyan");
  charts.off = mkChart("c_off", g, true);
  charts.rms = mkChart("c_rms", g);
  charts.frq = mkChart("c_frq", a);
  charts.tmp = mkChart("c_tmp", a, true);
  charts.skw = mkChart("c_skw", a);
  charts.dsp = mkChart("c_dsp", cy);
  charts.err = mkChart("c_err", cy, true);

  // Selection chart: lock % (left axis) + combined server count (right axis).
  const tick = {color: css("--dim"), font:{family:"ui-monospace, Menlo, Consolas, monospace",size:10}, maxTicksLimit:6};
  charts.sel = new Chart(document.getElementById("c_sel"), {
    type:"line",
    data:{labels:[],datasets:[
      {label:"lock %",data:[],borderColor:g,borderWidth:1.4,pointRadius:0,
       stepped:true,fill:true,
       backgroundColor:hexToRgba(g,.08),yAxisID:"y"},
      {label:"combined",data:[],borderColor:a,borderWidth:1.4,pointRadius:0,
       stepped:true,yAxisID:"y1"},
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:true,labels:{color:css("--dim"),
        font:{family:"ui-monospace, Menlo, Consolas, monospace",size:10},boxWidth:14,boxHeight:2}},
        tooltip:{intersect:false,mode:"index"}},
      scales:{
        x:{ticks:tick,grid:{color:css("--line")}},
        y:{min:0,max:100,ticks:tick,grid:{color:css("--line")}},
        y1:{min:0,position:"right",ticks:{...tick,precision:0},grid:{drawOnChartArea:false}}
      }
    }
  });

  // NTP server load: requests + drops per second, one axis.
  charts.ntp = new Chart(document.getElementById("c_ntp"), {
    type:"line",
    data:{labels:[],datasets:[
      {label:"req/s",data:[],borderColor:g,borderWidth:1.4,pointRadius:0,
       tension:.25,fill:true,backgroundColor:hexToRgba(g,.08)},
      {label:"drop/s",data:[],borderColor:css("--bad"),borderWidth:1.4,
       pointRadius:0,tension:.25},
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:true,labels:{color:css("--dim"),
        font:{family:"ui-monospace, Menlo, Consolas, monospace",size:10},boxWidth:14,boxHeight:2}},
        tooltip:{intersect:false,mode:"index"}},
      scales:{
        x:{ticks:tick,grid:{color:css("--line")}},
        y:{min:0,ticks:tick,grid:{color:css("--line")}}
      }
    }
  });

  // NTP clients: total seen (since chronyd start) vs active in last hour.
  charts.ncl = new Chart(document.getElementById("c_ncl"), {
    type:"line",
    data:{labels:[],datasets:[
      {label:"seen",data:[],borderColor:cy,borderWidth:1.4,pointRadius:0,
       stepped:true},
      {label:"active 1h",data:[],borderColor:g,borderWidth:1.4,pointRadius:0,
       stepped:true,fill:true,backgroundColor:hexToRgba(g,.08)},
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:true,labels:{color:css("--dim"),
        font:{family:"ui-monospace, Menlo, Consolas, monospace",size:10},boxWidth:14,boxHeight:2}},
        tooltip:{intersect:false,mode:"index"}},
      scales:{
        x:{ticks:tick,grid:{color:css("--line")}},
        y:{min:0,ticks:{...tick,precision:0},grid:{color:css("--line")}}
      }
    }
  });

  // Network throughput: rx + tx in kB/s, one axis.
  charts.net = new Chart(document.getElementById("c_net"), {
    type:"line",
    data:{labels:[],datasets:[
      {label:"rx",data:[],borderColor:cy,borderWidth:1.4,pointRadius:0,
       tension:.25,fill:true,backgroundColor:hexToRgba(cy,.08)},
      {label:"tx",data:[],borderColor:a,borderWidth:1.4,pointRadius:0,tension:.25},
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:true,labels:{color:css("--dim"),
        font:{family:"ui-monospace, Menlo, Consolas, monospace",size:10},boxWidth:14,boxHeight:2}},
        tooltip:{intersect:false,mode:"index"}},
      scales:{
        x:{ticks:tick,grid:{color:css("--line")}},
        y:{min:0,ticks:tick,grid:{color:css("--line")}}
      }
    }
  });

  // NMEA health: offset + std dev vs the PPS-disciplined system clock, one axis.
  charts.nmea = new Chart(document.getElementById("c_nmea"), {
    type:"line",
    data:{labels:[],datasets:[
      {label:"offset",data:[],borderColor:g,borderWidth:1.4,pointRadius:0,tension:.25},
      {label:"std dev",data:[],borderColor:a,borderWidth:1.4,pointRadius:0,tension:.25},
    ]},
    options:{
      responsive:true,maintainAspectRatio:false,animation:false,
      plugins:{legend:{display:true,labels:{color:css("--dim"),
        font:{family:"ui-monospace, Menlo, Consolas, monospace",size:10},boxWidth:14,boxHeight:2}},
        tooltip:{intersect:false,mode:"index"}},
      scales:{
        x:{ticks:tick,grid:{color:css("--line")}},
        y:{ticks:tick,grid:{color:css("--line")}}
      }
    }
  });
}

function labelsFor(ts){
  const dayFmt = hours > 48;
  return ts.map(t=>{
    const d = new Date(t*1000);
    return dayFmt
      ? (d.getMonth()+1)+"/"+d.getDate()+" "+String(d.getHours()).padStart(2,"0")+":00"
      : String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");
  });
}

// Minimum y-axis spans (in each chart's display units). Autoscale zooms
// all the way into the noise, making tight, healthy traces look wild;
// enforcing a floor on the visible range keeps small jitter looking
// small. If the data genuinely exceeds the span, autoscale takes over.
const MIN_SPAN = {frq: 1.0, tmp: 5, off: 2, skw: 0.05};
function applyMinSpan(key){
  const c = charts[key], span = MIN_SPAN[key], y = c.options.scales.y;
  const vals = c.data.datasets.flatMap(d => d.data).filter(v => v != null);
  if (!vals.length) return;
  const lo = Math.min(...vals), hi = Math.max(...vals);
  if (hi - lo >= span){ delete y.min; delete y.max; }
  else {
    const mid = (hi + lo) / 2;
    y.min = mid - span / 2;
    y.max = mid + span / 2;
  }
  c.update();
}

async function loadHistory(){
  const [r, re] = await Promise.all([
    fetch("/api/history?hours="+hours),
    fetch("/api/events?hours="+hours),
  ]);
  const h = await r.json();
  const ev = await re.json().catch(()=>({ok:false}));
  if (!h.ok) return;
  EVENTS = ev.ok ? ev.events : [];
  EVT0 = h.t[0]; EVSTEP = h.step; EVN = h.t.length;
  updateEvLegend();
  const L = labelsFor(h.t);
  const scale = (arr, f) => arr.map(v => v == null ? null : v * f);
  const put = (c, data) => { c.data.labels=L; c.data.datasets[0].data=data; c.update(); };
  put(charts.off, scale(h.offset, 1e6));
  put(charts.rms, scale(h.rms, 1e6));
  put(charts.frq, h.freq);
  put(charts.tmp, h.temp);
  put(charts.skw, h.skew);
  put(charts.dsp, scale(h.rootdisp, 1e6));
  put(charts.err, scale(h.srcerr, 1e9));
  charts.sel.data.labels = L;
  charts.sel.data.datasets[0].data = scale(h.lock, 100);
  charts.sel.data.datasets[1].data = h.combined;
  charts.sel.update();
  charts.nmea.data.labels = L;
  charts.nmea.data.datasets[0].data = scale(h.nmea_off, 1e6);
  charts.nmea.data.datasets[1].data = scale(h.nmea_sd, 1e6);
  charts.nmea.update();
  charts.ntp.data.labels = L;
  charts.ntp.data.datasets[0].data = h.ntp_rate;
  charts.ntp.data.datasets[1].data = h.ntp_drop;
  charts.ntp.update();
  charts.ncl.data.labels = L;
  charts.ncl.data.datasets[0].data = h.clients;
  charts.ncl.data.datasets[1].data = h.clients_act;
  charts.ncl.update();
  charts.net.data.labels = L;
  charts.net.data.datasets[0].data = scale(h.net_rx, 1e-3);
  charts.net.data.datasets[1].data = scale(h.net_tx, 1e-3);
  charts.net.update();
  Object.keys(MIN_SPAN).forEach(applyMinSpan);
}

// Running clock. On each /api/current refresh we anchor to the NTP server's
// clock (server_now) by computing an offset vs the browser clock; between
// refreshes the browser clock carries the tick.
const UTC_12H = __UTC_12H__, LOCAL_12H = __LOCAL_12H__;
let srvOffsetMs = 0;
const p2 = n => String(n).padStart(2,"0");
function fmtClock(h, m, s, twelve){
  if (!twelve) return p2(h)+":"+p2(m)+":"+p2(s);
  const ap = h < 12 ? "AM" : "PM";
  h = h % 12 || 12;
  return h+":"+p2(m)+":"+p2(s)+' <span class="ap">'+ap+"</span>";
}
// One fast tick drives both the LED and the digits, from the same time
// sample — so the blink starts in the same frame the second flips over.
let lastSec = null;
function tickClock(){
  const now = Date.now() + srvOffsetMs;
  ppsLed.classList.toggle("on", ppsActive && (now % 1000) < 100);
  const sec = Math.floor(now / 1000);
  if (sec === lastSec) return;
  lastSec = sec;
  const d = new Date(now);
  const utcLbl = document.documentElement.dataset.theme === "truetime" ? "Z" : "UTC";
  document.getElementById("clk_utc").innerHTML =
    fmtClock(d.getUTCHours(), d.getUTCMinutes(), d.getUTCSeconds(), UTC_12H)+"<small>"+utcLbl+"</small>";
  document.getElementById("clk_loc").innerHTML =
    fmtClock(d.getHours(), d.getMinutes(), d.getSeconds(), LOCAL_12H)+"<small>Local</small>";
}

function fmtLastRx(s){
  const n = Number(s);
  if (!isFinite(n)) return s;
  if (n < 120) return n + "s";
  if (n < 7200) return Math.round(n/60) + "m";
  return Math.round(n/3600) + "h";
}

async function loadSources(){
  const r = await fetch("/api/sources");
  const d = await r.json();
  const tb = document.querySelector("#srctbl tbody");
  if (!d.ok){ tb.innerHTML = '<tr><td colspan="9">'+d.error+'</td></tr>'; return; }
  tb.innerHTML = d.sources.map(s =>
    '<tr class="'+(s.state==="*" ? "sel" : "")+'">'+
    '<td>'+s.mode+s.state+'</td>'+
    '<td>'+s.name+'</td>'+
    '<td>'+s.stratum+'</td>'+
    '<td>'+s.poll+'</td>'+
    '<td>'+reachOct(s.reach)+'</td>'+
    '<td>'+fmtLastRx(s.lastrx)+'</td>'+
    '<td>'+fmtOffset(s.adj_offset)+'</td>'+
    '<td>'+fmtOffset(s.meas_offset)+'</td>'+
    '<td>'+fmtErr(s.est_err)+'</td>'+
    '</tr>').join("");
}

// Faux PPS LED state: lit only while the selected reference is PPS.
let ppsActive = false;
const ppsLed = document.getElementById("ppsled");

// Big readout: rolling odometer digits + crossfade between error/holdover.
const bigEl = document.getElementById("bigerr");
let bigMode = null;      // "err" | "hold"
let odoPattern = null;   // char-class signature of the current odometer

function buildOdo(text){
  odoPattern = text.replace(/[0-9]/g, "d");
  bigEl.innerHTML = "";
  for (const ch of text){
    if (ch >= "0" && ch <= "9"){
      const cell = document.createElement("span");
      cell.className = "odo";
      const reel = document.createElement("span");
      reel.className = "reel";
      for (let i = 0; i < 10; i++){
        const d = document.createElement("span");
        d.textContent = i;
        reel.appendChild(d);
      }
      reel.style.transform = "translateY(-" + ch + "em)";
      cell.appendChild(reel);
      bigEl.appendChild(cell);
    } else {
      const s = document.createElement("span");
      s.className = "st";
      s.textContent = ch;
      bigEl.appendChild(s);
    }
  }
}

function updateOdo(text){
  if (text.replace(/[0-9]/g, "d") !== odoPattern){ buildOdo(text); return; }
  const reels = bigEl.querySelectorAll(".reel");
  let i = 0;
  for (const ch of text)
    if (ch >= "0" && ch <= "9")
      reels[i++].style.transform = "translateY(-" + ch + "em)";
}

function setBig(mode, text){
  if (mode === bigMode){
    if (mode === "err") updateOdo(text);   // digits roll in place
    return;
  }
  bigMode = mode;
  bigEl.classList.add("swap");             // fade out...
  setTimeout(()=>{
    bigEl.classList.toggle("hold", mode === "hold");
    if (mode === "err") buildOdo(text);
    else { odoPattern = null; bigEl.textContent = text; }
    bigEl.classList.remove("swap");        // ...swap content, fade in
  }, 260);
}

async function loadCurrent(){
  const r = await fetch("/api/current");
  const c = await r.json();
  if (!c.ok){ document.getElementById("meta").textContent = c.error; return; }
  if (c.server_now) srvOffsetMs = c.server_now*1000 - Date.now();
  ppsActive = !!c.selected && /pps/i.test(c.ref_name || "");
  document.getElementById("host").textContent = c.hostname;
  const big = document.getElementById("bigerr");
  setBig(c.selected ? "err" : "hold",
         c.selected ? fmtErr(c.src_err) : "HOLDOVER");
  document.getElementById("refname").textContent = c.ref_name;
  const age = Math.round(Date.now()/1000 - c.ts);
  const meta = document.getElementById("meta");
  meta.textContent = "sample "+age+"s ago · poll "+c.poll+"s";
  meta.className = "meta" + (age > c.poll*3 ? " leap-bad" : "");
  document.getElementById("cells").innerHTML = CELLS.map(([k,f])=>
    '<div class="cell"><div class="k">'+k+'</div><div class="v">'+f(c)+'</div></div>'
  ).join("");
  document.getElementById("foot").textContent =
    c.samples.toLocaleString()+" samples since "+
    new Date(c.first_ts*1000).toLocaleString()+" · logger up "+fmtDur(c.service_uptime);
}

const savedH = Number(localStorage.getItem("dashWindow"));
if ([0.167,0.5,1,6,24,168,720].includes(savedH)) hours = savedH;
document.querySelectorAll(".ranges button").forEach(b=>{
  b.classList.toggle("on", Number(b.dataset.h) === hours);
  b.addEventListener("click", ()=>{
    document.querySelectorAll(".ranges button").forEach(x=>x.classList.remove("on"));
    b.classList.add("on");
    hours = Number(b.dataset.h);
    localStorage.setItem("dashWindow", hours);
    updateExportLink();
    loadHistory();
  });
});
function updateExportLink(){
  document.getElementById("exportwin").href = "/api/export.csv?hours=" + hours;
}
updateExportLink();

// Chart label tooltips: hover the label ~1s, or click/tap the (i) icon.
const openTips = [];
function closeAllTips(){ openTips.forEach(f=>f()); openTips.length = 0; }
document.querySelectorAll(".chart h2[data-tip]").forEach(h2=>{
  const ch = h2.closest(".chart");
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "info"; btn.textContent = "i";
  btn.setAttribute("aria-label", "About this chart");
  h2.appendChild(btn);
  const tip = document.createElement("div");
  tip.className = "tip"; tip.textContent = h2.dataset.tip;
  ch.appendChild(tip);
  let timer = null, pinned = false;
  const unpin = ()=>{ pinned = false; tip.classList.remove("show"); };
  h2.addEventListener("mouseenter", ()=>{ timer = setTimeout(()=>tip.classList.add("show"), 1000); });
  h2.addEventListener("mouseleave", ()=>{ clearTimeout(timer); if (!pinned) tip.classList.remove("show"); });
  btn.addEventListener("click", e=>{
    e.stopPropagation();
    const was = pinned;
    closeAllTips();
    if (!was){ pinned = true; tip.classList.add("show"); openTips.push(unpin); }
  });
});
document.addEventListener("click", closeAllTips);
document.addEventListener("keydown", e=>{ if (e.key === "Escape") closeAllTips(); });

// ---- Settings: theme -------------------------------------------------
const themeBtns = document.querySelectorAll("#themeseg button");
const sysDark = matchMedia("(prefers-color-scheme: dark)");

function restyleCharts(){
  const g=css("--vfd"), a=css("--amber"), cy=css("--cyan");
  const ln=css("--line"), dm=css("--dim"), bd=css("--bad");
  const palette = {off:[g],rms:[g],frq:[a],tmp:[a],skw:[a],dsp:[cy],err:[cy],sel:[g,a],nmea:[g,a],ntp:[g,bd],ncl:[cy,g],net:[cy,a]};
  for (const [k, cols] of Object.entries(palette)){
    const c = charts[k]; if (!c) continue;
    c.data.datasets.forEach((ds,i)=>{
      ds.borderColor = cols[i] || cols[0];
      if (ds.fill) ds.backgroundColor = hexToRgba(cols[i] || cols[0], .08);
    });
    Object.values(c.options.scales).forEach(s=>{
      if (s.ticks) s.ticks.color = dm;
      if (s.grid && s.grid.color) s.grid.color = ln;
    });
    if (c.options.plugins.legend && c.options.plugins.legend.labels)
      c.options.plugins.legend.labels.color = dm;
    c.update();
  }
  updateEvLegend();
}

function applyTheme(){
  const pref = localStorage.getItem("dashTheme") || "system";
  let theme;
  if (pref === "truetime") theme = "truetime";
  else if (pref === "system") theme = sysDark.matches ? "dark" : "light";
  else theme = pref;
  document.documentElement.dataset.theme = theme;
  themeBtns.forEach(b=>b.classList.toggle("on", b.dataset.t === pref));
  if (charts.off) restyleCharts();
}
themeBtns.forEach(b=>b.addEventListener("click", ()=>{
  localStorage.setItem("dashTheme", b.dataset.t);
  applyTheme();
}));
sysDark.addEventListener("change", ()=>{
  if ((localStorage.getItem("dashTheme") || "system") === "system") applyTheme();
});

applyTheme();
initCharts();
loadCurrent();
loadHistory();
loadSources();
tickClock();
setInterval(tickClock, 50);
setInterval(loadCurrent, 15000);
setInterval(loadSources, 15000);
setInterval(loadHistory, 60000);
</script>
</body>
</html>
"""

PAGE_RENDERED = (PAGE
                 .replace("__HOSTNAME__", html.escape(HOSTNAME))
                 .replace("__UTC_12H__", "true" if UTC_CLOCK_12_HOUR else "false")
                 .replace("__LOCAL_12H__", "true" if LOCAL_CLOCK_12_HOUR else "false")
                 ).encode()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        try:
            if url.path == "/":
                self._send(200, "text/html; charset=utf-8", PAGE_RENDERED)
            elif url.path == "/chart.umd.min.js":
                if CHARTJS is None:
                    self._send(404, "text/plain",
                               b"chart.umd.min.js missing next to script")
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/javascript")
                    self.send_header("Content-Length", str(len(CHARTJS)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(CHARTJS)
            elif url.path == "/api/current":
                self._send(200, "application/json", json.dumps(api_current()).encode())
            elif url.path == "/api/sources":
                self._send(200, "application/json", json.dumps(api_sources()).encode())
            elif url.path == "/api/history":
                q = parse_qs(url.query)
                hours = float(q.get("hours", ["24"])[0])
                hours = max(0.1, min(hours, RETENTION_DAYS * 24))
                self._send(200, "application/json", json.dumps(api_history(hours)).encode())
            elif url.path == "/api/export.csv":
                q = parse_qs(url.query)
                hours = None
                if "hours" in q:
                    hours = float(q["hours"][0])
                    hours = max(0.1, min(hours, RETENTION_DAYS * 24))
                stamp = time.strftime("%Y%m%d-%H%M%S")
                scope = "all" if hours is None else f"{hours:g}h"
                body = api_export_csv(hours)
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{HOSTNAME}-chrony-{scope}-{stamp}.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/api/events":
                q = parse_qs(url.query)
                hours = float(q.get("hours", ["24"])[0])
                hours = max(0.1, min(hours, RETENTION_DAYS * 24))
                self._send(200, "application/json", json.dumps(api_events(hours)).encode())
            else:
                self._send(404, "text/plain", b"not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send(500, "application/json",
                       json.dumps({"ok": False, "error": str(e)}).encode())

    def log_message(self, *args):
        pass  # keep journal quiet


def main():
    stop = threading.Event()
    t = threading.Thread(target=poller, args=(stop,), daemon=True)
    t.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print(f"chrony dashboard on :{PORT}, db={DB_PATH}, poll={POLL_INTERVAL}s")
    server.serve_forever()
    t.join(timeout=5)


if __name__ == "__main__":
    main()
