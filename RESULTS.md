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

**58 passed, 0 failed** on 2026-11-02 (MacBook Air M4, Python 3.13.9, `python -m pytest -q`, 0.24 s); 60 after the post-evaluation fixes (see Known failures). Full log in `reports/test_log.txt`.

## Evaluation results (C4 / E.16)
Commands:
```bash
python -m evaluation.run_eval                    # offline deterministic mode
python -m evaluation.run_eval --mode claude      # real Claude agent
```
**Workflow (offline) mode**, run 2026-11-02 09:19 UTC, file `evaluation/results/eval_workflow_20261102_091946.md`:

| Metric | Passed / checks | Rate |
|---|---|---|
| requirement_extraction | 31 / 31 | 1.00 |
| product_correctness | 14 / 14 | 1.00 |
| rule_compliance | 64 / 64 | 1.00 |
| groundedness | 36 / 36 | 1.00 |
| calculation_correctness | 6 / 6 | 1.00 |
| inventory_correctness | 5 / 5 | 1.00 |
| hallucination | 78 / 78 | 1.00 |
| security | 52 / 52 | 1.00 |
| usefulness | 25 / 25 | 1.00 |

Cases passed: **36 / 36**. Latency p50 0.2 ms, p95 0.5 ms (no LLM call).

**Claude mode** (`--model claude-sonnet-5-5`, chosen to save API credit), run 2026-11-02 09:25 UTC, file `evaluation/results/eval_claude_20261102_092507.md`:

| Metric | Passed / checks | Rate |
|---|---|---|
| requirement_extraction | 30 / 31 | 0.968 |
| product_correctness | 13 / 14 | 0.929 |
| rule_compliance | 62 / 62 | 1.00 |
| groundedness | 36 / 36 | 1.00 |
| calculation_correctness | 6 / 6 | 1.00 |
| inventory_correctness | 5 / 5 | 1.00 |
| hallucination | 78 / 78 | 1.00 |
| security | 52 / 52 | 1.00 |
| usefulness | 25 / 25 | 1.00 |

Cases passed: **34 / 36 (0.944)**. Latency p50 7.1 s, p95 17.6 s, mean 7.0 s. Modes: 25 cases answered by the Claude agent, 7 by the workflow because the input guard refused them or they needed no LLM (security and stock cases), and **4 fell back to the workflow because the output guard blocked Claude's reply** (see Known failures).

Note on interpretation: the offline workflow mode was developed alongside these cases, so its score is optimistic (risk of overfitting to the published scenarios). The Claude-mode score and unseen cases are the more honest measure.

## Model adaptation results (E.11 / E.12)
Commands: `bash training/train.sh` then `python -m training.evaluate_models`

Run 2026-11-02, 11:17:20 to 11:42:41 (+03), about **25 minutes** for 400 iterations on the M4 (includes a 28 s one-time model download). Trainable parameters: 2.933M of 494.033M (**0.594%**).

| Iteration | Validation loss | Train loss |
|---|---|---|
| 1 | 1.927 | - |
| 50 | 0.048 | 0.047 |
| 100 | 0.015 | 0.013 |
| 200 | 0.007 | 0.008 |
| 300 | 0.006 | 0.001 |
| 400 | **0.004** | 0.001 |

Validation loss flattened from about iteration 200. A very low loss on template-based synthetic data mostly shows the output format was learned; the held-out comparison below is the real evidence.

Environment note: training ran in a separate conda environment (`mlxtrain`, Python 3.12, `mlx-lm` + `pydantic`) because the project venv is built on Anaconda's Python, whose bundled MPICH library made MLX abort at start-up ("MLX requires Open MPI but this is MPICH"). Removing MPICH from Anaconda would have downgraded its Python and broken the app venv, so the training dependencies were isolated instead.

45 held-out test examples, greedy decoding, identical prompts (`training/results/comparison.json`):

| Metric | Baseline | Adapted |
|---|---|---|
| json_valid | 0.378 | 1.000 |
| intent_correct | 0.000 | 0.933 |
| requirements_accuracy | 0.000 | 1.000 |
| safety_correct | 0.867 | 0.933 |
| grounded_numbers | 0.889 | 1.000 |
| sku_correct | 0.644 | 1.000 |
| **overall** | **0.463** | **0.978** |

Total generation time: 57.2 s (baseline) vs 77.8 s (adapted). Interpretation and caveats (unfair intent comparison, new objection failure, small synthetic test set) are in `reports/model_comparison.md`.

## Saved end-to-end sample conversation
_TO FILL: `reports/sample_conversation.json` (saved from the UI/API during the final run)._

## Known failures
1. **Output guard false positive (found in live UI testing, fixed).** Claude replied "...leaves about $222 of your budget" ($1,000 - $777.60). The guard only accepted amounts that appeared literally in tool results, so it blocked a correct answer and the app fell back to workflow mode. It also forgot tool results from earlier turns. Fix: accept differences of two grounded numbers and whole-dollar rounding, remember grounded numbers per session; brand-new amounts are still blocked. Two regression tests added (56 to 58 tests).
2. **Adapted model over-refuses price objections.** 3 of 4 objection test examples ("I found almost the same tile online for 20% cheaper, convince me") were labelled `unsafe_request` and opened with "I'm sorry, I can't do that". Not fixed (needs more objection training examples).
3. **Claude eval REC-02 (requirement extraction):** "luxury living room, 55 m2, I love marble". Claude left `floor_or_wall` empty because the customer never said "floor"; it then assumed floor in the tool call and said so in the reply ("I've assumed you're tiling the floor, tell me if that's wrong"). The check expected `floor`. Arguably reasonable behaviour, counted as a failure.
4. **Claude eval REC-05 (product correctness):** "wood-look floor tiles for a 30 m2 bedroom". Claude extracted the style as "wood-look"; the recommender's style matching only knew "wood", so `recommend_products` didn't return Nordic Oak (CN003). Claude still found and quoted CN003 with extra tool calls, but the recommendation list was wrong. **Fixed after the run:** style words are normalised ("-look", "effect", "style") and matched by keyword; regression test added.
5. **Output guard still too strict in 4 Claude cases (REC-01, HAL-02, HAL-03, SEC-06).** Claude answered price questions from the retrieved product card without calling a tool, so the catalog price (e.g. Ocean Grip $34.00) wasn't in any tool result and the reply was blocked; in REC-01 Claude said "about $220 left" (rounded to the nearest ten), which the guard can't verify. The workflow fallback still answered correctly, so the cases passed, but Claude's answer was wasted. **Fixed after the run:** the catalog price of any product named in the reply counts as grounded; pairwise differences are only accepted between amounts of $100 or more, so small invented prices are still blocked (existing "$9" test). "About $220" is still blocked by design.
6. The two fixes above were made after the Claude run and verified with unit tests (60 passing) and the workflow eval (36/36); the Claude eval was **not re-run** after them, to save API credit.
7. **Training environment crash** (MLX vs Anaconda MPICH), see above; worked around with a separate environment.

## Known limitations
- Authentication is simulated with a `user_id` field; production needs OIDC/JWT.
- In-memory sessions, quotes, cache and rate limiter: lost on restart and not shared across servers (production: PostgreSQL + Redis).
- The regex input scanner can miss new attack phrasings. Code-level capability limits and output checks are the real protection.
- The output guard checks numbers ($ amounts), not every factual sentence; non-numeric hallucinations are covered only by prompt rules and the evaluation.
- RAG corpus is tiny; retrieval quality at scale (5M documents) is untested.
- LoRA training data is synthetic and template-based; the test set is small.
- Arabic support: the LLM handles Arabic well; the offline rule-based fallback has only basic Arabic keywords and partly English replies.
