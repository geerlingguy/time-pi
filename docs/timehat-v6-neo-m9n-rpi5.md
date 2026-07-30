# TimeHAT v6 + NEO-M9N on Raspberry Pi 5

This repo was originally aimed at Jeff Geerling's TimeHAT v4/ZED-F9T setup. The working local build is a TimeHAT v6 with the onboard M.2 u-blox NEO-M9N module on a Raspberry Pi 5 running Raspberry Pi OS/Debian 13 with kernel `6.18.34+rpt-rpi-2712`.

## Current Working Path

The working clock chain is:

1. NEO-M9N GNSS receiver exposes serial NMEA on `/dev/ttyAMA0`.
2. `gpsd` reads `/dev/ttyAMA0` at `38400` baud.
3. `gpspipe-socat` bridges `gpspipe -r` to `/dev/gps-ts2phc`.
4. `ts2phc` reads NMEA from `/dev/gps-ts2phc` and PPS timestamps from Intel i226 `SDP2`.
5. `ts2phc` disciplines the i226 PHC at `/dev/ptp0`.
6. Chrony disciplines the system UTC clock from `/dev/ptp0`.

There is no external SMA loopback in this setup. Time Appliances documents the TimeHAT v6 M.2 GNSS PPS as internally connected to i226 `SDP2`.

## Important UTC Detail

The PHC is on the PTP/TAI timescale, so it is currently about 37 seconds ahead of normal UTC. That is expected. Chrony must read it with `tai`, otherwise the system clock will be off by the GPS/TAI leap-second offset.

The Chrony PHC refclock should look like:

```text
refclock PHC /dev/ptp0 poll 0 dpoll -5 tai refid PHC precision 1e-9 trust
```

The serial GNSS source is useful as a sanity check but should not be selected as the main source:

```text
refclock SOCK /run/chrony.clk.ttyAMA0.sock refid GNSS precision 1e-3 poll 4 noselect offset +0.065
```

## GPSD

The NEO-M9N in this build answered at `38400` baud, not `115200`.

`/etc/default/gpsd`:

```text
DEVICES=""
GPSD_OPTIONS="/dev/ttyAMA0 -s 38400 -n"
USBAUTO="false"
```

Avoid changing the u-blox baud rate as part of PPS debugging. If baud-rate tuning is tested later, change only that one variable and verify with `gpsmon`, `cgps`, and `chronyc sources -v`.

## Patched i226 Driver

The stock Raspberry Pi kernel `igc` driver could configure `SDP2`, but `ts2phc` failed with:

```text
PTP_EXTTS_REQUEST2 failed: Operation not supported
```

The Time Appliances vendor `intel-igc-ppsfix_rpi5_6.12.62.zip` driver fixed the TimeHAT PPS edge behavior, but needed small compatibility changes for the local `6.18.34+rpt-rpi-2712` kernel:

- Replace old hrtimer setup with `hrtimer_setup`.
- Provide compatibility aliases for timer API changes.
- Provide `SKBTX_HW_TSTAMP_USE_CYCLES` when absent.
- Advertise `supported_extts_flags = PTP_STRICT_FLAGS | PTP_RISING_EDGE` so `PTP_EXTTS_REQUEST2` is accepted by newer PTP core code.

This is intentionally guarded to kernels matching `^6\.18\.`. DKMS rebuilds the module, but it does not make out-of-tree kernel API patches magically stable across future kernel series. If Raspberry Pi OS moves to a later kernel, re-test the patch and update the guard instead of assuming it is safe.

On a fresh Raspberry Pi OS image, the stock in-tree `igc` module may still autoload early even after DKMS installs the patched module. The playbook installs `timehat-igc.service`, which checks `/sys/module/igc/srcversion` at boot and reloads `igc` if the stock module is active. `ts2phc` is ordered after that service.

The repo patch is:

```text
patches/timehat-igc-ppsfix-rpi5-6.12.62-linux-6.18.patch
```

Expected installed module:

```text
/lib/modules/6.18.34+rpt-rpi-2712/updates/dkms/igc.ko.xz
```

Expected `dkms status`:

```text
igc/6.12.0-ppsfix.1, 6.18.34+rpt-rpi-2712, aarch64: installed (Original modules exist)
```

## Service State

Expected active services:

```text
gpsd
gpspipe-socat
ts2phc
chrony
```

Expected disabled/inactive services for this phase:

```text
phc2sys
ptp4l
```

Chrony owns disciplining the system clock from the PHC. `phc2sys` should stay off or it will fight Chrony.

Chrony should also be allowed to step large offsets at any time:

```text
makestep 1.0 -1
```

This matters at boot because `ts2phc` can step the PHC into place after Chrony has already started sampling it. Slow-slewing a 37 second startup error is not acceptable for this appliance.

Debian's `gpsd.service` ships with `After=chronyd.service` for Chrony SOCK refclock support. For this build, that ordering is counterproductive because the PHC pipeline should initialize before Chrony samples `/dev/ptp0`. The playbook shadows the packaged unit with `/etc/systemd/system/gpsd.service`, removing only that Chrony ordering. It then starts `chrony` after `ts2phc` and uses `/usr/local/sbin/wait-timehat-phc` to wait briefly for the PHC to look like TAI before Chrony starts.

Do not add a Chrony systemd ordering dependency after `gpsd`, `gpspipe-socat`, or `ts2phc`. Debian's `gpsd.service` already has `After=chronyd.service` for Chrony SOCK refclock support, so forcing Chrony after the GPS pipeline creates an ordering cycle. Let Chrony start first and rely on `makestep 1.0 -1` to correct startup jumps.

## Verification

Check PPS events from i226 `SDP2`:

```bash
sudo testptp -d /dev/ptp0 -L 2,1
sudo testptp -d /dev/ptp0 -e 5
```

The patched driver should report one external timestamp event per second, not rising/falling pairs about 100 ms apart.

Check `ts2phc`:

```bash
journalctl -u ts2phc -n 50 --no-pager
```

Good output has `/dev/ptp0 offset` values around single or tens of nanoseconds once settled.

To run `ts2phc` by hand, give it both the PHC sink and the NMEA/PPS source details. `ts2phc -s nmea` alone is incomplete and will fail with `no PPS sinks specified`.

```bash
sudo systemctl stop ts2phc
sudo ts2phc -c /dev/ptp0 -s nmea -m -l 7 \
  --leapfile /usr/share/zoneinfo/leap-seconds.list \
  --ts2phc.nmea_serialport /dev/gps-ts2phc \
  --ts2phc.pin_index 2
sudo systemctl start ts2phc
```

Check Chrony:

```bash
chronyc sources -v
chronyc tracking
```

Expected steady-state shape:

```text
#* PHC ... +/- tens of ns
Reference ID    : 50484300 (PHC)
Stratum         : 1
Leap status     : Normal
```

Check the TAI/UTC relationship:

```bash
sudo phc_ctl /dev/ptp0 cmp
```

Expected offset from `CLOCK_REALTIME` is close to `-37000000000ns`. This means the system clock is UTC and the PHC is TAI/PTP. That is correct when Chrony uses `tai`.

## Useful References

- TimeHAT vendor repo: https://github.com/Time-Appliances-Project/TimeHAT
- SatPulse Intel/TimeHAT build notes: https://satpulse.net/hardware/intel-build.html
- TimeHAT v6 + NEO-M9N product page: https://timeappliances.myshopify.com/products/timehatv6-neo-m9n
- Jeff Geerling Time Pi issue for NEO-M9N debugging: https://github.com/geerlingguy/time-pi/issues/11

## Fresh SD Card Validation

A useful fork validation test is:

1. Flash a fresh Raspberry Pi OS/Debian image.
2. Boot with the TimeHAT v6, NEO-M9N, antenna, and Ethernet attached.
3. Install only the minimum bootstrap dependencies needed to clone the fork and run Ansible.
4. Run the playbook from a default TimeHAT v6 config.
5. Reboot and verify:

```bash
systemctl is-active gpsd gpspipe-socat ts2phc chrony phc2sys ptp4l
chronyc tracking
chronyc sources -v
sudo phc_ctl /dev/ptp0 cmp
journalctl -b --no-pager | grep -Ei 'gpsd|gpspipe|ts2phc|chrony|System clock wrong|System clock was stepped'
```

Expected result: `gpsd`, `gpspipe-socat`, `ts2phc`, and `chrony` are active; `phc2sys` and `ptp4l` are inactive for this phase; Chrony selects `PHC`; and there are no repeated 37 second UTC/TAI corrections after startup.
