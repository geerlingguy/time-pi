#!/usr/bin/env python3
"""Public status page for a GNSS-disciplined NTP server (gpsd + ts2phc/phc2sys + chrony).

Python 3 stdlib only. See README.md for setup, data sources and security notes.
"""

import base64
import hashlib
import html
import ipaddress
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

BIND = os.environ.get("DASH_BIND", "")
PORT = int(os.environ.get("DASH_PORT", "80"))
TITLE = os.environ.get("DASH_TITLE", os.uname().nodename)
SUBTITLE = os.environ.get("DASH_SUBTITLE", "Stratum 1 NTP server, disciplined by GNSS")
RETENTION_DAYS = int(os.environ.get("DASH_RETENTION", "90"))
GPSD = ("127.0.0.1", int(os.environ.get("DASH_GPSD_PORT", "2947")))
DB_PATH = os.environ.get("DASH_DB", os.path.join(os.environ.get("STATE_DIRECTORY", "."), "time-status.db"))
POLL = 15           # seconds between chrony/systemd polls
STALE = 10          # seconds before a servo or GNSS reading counts as stale
CACHE_TTL = 30      # seconds to cache DB-backed API results
ADEV_EVERY = 600    # seconds between Allan deviation updates
MAX_CONN = 32       # concurrent HTTP connections; extras are dropped
MAX_LINE = 1 << 16  # longest gpsd line accepted
MAX_SATS = 256
WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
HISTORY_QUERIES = {f"range={k}": v for k, v in WINDOWS.items()}
GNSS_IDS = range(8)

# linuxptp servo lines, e.g. "/dev/ptp0 offset -3 s2 freq +1234" or "CLOCK_REALTIME phc offset 5 s2 freq -12 delay 456".
OFFSET_RE = re.compile(r"offset\s+(-?\d{1,12})\s+s([0-2])\s+freq\s+([+-]?\d{1,12})")
SERVO_SRC = {"ts2phc": "phc", "phc2sys": "sys"}
HOSTNAME_RE = re.compile(r"[A-Za-z0-9.-]{1,64}")

lock = threading.Lock()
live = {"gnss": {}, "sats": [], "servo": {}, "chrony": None, "peers": [], "adev": {}}
acc = {}  # (minute, src) -> per-minute servo accumulator
db_lock = threading.Lock()
cache_lock = threading.Lock()
cache = {}
state = {"db": None, "page": b"", "csp": ""}


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def finite(x, lo=-1e12, hi=1e12):
    """Return x as a float if it's a finite number within [lo, hi], else None."""
    if isinstance(x, bool) or not isinstance(x, (int, float, str)):
        return None
    try:
        x = float(x)
    except ValueError:
        return None
    return x if math.isfinite(x) and lo <= x <= hi else None


# ---------------------------------------------------------------- sources ----

def handle_gpsd(m):
    """Keep fix quality and per-constellation signal; never position, PRN, azimuth or elevation."""
    if not isinstance(m, dict):
        return
    with lock:
        g = live["gnss"]
        if m.get("class") == "TPV":
            mode = finite(m.get("mode"), 0, 3)
            leap = finite(m.get("leapseconds"), 0, 100)
            g.update(mode=int(mode or 0), ts=time.time())
            if leap is not None:
                g["leap"] = int(leap)
        elif m.get("class") == "SKY":
            for k in ("pdop", "tdop"):
                if k in m:
                    g[k] = finite(m[k], 0, 100)
            sats = m.get("satellites")
            if isinstance(sats, list):
                live["sats"] = [{"g": int(finite(s.get("gnssid"), 0, 7) or 0),
                                 "ss": finite(s.get("ss"), 0, 99) or 0,
                                 "u": s.get("used") is True}
                                for s in sats[:MAX_SATS] if isinstance(s, dict)]
                g["sky_ts"] = time.time()


def gpsd_reader():
    """Follow gpsd's JSON stream on localhost."""
    while True:
        try:
            with socket.create_connection(GPSD, timeout=30) as s, \
                    s.makefile("r", encoding="utf-8", errors="replace") as f:
                s.sendall(b'?WATCH={"enable":true,"json":true};\n')
                while line := f.readline(MAX_LINE):
                    if not line.endswith("\n"):  # oversized line; drop the connection
                        break
                    try:
                        handle_gpsd(json.loads(line))
                    except ValueError:
                        continue
        except OSError as e:
            log(f"gpsd: {e}")
        time.sleep(5)


def journal_reader():
    """Follow ts2phc/phc2sys servo lines from the journal."""
    cmd = ["journalctl", "-f", "-n", "0", "-o", "json", "--output-fields=MESSAGE,SYSLOG_IDENTIFIER",
           "-u", "ts2phc", "-u", "phc2sys"]
    while True:
        try:
            with subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True) as p:
                for line in p.stdout:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(e, dict):
                        continue
                    src, msg = SERVO_SRC.get(e.get("SYSLOG_IDENTIFIER")), e.get("MESSAGE")
                    m = OFFSET_RE.search(msg) if src and isinstance(msg, str) else None
                    if m:
                        record(src, int(m[1]), int(m[2]), int(m[3]))
            log(f"journalctl exited ({p.returncode})")
        except OSError as e:
            log(f"journalctl: {e}")
        time.sleep(5)


def record(src, offset_ns, servo_state, freq_ppb):
    now = time.time()
    with lock:
        live["servo"][src] = {"offset": offset_ns * 1e-9, "state": servo_state, "freq": freq_ppb, "ts": now}
        a = acc.setdefault((int(now) // 60 * 60, src),
                           {"total": 0, "n": 0, "sum": 0.0, "sumsq": 0.0, "min": None, "max": None, "freq": 0.0})
        a["total"] += 1
        if servo_state == 2:  # only locked samples count toward statistics
            a["n"] += 1
            a["sum"] += offset_ns
            a["sumsq"] += offset_ns * offset_ns
            a["min"] = offset_ns if a["min"] is None else min(a["min"], offset_ns)
            a["max"] = offset_ns if a["max"] is None else max(a["max"], offset_ns)
            a["freq"] += freq_ppb


def chronyc(*args):
    """Unprivileged chronyc over UDP to localhost (monitoring commands only, no admin socket)."""
    out = subprocess.run(["chronyc", "-h", "127.0.0.1", "-c", *args], capture_output=True, text=True,
                         timeout=5, check=False, stdin=subprocess.DEVNULL).stdout
    return [line.split(",") for line in out.splitlines() if line]


def refid_name(refid, name):
    if refid == "7F7F0101":  # chrony 'local' directive
        return "LOCL"
    try:
        text = bytes.fromhex(refid).rstrip(b"\0").decode("ascii")
        return text if text.isprintable() and text else name or refid
    except ValueError:
        return name or refid


def poll_chrony():
    try:
        t = chronyc("tracking")[0]
        tracking = {"ref": refid_name(t[0], t[1])[:16], "stratum": int(t[2]), "leap": t[13][:16]}
        stats = {r[0]: r for r in chronyc("sourcestats") if len(r) >= 8}
        rows = [r for r in chronyc("sources")[:32] if len(r) >= 10]
        peers = []
        for r, label in zip(rows, source_labels(rows)):
            st = stats.get(r[2])
            peers.append({"mode": r[0][:1], "state": r[1][:1], "addr": r[2][:64], "name": label, "stratum": int(r[3]),
                          "poll": int(r[4]), "reach": r[5][:3],
                          "lastrx": int(r[6]) if r[6].isdigit() else 1 << 31,
                          "offset": finite(r[7]), "err": finite(r[9]),
                          "sd": finite(st[7]) if st else None})
    except (OSError, subprocess.SubprocessError, IndexError, ValueError) as e:
        log(f"chronyc: {e}")
        return None, []
    return tracking, peers


def source_labels(rows):
    """Configured hostnames for servers set up by name; IP addresses for everything else.

    `chronyc -N` prints names as written in chrony.conf. Pool members all print the pool's
    name, so a name that repeats (or a mismatched listing) falls back to the IP address.
    """
    ips = [r[2][:64] for r in rows]
    named = [r[2][:64] for r in chronyc("-N", "sources")[:32] if len(r) >= 10]
    if len(named) != len(ips):
        return ips
    return [n if named.count(n) == 1 and n != ip and HOSTNAME_RE.fullmatch(n) else ip
            for n, ip in zip(named, ips)]


def read_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", encoding="ascii") as f:
            return int(f.read()) / 1000
    except (OSError, ValueError):
        return None


def display_names(peers):
    """Hide private/LAN addresses of NTP sources from the public page."""
    names, lan = {}, 0
    for p in peers:
        try:
            private = ipaddress.ip_address(p["addr"]).is_private
        except ValueError:
            private = False
        if private:
            lan += 1
            names[p["name"]] = f"LAN server {lan}"
        else:
            names[p["name"]] = p["name"]
    return names


# --------------------------------------------------------------- storage ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS offsets (  -- per-minute servo stats in ns (locked samples only)
  ts INTEGER, src TEXT, total INTEGER, n INTEGER, sum REAL, sumsq REAL, min REAL, max REAL, freq REAL,
  PRIMARY KEY (ts, src));
CREATE TABLE IF NOT EXISTS minutes (
  ts INTEGER PRIMARY KEY, locked INTEGER, used INTEGER, seen INTEGER, fix INTEGER, temp REAL);
CREATE TABLE IF NOT EXISTS peers (ts INTEGER, name TEXT, offset REAL);
CREATE INDEX IF NOT EXISTS peers_ts ON peers (ts);
"""


def db_exec(sql, args=(), many=False):
    with db_lock:
        db = state["db"]
        cur = db.executemany(sql, args) if many else db.execute(sql, args)
        rows = cur.fetchall()
        db.commit()
        return rows


def gnss_summary():
    """Called with `lock` held."""
    g, now = live["gnss"], time.time()
    sats = live["sats"] if now - g.get("sky_ts", 0) < 60 else []
    used = [s for s in sats if s["u"]]
    return {
        "fix": g.get("mode", 0) if now - g.get("ts", 0) < STALE else 0,
        "used": len(used), "seen": sum(1 for s in sats if s["ss"] > 0),
        "cno": sum(s["ss"] for s in used) / len(used) if used else None,
        "pdop": g.get("pdop"), "tdop": g.get("tdop"), "leap": g.get("leap"),
    }


def flush():
    """Write the minute that just ended to the DB."""
    minute = int(time.time()) // 60 * 60 - 60
    with lock:
        done = {k: acc.pop(k) for k in [k for k in acc if k[0] <= minute]}
        gnss = gnss_summary()
    db_exec("INSERT OR REPLACE INTO offsets VALUES (?,?,?,?,?,?,?,?,?)",
            [(ts, src, a["total"], a["n"], a["sum"], a["sumsq"], a["min"], a["max"],
              a["freq"] / a["n"] if a["n"] else None) for (ts, src), a in done.items()], many=True)
    phc = done.get((minute, "phc"))
    locked = bool(phc and phc["n"] >= phc["total"] / 2)
    db_exec("INSERT OR REPLACE INTO minutes VALUES (?,?,?,?,?,?)",
            (minute, int(locked), gnss["used"], gnss["seen"], gnss["fix"], read_temp()))


def allan(src, since):
    """Overlapping Allan deviation at octave τ from a servo's per-minute mean frequency correction."""
    rows = db_exec("SELECT ts, freq FROM offsets WHERE src=? AND ts>=? AND freq IS NOT NULL ORDER BY ts",
                   (src, since))
    if not rows:
        return []
    mean = sum(f for _, f in rows) / len(rows)  # constant offset doesn't change ADEV; removing it keeps precision
    phases, x, last = [], [0.0], None
    for ts, f in rows:
        if last is not None and ts - last != 60:  # gap: start a new contiguous segment
            phases.append(x)
            x = [0.0]
        x.append(x[-1] + (f - mean) * 1e-9 * 60)
        last = ts
    phases.append(x)
    out, m = [], 1
    while True:
        total = count = 0
        for x in phases:
            if len(x) > 2 * m:
                total += sum((c - 2 * b + a) ** 2 for a, b, c in zip(x, x[m:], x[2 * m:]))
                count += len(x) - 2 * m
        if count < 5 * m:  # fewer than ~5 independent samples at this τ
            return out
        tau = m * 60
        out.append([tau, math.sqrt(total / (2 * tau * tau * count))])
        m *= 2


def prune(cutoff):
    db_exec("DELETE FROM offsets WHERE ts < ?", (cutoff,))
    db_exec("DELETE FROM minutes WHERE ts < ?", (cutoff,))
    db_exec("DELETE FROM peers WHERE ts < ?", (cutoff,))


def adev_worker():
    while True:
        try:
            since = int(time.time() - 30 * 86400)
            adev = {src: allan(src, since) for src in ("phc", "sys")}
            with lock:
                live["adev"] = {k: v for k, v in adev.items() if v}
        except sqlite3.Error as e:
            log(f"adev: {e}")
        time.sleep(ADEV_EVERY)


def poller():
    last_prune, last_minute = 0, int(time.time()) // 60
    while True:
        try:
            tracking, peers = poll_chrony()
            now = time.time()
            new = [(int(now - p["lastrx"]), p["name"], p["offset"]) for p in peers
                   if p["mode"] == "^" and p["lastrx"] < POLL and p["reach"] != "0" and p["offset"] is not None]
            if new:
                db_exec("INSERT INTO peers VALUES (?,?,?)", new, many=True)
            with lock:
                live.update(chrony=tracking, peers=peers)
            if int(now) // 60 != last_minute:
                last_minute = int(now) // 60
                flush()
            if now - last_prune > 86400:
                prune(int(now - RETENTION_DAYS * 86400))
                last_prune = now
        except sqlite3.Error as e:
            log(f"db: {e}")
        time.sleep(POLL - time.time() % POLL)


# ------------------------------------------------------------------- api ----

def cached(key, fn):
    """Return fn() at most once per CACHE_TTL, computed by one thread at a time."""
    with cache_lock:
        hit = cache.get(key)
        if not hit or time.time() - hit[0] >= CACHE_TTL:
            hit = cache[key] = (time.time(), fn())
        return hit[1]


def servo_stats(src, since):
    n, s, sq, lo, hi = db_exec("SELECT SUM(n), SUM(sum), SUM(sumsq), MIN(min), MAX(max) FROM offsets "
                               "WHERE src=? AND ts>=?", (src, since))[0]
    if not n:
        return None
    mean = s / n
    return {"mean": mean * 1e-9, "sd": math.sqrt(max(0, sq / n - mean * mean)) * 1e-9,
            "max": max(abs(lo), abs(hi)) * 1e-9}


def window_stats():
    now, out = int(time.time()), {}
    for label, span in WINDOWS.items():
        w = {src: st for src in ("phc", "sys") if (st := servo_stats(src, now - span))}
        total, locked = db_exec("SELECT COUNT(*), SUM(locked) FROM minutes WHERE ts>=?", (now - span,))[0]
        w["avail"] = locked / total if total else None
        w["minutes"] = total  # recorded minutes; less than the window early on or after downtime
        out[label] = w
    days = {d: l / c for d, c, l in db_exec(
        "SELECT ts/86400, COUNT(*), SUM(locked) FROM minutes WHERE ts>=? GROUP BY 1",
        ((now // 86400 - 29) * 86400,))}
    out["days"] = [[d * 86400, days.get(d)] for d in range(now // 86400 - 29, now // 86400 + 1)]
    # Lock streak ends at the last unlocked minute, or a gap of more than 5 minutes in the record.
    row = db_exec("SELECT ts, locked FROM (SELECT ts, locked, ts - LAG(ts) OVER (ORDER BY ts) AS gap "
                  "FROM minutes) WHERE locked=0 OR gap>300 OR gap IS NULL ORDER BY ts DESC LIMIT 1")
    out["locked_since"] = (row[0][0] + (0 if row[0][1] else 60)) if row else None
    return out


def api_status():
    now = time.time()
    with lock:
        servo = {k: {"offset": v["offset"], "state": v["state"], "freq": v["freq"], "age": now - v["ts"]}
                 for k, v in live["servo"].items()}
        gnss = gnss_summary()
        sats = [{"g": s["g"], "ss": s["ss"], "u": s["u"]} for s in live["sats"]] if gnss["seen"] or gnss["used"] else []
        chrony, peers, adev = live["chrony"], live["peers"], live["adev"]
        gnss_age = now - live["gnss"].get("ts", 0)
    names = display_names(peers)
    peers = [{**{k: v for k, v in p.items() if k not in ("lastrx", "addr")}, "name": names[p["name"]]} for p in peers]
    phc = servo.get("phc")
    sys_clk = servo.get("sys")
    # The system clock (what NTP serves) must track the PHC too, when phc2sys logs offsets.
    phc_ok = (phc and phc["age"] < STALE and phc["state"] == 2
              and (not sys_clk or (sys_clk["age"] < STALE and sys_clk["state"] == 2)))
    # Health from each data source's freshness, not systemd state (a running daemon can still be stuck).
    sources = [{"name": "gpsd", "ok": gnss_age < STALE}, {"name": "chrony", "ok": chrony is not None},
               {"name": "ts2phc", "ok": bool(phc and phc["age"] < STALE)}]
    if "sys" in servo:  # only known when phc2sys logs offsets
        sources.append({"name": "phc2sys", "ok": servo["sys"]["age"] < STALE})
    if not chrony:
        status = ("bad", "NTP service down")
    elif phc_ok and gnss["fix"] >= 2:
        status = ("ok", "Locked to GNSS")
    elif phc_ok:
        status = ("warn", "Locked to PPS, no GNSS fix")
    else:
        status = ("warn", "Holdover")
    return {"status": status[0], "status_text": status[1], "servo": servo, "gnss": gnss, "sats": sats,
            "chrony": chrony, "peers": peers, "sources": sources, "temp": read_temp(),
            "stats": cached("stats", window_stats), "adev": adev, "now": time.time()}


def api_history(span):
    now = int(time.time())
    step = max(60, span // 300 // 60 * 60)
    since = now - span
    out = {"step": step, "since": since, "now": now, "servo": {}, "peers": {}}
    for src in ("phc", "sys"):
        rows = db_exec("SELECT ts/?*? AS b, SUM(n), SUM(sum), MIN(min), MAX(max) FROM offsets "
                       "WHERE src=? AND ts>=? AND n>0 GROUP BY b ORDER BY b", (step, step, src, since))
        if rows:
            out["servo"][src] = [[b, s / n * 1e-9, lo * 1e-9, hi * 1e-9] for b, n, s, lo, hi in rows]
    with lock:
        peers = [p for p in live["peers"] if p["mode"] == "^"]
    for name, shown in display_names(peers).items():
        rows = db_exec("SELECT ts/?*? AS b, AVG(offset) FROM peers WHERE name=? AND ts>=? GROUP BY b ORDER BY b",
                       (step, step, name, since))
        out["peers"][shown] = [list(r) for r in rows]
    return out


def to_json(obj):
    return json.dumps(obj, allow_nan=False, separators=(",", ":")).encode()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # listen backlog; Python's default of 5 refuses bursts
    slots = threading.BoundedSemaphore(MAX_CONN)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):  # pylint: disable=consider-using-with
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "time-status"
    timeout = 10  # seconds; drops slow or idle clients

    def send(self, code, body, ctype, cache_control):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", state["csp"])
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # pylint: disable=invalid-name
        url = urlsplit(self.path)
        try:
            if url.path == "/" and not url.query:
                self.send(200, state["page"], "text/html; charset=utf-8", "max-age=300")
            elif url.path == "/api/status" and not url.query:
                self.send(200, to_json(api_status()), "application/json", "no-store")
            elif url.path == "/api/history" and url.query in HISTORY_QUERIES:
                span = HISTORY_QUERIES[url.query]
                self.send(200, cached(f"hist-{span}", lambda: to_json(api_history(span))),
                          "application/json", "max-age=30")
            else:
                self.send(404, b"Not found\n", "text/plain; charset=utf-8", "max-age=300")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as e:  # pylint: disable=broad-exception-caught
            log(f"http: {type(e).__name__}: {e}")
            try:
                self.send(500, b"Internal error\n", "text/plain; charset=utf-8", "no-store")
            except OSError:
                pass

    do_HEAD = do_GET

    def send_error(self, code, message=None, explain=None):
        """Minimal error body for malformed requests, without echoing anything back."""
        self.close_connection = True
        try:
            self.send(code, b"Bad request\n", "text/plain; charset=utf-8", "no-store")
        except OSError:
            pass

    def version_string(self):
        return self.server_version

    def log_message(self, format, *args):  # pylint: disable=redefined-builtin
        pass


def csp_hash(page, tag):
    """CSP source for the single inline <script> or <style> in the page."""
    body = re.search(rf"<{tag}>(.*?)</{tag}>", page, re.S)[1]
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


def main():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8") as f:
        page = f.read().replace("__TITLE__", html.escape(TITLE)).replace("__SUBTITLE__", html.escape(SUBTITLE))
    state["page"] = page.encode()
    state["csp"] = (f"default-src 'none'; script-src {csp_hash(page, 'script')}; style-src {csp_hash(page, 'style')}; "
                    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    state["db"] = sqlite3.connect(DB_PATH, check_same_thread=False)
    state["db"].execute("PRAGMA journal_mode=WAL")
    state["db"].executescript(SCHEMA)
    for fn in (gpsd_reader, journal_reader, poller, adev_worker):
        threading.Thread(target=fn, daemon=True).start()
    server = Server((BIND, PORT), Handler)
    log(f"time-status on {BIND or '*'}:{PORT}, db {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
