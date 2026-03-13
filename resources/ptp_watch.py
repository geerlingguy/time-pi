#!/usr/bin/env python3
"""
ptp_watch.py – PTPv2 two‑shot listener

Requires root to open a raw socket. Works on Linux.

Usage:
  sudo python3 ptp_watch.py --iface eth0
"""

import argparse
import datetime
import os
import socket
import struct
import sys

# Global state to track the leap second offset
# Default is 0 until we hear an Announce message
current_utc_offset = 0

def ptp_to_utc(seconds: int, nanoseconds: int, offset: int) -> datetime.datetime:
    """Convert TAI (PTP) to UTC by subtracting the leap second offset."""
    return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + \
           datetime.timedelta(seconds=seconds - offset,
                              microseconds=nanoseconds / 1000)

def listen(iface: str | None = None):
    global current_utc_offset
    MCAST_GRP = '224.0.1.129'
    PTP_GENERAL_PORT = 320

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('', PTP_GENERAL_PORT))

    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print(f"Listening for PTP on {MCAST_GRP}:{PTP_GENERAL_PORT}...")

    try:
        while True:
            data, addr = sock.recvfrom(2048)
            if len(data) < 44:
                continue

            msg_type = data[0] & 0x0F

            # --- Handle Announce Message (0x0B) ---
            if msg_type == 0x0B and len(data) >= 64:
                # Flags are 2 bytes at offset 6.
                flags = struct.unpack_from(">H", data, 6)[0]
                # Standard PTPv2 bit positions for the 16-bit flags field:
                ptp_timescale = bool(flags & 0x0008)   # Bit 11
                utc_reasonable = bool(flags & 0x0004)  # Bit 10

                if utc_reasonable and ptp_timescale:
                    # currentUtcOffset is at offset 44 (2 bytes)
                    new_offset = struct.unpack_from(">h", data, 44)[0]
                    if new_offset != current_utc_offset:
                        current_utc_offset = new_offset
                        print(f"[*] Updated UTC Offset: {current_utc_offset}s (from {addr[0]})")

            # --- Handle Follow_Up Message (0x08) ---
            elif msg_type == 0x08:
                sec_hi, sec_lo = struct.unpack_from(">HI", data, 34)
                seconds = (sec_hi << 32) | sec_lo
                nanoseconds, = struct.unpack_from(">I", data, 40)

                utc = ptp_to_utc(seconds, nanoseconds, current_utc_offset)
                offset_str = f" [Offset: {current_utc_offset}s]" if current_utc_offset else " [No Offset]"
                print(f"Follow_Up (0x8) | {utc.isoformat()}{offset_str} from {addr[0]}")

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sock.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", help="Interface to listen on")
    args = parser.parse_args()
    if os.geteuid() != 0:
        sys.exit("Run as root.")
    listen(args.iface)

if __name__ == "__main__":
    main()
