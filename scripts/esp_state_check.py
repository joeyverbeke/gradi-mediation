"""
Send STATE? to the ESP bridge and print whatever comes back. Handy to confirm
the control channel is alive without touching the firmware.

Usage: uv run scripts/esp_state_check.py --port /dev/gradi-esp-mediate
"""

import argparse
import sys
import time

import serial


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/gradi-esp-mediate")
    parser.add_argument("--baud", type=int, default=921600)
    args = parser.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=2)
    ser.reset_input_buffer()
    ser.write(b"STATE?\n")
    time.sleep(0.1)
    resp = ser.read(256)
    print(resp.decode(errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
