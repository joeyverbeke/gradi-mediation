# third_party

This directory is reserved for locally cloned dependencies that we do not commit to the repository.

## whisper.cpp
1. Clone into this folder: `git clone https://github.com/ggerganov/whisper.cpp.git`.
2. Build with `make -j$(nproc)` or the CMake flow per upstream docs.
3. Download the small multilingual ggml model: `./models/download-ggml-model.sh small`.
4. Point the ASR scripts at `third_party/whisper.cpp/build/bin/whisper-cli` and the downloaded model.

Add additional dependency notes here as we grow the project.
