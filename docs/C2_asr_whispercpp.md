# Whisper.cpp ASR Orchestrator (C2)

## Overview
The `asr` module wraps the whisper.cpp CLI so desktop code can ship VAD-gated PCM segments to a local small ggml model and receive plain-text transcripts. The orchestrator writes temporary WAVs when handed raw PCM, shells out to the whisper.cpp binary, parses the JSON output, and returns the combined text plus per-segment snippets.

## Module Entry Points
- `asr.WhisperCppConfig` & `asr.WhisperCppTranscriber`
  - Configure paths to the whisper.cpp executable and the small ggml model.
  - Call `transcribe_pcm(pcm_bytes)` for in-memory segments or `transcribe_file(path)` for existing WAV files.
  - Each invocation yields a `TranscriptionResult` with the flattened transcript and the original JSON payload for downstream inspection.

## Desktop Test Script
Use `scripts/asr_transcribe.py` to mirror the human validation flow:

```bash
# 1. Record 10 fixed phrases with the ESP bridge from step B1
uv run scripts/esp_audio_tester.py --port /dev/gradi-esp-mediate record --seconds 5 --output phrase01.wav
...

# 2. Transcribe all WAVs with whisper.cpp
uv run scripts/asr_transcribe.py \
  --binary /path/to/whisper.cpp/build/bin/main \
  --model /path/to/models/ggml-small.bin \
  phrase01.wav phrase02.wav ...
```

- The script prints each transcription to stdout and writes `asr_results.txt` (unless overridden via `--output`).
- Depending on how you build whisper.cpp, the binary may live at `whisper.cpp/build/bin/main` (CMake) or `whisper.cpp/bin/main` (Makefile). Update the `--binary` path accordingly.
- Add extra whisper.cpp flags with `--extra-arg`, e.g., `--extra-arg --print-timestamps`.

## Validation Guidance
1. Speak a fixed list of 10 phrases into the ESP microphone bridge (capturing distinct WAV files).
2. Run the ASR script above and inspect `asr_results.txt`.
3. Manually compare each line against the ground-truth phrases and note deviations.

Latency spikes usually correlate with very long segments; keeping the VAD window near 5 s keeps whisper.cpp responsive on desktop CPUs. Use the smaller ggml model as specified to balance accuracy and throughput.
