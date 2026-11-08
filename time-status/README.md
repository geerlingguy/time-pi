# Time Status

A compact, public status page for the Time Pi: GNSS lock, PHC and system clock offsets, stability over time, satellites, and how this clock compares to other NTP servers.

Python 3 stdlib only (no pip packages, no JS libraries). History is kept in SQLite as per-minute aggregates, so the database stays small (a few MB per month).

## Data sources

| What | Where it comes from |
| --- | --- |
| PHC vs GPS PPS offset, servo state, frequency | `ts2phc` log lines in the journal (needs `ts2phc_logging_level: 6`, the default) |
| System clock vs PHC offset | `phc2sys` log lines (needs `phc2sys_logging_level: 6`), or chrony's PHC refclock (`chronyc sources`, every 15 s) when `chrony_discipline_clock: true` |
| Fix, satellites, C/N₀, DOP, GPS−UTC | `gpsd` JSON on `localhost:2947` |
| Stratum, reference, other NTP servers | `chronyc -h 127.0.0.1` `tracking`, `sources`, `sourcestats` (unprivileged UDP monitoring port); servers configured by hostname show that name (`chronyc -N`), pool members show their IP |

The footer dots show whether each data source (gpsd, chrony, ts2phc, and phc2sys if it logs offsets) has reported in the last 10 seconds.

The Allan deviation chart is computed every 10 minutes from the per-minute mean frequency correction of ts2phc (the NIC oscillator vs GPS) and phc2sys or chrony (the Pi's oscillator vs the PHC) over the last 30 days. Short τ includes GPS PPS noise and servo action, so the chart also draws an estimated PPS noise floor (√3·σ/τ, from the 24h PHC σ).

"Locked" means ts2phc reported servo state `s2` for at least half of the samples in a minute. Offset statistics only include locked samples. The NTP servers in `chrony.conf` (`pool ... noselect` by default) are used for comparison only; their offsets include network path asymmetry, so a few hundred µs is normal.

## Installation

Set `time_status_enabled: true` in `config.yml` (and optionally `time_status_title`, e.g. `time.jeffgeerling.com`), then run the playbook:

```
ansible-playbook main.yml
```

Or manually:

```
sudo mkdir -p /opt/time-status
sudo cp time_status.py index.html /opt/time-status/
sudo cp ../templates/time-status.unit.j2 /etc/systemd/system/time-status.service  # then fill in the {{ }} values
sudo systemctl daemon-reload
sudo systemctl enable --now time-status
```

## Configuration

Environment variables (set in the systemd unit):

  - `DASH_BIND`: listen address (default all; use `127.0.0.1` behind a local reverse proxy)
  - `DASH_PORT`: HTTP port (default `80`)
  - `DASH_GPSD_PORT`: gpsd port on localhost (default `2947`)
  - `DASH_TITLE` / `DASH_SUBTITLE`: page heading and tagline
  - `DASH_RETENTION`: days of history to keep (default `90`)
  - `DASH_DB`: SQLite path (default `/var/lib/time-status/time-status.db`)

To show a block of text at the top of the page (e.g. NTP client instructions), put an HTML snippet in `info.html` next to `time_status.py` (the `time_status_info` playbook variable), and restart the service. Scripts and styles in it are blocked by the page's CSP.

## Security

The page is meant to face the internet, so it exposes as little as possible:

  - **Fixed routes only:** `/`, `/api/status` and `/api/history?range=1h|24h|7d|30d` (exact match), `GET`/`HEAD` only. No static file serving, no parameters reach SQL (all queries are parameterized).
  - **Nothing that leaks location or the LAN:** gpsd latitude/longitude are never read, per-satellite PRN/azimuth/elevation are dropped (with a timestamp they'd reveal the antenna's location), and private-range NTP source addresses show as "LAN server N". Host uptime isn't published.
  - **No chrony admin access:** chronyc talks to the read-only UDP monitoring port, never chronyd's admin socket, so even a compromised dashboard can't change the time being served. (This is why there's no NTP requests/s stat; `serverstats` needs the admin socket.)
  - **Least privilege:** runs as a throwaway `DynamicUser` in a sandboxed unit (`ProtectClock`, `ProtectSystem=strict`, syscall filter, etc.) with only the `systemd-journal` group, for ts2phc/phc2sys offsets. Check with `systemd-analyze security time-status`.
  - **DoS limits:** at most 32 concurrent connections, a 10 s socket timeout, single-flight 30 s caching of DB queries, and `CPUQuota`/`MemoryMax`/`Nice` so a flood can't starve the time daemons. A CDN or reverse proxy in front is still a good idea.
  - **Browser:** strict `Content-Security-Policy` (hash-pinned inline script/style, `connect-src 'self'`, no framing); all API strings are inserted with `textContent`.
  - **Untrusted input:** gpsd JSON is size-capped and type-checked; chronyc and journal fields are length-capped and parsed as numbers before use.

CI runs `pylint`, `bandit` and an HTTP smoke test on the script.

`/api/history` sends `Cache-Control: max-age=30` so a CDN can cache it; `/api/status` is `no-store`, since it's used to estimate the visitor's clock offset.

## Resetting the database

```
sudo systemctl stop time-status
sudo rm -f /var/lib/time-status/time-status.db{,-wal,-shm}
sudo systemctl start time-status
```
