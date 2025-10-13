# Gradi Mediation

## Overview
Gradi Mediation implements an end-to-end speech mediation loop. Audio is captured on an ESP32-S3 with an ICS-43434 microphone, gated on the desktop with WebRTC VAD, transcribed by whisper.cpp, Faster-Whisper, or Vosk, refined through a vLLM prompt, and synthesised by Kokoro-FastAPI for playback on dual MAX98357A speakers. The desktop drives the state machine so that the microphone and speakers never contend.

## Hardware & Services
- ESP32-S3 + ICS-43434 mic + dual MAX98357A amplifiers, connected over USB CDC.
- Desktop services: WebRTC VAD (Python), ASR engines (whisper.cpp, Faster-Whisper, or Vosk), vLLM text transformer, Kokoro-FastAPI TTS.
- Optional GPU acceleration for Faster-Whisper when `--fw-device cuda` is selected.

## Repository Layout
- `firmware/esp32s3_audio_min/` – minimal Arduino sketch that continuously streams mic PCM and plays queued audio, now signalling playback completion.
- `desktop_vad/` – WebRTC VAD wrapper and configs for gating capture.
- `asr/` – whisper.cpp, Faster-Whisper, and Vosk transcribers plus shared result types.
- `llm/` – vLLM client utilities and prompt management.
- `tts/` – Kokoro streaming client and helpers.
- `controller/` – session controller, ESP bridge, and state machine logic.
- `scripts/` – command-line harnesses for validation of each module and the full pipeline.
- `docs/` – per-module specifications and validation notes (`B1`, `C1`, … `E1`).
- `third_party/` – placeholder for cloned upstream dependencies (see `third_party/README.md`).

## Setup
1. Create the Python environment:
   ```bash
   uv venv --python 3.10 --seed
   ```
2. Install Python dependencies inside the venv:
   ```bash
   uv pip install pyserial webrtcvad requests soundfile faster-whisper vosk numpy
   ```
   Add any extra libraries your platform needs beyond this baseline.
3. Populate `third_party/` following `third_party/README.md`:
   - Clone and build `whisper.cpp` (`make -j$(nproc)` or CMake) and download `ggml-small.bin`.
   - Download Faster-Whisper models into `third_party/faster-whisper/models/`.
   - Start vLLM and Kokoro-FastAPI with the models you intend to use.
4. Flash `firmware/esp32s3_audio_min/esp32s3_audio_min.ino` to your ESP32-S3 using Arduino IDE (board configured for the XIAO ESP32S3 pinout).

## Module Validation Commands
- **ESP audio bridge**
  ```bash
  uv run scripts/esp_audio_tester.py --port /dev/ttyACM0 record --seconds 5 --output esp_mic_test.wav
  uv run scripts/esp_audio_tester.py --port /dev/ttyACM0 play --input esp_mic_test.wav --target-rate 16000
  ```
  The helper script now performs the `READY`/`RESUME` handshake automatically and reads binary audio frames (`AUD0` headers) from the ESP stream.
- **Desktop VAD**
  ```bash
  uv run scripts/vad_test.py esp_mic_test.wav --aggressiveness 2
  uv run scripts/vad_test.py room_noise.wav --aggressiveness 3
  ```
- **ASR (choose one engine)**
  ```bash
  # whisper.cpp
  uv run scripts/asr_transcribe.py \
    --asr-engine whisper_cpp \
    --binary third_party/whisper.cpp/build/bin/whisper-cli \
    --model third_party/whisper.cpp/models/ggml-small.bin \
    phrase01.wav --output asr_results.txt

  # Faster-Whisper
  uv run scripts/asr_transcribe.py \
    --asr-engine faster_whisper \
    --fw-model-dir third_party/faster-whisper/models \
    --fw-device cuda \
    phrase01.wav --output asr_results.txt

  # Vosk
  uv run scripts/asr_transcribe.py \
    --asr-engine vosk \
    --vosk-model-dir third_party/vosk/models/vosk-model-small-en-us-0.15 \
    phrase01.wav --output asr_results.txt
  ```
- **LLM transform (vLLM)**
  ```bash
  uv run scripts/llm_transform.py --text "hello, how are you" --output llm_pairs.jsonl
  ```
- **Kokoro TTS streaming**
  ```bash
  uv run scripts/tts_stream.py --text "Testing Kokoro" --voice af_bella --response-format wav
  ```

## End-to-End Session
Run the orchestrator once all services are live. Pick the ASR engine that matches your setup.
```bash
uv run scripts/session_controller.py \
  --port /dev/ttyACM0 \
  --asr-engine faster_whisper \
  --fw-model-dir third_party/faster-whisper/models \
  --kokoro-voice af_bella \
  --kokoro-format pcm \
  --max-cycles 5
# For whisper.cpp add:
#   --whisper-binary third_party/whisper.cpp/build/bin/whisper-cli \
#   --whisper-model third_party/whisper.cpp/models/ggml-small.bin
# For Vosk add:
#   --asr-engine vosk \
#   --vosk-model-dir third_party/vosk/models/vosk-model-small-en-us-0.15
```
The controller writes structured JSONL logs to `logs/sessions/` and ensures the ESP playback acknowledgement arrives before the mic resumes.

## Troubleshooting
- Use `--verbose-esp` on `session_controller.py` to see raw serial protocol lines.
- Adjust VAD sensitivity with `--vad-aggressiveness`, `--min-segment-duration`, and `--min-segment-mean-abs` to suppress false triggers.
- Verify vLLM and Kokoro endpoints by hitting their `/health` or `/docs` routes before running the pipeline.
- Confirm Faster-Whisper GPU dependencies (`libcudnn`/CUDA) are available when using `--fw-device cuda`.

## Additional Documentation
Detailed specifications, validation checklists, and design notes for each milestone live in `docs/`. Contributor workflow expectations are captured in `AGENTS.md`.
