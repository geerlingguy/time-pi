# NTP and GPSd Status Dashboard

This is a simple Python-based dashboard meant to show Chrony and GPSd Status information and display it over HTTP.

It does not write anything to the disk and is compatible with Alpines Diskless or Immutable Root installation. 
No background Task is running and the info is only updated when a Client is connected to the webserver port.

(c) Andreas H. - @andi-blafasl

## Screenshot

<p align="center"><img alt="NTP & GPSD Dashboard" src="/resources/gpsd-dashboard.jpg" height="auto" width="600"></p>

## Installation

### Raspberry Pi OS

The systemd service unit is untested, because I don't have raspberry pi os at hands right now ;-)

```
sudo mkdir -p /opt/gpsd-dashboard
sudo cp gpsd-dashboard.py /opt/gpsd-dashboard/
sudo cp gpsd-dashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gpsd-dashboard
```

### Alpine Linux

```
apk add python3
mkdir -p /opt/gpsd-dashboard
cp gpsd-dashboard.py /opt/gpsd-dashboard/
cp gpsd-dashboard /etc/init.d/
rc-update add gpsd-dashboard default
rc-update gpsd-dashboard start
```

## Configuration

Inside `gpsd-dashboard.py`, there is one configuration options:

  - `PORT`: HTTP Port the dashboard will run on

