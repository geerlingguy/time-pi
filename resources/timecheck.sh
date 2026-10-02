#!/usr/bin/env bash
#
# timecheck.sh - Quick health check for a time-pi GPS/NTP/PTP grandmaster.
#
# Checks (in order):
#   1. GPS   - gpsd running, 3D fix, satellite count/signal, PPS present,
#              gpspipe-socat bridge for ts2phc
#   2. NIC   - PTP interface up, speed, hardware timestamping, driver errors
#   3. PTP   - ts2phc / ptp4l / phc2sys healthy, PHC disciplined, port MASTER,
#              clockClass/timeTraceable, PHC-vs-system offset
#   4. NTP   - chrony synced, sources reachable, selected refclock, stratum
#   5. SYS   - temperature, throttling, load
#
# Each line is marked [ OK ] / [WARN] / [FAIL]. Anything not OK prints
# indented "-> fix hints" under it. Exit code: 0 all ok, 1 warnings, 2 failures.
#
# Usage: sudo ./timecheck.sh [-v] [-n] [-i IFACE] [-p /dev/ptpN]
#   -v   verbose: also dump raw command output for each section
#   -n   no color
#   -i   PTP interface (default: from /etc/systemd/system/ptp4l.service, else eth1)
#   -p   PHC device (default: from ts2phc.service, else /dev/ptp0)
#
set -u

# ---------- tunables ----------
GPS_MIN_SATS_USED=4          # fewer used sats than this = FAIL
GPS_WARN_SATS_USED=6         # fewer than this = WARN
GPS_MIN_SNR=25               # avg SNR (dB-Hz) of used sats below this = WARN
GPS_TIME_MAX_DIFF=1          # seconds; GPS time vs system time
TS2PHC_MAX_OFFSET_NS=1000    # |PHC offset vs PPS| above this = WARN
TS2PHC_STALE_SEC=10          # no ts2phc log line in this many seconds = FAIL
PHC_SYS_WARN_NS=50000        # |PHC - system - TAI| above this = WARN (50us)
PHC_SYS_FAIL_NS=1000000      # ...above this = FAIL (1ms)
CHRONY_MAX_OFFSET_S=0.001    # system offset vs chrony ref above this = WARN
CHRONY_MAX_ROOTDISP_S=0.01   # root dispersion above this = WARN
JOURNAL_WINDOW="10 min ago"  # how far back to look for errors in logs
TEMP_WARN_C=70
TEMP_FAIL_C=80
LOAD_WARN=3.0
# ------------------------------

VERBOSE=0
COLOR=1
IFACE=""
PHC=""

while getopts "vni:p:h" opt; do
  case "$opt" in
    v) VERBOSE=1 ;;
    n) COLOR=0 ;;
    i) IFACE="$OPTARG" ;;
    p) PHC="$OPTARG" ;;
    h) sed -n '2,/^set -u/p' "$0" | sed 's/^# \{0,1\}//' | head -n -1; exit 0 ;;
    *) exit 1 ;;
  esac
done

[ -t 1 ] || COLOR=0
if [ "$COLOR" = 1 ]; then
  C_OK=$'\e[32m'; C_WARN=$'\e[33m'; C_FAIL=$'\e[31m'; C_HEAD=$'\e[1;36m'; C_DIM=$'\e[2m'; C_RST=$'\e[0m'
else
  C_OK=""; C_WARN=""; C_FAIL=""; C_HEAD=""; C_DIM=""; C_RST=""
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "Re-running with sudo (pmc, phc_ctl and ppstest need root)..."
  exec sudo -E "$0" "$@"
fi

N_OK=0; N_WARN=0; N_FAIL=0
ok()   { N_OK=$((N_OK+1));     printf '%s[ OK ]%s %s\n' "$C_OK"   "$C_RST" "$1"; }
warn() { N_WARN=$((N_WARN+1)); printf '%s[WARN]%s %s\n' "$C_WARN" "$C_RST" "$1"; }
fail() { N_FAIL=$((N_FAIL+1)); printf '%s[FAIL]%s %s\n' "$C_FAIL" "$C_RST" "$1"; }
hint() { printf '       %s-> %s%s\n' "$C_DIM" "$1" "$C_RST"; }
head_() { printf '\n%s== %s ==%s\n' "$C_HEAD" "$1" "$C_RST"; }
raw()  { [ "$VERBOSE" = 1 ] || return 0; printf '%s' "$C_DIM"; printf '%s\n' "$1" | sed 's/^/         | /'; printf '%s' "$C_RST"; }
have() { command -v "$1" >/dev/null 2>&1; }

svc_active() { systemctl is-active --quiet "$1" 2>/dev/null; }
svc_check() {   # svc_check <unit> <what it does>
  local u="$1" desc="$2"
  if svc_active "$u"; then
    ok "$u is running ($desc)"
    return 0
  fi
  local st; st=$(systemctl is-active "$u" 2>/dev/null); st=${st:-unavailable}
  local nr; nr=$(systemctl show -p NRestarts --value "$u" 2>/dev/null)
  if [ "$st" = activating ] || [ "${nr:-0}" -gt 0 ]; then
    fail "$u is $st and has restarted ${nr:-?} times - crash/restart loop ($desc)"
  else
    fail "$u is $st ($desc)"
  fi
  local tail6; tail6=$(journalctl -u "$u" -n 6 --no-pager -o cat 2>/dev/null)
  printf '%s\n' "$tail6" | sed "s/^/         ${C_DIM}| /;s/\$/${C_RST}/"
  if printf '%s\n' "$tail6" | grep -q 'PTP_EXTTS_REQUEST'; then
    hint "Kernel/NIC driver rejected the PPS-input (extts) request on the PHC's SDP pin. Config didn't change -> kernel did?"
    hint "  uname -r ; grep -iE 'linux-image|rpi.*kernel|linuxptp' /var/log/apt/history.log | tail"
    hint "  cat /sys/class/ptp/ptp0/pins/*       (SDP0 should be free or 'extts 0')"
    hint "  sudo ts2phc -c $PHC -s nmea -m -l 7 --ts2phc.nmea_serialport /dev/gps-ts2phc --ts2phc.pulsewidth 1000000 --ts2phc.extts_polarity rising   (then try 'both')"
    hint "Meanwhile PTP clients are getting WRONG time: sudo systemctl stop ptp4l, or switch to the chrony-PPS fallback (see README)."
  else
    hint "sudo journalctl -u $u -n 50 ; sudo systemctl restart $u"
  fi
  return 1
}

# Count journal lines matching a regex in the recent window.
jgrep() { journalctl -b -u "$1" --since "$JOURNAL_WINDOW" --no-pager -o cat 2>/dev/null | grep -Eic "$2"; }

# Discover iface / PHC from the deployed systemd units if not given.
if [ -z "$IFACE" ]; then
  IFACE=$(grep -oE -- '-i [a-z0-9]+' /etc/systemd/system/ptp4l.service 2>/dev/null | awk '{print $2}' | head -1)
  IFACE=${IFACE:-eth1}
fi
if [ -z "$PHC" ]; then
  PHC=$(grep -oE -- '-c /dev/ptp[0-9]+' /etc/systemd/system/ts2phc.service 2>/dev/null | awk '{print $2}' | head -1)
  PHC=${PHC:-/dev/ptp0}
fi

printf '%stime-pi health check%s  host=%s  iface=%s  phc=%s  %s\n' \
  "$C_HEAD" "$C_RST" "$(hostname)" "$IFACE" "$PHC" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
printf 'uptime:%s\n' "$(uptime | sed 's/.*up/ /;s/,  *[0-9]* user.*//')"

############################################################################
head_ "GPS"
############################################################################
GPSD_DEV=$(grep -oE '^DEVICES="[^"]*"' /etc/default/gpsd 2>/dev/null | cut -d'"' -f2)
GPSD_DEV=${GPSD_DEV:-/dev/ttyAMA0}
for d in $GPSD_DEV; do
  if [ -e "$d" ]; then ok "GPS serial device $d present"
  else
    fail "GPS serial device $d missing"
    hint "Check /boot/firmware/config.txt has 'dtparam=uart0=on' and cmdline.txt has no 'console=serial0,...'"
    hint "ls -l /dev/ttyAMA* /dev/serial*"
  fi
done

GPS_OK=0
if svc_check gpsd "GPS daemon"; then
  if ! have gpspipe; then
    warn "gpspipe not found; cannot inspect fix (apt install gpsd-clients)"
  elif ! have python3; then
    warn "python3 not found; cannot parse gpsd JSON"
  else
    GPS_JSON=$(timeout 6 gpspipe -w -n 8 2>/dev/null)
    raw "$(printf '%s\n' "$GPS_JSON" | grep -E '"class":"(TPV|SKY)"' | tail -2)"
    # Summarize: mode usedSats totalSats avgSNR maxSNR gpstime
    GPS_SUM=$(printf '%s\n' "$GPS_JSON" | python3 -c '
import sys, json
mode=None; used=None; total=None; snr=[]; t=None; dev=None
for line in sys.stdin:
    try: m=json.loads(line)
    except Exception: continue
    c=m.get("class")
    if c=="TPV":
        mode=m.get("mode",mode); t=m.get("time",t); dev=m.get("device",dev)
    elif c=="SKY":
        sats=m.get("satellites")
        if sats is not None:
            total=len(sats); u=[s for s in sats if s.get("used")]
            used=len(u); snr=[s.get("ss",0) for s in u if s.get("ss") is not None]
        else:
            used=m.get("uSat",used); total=m.get("nSat",total)
avg=sum(snr)/len(snr) if snr else 0
mx=max(snr) if snr else 0
print(mode if mode is not None else -1, used if used is not None else -1,
      total if total is not None else -1, round(avg,1), mx, t or "-", dev or "-")
' 2>/dev/null)
    read -r G_MODE G_USED G_TOTAL G_AVG G_MAX G_TIME G_DEV <<<"${GPS_SUM:-"-1 -1 -1 0 0 - -"}"

    if [ -z "$GPS_JSON" ]; then
      fail "gpsd returned no data (is the receiver talking? baud rate?)"
      hint "gpsmon -n   (should show NMEA streaming). If garbage/empty, baud mismatch:"
      hint "  check GPSD_OPTIONS in /etc/default/gpsd (-s 115200) vs module baud; see README 'GPS / GNSS Notes'"
      hint "sudo systemctl restart gpsd"
    else
      case "$G_MODE" in
        3) ok "GPS has a 3D fix (device $G_DEV)";;
        2) warn "GPS has only a 2D fix"; hint "Antenna view of sky is marginal; wait or improve antenna placement";;
        1|0|-1) fail "GPS has NO fix (mode=$G_MODE)"
                hint "Check antenna connection / sky view; 'cgps -s' to watch. Cold start can take minutes."
                hint "If sats are visible but never 'used', the module may need time or a COLDBOOT (ubxtool -p COLDBOOT)";;
      esac
      if [ "$G_USED" -lt 0 ]; then
        warn "No SKY report received (no satellite info in sample)"
      elif [ "$G_USED" -lt "$GPS_MIN_SATS_USED" ]; then
        fail "Satellites used: $G_USED of $G_TOTAL seen (need >= $GPS_MIN_SATS_USED)"
        hint "Antenna / sky view problem. Check SMA connector, antenna power (active antenna needs bias)."
      elif [ "$G_USED" -lt "$GPS_WARN_SATS_USED" ]; then
        warn "Satellites used: $G_USED of $G_TOTAL seen (marginal, want >= $GPS_WARN_SATS_USED)"
      else
        ok "Satellites used: $G_USED of $G_TOTAL seen"
      fi
      if [ "$G_USED" -gt 0 ]; then
        if awk "BEGIN{exit !($G_AVG < $GPS_MIN_SNR)}"; then
          warn "Signal strength weak: avg SNR ${G_AVG} dB-Hz (max $G_MAX); want >= $GPS_MIN_SNR"
          hint "Move antenna outdoors / away from the Pi + NIC (RF noise). Check for a damaged cable."
        else
          ok "Signal strength: avg SNR ${G_AVG} dB-Hz (max $G_MAX) on used sats"
        fi
      fi
      if [ "$G_TIME" != "-" ]; then
        GPS_EPOCH=$(date -u -d "$G_TIME" +%s 2>/dev/null || echo "")
        NOW=$(date -u +%s)
        if [ -n "$GPS_EPOCH" ]; then
          DIFF=$((NOW - GPS_EPOCH)); [ $DIFF -lt 0 ] && DIFF=$((-DIFF))
          if [ "$DIFF" -le "$GPS_TIME_MAX_DIFF" ]; then ok "GPS time agrees with system clock (within ${DIFF}s)"
          else
            fail "GPS time differs from system clock by ${DIFF}s (gps=$G_TIME)"
            if [ "$DIFF" -ge 36 ] && [ "$DIFF" -le 38 ]; then
              hint "Exactly ~37s = the TAI-UTC leap-second offset. The PHC is holding UTC instead of TAI, so"
              hint "phc2sys (-O 37) sets the system clock 37s off and PTP clients get it wrong too."
              hint "Usual cause: ts2phc not running / can't load leap-seconds.list (see PTP section)."
            fi
            hint "System clock is not being disciplined. Check phc2sys / chrony sections below."
            hint "If chrony has 'makestep 0 0', a large initial step is never corrected; try: sudo chronyc makestep"
          fi
          GPS_OK=1
        fi
      else
        warn "No time in TPV report yet"
      fi
    fi
  fi
fi

# PPS (GPIO) - used by chrony's PPS refclock if configured.
if ls /dev/pps* >/dev/null 2>&1; then
  PPSDEV=$(ls /dev/pps* | head -1)
  if have ppstest; then
    PPS_OUT=$(timeout 3 ppstest "$PPSDEV" 2>&1)
    raw "$(printf '%s\n' "$PPS_OUT" | tail -3)"
    if printf '%s\n' "$PPS_OUT" | grep -q 'assert'; then ok "PPS pulses arriving on $PPSDEV"
    else
      fail "No PPS pulses on $PPSDEV in 3s"
      hint "GPS has no fix (no PPS until fix), or pps-gpio overlay/GPIO pin wrong in /boot/firmware/config.txt"
    fi
  else
    ok "$PPSDEV present (install pps-tools to test pulses: apt install pps-tools)"
  fi
else
  if grep -q 'refclock PPS' /etc/chrony/conf.d/refclock.conf 2>/dev/null; then
    fail "chrony configured for PPS but no /dev/pps* device"
    hint "Add 'dtoverlay=pps-gpio,gpiopin=18' (or your pin) to /boot/firmware/config.txt and reboot"
  else
    ok "No /dev/pps* device (not configured in chrony; ts2phc uses NIC SDP PPS instead)"
  fi
fi

# gpspipe-socat bridge feeds NMEA to ts2phc.
if svc_check gpspipe-socat "NMEA bridge for ts2phc"; then
  if [ -e /dev/gps-ts2phc ]; then
    if timeout 2 head -c 1 /dev/gps-ts2phc 2>/dev/null | grep -q .; then
      ok "/dev/gps-ts2phc PTY is streaming NMEA"
    else
      fail "/dev/gps-ts2phc exists but no data in 2s (ts2phc will have no time-of-day)"
      hint "sudo systemctl restart gpspipe-socat  (and check gpsd is actually outputting NMEA: gpspipe -r -n 5)"
    fi
  else
    fail "/dev/gps-ts2phc PTY link missing"
    hint "sudo systemctl restart gpspipe-socat ; journalctl -u gpspipe-socat -n 20"
  fi
fi

############################################################################
head_ "NIC ($IFACE)"
############################################################################
NIC_OK=0
if [ ! -d "/sys/class/net/$IFACE" ]; then
  fail "Interface $IFACE does not exist"
  hint "lspci | grep -i ether ; dmesg | grep -iE 'igc|pcie'"
  hint "Pi 5 PCIe needs 'dtoverlay=pciex1-compat-pi5,mmio-hi' in /boot/firmware/config.txt. Reseat the HAT/FFC; power-cycle (not just reboot)."
else
  OPER=$(cat "/sys/class/net/$IFACE/operstate" 2>/dev/null)
  CARRIER=$(cat "/sys/class/net/$IFACE/carrier" 2>/dev/null || echo 0)
  SPEED=$(cat "/sys/class/net/$IFACE/speed" 2>/dev/null || echo "?")
  DUPLEX=$(cat "/sys/class/net/$IFACE/duplex" 2>/dev/null || echo "?")
  DRV=$(have ethtool && ethtool -i "$IFACE" 2>/dev/null | awk '/^driver:/{print $2}')
  if [ "$CARRIER" = 1 ] && [ "$OPER" = up ]; then
    ok "$IFACE link up: ${SPEED} Mb/s ${DUPLEX}-duplex (driver ${DRV:-?})"
    NIC_OK=1
    if [ "$SPEED" != 1000 ] && [ "$SPEED" != "?" ]; then
      warn "$IFACE is at ${SPEED} Mb/s; i226 on the Pi is only reliable at 1000"
      hint "sudo ethtool -s $IFACE autoneg on speed 1000 duplex full  (see /etc/network/if-up.d/$IFACE)"
      hint "Plug into a 1 GbE switch port, not 2.5 GbE"
    fi
  else
    fail "$IFACE link is $OPER (carrier=$CARRIER)"
    hint "Check cable/switch port. ip link show $IFACE ; sudo ethtool $IFACE"
    hint "If the i226 dropped off the bus: dmesg | grep -i igc ; a full power cycle usually brings it back"
  fi
  IPADDR=$(ip -4 -o addr show dev "$IFACE" 2>/dev/null | awk '{print $4}' | head -1)
  if [ -n "$IPADDR" ]; then ok "$IFACE has IP $IPADDR"
  else
    fail "$IFACE has no IPv4 address (PTP/NTP clients can't reach it)"
    hint "DHCP doesn't work reliably on the i226; set a static IP with: sudo nmtui"
  fi

  if have ethtool; then
    TS_OUT=$(ethtool -T "$IFACE" 2>/dev/null)
    raw "$TS_OUT"
    PHC_IDX=$(printf '%s\n' "$TS_OUT" | awk '/PTP Hardware Clock:/{print $4}')
    if [ -n "$PHC_IDX" ] && [ "$PHC_IDX" != "none" ]; then
      if printf '%s\n' "$TS_OUT" | grep -q 'hardware-transmit' && printf '%s\n' "$TS_OUT" | grep -q 'hardware-raw-clock'; then
        ok "Hardware timestamping supported; PHC index $PHC_IDX (/dev/ptp$PHC_IDX)"
      else
        fail "$IFACE reports PHC $PHC_IDX but no hardware tx/raw-clock timestamping"
      fi
      if [ "/dev/ptp$PHC_IDX" != "$PHC" ]; then
        fail "$IFACE's PHC is /dev/ptp$PHC_IDX but ts2phc/phc2sys are configured for $PHC"
        hint "Device numbering shifted (another PTP device enumerated first). Fix ts2phc_clock/phc2sys_source in config.yml or use a udev rule."
      fi
    else
      fail "$IFACE has no PTP hardware clock"
      hint "Wrong interface? Pi 5 onboard eth0 has no PHC. Use -i to pick the i226 interface."
    fi
  fi
  if [ ! -e "$PHC" ]; then
    fail "$PHC does not exist"
    hint "ls -l /dev/ptp* ; the NIC may not have enumerated (see dmesg | grep igc)"
  fi

  # TimeHAT needs the patched igc (DKMS) driver; a kernel update silently restores the stock one.
  if [ "${DRV:-}" = igc ]; then
    MODPATH=$(modinfo -n igc 2>/dev/null)
    DKMS_IGC=$(dkms status 2>/dev/null | grep -i '^igc' | head -3)
    raw "modinfo: $MODPATH"; raw "$DKMS_IGC"
    if [ -n "$DKMS_IGC" ] || [ -d /usr/src/igc-* ] 2>/dev/null; then
      if printf '%s' "$MODPATH" | grep -q '/updates/dkms/' || [ -f "/lib/modules/$(uname -r)/updates/dkms/igc.ko.xz" ] && cmp -s "/lib/modules/$(uname -r)/updates/dkms/igc.ko.xz" "$MODPATH"; then
        ok "Patched igc (TimeHAT PPS fix) is the active driver"
      else
        fail "Stock igc driver is loaded; TimeHAT PPS-fix DKMS module not installed for kernel $(uname -r)"
        hint "Kernel update replaced igc.ko. Rebuild/reinstall per TimeHAT README (dkms build/install, cp to kernel/drivers/.../igc/, depmod, update-initramfs, reboot)"
        hint "Symptom: ts2phc dies with 'PTP_EXTTS_REQUEST2 failed: Operation not supported'"
      fi
    else
      warn "igc driver is stock and no DKMS igc found; on a TimeHAT, ts2phc PPS-in needs the patched driver"
      hint "https://github.com/Time-Appliances-Project/TimeHAT  (intel-igc-ppsfix zip for your kernel)"
    fi
  fi

  # Error counters + driver complaints.
  RXERR=$(cat "/sys/class/net/$IFACE/statistics/rx_errors" 2>/dev/null || echo 0)
  TXERR=$(cat "/sys/class/net/$IFACE/statistics/tx_errors" 2>/dev/null || echo 0)
  RXDROP=$(cat "/sys/class/net/$IFACE/statistics/rx_dropped" 2>/dev/null || echo 0)
  if [ "$RXERR" -gt 0 ] || [ "$TXERR" -gt 0 ]; then
    warn "$IFACE error counters: rx_errors=$RXERR tx_errors=$TXERR rx_dropped=$RXDROP (since boot)"
    hint "sudo ethtool -S $IFACE | grep -iE 'err|drop|miss' ; suspect cable / speed negotiation"
  else
    ok "$IFACE error counters clean (rx_dropped=$RXDROP)"
  fi
  KMSG=$(journalctl -b -k --since "$JOURNAL_WINDOW" --no-pager -o cat 2>/dev/null | grep -iE "${DRV:-igc}|$IFACE" | grep -iE 'hang|reset|timeout|timed out|lost|fail|error|down' | tail -5)
  if [ -n "$KMSG" ]; then
    warn "Kernel reported ${DRV:-igc}/$IFACE trouble in the last ${JOURNAL_WINDOW#*}:"
    printf '%s\n' "$KMSG" | sed "s/^/         ${C_DIM}| /;s/\$/${C_RST}/"
    hint "Tx unit hangs / resets on the i226 usually mean a flaky PCIe link or bad speed negotiation; power-cycle, force 1 Gbps"
  else
    ok "No ${DRV:-igc} driver errors in kernel log (last ${JOURNAL_WINDOW%% ago})"
  fi
fi

############################################################################
head_ "PTP"
############################################################################
# --- leap-seconds.list: ts2phc needs it (unexpired) to put the PHC on TAI ---
LEAPFILE=$(grep -oE -- '--leapfile [^ ]+' /etc/systemd/system/ts2phc.service 2>/dev/null | awk '{print $2}')
LEAPFILE=${LEAPFILE:-/usr/share/zoneinfo/leap-seconds.list}
if [ ! -r "$LEAPFILE" ]; then
  fail "Leap seconds file $LEAPFILE missing (ts2phc will refuse to start)"
  hint "sudo apt install --reinstall tzdata   (or point --leapfile at a fresh copy from https://data.iana.org/time-zones/data/leap-seconds.list)"
else
  LEAP_EXP_NTP=$(grep -E '^#@' "$LEAPFILE" | awk '{print $2}' | head -1)
  LEAP_TAI=$(grep -vE '^#' "$LEAPFILE" | awk 'NF>=2{v=$2} END{print v}')
  if [ -n "$LEAP_EXP_NTP" ]; then
    LEAP_EXP=$(( LEAP_EXP_NTP - 2208988800 ))
    LEAP_EXP_DATE=$(date -u -d "@$LEAP_EXP" '+%Y-%m-%d' 2>/dev/null)
    DAYS_LEFT=$(( (LEAP_EXP - $(date +%s)) / 86400 ))
    if [ "$DAYS_LEFT" -lt 0 ]; then
      fail "Leap seconds file $LEAPFILE EXPIRED on $LEAP_EXP_DATE (ts2phc exits with 'leap-seconds.list expired')"
      hint "sudo apt update && sudo apt install tzdata   then   sudo systemctl restart ts2phc phc2sys"
      hint "If apt's tzdata is also stale, fetch https://data.iana.org/time-zones/data/leap-seconds.list and point ts2phc_extra_opts/--leapfile at it"
    elif [ "$DAYS_LEFT" -lt 30 ]; then
      warn "Leap seconds file expires in ${DAYS_LEFT} days ($LEAP_EXP_DATE); update tzdata soon or ts2phc will stop starting"
    else
      ok "Leap seconds file valid until $LEAP_EXP_DATE (TAI-UTC=${LEAP_TAI:-?}s)"
    fi
  else
    warn "Could not read expiry (#@ line) from $LEAPFILE"
  fi
fi

# --- ts2phc: PPS (NIC SDP) + NMEA -> PHC ---
if svc_check ts2phc "GPS PPS/NMEA -> $PHC"; then
  TS_LAST=$(journalctl -u ts2phc -n 40 --no-pager -o cat 2>/dev/null)
  raw "$(printf '%s\n' "$TS_LAST" | tail -4)"
  LAST_TS=$(journalctl -u ts2phc -n 1 --no-pager -o short-unix 2>/dev/null | awk '{print int($1)}')
  NOW=$(date +%s)
  AGE=$(( NOW - ${LAST_TS:-0} ))
  OFF_LINE=$(printf '%s\n' "$TS_LAST" | grep -E "$PHC offset" | tail -1)
  if [ -z "$LAST_TS" ]; then
    warn "ts2phc has no journal output"
  elif [ "$AGE" -gt "$TS2PHC_STALE_SEC" ] && [ -z "$OFF_LINE" ]; then
    fail "ts2phc is running but has never reported a $PHC offset (no PPS edges reaching the NIC)"
    hint "Wrong SDP pin? TimeHAT PPS-in is SDP2: add '--ts2phc.pin_index 2' to ts2phc_extra_opts (TimeHAT's cfg also uses extts_polarity rising)"
    hint "Test: sudo testptp -d $PHC -L 2,1 && sudo testptp -d $PHC -e 3   (1 event/sec = PPS is on pin 2)"
    hint "cat /sys/class/ptp/ptp0/pins/*  shows which pin is set to extts (func 1)"
  elif [ "$AGE" -gt "$TS2PHC_STALE_SEC" ]; then
    fail "ts2phc stopped logging ${AGE}s ago - PHC is free-running (NIC stopped delivering PPS events, or ts2phc hung)"
    hint "Is it blocked waiting for events?  sudo timeout 5 strace -p \$(pidof ts2phc) -e trace=poll,ppoll,read 2>&1 | tail -3"
    hint "Are PPS events still arriving?    sudo systemctl stop ts2phc; sudo testptp -d $PHC -e 3; sudo systemctl start ts2phc"
    hint "Patched igc edge filter may be dropping every edge: cat /sys/module/igc/parameters/edge_check_invert (try 1), then restart ts2phc"
    hint "Quick recovery: sudo systemctl restart ts2phc ; journalctl -u ts2phc -f  (watch whether it stops again after ~20s)"
    hint "If logging level is <6 this is expected; set ts2phc_logging_level: 6 for a heartbeat."
  elif [ -z "$OFF_LINE" ]; then
    fail "ts2phc running but not reporting $PHC offsets"
    printf '%s\n' "$TS_LAST" | grep -iE 'nmea|error|fail|warn|no |invalid' | tail -3 | sed "s/^/         ${C_DIM}| /;s/\$/${C_RST}/"
    hint "No PPS extts on the NIC, or no valid NMEA time. Check GPS fix above and /dev/gps-ts2phc."
  else
    OFF=$(printf '%s\n' "$OFF_LINE" | awk '{for(i=1;i<=NF;i++) if($i=="offset") print $(i+1)}')
    STATE=$(printf '%s\n' "$OFF_LINE" | grep -oE ' s[0-9] ' | tr -d ' ')
    AOFF=${OFF#-}
    if [ "$STATE" != "s2" ]; then
      warn "ts2phc servo state $STATE (not locked yet); last offset ${OFF}ns"
      hint "s0=unlocked, s1=stepping, s2=locked. Give it ~30s after restart; if stuck, check PPS."
    elif [ "${AOFF:-0}" -gt "$TS2PHC_MAX_OFFSET_NS" ]; then
      warn "ts2phc locked but PHC offset is ${OFF}ns (want < ${TS2PHC_MAX_OFFSET_NS}ns)"
    else
      ok "ts2phc locked (s2), $PHC offset ${OFF}ns vs GPS PPS"
    fi
  fi
  N=$(jgrep ts2phc 'nmea.*(invalid|not valid|timeout|no fix)|extts.*(error|fail)|failed')
  [ "${N:-0}" -gt 0 ] && { warn "ts2phc logged $N NMEA/extts complaints in the last ${JOURNAL_WINDOW%% ago}"; hint "journalctl -u ts2phc --since '$JOURNAL_WINDOW' | grep -iv offset | tail"; }
fi

# --- ptp4l: grandmaster on the NIC ---
PTP_MASTER=0
if svc_check ptp4l "PTP grandmaster on $IFACE"; then
  if ! have pmc; then
    warn "pmc not found; cannot query ptp4l state (apt install linuxptp)"
  else
    PMC_ALL=$(timeout 5 pmc -u -b 0 'GET PORT_DATA_SET' 'GET GRANDMASTER_SETTINGS_NP' 'GET DEFAULT_DATA_SET' 2>/dev/null)
    pmc_section() { printf '%s\n' "$PMC_ALL" | awk -v n="$1" '/RESPONSE MANAGEMENT/{p=(index($0,"MANAGEMENT " n " ")>0 || index($0,"MANAGEMENT " n "\t")>0 || $0 ~ ("MANAGEMENT " n "$"))} p'; }
    PDS=$(pmc_section PORT_DATA_SET)
    GMS=$(pmc_section GRANDMASTER_SETTINGS_NP)
    DDS=$(pmc_section DEFAULT_DATA_SET)
    raw "$PMC_ALL"
    PSTATE=$(printf '%s\n' "$PDS" | awk '/portState/{print $2}' | head -1)
    CLASS=$(printf '%s\n' "$GMS" | awk '/clockClass/{print $2}')
    TRACE=$(printf '%s\n' "$GMS" | awk '/timeTraceable/{print $2}')
    UTCOFF=$(printf '%s\n' "$GMS" | awk '/currentUtcOffset /{print $2}')
    UTCVALID=$(printf '%s\n' "$GMS" | awk '/currentUtcOffsetValid/{print $2}')
    PTPTS=$(printf '%s\n' "$GMS" | awk '/ptpTimescale/{print $2}')
    CLKID=$(printf '%s\n' "$DDS" | awk '/clockIdentity/{print $2}')
    if [ -z "$PSTATE" ]; then
      fail "pmc got no response from ptp4l (UDS socket /var/run/ptp4l not answering)"
      hint "sudo systemctl restart ptp4l ; journalctl -u ptp4l -n 30"
    else
      case "$PSTATE" in
        MASTER|GRAND_MASTER)
          ok "ptp4l port state: $PSTATE (clock $CLKID)"; PTP_MASTER=1 ;;
        FAULTY)
          fail "ptp4l port state: FAULTY"
          hint "Almost always 'timed out while polling for tx timestamp' on the i226. Try in order:"
          hint "  sudo systemctl restart ptp4l   ->   sudo ip link set $IFACE down && sudo ip link set $IFACE up   ->   power cycle"
          hint "  Check tx_timestamp_timeout in /etc/ptp4l.conf (repo default 100)" ;;
        LISTENING|PRE_MASTER|UNCALIBRATED|INITIALIZING)
          warn "ptp4l port state: $PSTATE (transitional; should become MASTER within ~10s)"
          hint "If it stays here: another grandmaster with better clockClass may be on the LAN (pmc -u -b 0 'GET PARENT_DATA_SET')" ;;
        SLAVE|PASSIVE)
          fail "ptp4l port state: $PSTATE - this Pi is NOT the master"
          hint "Another PTP master on the network is winning BMCA. 'masterOnly 1' should prevent SLAVE; check /etc/ptp4l.conf"
          hint "pmc -u -b 0 'GET PARENT_DATA_SET'  shows who it thinks the GM is" ;;
        *) warn "ptp4l port state: $PSTATE" ;;
      esac
      if [ -n "$CLASS" ]; then
        if [ "$CLASS" = 6 ]; then ok "clockClass 6 (GNSS-locked), timeTraceable=$TRACE, UTC offset ${UTCOFF}s valid=$UTCVALID ptpTimescale=$PTPTS"
        else
          warn "clockClass is $CLASS (expected 6 for a locked GPS grandmaster)"
          hint "The ExecStartPost pmc SET in ptp4l.service may have run before ptp4l was ready. Re-apply:"
          hint "  sudo systemctl restart ptp4l   (or run the pmc SET GRANDMASTER_SETTINGS_NP line from the unit by hand)"
        fi
        [ "$TRACE" = 1 ] || warn "timeTraceable=$TRACE (clients will treat time as untraceable)"
        [ "$UTCVALID" = 1 ] || warn "currentUtcOffsetValid=$UTCVALID (clients can't convert TAI->UTC)"
        [ "$UTCOFF" = 37 ] || warn "currentUtcOffset=$UTCOFF (expected 37 as of 2026; check leap-seconds.list)"
      fi
    fi
  fi
  # ptp4l's [N.NNN] prefix is seconds since boot; a FAULTY blip before 60s is just the link coming up.
  NF=$(journalctl -b -u ptp4l --since "$JOURNAL_WINDOW" --no-pager -o cat 2>/dev/null | grep -E 'FAULTY|timed out while polling for tx timestamp' | awk -F'[][]' '{t=$2+0; if (t>60) n++} END{print n+0}')
  if [ "${NF:-0}" -gt 0 ]; then
    warn "ptp4l logged $NF fault/tx-timestamp lines in the last ${JOURNAL_WINDOW%% ago}"
    journalctl -b -u ptp4l --since "$JOURNAL_WINDOW" --no-pager -o cat 2>/dev/null | grep -iE 'FAULTY|timed out' | awk -F'[][]' '$2+0>60' | tail -3 | sed "s/^/         ${C_DIM}| /;s/\$/${C_RST}/"
    hint "Flapping MASTER->FAULTY = NIC tx timestamps getting lost. If restart doesn't clear it, power-cycle the Pi."
  else
    ok "No ptp4l faults in the last ${JOURNAL_WINDOW%% ago}"
  fi
fi

# --- phc2sys: PHC -> system clock ---
PHC2SYS_ON=0
if svc_check phc2sys "$PHC -> system clock"; then
  PHC2SYS_ON=1
  NE=$(jgrep phc2sys 'fail|error|clockcheck|waiting|invalid')
  if [ "${NE:-0}" -gt 0 ]; then
    warn "phc2sys logged $NE errors in the last ${JOURNAL_WINDOW%% ago}"
    journalctl -b -u phc2sys --since "$JOURNAL_WINDOW" --no-pager -o cat 2>/dev/null | grep -iE 'fail|error|clockcheck|waiting|invalid' | tail -3 | sed "s/^/         ${C_DIM}| /;s/\$/${C_RST}/"
    hint "'clockcheck' = something else is also steering CLOCK_REALTIME (chrony refclock?). Only one should."
  else
    ok "No phc2sys errors in the last ${JOURNAL_WINDOW%% ago}"
  fi
fi

# --- PHC vs system clock (should differ by exactly TAI-UTC = 37s) ---
if [ -e "$PHC" ] && have phc_ctl; then
  CMP=$(timeout 5 phc_ctl "$PHC" cmp 2>&1)
  raw "$CMP"
  OFFNS=$(printf '%s\n' "$CMP" | grep -oE -- '-?[0-9]+ns' | head -1 | tr -d 'ns')
  if [ -n "$OFFNS" ]; then
    TAI=${UTCOFF:-37}
    DELTA=$(( OFFNS + TAI * 1000000000 ))
    ADELTA=${DELTA#-}
    if [ "$ADELTA" -gt "$PHC_SYS_FAIL_NS" ]; then
      fail "PHC vs system clock off by $(( DELTA / 1000 ))us beyond the ${TAI}s TAI offset (raw ${OFFNS}ns)"
      hint "phc2sys isn't steering the system clock (or something else is). Check phc2sys above, and that chrony has no competing refclock."
      hint "sudo systemctl restart phc2sys ; if offset is huge, phc2sys --step_threshold=1 should step it within a few seconds"
    elif [ "$ADELTA" -gt "$PHC_SYS_WARN_NS" ]; then
      warn "PHC vs system clock off by $(( DELTA / 1000 ))us beyond the ${TAI}s TAI offset (want < $(( PHC_SYS_WARN_NS / 1000 ))us)"
    else
      ok "System clock = PHC - ${TAI}s within ${ADELTA}ns"
    fi
  else
    warn "phc_ctl cmp gave no offset: $(printf '%s' "$CMP" | tail -1)"
  fi
fi

############################################################################
head_ "NTP (chrony)"
############################################################################
if svc_check chrony "NTP server"; then
  if ! have chronyc; then
    warn "chronyc not found"
  else
    TRK=$(chronyc tracking 2>/dev/null)
    SRC=$(chronyc -n sources 2>/dev/null)
    raw "$TRK"; raw "$SRC"
    REFID=$(printf '%s\n' "$TRK" | awk -F': ' '/^Reference ID/{print $2}')
    STRATUM=$(printf '%s\n' "$TRK" | awk -F': ' '/^Stratum/{print $2}')
    LEAP=$(printf '%s\n' "$TRK" | awk -F': ' '/^Leap status/{print $2}')
    SYSOFF=$(printf '%s\n' "$TRK" | awk '/^System time/{print $4}')
    ROOTDISP=$(printf '%s\n' "$TRK" | awk '/^Root dispersion/{print $4}')
    if [ -z "$TRK" ]; then
      fail "chronyc tracking returned nothing"
      hint "sudo journalctl -u chrony -n 30"
    else
      case "$LEAP" in
        Normal) ok "chrony leap status Normal, stratum $STRATUM, ref $REFID";;
        "Not synchronised") fail "chrony is NOT synchronised (ref $REFID)"
          hint "No usable source. If this box is meant to be stratum 1 from 'local stratum 1' + phc2sys, check phc2sys."
          hint "chronyc sources -v ; chronyc sourcestats";;
        *) warn "chrony leap status: $LEAP";;
      esac
      [ "$STRATUM" != 1 ] && [ -n "$STRATUM" ] && warn "chrony stratum is $STRATUM (expected 1)"
      if [ -n "$SYSOFF" ] && awk "BEGIN{exit !($SYSOFF > $CHRONY_MAX_OFFSET_S)}"; then
        warn "System time offset vs chrony reference is ${SYSOFF}s"
      else
        ok "System offset vs chrony reference ${SYSOFF:-?}s, root dispersion ${ROOTDISP:-?}s"
      fi
      if [ -n "$ROOTDISP" ] && awk "BEGIN{exit !($ROOTDISP > $CHRONY_MAX_ROOTDISP_S)}"; then
        warn "Root dispersion ${ROOTDISP}s is high (clients will see poor accuracy)"
      fi
    fi

    # Refclocks: which are configured, and is each reachable / selected?
    SEL_REF=""
    while read -r ms name stratum poll reach lastrx offset _; do
      mode=${ms:0:1}; state=${ms:1:1}; offset=${offset%%\[*}
      [ "$mode" = '#' ] || continue
      case "$state" in
        '*') ok "refclock $name SELECTED (reach $reach, offset $offset)"; SEL_REF="$name";;
        '+') ok "refclock $name combined (reach $reach, offset $offset)";;
        '-'|'?'|'x'|'~')
          if [ "$reach" = 0 ]; then
            fail "refclock $name unreachable (reach 0, state '$state')"
            case "$name" in
              PPS*) hint "No PPS edges reaching chrony: check /dev/pps0 above; 'lock NMEA' needs the NMEA source healthy too";;
              NMEA*|GPS*) hint "gpsd SHM not feeding chrony: is gpsd running with the GPS fixed? gpsd must start before chrony reads SHM 0";;
              PHC*) hint "PHC refclock: check $PHC exists and ts2phc is locked";;
            esac
          else
            warn "refclock $name reachable but not selected (state '$state', reach $reach, offset $offset)"
            [ "$state" = 'x' ] && hint "'x' = falseticker: its time disagrees with the others; check offsets in 'chronyc sources -v'"
          fi;;
      esac
    done <<<"$SRC"
    if ! printf '%s\n' "$SRC" | grep -q '^#'; then
      ok "No refclocks configured in chrony (serving from system clock via 'local stratum 1')"
    fi
    # Internet pool reachability (noselect, just a sanity check that NTP is alive)
    POOLREACH=$(printf '%s\n' "$SRC" | awk '$1 ~ /\^/ {print $5}' | grep -vc '^0$')
    POOLTOTAL=$(printf '%s\n' "$SRC" | grep -c '^\^')
    [ "${POOLTOTAL:-0}" -gt 0 ] && { [ "${POOLREACH:-0}" -gt 0 ] && ok "$POOLREACH/$POOLTOTAL internet NTP peers reachable (noselect, reference only)" || warn "0/$POOLTOTAL internet NTP peers reachable (no internet? fine if intentional)"; }

    # Two things steering CLOCK_REALTIME is a classic 'wonky time' cause.
    SEL_ANY=$(printf '%s\n' "$SRC" | awk 'substr($1,2,1)=="*"{print $2}' | head -1)
    if [ "$PHC2SYS_ON" = 1 ] && [ -n "$SEL_ANY" ]; then
      fail "BOTH phc2sys and chrony (selected source $SEL_ANY) are steering the system clock - they fight (phc2sys 'clockcheck' errors)"
      if [ -n "$(ls -A /run/chrony-dhcp 2>/dev/null)" ]; then
        hint "That source came from DHCP: $(cat /run/chrony-dhcp/* 2>/dev/null | tr '\n' ' ')"
        hint "Comment out 'sourcedir /run/chrony-dhcp' in /etc/chrony/chrony.conf (and add it to the chrony task in time-pi), then: sudo systemctl restart chrony"
      elif printf '%s' "$SEL_ANY" | grep -q '^[A-Z]'; then
        hint "Refclock $SEL_ANY is in chrony_refclock. Either remove it and keep phc2sys, or stop phc2sys and use 'refclock PHC $PHC ... tai'."
      else
        hint "grep -rE '^(server|pool|peer)' /etc/chrony/  -> add 'noselect' to that line or remove it"
      fi
    fi
    # Is anyone being served?
    CLIENTS=$(chronyc -n clients 2>/dev/null | awk 'NR>2 && $2>0' | wc -l)
    ok "chrony has served $CLIENTS NTP client address(es) since start"
  fi
fi

############################################################################
head_ "System"
############################################################################
TEMP=""
if have vcgencmd; then
  TEMP=$(vcgencmd measure_temp 2>/dev/null | grep -oE '[0-9]+(\.[0-9]+)?')
  THR=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
elif [ -r /sys/class/thermal/thermal_zone0/temp ]; then
  TEMP=$(awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp)
  THR=""
fi
if [ -n "$TEMP" ]; then
  if awk "BEGIN{exit !($TEMP >= $TEMP_FAIL_C)}"; then fail "SoC temperature ${TEMP}C"; hint "Check fan (fan-control service) and airflow; the i226 + GPS under a HAT run hot"
  elif awk "BEGIN{exit !($TEMP >= $TEMP_WARN_C)}"; then warn "SoC temperature ${TEMP}C"
  else ok "SoC temperature ${TEMP}C"; fi
fi
if [ -n "${THR:-}" ]; then
  if [ "$THR" = "0x0" ]; then ok "No under-voltage/throttling (get_throttled=0x0)"
  else
    warn "get_throttled=$THR (bit0 under-voltage, bit1 freq capped, bit2 throttled; bits 16-18 = occurred since boot)"
    hint "Under-voltage causes PCIe/NIC flakiness. Use the official 27W PSU; check HAT power draw."
  fi
fi
LOAD1=$(awk '{print $1}' /proc/loadavg)
if awk "BEGIN{exit !($LOAD1 > $LOAD_WARN)}"; then warn "1-min load average $LOAD1"; else ok "Load average $LOAD1"; fi
if [ -f /var/run/reboot-required ]; then warn "Reboot required (kernel/firmware update pending)"; fi

############################################################################
printf '\n%s== Summary ==%s  %s%d ok%s  %s%d warn%s  %s%d fail%s\n' \
  "$C_HEAD" "$C_RST" "$C_OK" "$N_OK" "$C_RST" "$C_WARN" "$N_WARN" "$C_RST" "$C_FAIL" "$N_FAIL" "$C_RST"
if [ "$N_FAIL" -gt 0 ]; then
  echo "Status: PROBLEMS - see [FAIL] lines above. Nuclear option: sudo systemctl restart gpsd gpspipe-socat ts2phc ptp4l phc2sys chrony"
  exit 2
elif [ "$N_WARN" -gt 0 ]; then
  echo "Status: mostly OK, review [WARN] lines. Run with -v for raw output."
  exit 1
else
  echo "Status: all good."
  exit 0
fi
