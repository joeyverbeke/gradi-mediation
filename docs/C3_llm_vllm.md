# vLLM Transform Module (C3)

## Overview
The `llm` package sends ASR transcripts to the local vLLM server (OpenAI-compatible API) and returns "make-it-perfect" rewrites. Prompts are wrapped in a stable system/user template that preserves speaker intent, tightens grammar, and enforces a conservative length cap using `max_tokens` plus an additional character guardrail. Defaults target the running instance started with:

```bash
uv run vllm serve hugging-quants/Meta-Llama-3.1-8B-Instruct-GPTQ-INT4 \
  --quantization gptq_marlin \
  --dtype half \
  --max-model-len 4096 \
  --gpu-memory-utilization 0.82 \
  --tensor-parallel-size 1 \
  --enforce-eager
```

## Module Entry Points
- `llm.VLLMConfig`
  - Holds the server base URL (`http://127.0.0.1:8000/v1` by default), model name, prompt template, and sampling knobs.
  - Adjust `max_tokens` to constrain output length or override the user prompt template when we refine guidance.
- `llm.VLLMTransformer`
  - Provides `.transform(text)` for single strings and `.transform_batch(iterable)` for lists.
  - Returns a `TransformResult` containing the cleaned text and the raw JSON payload for debugging.

Install the HTTP client dependency once per environment:

```bash
uv pip install requests
```

## Desktop Test Script
`scripts/llm_transform.py` mirrors the validation protocol by converting transcript lists into JSONL pairs:

```bash
# Assuming vLLM is already running on port 8000
uv run scripts/llm_transform.py \
  --base-url http://127.0.0.1:8000/v1 \
  --model hugging-quants/Meta-Llama-3.1-8B-Instruct-GPTQ-INT4 \
  --output llm_pairs.jsonl \
  asr_results.txt
```

Key details:
- Input files are parsed by blocks. Lines starting with `#` are ignored so `asr_results.txt` from step C2 works directly.
- Add extra samples inline with `--text "raw transcript here"`.
- Each JSONL line in `llm_pairs.jsonl` contains `{ "input": ..., "output": ... }` for manual review.

## Validation Guidance
1. Gather the 10 ASR outputs (e.g., from `asr_results.txt`).
2. Run the script above to generate `llm_pairs.jsonl`.
3. Manually inspect every pair, verifying that the perfected text preserves intent and stays within the expected brevity.

If verbosity drifts, lower `--max-tokens` or tighten the user prompt (`VLLMConfig.user_prompt_template`). For edge cases (e.g., empty transcripts), the transformer currently returns the model's direct response—future work may short-circuit and echo blanks.
