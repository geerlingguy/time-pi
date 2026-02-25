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

def ptp_to_utc(seconds: int, nanoseconds: int) -> datetime.datetime:
    """Convert PTP seconds/nanoseconds to a UTC datetime (1970 epoch)."""
    return datetime.datetime(1970, 1, 1,
                             tzinfo=datetime.timezone.utc) + \
           datetime.timedelta(seconds=seconds,
                              microseconds=nanoseconds / 1000)

def listen(iface: str | None = None):
    MCAST_GRP = '224.0.1.129'
    PTP_GENERAL_PORT = 320  # Port for Sync/Delay_Req messages

    # Create a standard UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    # Allow multiple processes to listen to the same PTP port
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Bind to the PTP general port
    sock.bind(('', PTP_GENERAL_PORT))

    # Tell the kernel to join the PTP multicast group
    mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

    print(f"Listening for PTP Multicast on {MCAST_GRP}:{PTP_GENERAL_PORT}...")

    try:
        while True:
            data, addr = sock.recvfrom(2048)
            if len(data) < 44:
                continue

            # Message type is the lower 4 bits of the first byte
            msg_type = data[0] & 0x0F

            # Only process Follow_Up messages (type 0x08)
            if msg_type != 0x08:
                continue

            # originTimestamp: 6 bytes seconds + 4 bytes nanoseconds at offset 34
            sec_hi, sec_lo = struct.unpack_from(">HI", data, 34)
            seconds = (sec_hi << 32) | sec_lo
            nanoseconds, = struct.unpack_from(">I", data, 40)

            # Convert directly to UTC (no leap second adjustment)
            utc = ptp_to_utc(seconds, nanoseconds)
            print(f"Follow_Up (0x8) | [{utc.isoformat()}] from {addr[0]}")

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        sock.close()

def main():
    parser = argparse.ArgumentParser(description="PTPv2 two‑shot watcher (1970 epoch)")
    parser.add_argument("--iface", help="Interface to listen on (default: all)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        sys.exit("Run as root/Administrator.")

    listen(args.iface)

if __name__ == "__main__":
    main()
