# Model adaptation (E.11, E.12)

## Goal
Teach a small open-weight model the **behaviour** of a Ceramica Nova sales assistant: classify intent, extract requirements into JSON, ask for missing information, refuse rule-breaking requests, and only quote numbers that appear in the FACTS given by our tools. Prices and stock are **not** trained into the model (E.13): they are passed in as FACTS at run time.

## Choices

| Item | Choice | Why |
|---|---|---|
| Base model | `mlx-community/Qwen2.5-0.5B-Instruct-4bit` | Open weights (Apache-2.0), instruction-tuned, supports Arabic and English, small enough to train on a 16 GB MacBook in minutes |
| Method | LoRA adapters on a 4-bit quantized base (QLoRA-style) with Apple MLX (`mlx_lm.lora`) | Parameter-efficient: base weights frozen, only small adapter matrices trained; no GPU server needed |
| Hardware | MacBook Air M4, 16 GB unified memory | Candidate's own laptop |
| Hyperparameters | rank 8, scale 20, dropout 0.05, LoRA on last 16 of 24 layers, lr 1e-4, batch 4, 400 iterations, max length 1024, loss on the answer only (`mask_prompt`) | Standard small-model LoRA settings; enough steps for ~6-7 epochs over 238 examples; see `lora_config.yaml` |
| Training duration | about 25 minutes (11:17:20 to 11:42:41 on 2026-11-02, Apple M4 16 GB), final validation loss 0.004 | |

## Dataset (`generate_dataset.py`)
- **238 training, 27 validation, 45 test** examples (requirement: at least 100 and 20).
- Categories: product recommendation (English and Arabic), requirement discovery, objections, unavailable inventory, safe refusal / inappropriate requests, product comparisons, unsuitable product requests, off-topic.
- **How labels are made:** the customer message comes from a template filled with random values (space, area, budget, style, product). The *labels* (intent, requirements) come from those values, so they are exact. The *facts* (recommendations, inventory, suitability) come from our deterministic tools, and the target reply only uses numbers from those facts.
- **Quality controls:** no duplicate prompts; no prompt appears in two splits; the test split uses **held-out phrasings** never seen in training; a test checks that every target reply is grounded in its FACTS (`tests/test_training.py`); fixed seeds make the dataset reproducible.

## How to run
```bash
# If your Python comes from Anaconda and MLX aborts with "MLX requires Open MPI but this is MPICH",
# use a separate environment for training (what we did):
#   conda create -n mlxtrain python=3.12 -y && conda activate mlxtrain && pip install "mlx-lm>=0.20" pydantic
pip install -r requirements-train.txt
bash training/train.sh                     # dataset + training, log saved to training/logs/train_log.txt
python -m training.evaluate_models         # baseline vs adapted, same test set and settings
```

## Evaluation (E.12)
`evaluate_models.py` runs the **same 45 test prompts** through the base model and the base model + adapter with **identical greedy decoding** (temperature 0, max 400 tokens) and scores six metrics: JSON validity, intent accuracy, requirement-field accuracy, safety (refuses unsafe, doesn't refuse normal requests), grounded numbers (every $ amount appears in FACTS/customer text), and SKU correctness. It reports both scores, per-category results, the examples that improved most, and the examples that got worse or didn't change (no cherry-picking). Results: `reports/model_comparison.md`.

## Limitations
- Synthetic, template-based data: the model learns our phrasing, and real customer language is messier. The held-out test phrasings reduce, but don't remove, this optimism.
- The test set is small (45), so differences of a few percent are noise.
- Scores are automatic rule-based checks; they don't judge persuasiveness or tone.
- A 0.5B model is much weaker than Claude at open reasoning; in the app it is suitable as a cheap, local, offline component (extraction/first response), not as the main reasoning engine.
- The adapter only knows the current catalog's style of facts; the facts themselves always come from tools.
