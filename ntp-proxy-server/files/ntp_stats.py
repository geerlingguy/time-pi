#!/usr/bin/env python3
"""Show NTP requests per second from chrony's serverstats log as a terminal bar chart.

Usage: ntp-stats [HOURS] [BUCKET_MINUTES]   (default: last 24 hours, 60-minute buckets)
"""
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
    for path in sorted(glob.glob(LOG + "*"), key=os.path.getmtime):
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
        if prev_ts and ts >= since:
            yield (ts, (ts - prev_ts).total_seconds(),
                   rx - prev_rx if rx >= prev_rx else rx,
                   drop - prev_drop if drop >= prev_drop else drop)
        prev_ts, prev_rx, prev_drop = ts, rx, drop


def draw_bar(value, peak):
    eighths = round(value / peak * WIDTH * 8) if peak else 0
    return "█" * (eighths // 8) + EIGHTHS[eighths % 8].strip()


def main():
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    bucket = int(sys.argv[2]) * 60 if len(sys.argv) > 2 else 3600
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    buckets = {}
    for ts, seconds, rx, drop in samples(since):
        start = datetime.fromtimestamp(ts.timestamp() // bucket * bucket, timezone.utc)
        total = buckets.setdefault(start, [0, 0, 0])
        total[0] += seconds
        total[1] += rx
        total[2] += drop
    if not buckets:
        sys.exit(f"No data in {LOG} for the last {hours} hours.")
    rates = {start: rx / seconds for start, (seconds, rx, _) in buckets.items() if seconds}
    peak = max(rates.values(), default=0)
    print(f"{'UTC':16}  {'req/s':>8}  {'dropped':>7}")
    for start in sorted(rates):
        print(f"{start:%Y-%m-%d %H:%M}  {rates[start]:8.2f}  {buckets[start][2]:7}  {draw_bar(rates[start], peak)}")
    print(f"\nTotal: {sum(b[1] for b in buckets.values())} requests, "
          f"{sum(b[2] for b in buckets.values())} dropped by rate limiting.")


if __name__ == "__main__":
    main()
