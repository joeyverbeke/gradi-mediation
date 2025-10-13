# ESP32-S3 Minimal Audio I/O Firmware (B1)

## Overview
The `firmware/esp32s3_audio_min/esp32s3_audio_min.ino` sketch keeps only the peripherals needed for the mediation loop: an ICS-43434 microphone on I2S1 (32-bit input, left channel) and a pair of MAX98357A DACs on I2S0 (16-bit mono output, shared left channel). The ESP32-S3 continuously streams microphone audio to the host while accepting explicit playback jobs over USB CDC—no BLE/Wi-Fi dependencies or extra UI remain.

## Serial Protocol
- **Handshake & flow control**
  - On boot the firmware emits `READY\n` and holds the microphone stream in a paused state.
  - The desktop resumes capture with `RESUME\n` and can later pause again with `PAUSE\n`. When paused, mic data is dropped instead of queued.
  - Optional requests such as `STATE?` still return ASCII responses, but normal operation keeps all subsequent traffic binary.
- **Microphone uplink (binary frames)**
  - Chunks are delivered as fixed-length headers followed by raw PCM:
    - 12-byte header: `0x30445541` magic (`'AUD0'`), version `0x01`, frame type `0x01` (audio), reserved `0x0000`, payload length (LE `uint32`).
    - Payload: `payload_length` bytes of 16-bit little-endian mono PCM.
  - Default payload is 1024 samples (2048 bytes) but the host must trust the announced byte count only.
  - If a log line ever appears between frames (e.g., `LOG …`), the desktop should treat it as ASCII and continue scanning for the next header.
- **Speaker playback (host initiated, unchanged)**
  - Playback still uses the ASCII control channel: `START <sample_rate> 1 16 <sample_count>` followed by `<sample_count>` 16-bit samples, then `END`.
  - This command path remains text-based because playback jobs are sporadic and easier to debug from a terminal.

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
  - Flushes buffered audio, resumes capture, then accumulates `seconds × 16 kHz × 16-bit` PCM from successive binary frames and writes a WAV wrapper.
- `uv run scripts/esp_audio_tester.py --port /dev/ttyACM0 play --input esp_mic_test.wav --target-rate 16000`
  - Validates the WAV header, folds multi-channel audio to mono, optionally downsamples to the requested rate, streams 16-bit PCM in paced 1 kB chunks, and issues `END` when complete.
- The script surfaces protocol timeouts and malformed headers immediately so the acceptance checks remain hands free.

## Error Feedback
Representative signals for rapid diagnosis:
- `READY` appears once after reset; no audio will flow until the host sends `RESUME`.
- Binary header mismatch (magic/version) indicates framing drift—flush the serial port and resume.
- `STATE STREAMING` – heartbeat confirming the microphone stream is active when requested via `STATE?`.

Because the microphone runs continuously once resumed, the host can pause/resume without resetting the board; clear the serial input before timing a new window and reissue `RESUME` after long playback jobs.
