"""
Quick mic probe: request AUD0 frames at 921600 baud, check framing, and report
RMS/peak to confirm whether audio samples are reaching the host.

Usage: uv run scripts/mic_probe.py --port /dev/gradi-esp-mediate --frames 5
"""

import argparse
import math
import struct
import sys
import time

import serial


def read_frame(ser: serial.Serial):
    header = ser.read(12)
    if len(header) < 12:
        return ("no_header", len(header))

    magic, version, frame_type, reserved, payload_bytes = struct.unpack(
        "<IBBH I", header
    )

    if magic != 0x30445541 or version != 1 or frame_type != 1:
        return ("bad_header", header)

    data = ser.read(payload_bytes)
    if len(data) != payload_bytes:
        return ("short_payload", len(data), payload_bytes)

    samples = struct.unpack("<%dh" % (payload_bytes // 2), data)
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    peak = max(abs(s) for s in samples)
    return ("ok", payload_bytes, rms, peak)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default="/dev/gradi-esp-mediate")
    parser.add_argument("--frames", type=int, default=5)
    args = parser.parse_args()

    ser = serial.Serial(args.port, 921600, timeout=2)
    ser.reset_input_buffer()
    ser.write(b"RESUME\n")
    time.sleep(0.1)

    for i in range(args.frames):
        result = read_frame(ser)
        print(f"frame {i}: {result}")


if __name__ == "__main__":
    sys.exit(main())
