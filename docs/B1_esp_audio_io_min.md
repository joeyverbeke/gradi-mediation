# ESP32-S3 Minimal Audio I/O Firmware (B1)

## Overview
The `firmware/esp32s3_audio_min/esp32s3_audio_min.ino` sketch keeps only the peripherals needed for the mediation loop: an ICS-43434 microphone on I2S1 (32-bit input, left channel) and a pair of MAX98357A DACs on I2S0 (16-bit mono output, shared left channel). The ESP32-S3 continuously streams microphone audio to the host while accepting explicit playback jobs over USB CDC—no BLE/Wi-Fi dependencies or extra UI remain.

## Serial Protocol
- **Microphone uplink (always on)**
  - The ESP continuously captures 16 kHz mono audio and emits chunks as:
    - `AUDIO <bytes>\n`
    - `<bytes>` raw little-endian 16-bit PCM samples immediately follow the newline.
  - Chunk size defaults to 1024 samples (2048 bytes) but the host should rely on the announced byte count, not the size.
  - Occasional informational lines (`LOG …`, `STATE …`) may appear between chunks.
- **Speaker playback (host initiated)**
  - To play PCM data, the host must send a newline-terminated header: `START <sample_rate> 1 16 <sample_count>`.
  - The ESP reconfigures I2S0 to the requested sample rate (common rates: 16 kHz, 22.05 kHz, 24 kHz, 32 kHz) and reads `<sample_count>` samples (2 × `<sample_count>` bytes) from the serial stream.
  - After all samples are delivered, the host sends `END` (newline terminated) to release the state machine.
  - The ESP prints diagnostic lines such as `Streaming …`, `Awaiting END footer`, and `Finished stream` for visibility.

## Host Interaction Notes
- Serial baud rate: 921600. The host should pool data in a background thread or request fixed-duration captures using the helper script below.
- Because the microphone stream never stops, the host should reset the serial input buffer right before capturing a timed window.
- Playback expects the PCM payload immediately after the `START` header; throttled writes (∼1 kB chunks) at 921600 baud keep the I2S DMA fed.

## Validation Procedure
1. **Record intelligibility test**
   - Run the helper script (see below) to grab 5 s of audio into `esp_mic_test.wav`.
   - Listen for clear speech with expected level and no clipping.
2. **Playback fidelity test**
   - Use the script to push `esp_mic_test.wav` (or a known-good 16 kHz mono tone) back to the ESP.
   - Confirm clean, crackle-free playback on both speakers; observe the `Finished stream` log.

## Desktop Test Script
- Create a Python environment with uv: `uv venv --python 3.10 --seed` and install PySerial: `uv pip install pyserial`.
- `uv run scripts/esp_audio_tester.py --port /dev/ttyACM0 record --seconds 5 --output esp_mic_test.wav`
  - Flushes buffered audio, then accumulates `seconds × 16 kHz × 16-bit` PCM from successive `AUDIO` chunks and writes a WAV wrapper.
- `uv run scripts/esp_audio_tester.py --port /dev/ttyACM0 play --input esp_mic_test.wav --target-rate 16000`
  - Validates the WAV header, folds multi-channel audio to mono, optionally downsamples to the requested rate, streams 16-bit PCM in paced 1 kB chunks, and issues `END` when complete.
- The script surfaces protocol timeouts and malformed headers immediately so the acceptance checks remain hands free.

## Error Feedback
Representative log lines for rapid diagnosis:
- `Invalid header: …` – malformed `START` command (check sample rate, channel count, or sample total).
- `Awaiting END footer` without a follow-up `Finished stream` – host failed to send the terminating `END` line.
- `STATE STREAMING` – heartbeat confirming the microphone stream is active.

Because the microphone runs continuously, the host can always retry captures without resetting the board; just clear the serial input before timing a new window.
