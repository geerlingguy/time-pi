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
