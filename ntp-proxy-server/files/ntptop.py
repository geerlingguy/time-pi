#!/usr/bin/python3 -I
"""ntptop: a btop-style terminal dashboard for a chrony NTP server."""

import collections
import curses
import locale
import os
import socket
import subprocess  # nosec B404
import time
from types import SimpleNamespace

CHRONYC = "/usr/bin/chronyc"
CHRONYC_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
BLOCKS = " ▁▂▃▄▅▆▇█"
SUPERSCRIPT = "⁰¹²³⁴⁵⁶⁷⁸⁹"
HISTORY = 4000
MIN_COL_WIDTH = 50
CLIENTS_INTERVAL = 10
TOP_CLIENTS = 10

# Key, title. Key 1 is the graph; the rest are text panels.
PANELS = [
    ("1", "requests"),
    ("2", "server"),
    ("3", "offenders"),
    ("4", "sources"),
    ("5", "tracking"),
    ("6", "net"),
    ("7", "system"),
]

# Index and label of the useful chronyc -c serverstats fields.
SERVERSTATS_FIELDS = [
    (0, "NTP received"), (1, "NTP dropped"), (4, "Clientlog dropped"), (8, "Interleaved NTP"),
]

# Curses color attributes, set by init_colors().
C = SimpleNamespace(green=0, yellow=0, red=0, cyan=0, blue=0, magenta=0,
                    dim=curses.A_DIM, bold=curses.A_BOLD)


def clean(text):
    """Strip non-printable characters so output can't inject terminal escapes."""
    return "".join(ch for ch in text if ch.isprintable())


def chronyc(command, timeout=2):
    try:
        out = subprocess.run([CHRONYC, "-c", "-n", command], capture_output=True, text=True,  # nosec B603
                             timeout=timeout, env=CHRONYC_ENV, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode:
        return None
    return [clean(line).split(",") for line in out.stdout.strip().splitlines()]


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def default_iface():
    try:
        for line in read("/proc/net/route").splitlines()[1:]:
            parts = line.split()
            if parts[1] == "00000000":
                return parts[0]
    except OSError:
        pass
    return "eth0"


def net_counters(iface):
    """Return (rx bytes, rx packets, tx bytes, tx packets)."""
    try:
        for line in read("/proc/net/dev").splitlines():
            name, _, rest = line.partition(":")
            if name.strip() == iface:
                v = rest.split()
                return [int(v[0]), int(v[1]), int(v[8]), int(v[9])]
    except OSError:
        pass
    return []


def cpu_ticks():
    """Return (busy, total) jiffies across all CPUs."""
    v = [int(x) for x in read("/proc/stat").splitlines()[0].split()[1:9]]
    return sum(v) - v[3] - v[4], sum(v)


def meminfo():
    m = {}
    for line in read("/proc/meminfo").splitlines():
        k, v = line.split(":", 1)
        m[k] = int(v.split()[0]) * 1024
    return m["MemTotal"], m["MemTotal"] - m["MemAvailable"]


def find_pid(name):
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            if read(f"/proc/{pid}/comm").strip() == name:
                return pid
        except OSError:
            pass
    return None


def proc_usage(pid):
    """Return (cpu seconds, rss bytes) for a pid."""
    try:
        v = read(f"/proc/{pid}/stat").rsplit(")", 1)[1].split()
        return ((int(v[11]) + int(v[12])) / os.sysconf("SC_CLK_TCK"),
                int(v[21]) * os.sysconf("SC_PAGE_SIZE"))
    except (OSError, IndexError, ValueError):
        return None


def num(s):
    try:
        return float(s)
    except ValueError:
        return 0.0


def fmt_secs(s, signed=False):
    a = abs(s)
    for unit, mult in (("s", 1), ("ms", 1e3), ("µs", 1e6), ("ns", 1e9)):
        if a * mult >= 1 or unit == "ns":
            break
    return f"{s * mult:{'+' if signed else ''}.1f}{unit}"


def fmt_si(n, unit=""):
    for suffix, mult in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= mult:
            return f"{n / mult:.1f}{suffix}{unit}"
    return f"{n:.0f}{unit}" if n >= 10 or n == 0 else f"{n:.1f}{unit}"


def fmt_bytes(n):
    for suffix in ("B", "KiB", "MiB", "GiB"):
        if n < 1024:
            return f"{n:.1f} {suffix}"
        n /= 1024
    return f"{n:.1f} TiB"


def fmt_dur(s):
    s = int(s)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s}s" if m else f"{s}s"


class Stats:  # pylint: disable=too-many-instance-attributes
    def __init__(self):
        self.iface = default_iface()
        self.req = collections.deque(maxlen=HISTORY)
        self.drop = collections.deque(maxlen=HISTORY)
        self.tracking, self.sources, self.server, self.clients = [], [], [], []
        self.net, self.net_rate = [], []
        self.sys = {}
        self.chronyd_pid = find_pid("chronyd")
        self.prev = self.prev_sys = None
        self.clients_next = 0

    def sample(self):
        now = time.monotonic()
        t = chronyc("tracking") or []
        self.tracking = t[0] if t and len(t[0]) >= 14 else []
        self.sources = [s for s in chronyc("sources") or [] if len(s) >= 10]
        s = chronyc("serverstats")
        self.server = [num(v) for v in s[0]] if s else []
        self.net = net_counters(self.iface)

        if self.prev:
            dt = now - self.prev[0]
            ps, pn = self.prev[1], self.prev[2]
            # Counters reset if chronyd restarts.
            if self.server and ps and self.server[0] >= ps[0]:
                self.req.append((self.server[0] - ps[0]) / dt)
                self.drop.append((self.server[1] - ps[1]) / dt)
            if self.net and pn:
                self.net_rate = [(a - b) / dt for a, b in zip(self.net, pn)]
        self.prev = (now, self.server, self.net)
        self.sample_system(now)

        # Client list can be large, so poll it less often and keep the last good one.
        if now >= self.clients_next:
            clients = chronyc("clients", timeout=5)
            if clients is not None:
                self.clients = [c for c in clients if len(c) >= 6]
            self.clients_next = now + CLIENTS_INTERVAL

    def sample_system(self, now):
        busy, total = cpu_ticks()
        chrony = proc_usage(self.chronyd_pid) if self.chronyd_pid else None
        if chrony is None:
            self.chronyd_pid = find_pid("chronyd")
        t = os.times()
        # Includes the chronyc calls ntptop makes.
        own = t.user + t.system + t.children_user + t.children_system
        mem_total, mem_used = meminfo()
        self.sys.update(mem_total=mem_total, mem_used=mem_used, chrony_rss=chrony[1] if chrony else None)
        if self.prev_sys:
            pt, pb, ptot, pc, po = self.prev_sys
            dt = now - pt
            self.sys["cpu"] = (busy - pb) / max(1, total - ptot) * 100
            self.sys["own"] = (own - po) / dt * 100
            if chrony and pc:
                self.sys["chrony_cpu"] = (chrony[0] - pc[0]) / dt * 100
        self.prev_sys = (now, busy, total, chrony, own)


def init_colors():
    try:
        curses.curs_set(0)
    except curses.error:
        pass  # Terminal can't hide the cursor.
    if not curses.has_colors():
        return
    curses.start_color()
    bg = -1
    try:
        curses.use_default_colors()
    except curses.error:
        bg = curses.COLOR_BLACK
    for i, name in enumerate(("green", "yellow", "red", "cyan", "blue", "magenta"), 1):
        curses.init_pair(i, getattr(curses, f"COLOR_{name.upper()}"), bg)
        setattr(C, name, curses.color_pair(i))


class Screen:
    def __init__(self, scr):
        self.scr = scr

    def put(self, y, x, text, attr=0, maxw=None):
        if maxw is not None:
            text = text[:max(0, maxw)]
        try:
            self.scr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def box(self, rect, key, title, extra=""):
        y, x, h, w = rect
        self.put(y, x, "╭" + "─" * (w - 2) + "╮", C.blue)
        for r in range(1, h - 1):
            self.put(y + r, x, "│", C.blue)
            self.put(y + r, x + w - 1, "│", C.blue)
        self.put(y + h - 1, x, "╰" + "─" * (w - 2) + "╯", C.blue)
        self.put(y, x + 2, SUPERSCRIPT[int(key)], C.magenta | C.bold)
        self.put(y, x + 3, title, C.bold)
        if extra and len(extra) + len(title) + 8 < w:
            self.put(y, x + w - len(extra) - 3, extra, C.dim)

    def graph(self, rect, data):
        """Plot newest values right-aligned, scaled to the visible peak."""
        y, x, h, w = rect
        vals = list(data)[-w:]
        peak = max(vals, default=0) or 1
        off = w - len(vals)
        for i, v in enumerate(vals):
            eighths = round(v / peak * h * 8)
            for r in range(h):
                fill = min(8, max(0, eighths - r * 8))
                if fill:
                    self.put(y + h - 1 - r, x + off + i, BLOCKS[fill], C.green)
        self.put(y, x, fmt_si(peak), C.dim)
        self.put(y + h - 1, x, "0", C.dim)


def offset_attr(secs):
    a = abs(secs)
    return C.green if a < 1e-3 else C.yellow if a < 10e-3 else C.red


def kv(label, value, attr=0):
    return [(f"{label:<18}", C.dim), (value, attr)]


def meter(pct, width=10):
    n = round(min(100, max(0, pct)) / 100 * width)
    attr = C.green if pct < 60 else C.yellow if pct < 85 else C.red
    return [("■" * n, attr), ("■" * (width - n) + " ", C.dim)]


def tracking_lines(st):
    t = st.tracking
    if not t:
        return [[("chronyc tracking unavailable", C.red)]]
    return [
        kv("Reference", f"{t[1]} ({t[0]})", C.cyan),
        kv("Stratum", t[2]),
        kv("Leap status", t[13], C.green if t[13] == "Normal" else C.red),
        kv("Last update", f"{fmt_dur(time.time() - num(t[3]))} ago"),
        kv("System offset", fmt_secs(num(t[4]), True), offset_attr(num(t[4]))),
        kv("Last offset", fmt_secs(num(t[5]), True), offset_attr(num(t[5]))),
        kv("RMS offset", fmt_secs(num(t[6])), offset_attr(num(t[6]))),
        kv("Frequency", f"{num(t[7]):+.3f} ppm"),
        kv("Skew", f"{num(t[9]):.3f} ppm"),
        kv("Root delay", fmt_secs(num(t[10]))),
        kv("Root dispersion", fmt_secs(num(t[11]))),
    ]


def sources_lines(st):
    if not st.sources:
        return [[("chronyc sources unavailable", C.red)]]
    width = max(15, min(39, max(len(s[2]) for s in st.sources)))
    lines = [[(f"   {'Source':<{width}} St Poll Reach LastRx   Offset    ±Err", C.dim)]]
    state_attr = {"*": C.green | C.bold, "+": C.cyan, "-": 0}
    for s in st.sources:
        offset = num(s[7])
        lines.append([
            (f"{s[0]}{s[1]} {s[2]:<{width}.{width}} ", state_attr.get(s[1], C.red)),
            (f"{s[3]:>2} {2 ** int(num(s[4])):>4} ", 0),
            (f"{s[5]:>5} ", C.green if s[5] == "377" else C.yellow),
            (f"{fmt_dur(num(s[6])):>6} ", 0),
            (f"{fmt_secs(offset, True):>8} ", offset_attr(offset)),
            (f"{fmt_secs(num(s[9])):>7}", C.dim),
        ])
    return lines


def server_lines(st):
    s = st.server
    if not s:
        return [[("chronyc serverstats unavailable (run with sudo)", C.red)]]
    drop = st.drop[-1] if st.drop else 0
    lines = [
        kv("Requests/s", fmt_si(st.req[-1] if st.req else 0), C.green | C.bold),
        kv("Dropped/s", fmt_si(drop), C.red if drop else 0),
        kv("Dropped total", f"{s[1] / s[0] * 100 if s[0] else 0:.2f}%", C.yellow if s[1] else 0),
    ]
    lines += [kv(label, f"{s[i]:,.0f}") for i, label in SERVERSTATS_FIELDS if i < len(s)]
    return lines


def offender_lines(st):
    if not st.clients:
        return [[("chronyc clients unavailable (run with sudo)", C.red)]]
    # Most drops first, then most frequent (smallest log2 interval).
    rows = sorted(st.clients, key=lambda c: (-num(c[2]), num(c[3]) if c[3] != "-" else 99))[:TOP_CLIENTS]
    width = max(15, min(39, max(len(c[0]) for c in rows)))
    lines = [[(f"{'Client':<{width}}  Packets  Dropped  Drop%  Every", C.dim)]]
    for c in rows:
        pkts, drop = num(c[1]), num(c[2])
        every = "-" if c[3] == "-" else f"{2 ** num(c[3]):.2g}s"
        attr = C.red if drop else 0
        lines.append([
            (f"{c[0]:<{width}.{width}}  ", attr | (C.bold if drop else 0)),
            (f"{fmt_si(pkts):>7}  {fmt_si(drop):>7}  {drop / pkts * 100 if pkts else 0:>4.0f}%  ", attr),
            (f"{every:>5}", C.dim),
        ])
    lines.append([(f"{len(st.clients)} clients, refreshed every {CLIENTS_INTERVAL}s", C.dim)])
    return lines


def net_lines(st):
    if not st.net:
        return [[(f"{st.iface}: no counters", C.red)]]
    r = st.net_rate or [0, 0, 0, 0]
    return [
        kv("Interface", st.iface, C.cyan),
        kv("RX", f"{fmt_si(r[0] * 8, 'bps'):>9}  {fmt_si(r[1]):>6} pkt/s", C.green),
        kv("TX", f"{fmt_si(r[2] * 8, 'bps'):>9}  {fmt_si(r[3]):>6} pkt/s", C.cyan),
        kv("RX since boot", fmt_bytes(st.net[0])),
        kv("TX since boot", fmt_bytes(st.net[2])),
    ]


def system_lines(st):
    s = st.sys
    cpu = s.get("cpu", 0)
    mem_pct = s["mem_used"] / s["mem_total"] * 100
    chrony = (f"{s.get('chrony_cpu', 0):.1f}% CPU  {fmt_bytes(s['chrony_rss'])}"
              if s.get("chrony_rss") else "not found")
    return [
        kv("CPU", "") + meter(cpu) + [(f"{cpu:.1f}%", 0)],
        kv("Memory", "") + meter(mem_pct) + [(f"{fmt_bytes(s['mem_used'])} / {fmt_bytes(s['mem_total'])}", 0)],
        kv("Load", " ".join(read("/proc/loadavg").split()[:3])),
        kv("chronyd", chrony, C.cyan),
        kv("ntptop", f"{s.get('own', 0):.1f}% CPU", C.dim),
    ]


TEXT_PANELS = {"2": server_lines, "3": offender_lines, "4": sources_lines, "5": tracking_lines,
               "6": net_lines, "7": system_lines}


def draw_header(scr, w, visible, interval):
    up = fmt_dur(float(read("/proc/uptime").split()[0]))
    load = " ".join(read("/proc/loadavg").split()[:3])
    left = (f" ntptop  {clean(socket.gethostname())}  up {up}  load {load}  "
            f"{time.strftime('%H:%M:%S UTC', time.gmtime())}")
    scr.put(0, 0, left, C.bold, w)
    right = [(f"{SUPERSCRIPT[int(k)]}{t} ", C.magenta if visible[k] else C.dim) for k, t in PANELS]
    right.append((f"  +/- {interval:g}s  ? help  q quit ", C.dim))
    if len(left) + sum(len(t) for t, _ in right) + 2 > w:
        right = right[-1:]
    x = w - sum(len(t) for t, _ in right)
    for text, attr in right:
        scr.put(0, x, text, attr)
        x += len(text)


def draw_requests(scr, st, rect, interval):
    w = rect[3]
    vis = list(st.req)[-(w - 2):] or [0]
    drop = st.drop[-1] if st.drop else 0
    extra = (f" {fmt_si(vis[-1])} req/s  avg {fmt_si(sum(vis) / len(vis))}  peak {fmt_si(max(vis))}"
             f"  drop {fmt_si(drop)}/s  ({fmt_dur((w - 2) * interval)}) ")
    scr.box(rect, "1", "requests", extra)
    y, x, h, _ = rect
    scr.graph((y + 1, x + 1, h - 2, w - 2), st.req)


def layout_columns(texts, ncols):
    """Put each text panel in the shortest column."""
    cols = [[] for _ in range(ncols)]
    heights = [0] * ncols
    for panel in texts:
        i = heights.index(min(heights))
        cols[i].append(panel)
        heights[i] += len(panel[2]) + 2
    return cols, max(heights, default=0)


def draw_text_panel(scr, rect, key, title, lines):
    y, x, h, w = rect
    scr.box(rect, key, title)
    for r, line in enumerate(lines[:h - 2]):
        lx = x + 2
        for text, attr in line:
            scr.put(y + 1 + r, lx, text, attr, x + w - 2 - lx)
            lx += len(text)


def draw_columns(scr, cols, top, size):
    h, w = size
    col_w = w // len(cols)
    for c, panels in enumerate(cols):
        x = c * col_w
        cw = w - x if c == len(cols) - 1 else col_w
        y = top
        for panel in panels:
            ph = min(len(panel[2]) + 2, h - y)
            if ph < 3:
                break
            draw_text_panel(scr, (y, x, ph, cw), *panel)
            y += ph


def draw_help(scr, size):
    lines = [[(f"{'Key':<8}", C.bold), ("Action", C.bold)]]
    lines += [[(f"{k:<8}", C.magenta), (f"Show/hide {t}", 0)] for k, t in PANELS]
    lines += [[(f"{k:<8}", C.magenta), (a, 0)] for k, a in (
        ("+ / -", "Slower / faster sampling (0.5-10s)"),
        ("? / h", "Show/hide this help"),
        ("Esc", "Close help"),
        ("q", "Quit"),
    )]
    h, w = size
    ph, pw = min(h, len(lines) + 2), min(w, 46)
    y, x = (h - ph) // 2, (w - pw) // 2
    for r in range(ph):
        scr.put(y + r, x, " " * pw)
    draw_text_panel(scr, (y, x, ph, pw), "0", "help", lines)


def draw(scr, st, visible, interval, show_help=False):
    scr.scr.erase()
    h, w = scr.scr.getmaxyx()
    draw_header(scr, w, visible, interval)
    texts = [(k, t, TEXT_PANELS[k](st)) for k, t in PANELS if k in TEXT_PANELS and visible[k]]
    cols, text_h = layout_columns(texts, max(1, min(len(texts), w // MIN_COL_WIDTH))) if texts else ([], 0)

    # Text panels get their natural height; the graph gets what's left (at least 4 rows).
    top = 1
    if visible["1"]:
        graph_h = max(4, h - 1 - text_h)
        draw_requests(scr, st, (top, 0, graph_h, w), interval)
        top += graph_h
    if cols:
        draw_columns(scr, cols, top, (h, w))
    if show_help:
        draw_help(scr, (h, w))
    scr.scr.refresh()


def main(stdscr):
    init_colors()
    scr = Screen(stdscr)
    stdscr.timeout(200)
    st = Stats()
    visible = {k: True for k, _ in PANELS}
    interval = 1.0
    st.sample()
    next_sample = time.monotonic() + interval
    key, show_help = 0, False
    while True:
        if time.monotonic() >= next_sample:
            st.sample()
            next_sample = time.monotonic() + interval
            key = 0
        if key != -1:
            draw(scr, st, visible, interval, show_help)
        key = stdscr.getch()
        if key in (ord("q"), ord("Q")) or (key == 27 and not show_help):
            break
        if key in (ord("?"), ord("h"), ord("H")) or (key == 27 and show_help):
            show_help = not show_help
        elif 0 < key < 256 and chr(key) in visible:
            visible[chr(key)] = not visible[chr(key)]
        elif key in (ord("+"), ord("=")):
            interval = min(10.0, interval * 2)
        elif key == ord("-"):
            interval = max(0.5, interval / 2)


if __name__ == "__main__":
    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
