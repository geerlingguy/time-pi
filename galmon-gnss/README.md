# Galmon GNSS Station

A dedicated Raspberry Pi feeding raw GNSS data to the [Galmon](https://galmon.eu) monitoring network, using [ubxtool](https://github.com/berthubert/galmon) and a u-blox receiver.

## Hardware

- Raspberry Pi 4 or 5 (ubxtool is light; wired Ethernet preferred)
- [Jimmy Paputto L1/L5 GNSS RTK HAT](https://github.com/jimmypaputto/gnsshat) (u-blox NEO-F9P), with a **USB cable from the HAT's USB port to the Pi**. ubxtool talks to the receiver over USB, not SPI/UART.
- Multi-band GNSS antenna. If it's fed through a splitter:
  - The NEO-F9P is L1/L5, so the splitter must pass L5/E5a (~1176 MHz) as well as L1 (~1575 MHz).
  - Only one splitter output usually passes antenna DC bias. DC-blocked outputs should present a load so the receiver doesn't report an open antenna.

Upstream ubxtool only supports the ZED-F9P's L1/L2 signals and exits on a NEO-F9P with `Got nack on F9P GNSS setting`, so `example.config.yml` builds from [a fork](https://github.com/geerlingguy/galmon/tree/neo-f9p) with NEO-F9P support. For other receivers, set `galmon_repo` and `galmon_version` back to upstream.

Any F9 receiver tracks all four constellations at once. Older u-blox 8 receivers can't; see [Operator.md](https://github.com/berthubert/galmon/blob/master/Operator.md).

## Install

1. [Request a station ID](https://github.com/berthubert/galmon/blob/master/Operator.md) from the Galmon project. You'll get an ID and a destination hostname.
2. Flash Raspberry Pi OS Lite (64-bit, Trixie) with Raspberry Pi Imager, setting the hostname, user, and SSH key.
3. Plug in the receiver and confirm it shows up (the `by-id` path survives renumbering):

   ```
   ls -l /dev/serial/by-id/
   ```

4. In this directory, copy `example.hosts.ini` to `hosts.ini`, and `example.config.yml` to `config.yml`. Set your station ID, destination, owner, and remark in `config.yml`, and `galmon_port` if your receiver's `by-id` path differs.
5. Run the playbook (the first run builds ubxtool, which takes a few minutes):

   ```
   ansible-playbook main.yml
   ```

The playbook builds `ubxtool` and `navdump` from source (the Galmon apt repo only covers Buster), runs ubxtool as a systemd service, and enables the Pi's hardware watchdog.

## Verify

On the Pi:

```
journalctl -u ubxtool -f
```

To decode what the receiver sends, stop the service (only one program can use the receiver at a time) and run:

```
sudo systemctl stop ubxtool
ubxtool --wait --port /dev/ttyACM0 --stdout --gps --galileo --glonass --beidou | navdump
sudo systemctl start ubxtool
```

Within a few minutes, your station should appear on [galmon.eu/observers.html](https://galmon.eu/observers.html). If it doesn't, check that outbound TCP port 29603 is open: `nc -vz <destination> 29603`.

## Monitoring

Run `galmontop` on the Pi for a live dashboard (`galmontop --once` prints it once):

- **Local:** ubxtool's service state, uptime and restarts; the upload connection to Galmon (bytes acknowledged, rate, RTT, retransmits); and the signals ubxtool last logged per constellation.
- **galmon.eu:** what Galmon's server sees for your station, from [observers.json](https://galmon.eu/observers.json), refreshed every minute: when it last heard from you, clock offset and accuracy, and each satellite's signal strength, elevation, azimuth, pseudorange residual, quality, and whether it's used in the fix.

The station ID comes from the running ubxtool; pass `--station <id>` to override it.

## Notes

- ubxtool reconfigures the receiver (non-persistently) every time it starts, and disables NMEA on USB, so don't share the receiver with gpsd.
- Galmon rounds your station's position server-side before publishing it. To also blur it before it's sent, add `--fuzz-position <meters>` to `galmon_extra_opts`.
- To upgrade, rerun the playbook. It pulls `galmon_version` from `galmon_repo`, then rebuilds and restarts ubxtool if anything changed.
