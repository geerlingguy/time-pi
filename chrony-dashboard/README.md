# Chrony Tracking Dashboard

This is a simple Python-based dashboard meant to track Chrony timing information and display it over HTTP.

It also includes a light, dark, and 'TrueTime' vintage theme.

## Screenshot

<p align="center"><img alt="Chrony Dashboard" src="/resources/chrony-dashboard.jpg" height="auto" width="600"></p>

## Installation

```
sudo mkdir -p /opt/chrony-dashboard
sudo cp chrony_dashboard.py chart.umd.min.js /opt/chrony-dashboard/
sudo cp chrony-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now chrony-dashboard
```

## Configuration

Inside `chrony_dashboard.py`, there are a few configuration options:

  - `DASH_PORT`: HTTP Port the dashboard will run on
  - `DASH_DB`: Path to the SQLite database where chrony stats will be stored
  - `DASH_POLL`: Poll interval, in seconds
  - `DASH_RETENTION`: Days of history to keep
  - `DASH_IFACE`: Can be used to specify an interface for network throughput statistics e.g. `DASH_IFACE=eth0`.

## Resetting the Database

If you'd like to reset the database, for any reason, stop the service, delete the database files, and restart the service:

```
sudo systemctl stop chrony-dashboard
sudo rm -f /var/lib/chrony-dashboard/chrony.db{,-wal,-shm}
sudo systemctl start chrony-dashboard
```

If you just want to empty the database—even while the service is running, and you have the `sqlite3` CLI installed:

```
sudo sqlite3 /var/lib/chrony-dashboard/chrony.db 'DELETE FROM tracking; DELETE FROM events; VACUUM;'
```
