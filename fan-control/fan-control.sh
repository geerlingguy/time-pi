#!/bin/bash
# fan-control <0-4|auto> — manual Pi 5 fan control
THERMAL=/sys/class/thermal/thermal_zone0/mode
FAN=/sys/class/thermal/cooling_device0/cur_state

case "$1" in
  auto)
    echo enabled > "$THERMAL"
    echo "Fan control returned to firmware/kernel."
    ;;
  [0-4])
    echo disabled > "$THERMAL"
    echo "$1" > "$FAN"
    echo "Fan pinned at state $1 ($(awk -v s=$1 'BEGIN{split("0 75 125 175 250",d," ");printf "%d%%", d[s+1]/255*100}') duty)"
    ;;
  *)
    echo "Usage: fan-control <0-4|auto>   (0=off 1=30% 2=49% 3=69% 4=98%)"
    exit 1
    ;;
esac
