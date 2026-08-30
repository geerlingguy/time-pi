# Pi Fan Control

This is a simple utility to control Pi 5 fan speeds at startup.

It is useful to force a particular fan speed or thermal profile to stabilize the onboard oscillator on the Pi.

## Installation

```
sudo cp ./fan-control.sh /usr/local/bin/fan-control
sudo cp ./fan-control.service /etc/systemd/system/fan-control.service
sudo chmod +x /usr/local/bin/fan-control
sudo systemctl daemon-reload
sudo systemctl enable --now fan-control.service
```
