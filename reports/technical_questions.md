# Section A: AI/ML/LLM Engineering Reasoning

## A1. Production model failure

Training 98.6%, validation 84.1%, first production 82.7%, after 90 days 64.3%.

**What the numbers say.** The 14.5-point gap between training and validation means the model **overfits**. The small validation-to-production drop (84.1 to 82.7) is normal. The big later fall (82.7 to 64.3) happened **over time**, which points to something changing after deployment rather than the model itself.

**Possible causes**

| # | Cause | Type | Why it fits |
|---|---|---|---|
| 1 | Overfitting (model memorised training data) | Model | 98.6 vs 84.1 gap |
| 2 | Data drift: the input distribution changed (new products, seasons, customer mix) | Data | Gradual drop over 90 days |
| 3 | Concept drift: the relationship between inputs and the label changed (e.g. customer behaviour changed) | Data | Same inputs now mean something else |
| 4 | Training/serving skew: features computed differently in production (different preprocessing, units, missing-value handling) | Infrastructure | Production pipeline is not the training pipeline |
| 5 | Broken upstream data pipeline: a field became null, renamed, or delayed | Infrastructure | Can cause a sudden drop inside the 90 days |
| 6 | Label/measurement problems: production "ground truth" is late, noisy or defined differently from training labels | Measurement | Accuracy may look worse than it really is |
| 7 | Feedback loop: the model's own decisions changed the data it now sees | Data | Common in recommendation and fraud systems |
| 8 | Data leakage in training/validation (a feature that leaked the label) | Model/Data | Inflated offline scores that production can't reach |

**How I would find the real cause (in order)**
1. **Check the measurement first.** Is production accuracy calculated with the same label definition, on a representative sample, with labels that have fully arrived? A wrong metric wastes every other step.
2. **Plot accuracy over time** (daily). A sudden step means a pipeline or deployment change: compare with the deploy and data-pipeline logs. A gradual slope means drift.
3. **Compare feature distributions** between training and recent production data (PSI, KS test, null rates, new categories). Big shifts point to data drift or a broken feature.
4. **Replay test:** run the same raw production records through the training feature pipeline and the serving pipeline. Different outputs mean training/serving skew.
5. **Slice the errors** by segment (product, region, channel, new versus old customers). If one segment collapsed, the cause is local.
6. **Retrain on recent labelled data** and evaluate on the latest weeks. If accuracy recovers, it was drift. If not, look at labels or features.
7. **Fix and prevent:** regularisation and simpler models for overfitting; drift monitoring with alerts; scheduled retraining; one shared feature pipeline for training and serving.

## A2. RAG versus fine-tuning for 300,000 frequently changing documents

**Fine-tuning on all documents is the wrong tool.** Fine-tuning teaches *behaviour and style*. It is a poor way to store *facts*:
- documents change often, so the model would be out of date the day after training, and every update means retraining;
- you can't cite the source, and you can't remove one document (for example a deleted or confidential one);
- the model may still hallucinate details it half-remembers;
- access control is impossible: every user would "see" every document inside the weights.

**What I would choose**

| Approach | When I would use it |
|---|---|
| Prompting | First step for any behaviour: clear instructions, examples, output format. Cheapest; change in minutes. |
| RAG | The answer for these 300k documents: index them, retrieve the relevant chunks per question, answer with citations. Updates are just re-indexing; permissions can be filtered per user. |
| Tool calling | Live or exact data: inventory, prices, account status, calculations, actions like sending a quote. |
| LoRA/QLoRA | When the model needs a consistent *behaviour* that prompting can't reliably get: domain tone, output format, classification labels, a smaller cheaper model doing a narrow task well. Cheap to train, can swap adapters. |
| Full fine-tuning | Rarely: very large, stable datasets, a big budget, a new language/domain the base model lacks, and you own the model. Expensive and risky (forgetting). |

**Recommendation to management:** RAG over the documents + tools for live data, prompt engineering for behaviour, and optionally a LoRA adapter if we need a cheaper small model for a narrow task. That is exactly the split used in this project.

## A3. LLM inference parameters

| Parameter | What it does in practice | Accuracy | Creativity | Memory | Latency | Cost |
|---|---|---|---|---|---|---|
| Temperature | Scales randomness when picking the next token. 0 = always the most likely token | Low temp = more consistent, better for facts and JSON | High temp = more varied | none | none | none |
| Top-p | Only samples from the smallest set of tokens whose probability adds up to p | Lower p = safer | Higher p = more diverse | none | none | none |
| Tokens | Units of text (around 3/4 of a word in English; Arabic often uses more tokens per word) | Too few output tokens truncates answers | - | More tokens = more KV cache | Output tokens dominate latency | Billed per input and output token |
| Context window | Maximum tokens the model can read at once | More relevant context helps, but irrelevant context distracts ("lost in the middle") | - | Grows with context (KV cache) | Longer prompts = slower first token | Input tokens cost money every call |
| Embeddings | Vectors that represent meaning; used for retrieval, not generation | Better embeddings = better retrieval = better grounded answers | - | Index size = docs x dimensions | Fast; one embedding call per query | Cheap, mostly one-off at indexing |
| KV cache | Stores attention keys/values of earlier tokens so they aren't recomputed | none | none | Uses lots of GPU memory for long contexts | Big speed-up for generation and repeated prefixes (prompt caching) | Provider prompt caching cuts input cost |
| Quantization | Stores weights in fewer bits (16 to 8 or 4) | Small loss, usually acceptable at 8-bit, more noticeable at 4-bit | none | 2-4x less memory | Often faster on limited hardware | Cheaper hardware |

In this project: temperature 0 for evaluation (repeatable), structured outputs for JSON, a 4-bit quantized base model for LoRA training on a laptop, and catalog data kept short in the context to save tokens.

## A4. Model routing strategy

Rule: use the **cheapest model that meets the quality bar for that task**, measured with evals, with an escalation path.

| Task | Model | Reasoning |
|---|---|---|
| 1. Greeting classification | Gamma (small) | Trivial pattern; latency matters most; rules or a tiny model are enough |
| 2. Product extraction | Gamma, escalate to Beta if validation fails or confidence is low | Structured output with schema validation makes errors detectable, so the cheap model is safe to try first |
| 3. Difficult customer reasoning | Alpha (large) | Multi-constraint trade-offs, objections, unusual cases; rare, so the high price affects few requests |
| 4. Summarization | Beta (medium) | Needs good language quality but not deep reasoning; long inputs make Alpha expensive ($15/M) |
| 5. Safety classification | Gamma as first filter + deterministic rules; borderline cases to Beta | Must run on every message, so cost and latency matter; never the only defence |
| 6. Simple FAQ responses | Gamma + RAG (or a cache) | Answer comes from retrieved documents; small model just phrases it |

Cost example: at 1M input tokens per day, sending everything to Alpha costs $15/day, everything to Gamma $0.30/day. Routing 80% to Gamma, 15% to Beta and 5% to Alpha costs about 0.24 + 0.45 + 0.75 = **$1.44/day**, roughly 10x cheaper than all-Alpha, with the large model kept for the requests that need it.
Safeguards: a fallback chain if a model fails, per-route eval sets to prove the small model is good enough, and monitoring per route.

In this repo the gateway's provider list and the `LLM_MODEL` setting are where routing plugs in. Today all Claude calls use one configurable model; per-task routing is a documented next step.

## A5. Is 95% good enough?

It depends on **what the 5% failures are and what they cost**. My decision framework:

1. **Severity of a failure.** A wrong tile colour suggestion is cheap. A wrong price on a binding quote, a false "in stock" promise, or leaking another customer's data is expensive or illegal.
2. **Failure type: false positives vs false negatives.** Blocking a legitimate customer (false positive) is annoying. Letting a prompt-injection through (false negative) can be serious. Look at the confusion matrix, not one accuracy number.
3. **User impact and volume.** 5% of 100,000 conversations is 5,000 bad experiences per day.
4. **Reversibility.** A chat suggestion can be corrected; an automatically sent quote or an order cannot easily be undone.
5. **Confidence and calibration.** Can the system tell when it is unsure? If low-confidence cases can be routed to humans, the effective error rate drops.
6. **Human oversight.** Is there a human approval step for risky actions (as with discounts above 12% here)?
7. **Monitoring and rollback.** Can we detect degradation quickly and roll back?
8. **Comparison with the current process.** If human staff are at 90%, 95% may already be an improvement.
9. **Evaluation quality.** Is the 95% measured on representative, recent, adversarial data, or on an easy test set?

**My answer:** I would approve a **limited, staged deployment** (shadow mode, then a small percentage of traffic) only if the critical failure categories (prices, stock, data leakage, safety) are at or near 0% because they are enforced in deterministic code, with human approval for irreversible actions, monitoring, and a rollback plan. I would not approve full automatic deployment based on one 95% number.

## A6. AI engineering judgment

| # | Problem | Approach | Justification |
|---|---|---|---|
| 1 | Calculate tile quantities | Deterministic software | Exact arithmetic with rounding rules; must be correct every time and testable (our `calculate_tile_quantity`) |
| 2 | Predict which customers may churn | Classical ML | Tabular history (purchases, recency, complaints) to probability; gradient boosting or logistic regression is cheaper, explainable and measurable |
| 3 | Answer questions from company policy documents | RAG | Policies change; answers must cite sources; no retraining needed |
| 4 | Automatically retrieve current inventory | Deterministic software (a tool/API call) | Live authoritative data; an LLM must never guess it (BR-04) |
| 5 | Generate sales recommendations | Hybrid | Deterministic filtering and ranking on hard rules + LLM to understand needs and explain the choice (our recommender + agent) |
| 6 | Calculate taxes | Deterministic software | Legal rules, exact numbers, auditable |
| 7 | Extract structured information from customer messages | LLM with structured output (optionally fine-tuned) | Free text in two languages; schema validation catches bad output; a fine-tuned small model can do it cheaper |
| 8 | Automatically send quotations after customer approval | Agent + deterministic workflow with human-in-the-loop | The agent decides *when* to call the tool, but the quote is created by code, approval is a recorded customer action, and discounts above 12% need a human |
