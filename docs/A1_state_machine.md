# Session State Machine Spec (Desktop VAD Authority)

## Overview
The mediation desktop orchestrates a tightly ordered loop that starts and ends in `Idle`. The ESP32-S3 never performs VAD; it only captures microphone audio when instructed and plays audio when commanded. Whisper.cpp, vLLM, and Kokoro-FastAPI each consume the previous stage’s output, and only one subsystem may hold the microphone or speakers at a time. Timeouts route the flow into `ErrorTimeout`, where the desktop releases every resource before re-arming the VAD gate.

## Resource Ownership by State
| State | Purpose | Mic Owner | Speaker Owner | Entry Actions | Exit Trigger |
| --- | --- | --- | --- | --- | --- |
| `Idle` | System quiescent, VAD armed, ready for capture request | Desktop (ready to command ESP) | Desktop (ready) | Ensure ESP mic and DAC are idle; arm desktop VAD | Desktop issues `CaptureRequested` or `VADArmed` self-check |
| `CaptureRequested` | Desktop requests ESP to stream raw audio | ESP capture task (mic active, exclusive) | Desktop (idle) | Send `START_CAPTURE` to ESP; begin buffering PCM | Capture window closed or timeout |
| `ASR` | Whisper.cpp transcribes buffered audio | Desktop (mic stopped) | Desktop (idle) | Seal capture file, release mic; invoke Whisper.cpp | Transcript ready or timeout |
| `LLMTransform` | vLLM refines transcript into response | Desktop | Desktop | Pass sanitized transcript to vLLM | Response ready or timeout |
| `TTSSynthesis` | Kokoro-FastAPI creates speech audio | Desktop | Desktop (buffering) | Submit response text to TTS; reserve speaker bus | Audio buffer ready or timeout |
| `Playback` | ESP plays synthesized speech | Desktop (mic halted) | ESP playback task (speakers active) | Send `STOP_CAPTURE`; stream synthesized PCM to ESP | Playback finished/ack or timeout |
| `ReturnToIdle` | Post-playback cleanup | Desktop | Desktop | Confirm playback stop, flush buffers, re-enable mic control | Cleanup complete, VAD rearmed, or timeout |
| `ErrorTimeout` | Recovery from timeout/error at any stage | Desktop | Desktop | Halt outstanding jobs; send `STOP_CAPTURE`/`STOP_PLAYBACK`; clear pipelines | Manual or automatic reset after recovery period |

## Transitions (Eight Human-Facing Steps)
Each step corresponds to an observable console log or audible outcome to satisfy the validation protocol.
| Step | Event & Validation | From → To | Resulting Actions |
| --- | --- | --- | --- |
| 1 | `DesktopCaptureCommand` (operator clicks/taps capture) | `Idle` → `CaptureRequested` | Desktop log: “Capture requested”; ESP starts microphone streaming. |
| 2 | `CaptureBufferClosed` (desktop VAD stops, ESP confirms file length) | `CaptureRequested` → `ASR` | Mic stream sealed; whisper.cpp invoked; log: “ASR started”. |
| 3 | `TranscriptReady` (whisper.cpp writes transcript) | `ASR` → `LLMTransform` | Log contains transcript snippet; mic remains idle. |
| 4 | `ResponseReady` (vLLM returns text) | `LLMTransform` → `TTSSynthesis` | Response text logged; TTS request queued. |
| 5 | `SpeechReady` (Kokoro returns PCM/stream URL) | `TTSSynthesis` → `Playback` | Desktop stops mic (if not already) and pushes audio to ESP; speaker ownership shifts to ESP. |
| 6 | `PlaybackComplete` (ESP playback ack or audible finish) | `Playback` → `ReturnToIdle` | Console event “Playback finished”; speaker bus released. |
| 7 | `SessionCleanupComplete` (buffers flushed, mic re-armed) | `ReturnToIdle` → `Idle` | Desktop confirms VAD armed; mic ownership returns to desktop controller. |
| 8 | `VADArmedHeartbeat` (operator-visible log every cycle) | `Idle` → `Idle` | Self-loop showing Idle is stable and ready for next capture; provides the 8th read-through validation step.

The eight steps above form a closed loop that can be read sequentially, providing a one-to-one mapping between the operator’s checklist and the state transitions that return the system to `Idle`.

## Timeout and Error Paths
- Any active state (`CaptureRequested`, `ASR`, `LLMTransform`, `TTSSynthesis`, `Playback`, `ReturnToIdle`) that exceeds its watchdog limit triggers `TimeoutExceeded` → `ErrorTimeout`.
- `ErrorTimeout` immediately revokes mic and speaker rights from the ESP, cancels Whisper/vLLM/Kokoro jobs, and emits an “error timeout” log for human review.
- Recovery uses `OperatorReset` or `AutoReset` to transition `ErrorTimeout` → `Idle`, reinitializing subsystems before the next capture attempt.

## Ownership Guarantees
- Microphone is active only in `CaptureRequested`; it is explicitly halted as part of the transition into `ASR` and reaffirmed before `Playback`.
- Speakers are driven only in `Playback`; all other states keep the DAC silent.
- `ReturnToIdle` ensures both mic and speakers are idle before the VAD heartbeat (Step 8) confirms readiness.

## Desktop VAD Authority
- Desktop VAD determines the boundaries of capture and is the sole source of `CaptureBufferClosed`.
- ESP never self-triggers capture or playback; it responds to desktop commands, preventing overlap.

## Validation Hooks
- Each transition generates a deterministic log string and/or audible result (recorded WAV or playback) so a human can verify the progression.
- Watchdog timers surface in logs to aid human inspection during `ErrorTimeout` incidents.
