# third_party

This directory is reserved for locally cloned dependencies that we do not commit to the repository.

## whisper.cpp
1. Clone into this folder: `git clone https://github.com/ggerganov/whisper.cpp.git`.
2. Build with `make -j$(nproc)` or the CMake flow per upstream docs.
3. Download the small multilingual ggml model: `./models/download-ggml-model.sh small`.
4. Point the ASR scripts at `third_party/whisper.cpp/build/bin/whisper-cli` and the downloaded model.

Add additional dependency notes here as we grow the project.

## faster-whisper
1. Create a workspace: `mkdir -p third_party/faster-whisper`.
2. Install deps inside the project venv: `uv pip install faster-whisper soundfile`.
3. Download a model (example) into `third_party/faster-whisper/models/`:
   ```bash
   cd third_party/faster-whisper
   uv run python - <<'PY'
   from faster_whisper import download_model
   download_model("small", output_dir="models")
   PY
   ```
4. Run the latency sanity-check script: `uv run scripts/faster_whisper_test.py phrase01.wav --device cuda`.
5. Point new ASR integrations at `third_party/faster-whisper/models` (or any model directory you downloaded).

## vosk
1. Create the model folder: `mkdir -p third_party/vosk/models`.
2. Download and extract a model, e.g.:
   ```bash
   cd third_party/vosk/models
   curl -LO https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
   unzip vosk-model-small-en-us-0.15.zip
   ```
3. Install the Python bindings inside the venv: `uv pip install vosk soundfile numpy`.
4. Validate the install with: `uv run scripts/vosk_test.py phrase01.wav --model-dir third_party/vosk/models/vosk-model-small-en-us-0.15`.
5. Point the ASR scripts or session controller at the extracted directory via `--vosk-model-dir`.
