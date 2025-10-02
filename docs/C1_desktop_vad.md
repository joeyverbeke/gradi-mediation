# Desktop WebRTC VAD Module (C1)

## Overview
The `desktop_vad` package wraps the WebRTC VAD engine so the desktop controller can gate audio capture windows before handing them to Whisper. It accepts 16-bit PCM frames (e.g., streamed from the ESP32 microphone capture) and emits timestamp pairs describing speech regions. The defaults are tuned for 16 kHz mono audio with modest hangover to avoid choppy segmentation.

## Module Entry Points
- `desktop_vad.WebRTCVADProcessor`
  - Instantiate with `VADConfig` and call `.process(pcm_bytes)` or `.process_frames(frames)`.
  - Returns a list of `(start_time_seconds, end_time_seconds)` tuples merged across tiny gaps (<60 ms).
- `desktop_vad.detect_voiced_segments`
  - Convenience function using keyword overrides when only raw PCM bytes are available.

## Desktop Test Script
A CLI helper mirrors the human validation protocol and lives at `scripts/vad_test.py`:

```bash
uv run scripts/esp_audio_tester.py --port /dev/ttyACM0 record --seconds 5 --output esp_mic_test.wav  # capture from ESP
uv pip install webrtcvad  # once per venv if not already installed
uv run scripts/vad_test.py esp_mic_test.wav
uv run scripts/vad_test.py room_noise.wav --aggressiveness 3
```

- Inputs must be 16-bit PCM WAV files. Downmix to mono before testing.

The script prints lines such as `speech: 0.480s -> 2.760s`; an empty list reports `no speech detected`.

## Validation Guidance
1. **Speech sample (`esp_mic_test.wav`)**
   - Confirm the timestamps align with the spoken region you hear.
2. **Room-noise sample**
   - Expect `no speech detected` under quiet or typical background noise conditions.

Adjust aggressiveness (higher trims more aggressively) or the start/stop trigger frames if later integration reveals over/under sensitivity. The processor merges segments separated by <60 ms and ignores activations shorter than 0.2 s to prevent rapid toggling and suppress single-frame pops.
