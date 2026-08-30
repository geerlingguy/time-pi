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

## Configuration

To tweak the configured fan speed, edit the `ExecStart` line inside `fan-control.service`:

```
sudo nano /etc/systemd/system/fan-control.service
```

Then change the number at the end to an integer from 0-4:

  - `0`: Fan off
  - `1`: PWM `75` (30% fan speed)
  - `2`: PWM `125` (50% fan speed)
  - `3`: PWM `175` (70% fan speed)
  - `4`: PWM `250` (100% fan speed)

You can verify the current state with:

```
echo -e "\n
mode: $(cat /sys/class/thermal/thermal_zone0/mode) | \
state: $(cat /sys/class/thermal/cooling_device0/cur_state)/$(cat /sys/class/thermal/cooling_device0/max_state) | \
rpm: $(cat /sys/devices/platform/cooling_fan/hwmon/*/fan1_input 2>/dev/null || echo n/a) | \
temp: $(awk '{printf "%.1f°C", $1/1000}' /sys/class/thermal/thermal_zone0/temp)"
```

This should output data like:

```
mode: disabled | state: 3/4 | rpm: 6410 | temp: 39.7°C
```
