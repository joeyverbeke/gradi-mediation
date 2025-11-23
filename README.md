# Gradi Mediation

## Overview
Gradi Mediation orchestrates an end-to-end speech mediation loop. Audio is captured on an ESP32-S3 microphone bridge, gated with WebRTC VAD on the desktop, transcribed with the ASR engine of your choice, routed through an LLM, and rendered with Kokoro TTS before playback on the ESP. The desktop controller coordinates READY/PAUSE/RESUME transitions so capture and playback never collide.

## Hardware & Services
- ESP32-S3 + ICS-43434 mic + dual MAX98357A over USB CDC, with the Seeed Studio XIAO mmWave radar generating `PRESENCE` events.
- Desktop services: WebRTC VAD, ASR engines (whisper.cpp, Faster-Whisper, Vosk), vLLM sampler, and Kokoro-FastAPI voices.
- Optional GPU acceleration is available for Faster-Whisper when started with `--fw-device cuda`.

## Quick Start
1. Create the Python environment:
   ```bash
   uv venv --python 3.10 --seed
   ```
2. Install the baseline dependencies (add any platform-specific extras as needed):
   ```bash
   uv pip install pyserial webrtcvad requests soundfile faster-whisper vosk numpy
   ```
   Python 3.10 users also need `uv pip install tomli`.
3. Populate `third_party/` as described in `third_party/README.md` (clone/build whisper.cpp, download Faster-Whisper/Vosk models, start vLLM and Kokoro).
4. Flash `firmware/esp32s3_audio_min/esp32s3_audio_min.ino` to the ESP32-S3 using the Arduino IDE configured for the XIAO ESP32-S3 layout.

## Running the Stack

### Direct session controller
Use the controller once the ESP and backend services are reachable:
```bash
uv run scripts/session_controller.py \
  --asr-engine faster_whisper \
  --fw-model-dir third_party/faster-whisper/models \
  --kokoro-voice af_bella
```
Switch `--asr-engine` and supply the corresponding flags for whisper.cpp or Vosk as needed. Add `--verbose-esp` when debugging the serial protocol.

### Supervisor
Update `controller/services.toml` to reflect your workspace (venv path, model folders, CLI flags). Launch the service bundle with:
```bash
uv run controller/startup.py up --port /dev/gradi-esp-mediate --attach gradi-mediate
```
Omit `--port` to reuse the manifest setting or fall back to `/dev/gradi-esp-mediate`. While the supervisor runs you can inspect state and logs from another shell:
```bash
uv run controller/startup.py status
uv run controller/startup.py logs vllm --lines 80
uv run controller/startup.py down
```
Supervisor state lives in `logs/services/` alongside per-service log files.

## Validation Tools
- `uv run scripts/esp_audio_tester.py --port <device> record --seconds 5 --output esp_mic_test.wav` — sanity-check ESP capture/playback.
- `uv run scripts/vad_test.py <wav> --aggressiveness 2` — inspect WebRTC VAD gating on captured samples.
- `uv run scripts/asr_transcribe.py --asr-engine faster_whisper ...` — smoke-test the ASR engines (see script `--help` for engine-specific flags).
- `uv run scripts/llm_transform.py --text "hello" --output llm_pairs.jsonl` — verify the LLM hop.
- `uv run scripts/tts_stream.py --text "Testing Kokoro" --voice af_bella` — confirm Kokoro streaming output.

## Logs & Artifacts
- Session controller runs write JSONL traces to `logs/sessions/`.
- Supervisor-managed services write rotating logs under `logs/services/`.
Capture representative WAV/JSONL artifacts during validation to accompany roadmap checkpoints.

## Troubleshooting
- Use `--verbose-esp` to diagnose serial link issues; pause/resume events should mirror ESP READY lines.
- If Faster-Whisper fails on GPU, set `--fw-device cpu` or verify CUDA and cuDNN libraries are installed.
- Confirm vLLM and Kokoro endpoints respond to `/health` before starting the controller or supervisor.

## Further Reading
Detailed specifications, validation notes, and roadmap checkpoints live in `docs/`. Contributor expectations and task routing are in `AGENTS.md`.
