# Kokoro Streaming TTS Module (D1)

## Overview
The `tts` package wraps Kokoro-FastAPI's OpenAI-compatible `/v1/audio/speech` endpoint so we can capture playable audio chunks as soon as they arrive. The client exposes a generator (`KokoroStreamer.stream_synthesis`) that yields raw `SynthesisChunk` objects followed by a completion record, and a convenience method to write straight to disk while collecting latency metrics.

Kokoro-FastAPI should be running locally (default `http://127.0.0.1:8880`). Use the Docker images or `start-gpu.sh`/`start-cpu.sh` from the upstream repo. The API documentation is available at `http://127.0.0.1:8880/docs`.

## Module Entry Points
- `tts.KokoroConfig`
  - Configure base URL, endpoint (`/audio/speech`), model name (`kokoro`), voice (`af_bella`, mixes, etc.), response format (wav/mp3/pcm…), chunk size, and any additional JSON payload fields.
- `tts.KokoroStreamer`
  - `.stream_synthesis(text)` yields audio chunks immediately; the final chunk includes total bytes and elapsed time.
  - `.stream_to_file(text, path)` saves the output and returns a `SynthesisMetadata` summary (chunk count, first-chunk latency, content type).

Install the HTTP client dependency once per environment:

```bash
uv pip install requests
```

## Desktop Test Script
`scripts/tts_stream.py` mirrors the validation protocol and defaults to `tts_stream.wav`:

```bash
uv run scripts/tts_stream.py \
  --base-url http://127.0.0.1:8880/v1 \
  --voice af_bella \
  --response-format wav \
  --text "When the sun set, the city lights shimmered like a second sky." \
  --output tts_stream.wav
```

Key options:
- Accepts text via `--text`, `--text-file`, or stdin.
- Forward Kokoro-specific payloads with `--extra key=value` (repeatable), e.g. `--extra normalization_options='{"normalize": false}'`.
- `--progress-every` controls how often chunk statistics print while streaming.

## Validation Guidance
1. Synthesize a ~10 s sentence and write `tts_stream.wav`.
2. Confirm the first chunk log appears quickly (<1 s ideal) and note total elapsed time and bytes in the CLI summary.
3. Listen to the WAV; prosody should sound natural with no audible seams between chunks.

If early chunks take longer than expected or seams are audible, experiment with `--chunk-bytes` (smaller values often reduce latency) and Kokoro's response format (`pcm` vs `wav`), then retest.
