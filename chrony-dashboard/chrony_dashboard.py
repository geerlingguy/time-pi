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
"""

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
    temp_c        REAL    -- SoC temperature, °C
);
CREATE INDEX IF NOT EXISTS idx_tracking_ts ON tracking (ts);
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
                       ("temp_c", "REAL")):
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
        return sample
    except Exception as e:  # chronyd down, parse change, etc.
        print(f"poll failed: {e}", file=sys.stderr)
        return None


def poller(stop_event):
    conn = db_connect()
    conn.executescript(SCHEMA)
    ensure_columns(conn)
    last_prune = 0.0
    while not stop_event.is_set():
        sample = poll_once()
        if sample:
            conn.execute(
                """INSERT OR REPLACE INTO tracking
                   (ts, stratum, sys_offset, last_offset, rms_offset,
                    freq_ppm, resid_ppm, skew_ppm, root_delay, root_disp,
                    leap, ref_name, src_reach, src_err, selected, combined,
                    nmea_offset, nmea_sd, temp_c)
                   VALUES (:ts,:stratum,:sys_offset,:last_offset,:rms_offset,
                           :freq_ppm,:resid_ppm,:skew_ppm,:root_delay,
                           :root_disp,:leap,:ref_name,:src_reach,:src_err,
                           :selected,:combined,:nmea_offset,:nmea_sd,:temp_c)""",
                sample,
            )
            conn.commit()
        now = time.time()
        if now - last_prune > 6 * 3600:
            cutoff = int(now - RETENTION_DAYS * 86400)
            conn.execute("DELETE FROM tracking WHERE ts < ?", (cutoff,))
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
                  temp_c
           FROM tracking ORDER BY ts DESC LIMIT 1"""
    ).fetchone()
    first = conn.execute("SELECT MIN(ts), COUNT(*) FROM tracking").fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "no samples yet"}
    keys = ["ts", "stratum", "sys_offset", "last_offset", "rms_offset",
            "freq_ppm", "resid_ppm", "skew_ppm", "root_delay", "root_disp",
            "leap", "ref_name", "src_reach", "src_err", "selected", "combined",
            "nmea_offset", "nmea_sd", "temp_c"]
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
                  AVG(nmea_offset), AVG(nmea_sd), AVG(temp_c)
           FROM tracking WHERE ts >= :since
           GROUP BY bucket ORDER BY bucket""",
        {"step": step, "since": since},
    ).fetchall()
    conn.close()
    # Pad the full window: one entry per bucket from `since` to now, with
    # nulls where no samples exist, so short histories show against the
    # complete time axis instead of stretching to fill it.
    by_bucket = {r[0]: r for r in rows}
    empty = (None,) * 13
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
    }


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
<title>truetime-pi · chrony</title>
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
  --bg:#d5d0c1;        /* chassis beige */
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
.seg button,.danger{
  font:600 11px/1 ui-monospace,Menlo,Consolas,monospace;letter-spacing:.1em;
  color:var(--ink);background:var(--bg);border:1px solid var(--line);
  border-radius:3px;padding:7px 12px;cursor:pointer}
.seg button:hover{border-color:var(--vfd)}
.seg button.on{color:var(--bg);background:var(--vfd);border-color:var(--vfd)}
.seg button:focus-visible,.danger:focus-visible{outline:2px solid var(--vfd);outline-offset:2px}
.danger{color:var(--bad);border-color:var(--bad)}
.danger:hover{color:var(--bg);background:var(--bad)}
.sethint{font-size:10px;color:var(--dim);letter-spacing:.04em}

/* ---- TrueTime theme component styling -------------------------------- */
#ttlogo{display:none}
[data-theme="truetime"] #ttlogo{display:block}
[data-theme="truetime"] .mast h1{display:none}
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
[data-theme="truetime"] .clock .loc{color:#d94a17;font-size:14px;margin-top:8px}
[data-theme="truetime"] .clock .utc small,
[data-theme="truetime"] .clock .loc small{
  font-family:ui-monospace,Menlo,Consolas,monospace;color:#8a5c42}
[data-theme="truetime"] .clock .ap{
  font-family:ui-monospace,Menlo,Consolas,monospace;
  font-size:12px;font-weight:600;color:#d94a17;letter-spacing:.08em}
[data-theme="truetime"] .ranges button,
[data-theme="truetime"] .seg button{
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
    <svg id="ttlogo" viewBox="0 0 142 32" width="126" role="img" aria-label="TrueTime Pi logo">
      <text x="2" y="20" textLength="96" lengthAdjust="spacingAndGlyphs"
        font-family="Georgia,'Times New Roman',serif" font-style="italic"
        font-weight="700" font-size="20" fill="#1e2f8f">TrueTime</text>
      <text x="106" y="20" textLength="20" lengthAdjust="spacingAndGlyphs"
        font-family="Georgia,'Times New Roman',serif" font-style="italic"
        font-weight="700" font-size="20" fill="#a3231a">Pi</text>
      <path d="M8 25 H122 L118 28 H2 Z" fill="#1e2f8f"/>
    </svg>
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
</div>

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
      <span class="setlbl">Data</span>
      <button type="button" class="danger" id="resetbtn">Reset database</button>
      <span class="sethint">Deletes all logged samples. Logging restarts immediately.</span>
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
  ["Residual freq",  c => fmt(c.resid_ppm,3)+" <small>ppm</small>"],
  ["Skew",           c => fmt(c.skew_ppm,3)+" <small>ppm</small>"],
  ["Root dispersion",c => fmtOffset(c.root_disp)],
  ["NMEA offset",    c => c.nmea_offset==null ? "—" : fmtOffset(c.nmea_offset)],
  ["NMEA std dev",   c => c.nmea_sd==null ? "—" : fmtOffset(c.nmea_sd)],
  ["Leap status",    c => c.leap],
];

let charts = {}, hours = 24;

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

async function loadHistory(){
  const r = await fetch("/api/history?hours="+hours);
  const h = await r.json();
  if (!h.ok) return;
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
    loadHistory();
  });
});

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
  const ln=css("--line"), dm=css("--dim");
  const palette = {off:[g],rms:[g],frq:[a],tmp:[a],skw:[a],dsp:[cy],err:[cy],sel:[g,a],nmea:[g,a]};
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

// ---- Settings: reset database ---------------------------------------
document.getElementById("resetbtn").addEventListener("click", async ()=>{
  if (!confirm("Delete ALL logged samples from the database? This cannot be undone.")) return;
  try {
    const r = await fetch("/api/reset", {method:"POST"});
    const d = await r.json();
    if (!d.ok) throw new Error(d.error);
    loadCurrent(); loadHistory(); loadSources();
  } catch (e) {
    alert("Reset failed: " + e.message);
  }
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
            else:
                self._send(404, "text/plain", b"not found")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._send(500, "application/json",
                       json.dumps({"ok": False, "error": str(e)}).encode())

    def do_POST(self):
        if urlparse(self.path).path != "/api/reset":
            self._send(404, "text/plain", b"not found")
            return
        try:
            conn = db_connect()
            conn.execute("DELETE FROM tracking")
            conn.commit()
            conn.execute("VACUUM")
            conn.close()
            self._send(200, "application/json", json.dumps({"ok": True}).encode())
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
