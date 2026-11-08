# NTP Proxy Server

A public stratum 2 NTP server on a VPS, synced to the Time Pi over WireGuard. It serves public NTP (e.g. for the [NTP Pool](https://www.ntppool.org)) without exposing the Time Pi's network. See [#48](https://github.com/geerlingguy/time-pi/issues/48) for background.

- **Tunnel:** the Time Pi connects *out* to the VPS, so nothing needs to be opened on the Pi's network. Each side generates its own WireGuard private key, which never leaves that host.
- **Pi isolation:** the Pi only accepts NTP over the tunnel, and never routes the VPS into its LAN.
- **Refid:** clients see the private tunnel address as the server's refid, not the Pi's public IP.
- **Sources:** chrony on the VPS prefers the Time Pi. It also follows Cloudflare and NIST, which flag a bad path and take over if the tunnel goes down.
- **Hardening:** [`geerlingguy.security`](https://github.com/geerlingguy/ansible-role-security) and [`geerlingguy.firewall`](https://github.com/geerlingguy/ansible-role-firewall) lock down the VPS. The only open ports are SSH, NTP and WireGuard, and NTP is rate limited.

## Install

The Time Pi should already be set up with the main playbook.

1. Get a KVM VPS (not OpenVZ/LXC) running Debian or Ubuntu, with the lowest round-trip time to the Time Pi you can find. Check it with:

   ```
   systemd-detect-virt        # on the VPS: should print "kvm"
   ping -c 20 <vps-ip>        # from the Time Pi
   ```

2. Add a user account with `sudo` access on the VPS, and ensure an SSH key is configured.

3. Point a DNS-only (not proxied) `A` record, and an `AAAA` record if the VPS has IPv6, at the VPS.

4. Copy `example.hosts.ini` to `hosts.ini` and set the VPS's hostname and user, and the Time Pi's.

5. Install the required roles and run the playbook (`-K` prompts for the VPS user's `sudo` password):

   ```
   ansible-galaxy role install -r requirements.yml
   ansible-playbook main.yml -K
   ```

6. Verify it's working on the VPS:

   ```
   sudo wg show               # should show a recent handshake
   chronyc sources            # should show ^* on 10.99.0.1
   ```

7. Test it from another computer (prints the offset without changing the clock):

   ```
   sntp ntp-01.jeffgeerling.com                                     # macOS
   sudo chronyd -Q -t 10 'server ntp-01.jeffgeerling.com iburst'    # Linux with chrony
   ```

After it's been stable for a few days, [add it to the NTP Pool](https://www.ntppool.org/manage) at the lowest netspeed and raise it gradually.

## Monitoring

The playbook logs chrony's server counters every minute to `/var/log/chrony/serverstats.log`, rotated along with chrony's other logs. Each line is a UTC timestamp followed by the fields from `chronyc -c serverstats` (NTP requests received, dropped by rate limiting, etc.). The counters reset when chrony restarts.

To chart NTP requests per second in the terminal, run `ntp-stats` on the VPS:

```
ntp-stats            # last 24 hours, hourly
ntp-stats 168        # last 7 days, hourly
ntp-stats 2 10       # last 2 hours, 10-minute buckets
```

The NTP Pool's monitors also publish offsets and scores for the server at `https://www.ntppool.org/scores/<vps-ip>`.
