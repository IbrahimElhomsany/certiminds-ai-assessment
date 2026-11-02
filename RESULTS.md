# RESULTS: evidence of execution

> Every number in this file must come from a real run on the candidate's machine.
> Sections marked _TO FILL_ are completed only after running the command shown.

## Environment
- Hardware: MacBook Air, Apple M4, 16 GB RAM, macOS 26.3
- Python 3.13.9 (virtual environment `.venv`)
- LLM: Claude API, model `claude-opus-5-5` (via `LLM_MODEL`)
- Training: MLX-LM, base `mlx-community/Qwen2.5-0.5B-Instruct-4bit`

## How the application was executed
```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn backend.main:app --reload       # UI: http://127.0.0.1:8000  API docs: /docs
```

## Test results
Command: `python -m pytest -v | tee reports/test_log.txt`

_TO FILL: number passed / failed, date. Full log in `reports/test_log.txt`._

## Evaluation results (C4 / E.16)
Commands:
```bash
python -m evaluation.run_eval                    # offline deterministic mode
python -m evaluation.run_eval --mode claude      # real Claude agent
```
_TO FILL from `evaluation/results/eval_*.md`: case pass rate and per-metric table for both modes._

Note on interpretation: the offline workflow mode was developed alongside these cases, so its score is optimistic (risk of overfitting to the published scenarios). The Claude-mode score and unseen cases are the more honest measure.

## Model adaptation results (E.11 / E.12)
Commands: `bash training/train.sh` then `python -m training.evaluate_models`

_TO FILL from `training/logs/train_log.txt`: training time, initial and final train/validation loss._

_TO FILL from `reports/model_comparison.md`: baseline vs adapted scores per metric._

## Saved end-to-end sample conversation
_TO FILL: `reports/sample_conversation.json` (saved from the UI/API during the final run)._

## Known failures
_TO FILL with real failures from the evaluation and tests (e.g. failed eval cases and why)._

## Known limitations
- Authentication is simulated with a `user_id` field; production needs OIDC/JWT.
- In-memory sessions, quotes, cache and rate limiter: lost on restart and not shared across servers (production: PostgreSQL + Redis).
- The regex input scanner can miss new attack phrasings. Code-level capability limits and output checks are the real protection.
- The output guard checks numbers ($ amounts), not every factual sentence; non-numeric hallucinations are covered only by prompt rules and the evaluation.
- RAG corpus is tiny; retrieval quality at scale (5M documents) is untested.
- LoRA training data is synthetic and template-based; the test set is small.
- Arabic support: the LLM handles Arabic well; the offline rule-based fallback has only basic Arabic keywords and partly English replies.
