# Repository Guidelines

## Project Structure & Module Organization
- `firmware/` holds ESP32-S3 sketches focused on USB commands and I2S wiring.
- Desktop logic lives in `desktop_vad/`, `asr/`, `llm/`, `tts/`, with orchestration in `controller/`.
- `scripts/` provides validation CLIs (`esp_audio_tester.py`, `vad_test.py`, `asr_transcribe.py`, `llm_transform.py`, `session_controller.py`).
- Module notes and validation checklists stay in `docs/`; runtime traces land under `logs/` (JSONL) or gitignored WAV/JSONL artifacts.
- `third_party/README.md` explains how to fetch whisper.cpp and Kokoro-FastAPI clients without committing vendor code.

## Build, Test, and Development Commands
- `uv venv --python 3.10 --seed` — create the project environment.
- `uv pip install webrtcvad requests soundfile` — install Python dependencies used across modules.
- `uv run scripts/esp_audio_tester.py --port /dev/ttyACM0 record --seconds 5 --output esp_mic_test.wav` — confirm ESP capture and transport.
- `uv run scripts/vad_test.py esp_mic_test.wav` — inspect WebRTC VAD start/stop markers.
- `uv run scripts/asr_transcribe.py --binary third_party/whisper.cpp/build/bin/whisper-cli --model third_party/whisper.cpp/models/ggml-small.bin phrase01.wav` — verify Whisper.cpp integration.
- `uv run scripts/session_controller.py --max-cycles 1` — exercise the full Idle→Playback cycle with Kokoro streaming.

## Coding Style & Naming Conventions
- Python uses 4-space indentation, type hints, and descriptive module-level helpers; prefer `snake_case` filenames and `CamelCase` dataclasses.
- Arduino/C++ should keep constants in `constexpr`, document pin maps inline, and avoid heap allocation inside the audio loop.
- Logs emitted to stdout must remain single-line JSON to keep ingestion simple.

## Testing Guidelines
- Each module expects manual validation; capture WAVs, transcript files, or session logs before merging changes.
- Future automated coverage belongs under `tests/` mirroring package paths, using `pytest` and files named `test_<module>.py`.
- Attach session traces from `logs/sessions/session_*.jsonl` when reporting regressions or odd audio behavior.

## Commit & Pull Request Guidelines
- Use short imperative commits (e.g., `Add VAD noise gate`). Keep firmware, desktop, and doc updates isolated where practical.
- Reference roadmap identifiers (B1, C2, E1, etc.) in commit or PR descriptions and list the validation commands you ran.
- PRs should summarize observed behavior, link relevant artifacts, and note any follow-up risks or TODOs.

## Agent Workflow Tips
- Re-run `uv run <script> --help` after adding flags to confirm documentation.
- Update the matching `docs/C*_*.md` whenever behavior or validation steps change.
- Align work with the four-pass integration plan from `00_summary` to avoid cross-wiring modules prematurely.
