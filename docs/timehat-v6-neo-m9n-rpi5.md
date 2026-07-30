# TimeHAT v6 + NEO-M9N on Raspberry Pi 5

This is a fork of Jeff Geerling's Time Pi repo, which was originally aimed at the TimeHAT v4/ZED-F9T setup. Over time, changes and updates in hardware in software have drifted, and it doesn't work out of the box with the v6+Neo combo that I have.

This fork is is focused on a TimeHAT v6 with the onboard M.2 ublox Neo-M9N module on a Raspberry Pi 5 running Raspberry Pi OS/Debian 13 with kernel `6.18.34+rpt-rpi-2712`, though I suspect it will work for most or all 6.18 kernels.

Information and processes should be correct as of 30 July 2026.

## Hardware and OS

This was tested (repeatedly) on:
 * Raspberry Pi 5 4GB
 * [TimeHAT v6 from Time Appliances](https://timeappliances.myshopify.com/products/raspberry-pi-5-pcie-hat-with-i226-nic-timehat)
 * [Neo-M9N GNSS M.2 OCP Module](https://timeappliances.myshopify.com/products/ocp-m2-neo-m9n-gnss)
 * Raspberry Pi OS (64-bit) - Debian Trixie based, released 2026-06-18, flashed with the Raspberry Pi Imager

 Note: Time Appliances has moved away from Tindie and to a [Shopify store](https://timeappliances.myshopify.com/).  Most google searches and links will still send you to the tendie store.

## Pre-Ansible setup

This fork of Jeff's playbook assumes that you:
 * Have plugged the TimeHATv6 onto your Raspberry Pi 5.
 * Have connected the flexible PCI-e cable from your Pi to the TimeHATv6
 * Have installed the ublox Neo-M9N in the M.2 slot and screwed it down
 * Have attached a GPS antenna to the tiny surface mount connector (U.FL or MHF4 or whatever it is) on the Neo-M9N
 * Have set the business end of the GPS antenna somewhere where it can plausibly see satellites
 * Have an SSH keypair that was installed onto the sd card as part of the flashing process, or otherwise you set it up yourself later
 * Are plugged in via the Raspberry Pi's original NIC (IMPORTANT)
 * The Intel NIC that is onboard of the TimeHATv6 is NOT plugged in (IMPORTANT)
 * Have a GPS signal, which you can tell by supplying power to the pi, and waiting for the green light on the Neo card to start blinking once per second (you don't have to actually turn the pi on, weirdly)
 * Are able to SSH to the Raspberry Pi via IP address or hostname, without having to put in a passphrase, and are capable of becoming root without having to supply a password
 * Have [installed Ansible](https://docs.ansible.com/projects/ansible/latest/installation_guide/intro_installation.html) on your main machine.

 NOTE: In my testing, the Pi will not boot if the Intel NIC on the TimeHATv6 is plugged in.  If you want to use it, read on below.

 ## How to use

Similar to Jeff's original version of this playbook, copy the exmaple.config.yml to config.yml and edit to change any options you need to, and copy the example.hosts.ini to hosts.ini and replace the default example with your Raspberry Pi's IP address or hostname.  Note that there are a number of options in config.yml that will make a TimeHATv6 + Neo-M9N functional but will likely break other installs, so if this is ever merged to Jeff's playbook, those will have to be gated behind checks.

Then, run `ansible-playbook -i hosts.ini main.yml`.  This will take a considerable amount of time; I added an optional step to do a full apt upgrade along the way, but even without that, it will take probably 5-10 minutes and reboot at least once if not twice or three times.  (The reboots make it impractical to run the playbook locally on the pi, at least without a lot of faffing about.).  Once the playbook completes successfully, you can plug the intel nic in, if you want to do PTP.  No other configuration is required, assuming your options are set appropriately in config.yml (discussed below).  If you don't mind having both nics plugged in, you're done.  If you want to ditch the Pi nic and only use the intel nic, you'll need to swap the cable after a successful playbook execution, and you may need to double check your config.yml `i226_<whatever>` settings, but that's it.  Note that the IP address of the Pi will likely change (different MAC) when you do this.

To use PTP, as long as the intel nic is plugged into the same network as your other PTP-capable devices, and your switches support PTP (as my Ubiquiti switches do), it should just ... work.  I mean, you don't even have to give the intel nic an ip address, which I didn't know prior to working on this project - as long as `ip a` reports that your intel nic, which in my case is `eth1`, shows `state UP`, then you can get PTP on another box.


## Options in the config.yml

Where appropriate, I have added comments to the example.config.yml that will hopefully make sense of the new options.  I have also added [chrony_refclock.md] in the same directory as this file that goes into detail about the chrony configuration that worked for me, so that you can make intelligent choices about what, if anything, you want to change.

### Highlights and notable new config options:

 * `apt_upgrade_enabled: false` and associated options - set to true if you want Ansible to run an apt upgrade before doing anything else.  I mostly added this because I was doing a lot of clean room testing (flash SD card, run playbook, fix bugs, flash sd card, run playbook, fix bugs), and I just wanted my OS up to date.  
 * `chrony_refclock:` - See [chrony_refclock.md] for more details.  
 * `i226_force_1gbps:` - I left this on, which is Jeff's default.  I didn't test it at 2.5gbps.  For all I know it might work fine.  I just don't need faster than 1gbps on my time pi.
 * `i226_manage_networkmanager: false` - When false, this will not give the intel nic a DHCP config.  When true, it will.  Note: DHCP, or indeed, an ip address at all, is not required for PTP to work.
 * `i226_network_role: [ptp | primary]` - only does anything when `i226_manage_networkmanager` is true.  When set to `ptp`, this will let the intel nic get a DHCP address, but will not give it a default route.  This stops it from fighting with the other nic if they are both plugged in.  It might work fine but it's bad practice.  When set to `primary`, it will treat it as a normal nic, allowing it to get a DHCP address and set whatever routes it feels are appropriate.  This is for when you want to use only the intel nic, and you want to unplug the Pi's nic.
 * `timehat_igc_driver_<whatever>` - when enabled is true, these tell the playbook to patch, build, and install the intel driver.  The stock debian one doesn't work, and the 6.12 version provided by timehat doesn't work with the 6.18 kernel.


# Technical details

Everything below here is in the weeds, and a lot of it was generated by AI in the process of debugging and getting this to work.  If you need an AI agent to look at this code, you should be able to feed it this file as effectively an `AGENTS.md` standin and it should have a decent understanding of how things work and where to look, including diagnostic commands.

## Current Working Path

The working clock chain is:

1. Neo-M9N GNSS receiver exposes serial NMEA on `/dev/ttyAMA0`.
2. `gpsd` reads `/dev/ttyAMA0` at `38400` baud.
3. `gpspipe-socat` bridges `gpspipe -r` to `/dev/gps-ts2phc`.
4. `ts2phc` reads NMEA from `/dev/gps-ts2phc` and PPS timestamps from Intel i226 `SDP2`.
5. `ts2phc` disciplines the i226 PHC at `/dev/ptp0`.
6. Chrony disciplines the system UTC clock from `/dev/ptp0`.
7. `ptp4l` serves PTP from the i226 on `eth1` when PTP is enabled.

## GPSD

The NEO-M9N in this build answered at `38400` baud, not `115200`.

`/etc/default/gpsd`:

```text
DEVICES=""
GPSD_OPTIONS="/dev/ttyAMA0 -s 38400 -n"
USBAUTO="false"
```

Avoid changing the ublox baud rate as part of PPS debugging. If baud-rate tuning is tested later, change only that one variable and verify with `gpsmon`, `cgps`, and `chronyc sources -v`.  I did test this multiple times at multiple points of the setup while working on this, and I could never get it to read at 115200, so I've given up on it.

## Patched i226 Driver

The stock Raspberry Pi kernel `igc` driver could configure `SDP2`, but `ts2phc` failed with:

```text
PTP_EXTTS_REQUEST2 failed: Operation not supported
```

The Time Appliances vendor `intel-igc-ppsfix_rpi5_6.12.62.zip` driver fixed the TimeHAT PPS edge behavior, but needed small compatibility changes for the local `6.18` kernel:

- Replace old hrtimer setup with `hrtimer_setup`.
- Provide compatibility aliases for timer API changes.
- Provide `SKBTX_HW_TSTAMP_USE_CYCLES` when absent.
- Advertise `supported_extts_flags = PTP_STRICT_FLAGS | PTP_RISING_EDGE` so `PTP_EXTTS_REQUEST2` is accepted by newer PTP core code.

This is intentionally guarded to kernels matching `^6\.18\.`. DKMS rebuilds the module, but it does not make out-of-tree kernel API patches magically stable across future kernel series. If Raspberry Pi OS moves to a later kernel, re-test the patch and update the guard instead of assuming it is safe.

On a fresh Raspberry Pi OS image, the stock `igc` module may still autoload early even after DKMS installs the patched module. The playbook installs `timehat-igc.service`, which checks `/sys/module/igc/srcversion` at boot and reloads `igc` if the stock module is active. `ts2phc` is ordered after that service.

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
timehat-igc
gpsd
gpspipe-socat
ts2phc
chrony
ptp4l
```

Expected disabled/inactive services:

```text
phc2sys
```

Chrony owns disciplining the system clock from the PHC. `phc2sys` should stay off or it will fight Chrony.

`ptp4l` serves the disciplined PHC to PTP clients. It is normal for `ptp4l` to start with the `eth1` port in `FAULTY` state when the Intel cable is unplugged. After the cable is connected and link comes up, it should move to `MASTER`.

## Intel i226 Network Role

On a fresh SD card, boot with the onboard Raspberry Pi Ethernet connected and the Intel i226 cable unplugged. Run the playbook to completion, including the boot overlay reboot, the DKMS driver reboot, and the final setup reboot. After the playbook finishes, plug in the Intel cable if PTP is wanted.

The default TimeHAT v6 config disables DHCP for `eth1` by setting `i226_manage_networkmanager: false`.  If set to true, it uses DHCP on `eth1` for UDP PTP but suppresses default routes:

```yaml
i226_manage_networkmanager: false # set to true to enable DHCP on eth1
i226_network_role: ptp
```

Expected shape when `true` and `ptp`:

```text
eth0: DHCP address and default route
eth1: DHCP address, no default route
wlan0: optional backup address/route
```

This keeps SSH/NTP management on the onboard Pi NIC while PTP packets source from the Intel NIC.

To make the Intel NIC the primary network interface as well as the PTP interface, change:

```yaml
i226_network_role: primary
```

Rerun the playbook, confirm the Intel NIC's DHCP lease, then move/unplug cables as desired. Keep console, Wi-Fi, or onboard Ethernet available until the new address is known.

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

Check PTP grandmaster state:

```bash
sudo pmc -u -b 0 "GET PORT_DATA_SET" "GET GRANDMASTER_SETTINGS_NP"
```

Expected with `eth1` plugged in:

```text
portState               MASTER
clockClass              6
currentUtcOffset        37
ptpTimescale            1
timeTraceable           1
timeSource              0x20
```

From a Linux client without hardware timestamping, software timestamping can still verify protocol discovery:

```bash
sudo timeout 180 ptp4l -m -i <client_iface> -S -s
```

Good output includes:

```text
new foreign master <some.hex.value>-1
selected best master clock <some.hex.value>
LISTENING to UNCALIBRATED on RS_SLAVE
[a bunch of lines that look like master offset  <number> s0 freq   <number> path delay     <number>]
UNCALIBRATED to SLAVE on MASTER_CLOCK_SELECTED
[continued lines showing master offset]
```

Software timestamping proves the protocol path, not nanosecond-grade client sync.  If you have a box with a PTP-capable nic, then you can drop the `-S` from the `ptp4l` command to use hardware timing.

## Useful References

- TimeHAT vendor repo: https://github.com/Time-Appliances-Project/TimeHAT
- SatPulse Intel/TimeHAT build notes: https://satpulse.net/hardware/intel-build.html
- TimeHAT v6 + NEO-M9N product page: https://timeappliances.myshopify.com/products/timehatv6-neo-m9n
- Jeff Geerling Time Pi issue for NEO-M9N debugging: https://github.com/geerlingguy/time-pi/issues/11

## Validation

A useful validation test is:

```bash
systemctl is-active gpsd gpspipe-socat ts2phc chrony phc2sys ptp4l
chronyc tracking
chronyc sources -v
sudo phc_ctl /dev/ptp0 cmp
journalctl -u ptp4l -b --no-pager -n 80
journalctl -b --no-pager | grep -Ei 'gpsd|gpspipe|ts2phc|chrony|System clock wrong|System clock was stepped'
```

Expected result: `gpsd`, `gpspipe-socat`, `ts2phc`, `chrony`, and `ptp4l` are active; `phc2sys` is inactive; Chrony selects `PHC`; `ptp4l` reaches `MASTER` after `eth1` link comes up; and there are no repeated 37 second UTC/TAI corrections after startup.
