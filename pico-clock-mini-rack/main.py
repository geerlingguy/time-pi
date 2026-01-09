from machine import UART, Pin, I2C
from ht16k33segment import HT16K33Segment
from micropyGPS import MicropyGPS
import time

# 1. SETUP DISPLAY
i2c = I2C(0, sda=Pin(4), scl=Pin(5), freq=400000)
display = HT16K33Segment(i2c)
display.set_brightness(15)

# 2. SETUP GPS (UART0: TX=GP0, RX=GP1)
gps_uart = UART(0, baudrate=9600, tx=Pin(0), rx=Pin(1))
my_gps = MicropyGPS(location_formatting='dd')

# 3. TIMEZONE & STATE TRACKING
STD_OFFSET = -6
had_fix = False  # Tracks previous state to prevent console spam

def get_local_time(gps_obj):
    h, m, s = gps_obj.timestamp
    return (h + STD_OFFSET) % 24, m, s

def show_dashes():
    for i in range(4):
        display.set_glyph(0x40, i)
    display.set_colon(False)
    display.draw()

# --- STARTUP: WAIT FOR FIRST 3D FIX ---
print("System Starting... Waiting for initial 3D GPS fix.")
while True:
    if gps_uart.any():
        raw_data = gps_uart.read()
        for b in raw_data:
            try: my_gps.update(chr(b))
            except: continue

    if my_gps.fix_type == 3:
        print("Initial 3D Fix Acquired!")
        had_fix = True
        break

    show_dashes()
    time.sleep(0.1)

# --- MAIN LOOP ---
while True:
    if gps_uart.any():
        raw_data = gps_uart.read()
        for b in raw_data:
            try: my_gps.update(chr(b))
            except: continue

    # Check for State Changes in 3D Fix
    if my_gps.fix_type < 3 and had_fix:
        print("ALERT: 3D Fix Lost! Time may drift.")
        had_fix = False
    elif my_gps.fix_type == 3 and not had_fix:
        print("SUCCESS: 3D Fix Regained.")
        had_fix = True

    hours, minutes, seconds = get_local_time(my_gps)

    display.set_number(hours // 10, 0)
    display.set_number(hours % 10, 1)
    display.set_number(minutes // 10, 2)
    display.set_number(minutes % 10, 3)

    # Colon Logic: Flash if 3D Fix is active, Solid if Lost
    if had_fix:
        display.set_colon(int(seconds) % 2 == 0)
    else:
        display.set_colon(True)

    display.draw()
    time.sleep(0.1)
