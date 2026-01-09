# Pico 1U GPS Clock for Mini Rack

I wanted to have the most accurate timepiece possible mounted in my mini rack. Therefore I built this:

<p align="center"><img alt="Raspberry Pi Pico GPS Clock for Mini Rack" src="/pico-clock-mini-rack/pico-gps-clock-mini-rack.jpg" height="auto" width="600"></p>

This is a GPS-based clock running on a Raspberry Pi Pico in a custom 1U 10" rack faceplate. The clock displays time based on a GPS input, and will not display time until a GPS timing lock has been acquired.

  - When you turn on the Pico, the display reads `----`
  - Upon 3D fix, you get a time on the clock, and the colon starts blinking
  - If the 3D fix is lost, the colon goes solid
  - When the 3D fix is regained, the colon starts blinking again

For full details on designing and building this clock, see [Issue #4: Build 7-segment clock display for Mini Rack](https://github.com/geerlingguy/time-pi/issues/4).

## Hardware

For my clock, I'm using a Raspberry Pi Pico 2, but you should be able to use any model Pico (including the Wireless versions). Here are all the parts required (some links are affiliate links):

  - [Adafruit 0.56" 4-Digit 7-Segment Display w/I2C Backpack](https://www.adafruit.com/product/879)
  - [Raspberry Pi Pico 2 (with Headers)](https://www.raspberrypi.com/products/raspberry-pi-pico-2/)
    - If you don't have the 'with Header' version, solder on [2x 20 pin headers](https://amzn.to/4jClLn1).
  - [Waveshare L76K GNSS Module for Pico](https://amzn.to/4qgiFaQ)
  - [15cm jumper wires](https://amzn.to/4aT5jwj)
  - [1220 3V Lithium coin cell battery](https://amzn.to/3YzptnL) (if you want RTC clock holdover)
  - [M2x6mm screws](https://amzn.to/45NaeLM) (optional, to hold the display in securely)

## 3D Printed 1U mini rack faceplate

The faceplate design is hosted on Printables: [Pico GPS Clock - 1U Mini Rack faceplate](https://www.printables.com/model/1549682).

The tolerances for the 7 segment display are fairly tight, so you don't necessarily need the M2 screws to hold it in place.

## Software

Plug the Pico into a computer running Thonny, and [install MicroPython following Raspberry Pi's guide](https://www.raspberrypi.com/documentation/microcontrollers/micropython.html). Optionally plug the Waveshare GPS HAT into the Pico at this time, to verify it powers on too.

Copy the MicroPython script `main.py` into the root directory of the Pico. Also copy the following files from this repository to the Pico, alongside `main.py`:

  - `ht16k33.py` and `ht16k33segment.py`, both from [smittytone/HT16K33-Python](https://github.com/smittytone/HT16K33-Python)
  - `micropyGPS.py`, from [inmcm/micropyGPS](https://github.com/inmcm/micropyGPS)

Run the script, to make sure it compiles and runs without errors. Then unplug the Pico.

Saving the script as `main.py` means MicroPython will automatically run the script when you plug in power, even if you don't have it plugged into your computer.

## Assembly

<p align="center"><img alt="Raspberry Pi Pico GPS Clock 1U mini rack assembly" src="/pico-clock-mini-rack/pico-gps-clock-assembly-back.jpg" height="auto" width="600"></p>

  1. Insert clock face into clock face cutout, making sure the four through-hole pins on the board are in the small cutout made for that purpose. Ensure the clock face is flush with the front of the panel (the PCB should be in contact with the raised part of the print).
  2. Plug GPS antenna lead into the U.fl connector on the Waveshare HAT board, passing over the top of the board and over the battery, so it exits the correct side once the Pico is installed.
  3. Plug the Pico into the Waveshare HAT. The HAT's button and switch should be on the same side as the Pico's USB plug.
  4. Secure the female SMA jack through the faceplate using the included nut and washer.
  5. Using 4 female-to-female dupont jumper wires, plug the following pins to each other (see illustration above):

| 7-segment pin | Pi Pico pin   |
| :------------ | :------------ |
| VCC           | 36 (VCC)      |
| GND           | 38 (GND)      |
| SDA           | 6 (SDA/Data)  |
| SCL           | 7 (SCL/Clock) |

Once that's finished, plug micro USB power into the Pico, being careful not to push down on the 'BOOT' button under the Pico while doing so, and plug in a GPS antenna to the SMA jack.

That's it! You should now have a fully functional clock.

It will read `----` until a 3D position fix is acquired (this is required for a precise timing lock).

It usually takes 15-30s the first time to acquire a timing signal, or 3-5s if you just unplug it and plug it back in within a few minutes.

If you're

## PPS Output

Currently, there is no PPS output available from the Waveshare HAT.

I spoke to Waveshare support, and they suggested I could add on a small bodge wire to get PPS from the relevant PCB trace (which provides the pulse for the PPS LED):

> The PPS LED does flash, but this signal is usually not directly exposed to the pins of the Pico HAT. If you wish to use this signal externally for verification or detection, you may need to check the schematic diagram to see if there are any relevant lines that can be connected, or consider using additional hardware circuits to lead this signal to the pin.

<p align="center"><img alt="Waveshare Pico GPS HAT Schematic - PPS detail" src="/pico-clock-mini-rack/waveshare-support-schematic.png" height="auto" width="600"></p>

<p align="center"><img alt="Waveshare Pico GPS HAT - PPS resistor highlighted" src="/pico-clock-mini-rack/waveshare-support-picture-highlighted.png" height="auto" width="600"></p>
