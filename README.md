# Time Pi

[![CI](https://github.com/geerlingguy/time-pi/actions/workflows/ci.yml/badge.svg)](https://github.com/geerlingguy/time-pi/actions/workflows/ci.yml)

<p align="center"><img alt="Raspberry Pi 5 with TimeHAT V2" src="/resources/time-pi.jpeg" height="auto" width="600"></p>

A Raspberry Pi stratum 1 time server.

Takes in GPS (or potentially other stratum 0 time sources), spits out NTP, PTP, etc.

## Setup - Grandmaster

### Hardware

To run a decent time server (with high accuracy), you need a few things:

  - A computer running Linux (all the nice tooling for network time is available here)
  - A high quality time source (GPS is most commonly used these days)
  - A network adapter capable of hardware timestamping (Intel and ASIX make some good NICs for time-related applications)

There are many options for each of the above items—for example, many use [Adafruit's Ultimate GPS HAT](https://www.adafruit.com/product/2324) or its [USB equivalent](https://www.adafruit.com/product/4279) for GPS acquisition, and for PTP, you can use a Compute Module 4 or 5's built-in NIC (with [PPS in or out](https://www.jeffgeerling.com/blog/2022/ptp-and-ieee-1588-hardware-timestamping-on-raspberry-pi-cm4)), or add on a compatible NIC on the Pi 5 with something like the [uPCIty Lite](https://amzn.to/4iUn9ke).

My own hardware configuration—which is the basis for the code in this repository, consists of:

  - Computer: Raspberry Pi 5 model B
  - Time source: u-blox ZED-F9T-00B-01 (installed on [TimeHAT V4](https://github.com/geerlingguy/raspberry-pi-pcie-devices/issues/674))
  - NIC: Intel i226-LM (installed on [TimeHAT V4](https://github.com/geerlingguy/raspberry-pi-pcie-devices/issues/674))

A precise GPS signal for nanosecond-accurate time requires a decent antenna with as clear a view of the sky as possible. Some GPS receivers are better than others, but even USB receivers will do better than NTP!

I use a small [active GPS antenna](https://amzn.to/4gdhBj1) for testing, but for permanent installation it is best to mount a [higher-quality GPS antenna](https://www.meinbergglobal.com/english/products/gps-glonass-l1-antenna.htm) outside, clear of obstructions.

### Software

Make sure you have Ansible installed. Copy the following example files and customize them according to your setup:

  - `example.hosts.ini` to `hosts.ini`
  - `example.config.yml` to `config.yml`

Now run the Ansible playbook:

```
ansible-playbook main.yml
```

This playbook will configure:

  - [GPSd](https://gpsd.gitlab.io/gpsd/gpsd.html): interface with the u-blox GPS and provide GPS data to other applications
  - [Chrony](https://chrony-project.org): NTP server, which also sync GPS time (with Internet NTP server backup) to the system clock
  - [Linux PTP](https://linuxptp.nwtime.org): synchronize the system clock to the NIC PHC (Physical Hardware Clock), and set up the Pi as a PTP grandmaster clock

> **Intel i226 Notes**: Currently I can't get the i226 to work with DHCP at all, so I have to manually set an IP address using `nmtui`. It also [doesn't work at 2.5 Gbps currently](https://github.com/geerlingguy/raspberry-pi-pcie-devices/issues/674#issuecomment-2533117275), and it can't be overridden via Linux, so I make sure to plug it into a 1 Gbps port on my network.

> **TimeHAT igc driver**: PPS input on the TimeHAT's i226 (used by `ts2phc`) needs a patched Intel `igc` driver, which is **not** in this repository. It comes from the [Time-Appliances-Project/TimeHAT](https://github.com/Time-Appliances-Project/TimeHAT) repo as a zip per kernel series (`igc_ppsfix_zip_url` in `config.yml`, e.g. `intel-igc-ppsfix_rpi5_6.12.62.zip`). The playbook (`tasks/igc-dkms.yml`) builds it with DKMS and swaps it in for the stock module, and re-applies it after a kernel update puts the stock driver back. Without it, `ts2phc` fails with `PTP_EXTTS_REQUEST2 failed: Operation not supported`. If you move to a new kernel series, check the TimeHAT repo for a matching zip and update `igc_ppsfix_zip_url` and `igc_dkms_version`.

## Setup - Client Pis

For client Pis, make sure you have them listed under a `[clients]` heading in your `hosts.ini` file, then run the Ansible playbook:

```
ansible-playbook ptp-clients.yml
```

This should configure the clients to acquire PTP time from the grandmaster Pi.

> **Note**: This playbook is still under active development. See [Issue #1](https://github.com/geerlingguy/time-pi/issues/1) for the latest.

## GPS / GNSS Notes

Using u-blox GPS modules, you may encounter a baud rate mismatch. Many newer u-blox modules default to `38400` baud, while older modules default to `9600` baud. This project recommends `115200` baud for slightly faster timing updates.

```
# Get the protocol version ('PROTVER')
ubxtool -p MON-VER
...
UBX-MON-VER:
  swVersion EXT CORE 4.04 (7f89f7)
  hwVersion 00190000
  extension ROM BASE 0x118B2060
  extension FWVER=SPG 4.04
  extension PROTVER=32.01
...

# Set the version in ubxtool options
export UBXOPTS="-P 32.01"

# Set the baud rate to 115200
ubxtool -S 115200

# Persist the setting
ubxtool -p SAVE -P 32.01
ubxtool -p COLDBOOT -P 32.01

# If running GPSd, update the rate gpsd's config and restart gpsd
sudo nano /etc/default/gpsd
sudo systemctl restart gpsd
```

`ubxtool` is installed as part of the `gpsd-clients` package, which is automatically installed by this playbook.

For more on how to set the baud rate (or tweak other GPS module parameters), see [millerjs.org's ubxtool page](https://wiki.millerjs.org/ubxtool) and the [ubxtool examples](https://gpsd.io/ubxtool-examples.html) page. You can also configure most options via `pygpsclient` using a GUI.

### Antenna cable delay

The GNSS signal takes time to travel down the antenna cable, so the receiver's PPS arrives late by that much. u-blox timing receivers compensate with `CFG-TP-ANT_CABLEDELAY` (in ns), and because the receiver applies it, the PHC, PTP and NTP all benefit. The ZED-F9T defaults to **50 ns**, not 0.

Delay is cable length × 3.336 ÷ velocity factor (ns/m):

| Cable | Velocity factor | ns/m | ns/ft |
| --- | --- | --- | --- |
| RG-174, RG-58 (solid dielectric) | 0.66 | 5.05 | 1.54 |
| LMR-195 | 0.80 | 4.17 | 1.27 |
| Belden H155 | 0.81 | 4.12 | 1.26 |
| LMR-240 | 0.84 | 3.97 | 1.21 |
| LMR-400 | 0.85 | 3.92 | 1.19 |

For example, 20 m of H155 is about 82 ns. Antenna, adapters and splitters add a few ns more (check their datasheets). For better than a few ns, measure the cable with a TDR; a NanoVNA can do it. See [#18](https://github.com/geerlingguy/time-pi/issues/18) for more discussion.

To set it with the playbook, put the value in `config.yml` and run the playbook:

```yaml
gnss_cable_delay_ns: 82
```

Then check the value the receiver is using (use the `PROTVER` from `ubxtool -p MON-VER`):

```
$ ubxtool -P 29.20 -g CFG-TP-ANT_CABLEDELAY,0
...
    item CFG-TP-ANT_CABLEDELAY/0x30050001 val 82
```

To set it by hand instead:

```
ubxtool -P 29.20 -z CFG-TP-ANT_CABLEDELAY,82    # saved to RAM and flash
```

Changing it shifts the PPS by the difference from the old value; ts2phc pulls the PHC over within a few seconds. For receivers without this setting, ts2phc's `ts2phc.extts_correction` can compensate instead (see `man ts2phc`).

### TIME mode (fixed antenna position)

By default the receiver solves for position and time every second. If the antenna never moves, a timing receiver like the ZED-F9T can use a known, fixed position and solve only for time, which is more accurate. u-blox calls this TIME mode. See [#18](https://github.com/geerlingguy/time-pi/issues/18) for the full walkthrough.

**1. Survey in** the antenna position (at least 24 hours, until it's accurate to 1 m):

```
ubxtool -P 29.20 -z CFG-TMODE-SVIN_MIN_DUR,86400
ubxtool -P 29.20 -z CFG-TMODE-SVIN_ACC_LIMIT,10000
ubxtool -P 29.20 -z CFG-TMODE-MODE,1
```

`-z` saves to RAM, BBR and flash. The survey only ends once *both* limits are met, and without RTK corrections a very tight `SVIN_ACC_LIMIT` (in 0.1 mm) may never be reached. A 1 m position error adds no more than about 3 ns of timing error.

**2. Wait** for `valid 1`. `meanV` is the variance in mm², so √`meanV` is the accuracy in mm:

```
$ ubxtool -P 29.20 -w 0.5 -p TIM-SVIN | grep -A3 TIM-SVIN
UBX-TIM-SVIN:
  dur 86401 meanX [redacted] meanY [redacted] meanZ [redacted] meanV 6551
  obs 86402 valid 1 active 0
```

The receiver then switches to TIME mode on its own (`fixType 5`):

```
$ ubxtool -P 29.20 -w 2 -p NAV-PVT | grep fixType
  tAcc 21 nano 399884 fixType 5 flags x1 flags2 xea
```

**3. Save the position as fixed.** In survey-in mode (`1`) it re-surveys after every power cycle. Copy `meanX`/`meanY`/`meanZ` (cm) into fixed mode (`2`), and set `FIXED_POS_ACC` (0.1 mm) to the survey accuracy:

```
ubxtool -P 29.20 -z CFG-TMODE-POS_TYPE,0
ubxtool -P 29.20 -z CFG-TMODE-ECEF_X,<meanX>
ubxtool -P 29.20 -z CFG-TMODE-ECEF_Y,<meanY>
ubxtool -P 29.20 -z CFG-TMODE-ECEF_Z,<meanZ>
ubxtool -P 29.20 -z CFG-TMODE-FIXED_POS_ACC,1000
ubxtool -P 29.20 -z CFG-TMODE-MODE,2
```

Confirm `CFG-TMODE-MODE` is `val 2` in ram, bbr and flash (`ubxtool -P 29.20 -w 0.5 -g CFG-TMODE-MODE`). `resources/timecheck.sh` also reports the current mode.

> **Warning**: A wrong fixed position gives wrong time, and nothing warns you. If the antenna moves, set `CFG-TMODE-MODE,1` and survey again. Also note that the surveyed coordinates are your antenna's exact location, so redact them before you share output.

See this issue for more: [Debug NEO-M9N module on TimeHAT V2](https://github.com/geerlingguy/time-pi/issues/11).

## Debugging

Quick one-shot health check (GPS fix/signal, NIC + patched igc driver, ts2phc/ptp4l/phc2sys, chrony, temps), with fix hints for anything that isn't OK:

```
sudo resources/timecheck.sh      # add -v for raw command output
```

Some handy commands:

```
# GPS-related debugging
sudo systemctl status gpsd  # check gpsd status
gpsmon -n  # monitors gpsd output
cgps -s  # also monitors gps output

# PTP timestamping debugging
ethtool -T eth0  # or eth1, lists hardware clock info

# Chrony debugging
chronyc sources -v  # shows sources with documentation of fields
chronyc tracking  # shows detailed timing data

# Check on NTP service from another computer
ntpdate -q [ip of grandmaster]

# Check on PTP clocks and offsets (assuming ptp4l is running)
wget https://tsn.readthedocs.io/_downloads/5d05630fceb3e7ac1582bc94c7468fc3/check_clocks.c
gcc -o check_clocks check_clocks.c
sudo ./check_clocks -d eth0  # or eth1 (the interface you're using for PTP)

# Check offset between NIC PHY and system clock
sudo phc_ctl eth1 cmp  # should be nearly -37000000000ns

# Use Wireshark's CLI to monitor PTP timestamps
tshark -i en0 -f "udp port 319 or 320" -V -Y "ptp" | grep "OriginTimestamp"

# Use ptp4l to listen for PTP messages
sudo ptp4l -i eth0 -m -s -A
```

Much of the work that went into this project was documented in [this thread on the TimeHat v2](https://github.com/geerlingguy/raspberry-pi-pcie-devices/issues/674).

### PTP Debugging

To see how PTP timing is functioning, specifically, you can run the following tools on another computer on the network:

  - [ptp-watch.py](resources/ptp_watch.py): Python script to monitor PTP timestamps on the network from any other Linux computer (run with `sudo python3 ptp_watch.py --iface eth0`)
  - [Wireshark](https://www.wireshark.org/): Run with filter "ptp", and it will show Announce/Sync/Follow_Up messages, allowing you to inspect the entire structured PTP message.
    - You can also monitor using the built-in CLI, `tshark` (see example command above)
  - [PTP Trackhound](https://www.ptptrackhound.com/#/home): A free tool (requires email subscription to Meinberg) that focuses exclusively on PTP traffic, giving more timing-related analytics.

### Debugging with an Oscilloscope for ns-level precision

Once you have PTP set up and multiple computers are in sync, it's useful to see how close the synchronized timing is with an oscilloscope (completely removed from the Linux OS layer).

Assuming all your computers/NICs have PPS outputs, you can connect different oscilloscope channels to those PPS outputs.

Then check if the rising edge of the timing pulse is close—on my LAN, I expect to see well under 100 ns of variance, with 20-30 ns more common. In some scenarios, you can get this down to single-digit ns.

## Slave / Client Setup

For PTP, you need to install and configure PTP for Linux on slave/client machines, and synchronize them to the master/server node as well.

An example configuration for a slave/client node is set up in `ptp-client-node.yml`, and further examples may be provided in the future.

## Other Pi Time projects in this repository

  - [Raspberry Pi Pico Mini Rack GPS Clock](./pico-clock-mini-rack/README.md)
  - [Time Status page](./time-status/README.md) (public NTP/GNSS status, enable with `time_status_enabled`)
  - [chronytop](https://github.com/geerlingguy/chronytop) (TUI dashboard, run `sudo chronytop`; disable with `chronytop_install: false`)
  - [NTP Proxy Server](./ntp-proxy-server/README.md) (public NTP from a VPS synced to the Time Pi over WireGuard)
  - [Chrony Dashboard](./chrony-dashboard/README.md)
  - [Fan Control](./fan-control/README.md)

## Other Hobbyist Time Servers and Pi Time Builds

  - [Austin's Nerdy Things: Nanosecond accurate PTP Pi server](https://austinsnerdythings.com/2025/02/18/nanosecond-accurate-ptp-server-grandmaster-and-client-tutorial-for-raspberry-pi/)
  - [Austin's Nerdy Things: Microsecond Accurate NTP for Raspberry Pi with GPS PPS](https://austinsnerdythings.com/2025/02/14/revisiting-microsecond-accurate-ntp-for-raspberry-pi-with-gps-pps-in-2025/)
  - [Andreas Spiess: NTP Server from GPS Satellites](https://www.youtube.com/watch?v=RKRN4p0gobk)
  - [Time-Card-Flex CM4 and CM5 with OCP-TAP Time Card](https://github.com/regymm/Time-Card-Flex/blob/0637ed49693192d11c7df9ac883c444ce62d2b25/DOC/Setup-and-Usage.md)
  - [Conor Robinson's Raspberry Pi NTP Server](https://conorrobinson.ie/raspberry-pi-ntp-server-part-6/)
  - [Jeff Geerling: Time Card mini for GPS and OXCO on Pi](https://www.jeffgeerling.com/blog/2023/time-card-mini-adds-pi-gps-and-oxco-your-pc)
  - [Raspberry Pi 5 Stratum 1 NTP Server with Uptronics GPS HAT](https://github.com/tiagofreire-pt/rpi_uputronics_stratum1_chrony/)
  - [BeagleBone Black PRU PPS (IEP hardware timestamping)](https://github.com/dniminenn/bbs-pps-pru)

## Other Time Resources I found Interesting

  - [LeapSecond.com](http://www.leapsecond.com) (great resources for timing nerds)
  - [Time-Nuts Mailing List](http://www.leapsecond.com/time-nuts.htm) (for amateurs who are interested in precise Time & Frequency)
  - [Where does my computer get the time from?](https://dotat.at/@/2023-05-26-whence-time.html) (good overview of the sources of modern NTP + GPS time, with the history of each source)
  - [Satpulse time server architecture](https://satpulse.net/2025/05/21/time-server-architecture.html) (System clock vs PHC vs NIC, and how time is transferred internally and through PTP and NTP)
  - [pixie Pi 5 GPS Time Server](https://github.com/josh-blake/pixie/wiki) (Guide for improving accuracy with realtime kernel, Ethernet NIC options, etc.)

## License

GPLv3 or Later

## Author

[Jeff Geerling](https://www.jeffgeerling.com), with assistance from Ahmad Byagowi and Oleg Obleukhov from Meta.
