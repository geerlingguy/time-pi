## Reference for `chrony_refclock` on TimeHATv6 with Neo-M9N

I recommend using this stanza in your config.yml:

```
chrony_refclock: |
  refclock PHC /dev/ptp0 poll 0 dpoll -5 tai refid PHC precision 1e-9 trust
  refclock SOCK /run/chrony.clk.ttyAMA0.sock refid GNSS precision 1e-3 poll 4 noselect offset +0.065
```
or at least the first line - the GNSS / gpsd clock is optional, if still useful.  But I understand that it likely needs an explanation.  

## Explanation

This is telling Chrony about two local time sources:
 * PHC  = the precise hardware clock on the Intel NIC (hardware interrupt)
 * GNSS = the lower-precision serial GPS/NMEA time stream (software interrupt)

With the above config, chrony uses the PHC one. The GNSS is mostly left in as a sanity check.

### PHC Line

`refclock PHC /dev/ptp0 poll 0 dpoll -5 tai refid PHC precision 1e-9 trust`

Let's break it down.

`refclock PHC /dev/ptp0`

Read time from the hardware clock at /dev/ptp0. That is the Intel i226 PHC. The playbook will force ts2phc to discipline that PHC from GPS PPS.

`poll 0`

Sample it once per second.

`dpoll -5`

For PHC refclocks, this controls the lower-level driver polling. -5 means 2^-5 seconds, about 31.25 ms. So Chrony checks the PHC frequently, then produces 1-second source samples.

`tai`

This is where the purists might get mad at me, but I want my NTP server to serve the same time that I get on my phone or my laptop from regular NTP sources.  By default, because of the high precision (I think? I guess people assume if you're doing PTP, you are a researcher or crazy), the PHC is on International Atomic Time (TAI time), not normal UTC - meaning it does not account for leap seconds.  It advances forward in time at a rate of one-second-per-second, forever.  TAI is currently 37 seconds ahead of UTC; UTC has applied leap seconds every few years because the Earth's rotational speed is actually slowing. `tai` tells Chrony: “convert this to UTC before disciplining the system clock.”  Without this, you get a 37-second offset, and all your NTP client machines will be inexplicably not quite on time.   There may be a better way to do this, but it's just where I landed.

If you _do_ want to use this for research purposes, or you know specifically _why_ you would want TIA time instead of UTC, and you're ok to deal with the offset yourself, feel free to leave this out.  

`refid PHC`

This is just the display name in `chronyc sources`.

`precision 1e-9`

Tell Chrony this source is nanosecond-class precision, cause we ballin'.

`trust`

Tell Chrony to trust this source even if other sources disagree. Reasonable here because this is the whole point of the appliance. If you don't turn this on, then what are we doing here?  Why have you spent $600 on a Raspberry Pi that listens to voices from the heavens?

### GNSS Line

`refclock SOCK /run/chrony.clk.ttyAMA0.sock refid GNSS precision 1e-3 poll 4 noselect offset +0.065`

Now we break this down.

`refclock SOCK /run/chrony.clk.ttyAMA0.sock`

Read timestamps from gpsd through Chrony’s SOCK refclock interface. This is based on serial GPS/NMEA messages from /dev/ttyAMA0.

`refid GNSS`

Same as above, this is just what it shows as in `chronyc sources`.

`precision 1e-3`

Treat it as millisecond-ish precision. Serial NMEA timing is much sloppier than PPS/PHC, because software interrupts just are like that.

`poll 4`

Poll every 2^4 seconds, so every 16 seconds.  I have no idea why this is given in exponent form, but whatever.

`noselect`

Don't actually use this source to discipline the system clock.  Just let it vibe in the list for diagnostic purposes.  It still shows up in `chronyc sources`, which is useful because you can see that GPS serial time exists and is vaguely near reality, but chrony won’t choose it over the PHC source, or combine it into the clock calculation.

`offset +0.065`

Apply a 65 ms fudge factor to the serial GNSS source. NMEA sentences arrive after the actual second they describe because serial output and receiver processing take time.  This makes the GNSS sanity source less wildly off.  Not strictly necessary and also can vary with a lot of factors; leave it off or figure out a more precise one for yourself if you want to, but again, since this time source isn't going to be used, :shrug:?