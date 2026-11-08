#!/usr/bin/env python3
"""Show NTP requests per second from chrony's serverstats log as a terminal bar chart."""
import argparse
import glob
import gzip
import os
import sys
from datetime import datetime, timedelta, timezone

LOG = "/var/log/chrony/serverstats.log"
EIGHTHS = " ▏▎▍▌▋▊▉"
WIDTH = 50


def read_lines():
    """Yield log lines oldest first, including rotated (and gzipped) logs."""
    if not os.access(os.path.dirname(LOG), os.R_OK | os.X_OK):
        sys.exit(f"Can't read {os.path.dirname(LOG)}; run with sudo.")
    paths = sorted(glob.glob(LOG + "*"), key=os.path.getmtime)
    if not paths:
        sys.exit(f"{LOG} doesn't exist yet; it's written every minute by cron.")
    for path in paths:
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", encoding="utf-8") as f:
            yield from f


def samples(since):
    """Yield (time, seconds, requests, dropped) between log lines; a counter drop means chrony restarted."""
    prev_ts = prev_rx = prev_drop = None
    for line in read_lines():
        fields = line.strip().split(",")
        try:
            ts = datetime.fromisoformat(fields[0].replace("Z", "+00:00"))
            rx, drop = int(fields[1]), int(fields[2])
        except (ValueError, IndexError):
            continue
        # Skip deltas spanning a gap in the log (e.g. the VPS was down), so they don't smear across buckets.
        if prev_ts and ts >= since and (ts - prev_ts).total_seconds() <= 300:
            yield (ts, (ts - prev_ts).total_seconds(),
                   rx - prev_rx if rx >= prev_rx else rx,
                   drop - prev_drop if drop >= prev_drop else drop)
        prev_ts, prev_rx, prev_drop = ts, rx, drop


def draw_bar(value, peak):
    eighths = round(value / peak * WIDTH * 8) if peak else 0
    return "█" * (eighths // 8) + EIGHTHS[eighths % 8].strip()


def parse_args():
    """Return (hours, bucket seconds) from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("hours", nargs="?", type=float, default=24, help="how far back to show (default: 24)")
    parser.add_argument("minutes", nargs="?", type=int, default=60, help="minutes per bar (default: 60)")
    args = parser.parse_args()
    if args.hours <= 0 or args.minutes < 1:
        parser.error("hours must be > 0 and minutes >= 1")
    return args.hours, args.minutes * 60


def main():
    hours, bucket = parse_args()
    now = datetime.now(timezone.utc)
    first = int((now - timedelta(hours=hours)).timestamp() // bucket * bucket)
    since = datetime.fromtimestamp(first, timezone.utc)
    buckets = {}
    for ts, seconds, rx, drop in samples(since):
        total = buckets.setdefault(int(ts.timestamp() // bucket * bucket), [0, 0, 0])
        total[0] += seconds
        total[1] += rx
        total[2] += drop
    rates = {start: rx / seconds for start, (seconds, rx, _) in buckets.items() if seconds}
    peak = max(rates.values(), default=0)
    print(f"{'UTC':16}  {'req/s':>8}  {'dropped':>7}")
    # Stop a minute short of now, since the current minute isn't logged yet.
    for start in range(first, int(now.timestamp()) - 60, bucket):
        label = f"{datetime.fromtimestamp(start, timezone.utc):%Y-%m-%d %H:%M}"
        if start in rates:
            print(f"{label}  {rates[start]:8.2f}  {buckets[start][2]:7}  {draw_bar(rates[start], peak)}")
        else:
            print(f"{label}  {'-':>8}  {'-':>7}  no data")
    print(f"\nTotal: {sum(b[1] for b in buckets.values())} requests, "
          f"{sum(b[2] for b in buckets.values())} dropped by rate limiting.")


if __name__ == "__main__":
    main()
