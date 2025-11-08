# Session Controller Specification (E1)

## Purpose and Scope
The session controller coordinates the speech loop described in the overall project: detect voiced segments from the continuously captured ESP32-S3 microphone stream, transcribe and perfect them, synthesize a reply, and stream playback to the ESP speakers. Its responsibilities are to:
- Own the authoritative state machine (`Idle → CaptureRequested → ASR → LLMTransform → TTSSynthesis → Playback → ReturnToIdle` with `ErrorTimeout`).
- Decide when to carve audio segments, when to stop forwarding mic data, and when to release the speakers so that mic and speakers never contend.
- Invoke the desktop modules (VAD, Whisper.cpp, vLLM, Kokoro-FastAPI) in sequence, handing data between them and back to the ESP.
- Emit structured logs with timestamps for traceability and validation.

## Operating Assumptions
- The ESP firmware continuously streams PCM microphone frames over USB CDC. Desktop VAD is the only authority for when to mark start/stop.
- Playback uses the existing serial protocol confirmed in B1; as soon as TTS chunks arrive they are piped directly to the ESP player.
- All external services are already running: whisper.cpp binary accessible, vLLM on `:8000`, Kokoro-FastAPI on `:8880/v1`.
- Controller runs in Python on the same uv-managed environment as prior modules.

## State Machine Details
| State | Entry Actions | Exit Condition / Event | Next State |
|-------|---------------|-------------------------|------------|
| Idle | Flush buffers, ensure mic stream reader is armed, set `resource.mic=available`, `resource.spk=available` | VAD detects consecutive speech frames (CaptureRequest event) | CaptureRequested |
| CaptureRequested | Mark `resource.mic=owned`, start segment buffer, log `capture_start` timestamp | VAD stop trigger fires (segment end) or timeout | ASR (if segment) / ErrorTimeout |
| ASR | Freeze mic buffer for segment, write WAV/PCM to disk tmp, invoke `WhisperCppTranscriber.transcribe_pcm` | Success → transcript ready; Failure → ErrorTimeout | LLMTransform / ErrorTimeout |
| LLMTransform | Call `VLLMTransformer.transform` with transcript, log completion | Success → text ready; Failure → ErrorTimeout | TTSSynthesis / ErrorTimeout |
| TTSSynthesis | Acquire `resource.spk`, pause mic forwarding to ESP (keep buffering to avoid loss), start Kokoro stream via `KokoroStreamer.stream_synthesis`; hand chunks to playback queue | First chunk arrives (log `tts_first_chunk` event). Completion triggers Playback. Errors → ErrorTimeout | Playback / ErrorTimeout |
| Playback | Push Kokoro audio chunks to ESP playback channel; log `playback_start` and `playback_end` | ESP signals playback done or chunk stream ends | ReturnToIdle |
| ReturnToIdle | Release `resource.spk`, discard temp files, unpause mic forwarding | Clean-up complete | Idle |
| ErrorTimeout | Cancel in-flight operations, release resources, log error reason | Recovery complete | Idle |

## Event Flow and Resource Ownership
- **CaptureRequest**: triggered by `WebRTCVADProcessor` when speech frames exceed start threshold. Controller notes start timestamp and begins collecting PCM into a ring buffer dedicated to the current session while the ESP remains in streaming mode.
- **SegmentComplete**: triggered when VAD sees sufficient trailing silence. Mic ownership remains with controller until segment is processed. No new playback may start until `resource.mic` is released.
- **TTSReady**: Kokoro stream begins. Controller transitions resources: `resource.mic=paused`, `resource.spk=owned`, issues `PAUSE` to the ESP bridge so no mic audio is emitted during playback.
- **PlaybackDone**: fired when Kokoro stream final chunk delivered and ESP playback completes; controller sends `RESUME` to re-enable mic streaming after a short guard delay.
- **Timeout/Error**: any stage exceeding its SLA (e.g., ASR > 15 s, vLLM > 20 s, Kokoro first chunk > 5 s, ESP playback stall) transitions to ErrorTimeout, logs cause, and hard-resets resources.

## Module Integration
- **Microphone ingestion**: background task that continually reads ESP PCM frames into a rolling buffer. When controller enters CaptureRequested it locks the segment start index; on SegmentComplete it slices the buffer for VAD-defined start/end.
- **ASR**: use `WhisperCppTranscriber.transcribe_pcm` with sample rate 16 kHz. Intermediate WAVs stored under `tmp/controller/<timestamp>/` for debugging.
- **LLM**: `VLLMTransformer.transform` keeps response under configured max tokens; controller enforces fallback (if LLM output empty, re-run once or echo raw transcript).
- **TTS**: `KokoroStreamer.stream_synthesis` yields audio chunks; controller forwards each chunk to ESP playback command channel immediately, ensuring mic stream remains paused until final chunk flushed.
- **ESP playback**: reuse B1 protocol (`START sample_rate channels bits length` + raw stream + `END`). Controller sequences commands so the ESP never receives `START` while mic capture segment is active.

## Logging & Telemetry
- Structured log per transition with ISO timestamp, state name, session UUID, and metadata (duration, byte counts, transcript length).
- Additional markers: `event.capture_start`, `event.capture_end`, `asr.latency_ms`, `llm.latency_ms`, `tts.first_chunk_latency_ms`, `playback.duration_ms`.
- In case of ErrorTimeout, log error type and recovery actions. Ensure final line per cycle reads `state=Idle resources=mic:free spk:free`.

## Failure Handling & Timeouts
- Guard durations: CaptureRequested <= 6 s, ASR <= 15 s, LLM <= 20 s, TTS first chunk <= 5 s, Playback <= 20 s.
- On timeout: stop reading Kokoro stream, send ESP `END` to halt playback, drop buffered PCM, and transition to ErrorTimeout.
- Ensure controller waits for VAD to quiet for at least 200 ms before releasing mic to avoid immediate re-trigger from trailing noise.

## Validation Procedure
1. Start ESP firmware, vLLM, and Kokoro services.
2. Launch the controller:
   ```bash
   uv run scripts/session_controller.py \
     --port /dev/gradi-esp-mediate \
     --whisper-binary third_party/whisper.cpp/build/bin/whisper-cli \
     --whisper-model third_party/whisper.cpp/models/ggml-small.bin \
     --max-cycles 5
   ```
3. Speak five distinct sentences spaced apart so Idle periods exist between them.
4. Observe logs: each cycle must progress through CaptureRequested → ... → ReturnToIdle with timestamps and no overlapping mic/speaker ownership (JSONL copies are written to `logs/sessions/session_*.jsonl`).
5. After each playback verify audible response via ESP speakers; confirm final log line after five cycles indicates `Idle` with resources free.

## Open Questions / Follow-ups
- Confirm ESP playback API guarantees acknowledgement after final chunk (if not, add explicit ACK timeout).
- Decide whether to buffer transcripts for auditing (store under `logs/sessions/SESSION_ID/`).
- Evaluate whether controller should support multi-turn queueing (future work) versus strictly serial cycles.
