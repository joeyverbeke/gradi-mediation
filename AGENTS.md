# Repository Guidelines

## Project Structure & Module Organization
- `firmware/` holds the ESP32-S3 sketches; keep minimal I/O logic here.
- `scripts/` contains Python CLIs for validation (`esp_audio_tester.py`, `vad_test.py`, `asr_transcribe.py`, `llm_transform.py`).
- `desktop_vad/`, `asr/`, and `llm/` expose importable desktop modules for VAD, Whisper.cpp orchestration, and vLLM transforms.
- `docs/` stores step-by-step module notes (C1–C3), while `third_party/README.md` explains how to fetch external dependencies.
- Audio artifacts, temp results, and venv files are gitignored—store review outputs (e.g., `llm_pairs.jsonl`) locally unless required for a report.

## Build, Test, and Development Commands
- `uv venv --python 3.10 --seed` — create the project virtual environment.
- `uv pip install webrtcvad requests` — install common Python dependencies.
- `uv run scripts/esp_audio_tester.py --help` — inspect ESP record/playback options.
- `uv run scripts/vad_test.py esp_mic_test.wav` — run desktop VAD against a captured WAV.
- `uv run scripts/asr_transcribe.py --binary third_party/whisper.cpp/build/bin/whisper-cli --model third_party/whisper.cpp/models/ggml-small.bin phrase01.wav` — invoke Whisper.cpp on sample audio.
- `uv run scripts/llm_transform.py asr_results.txt --output llm_pairs.jsonl` — send transcripts to vLLM and produce review pairs.

## Coding Style & Naming Conventions
- Python: 4-space indentation, type hints, f-strings, and concise helper functions; follow existing module layout (`*_client.py`, `*_processor.py`).
- Arduino/C++: keep includes minimal, prefer `constexpr` for hardware constants, and document pin maps inline.
- Filenames should describe functionality (`esp_audio_tester.py`, `whisper_cpp.py`); avoid spaces or camelCase in new files.

## Testing Guidelines
- Manual validation is required per module: listen to recorded WAVs, inspect VAD timestamps, review ASR transcripts, and audit vLLM rewrites.
- When adding automated tests, colocate them under `tests/` mirroring module paths and name the files `test_<module>.py`.
- Capture any new human-validation procedure in the relevant `docs/C*.md` entry.

## Commit & Pull Request Guidelines
- Use short imperative commit messages (e.g., “Add vLLM transform CLI”).
- Group related changes per JSON milestone; avoid mixing firmware and desktop updates in one commit.
- PRs should link the tracked module ID (C1, C2, etc.), summarize validation evidence, and note any manual steps (commands run, WAVs reviewed).
