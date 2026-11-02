"""Dataset quality checks (E.11) and the comparison scorer (E.12)."""
import json

from training.evaluate_models import aggregate, load_split, score
from training.generate_dataset import build


def test_dataset_sizes_meet_minimums():
    train, _ = load_split("train")
    valid, _ = load_split("valid")
    test, _ = load_split("test")
    assert len(train) >= 100 and len(valid) + len(test) >= 20


def test_no_prompt_leaks_between_splits():
    prompts = {n: {r["messages"][1]["content"] for r in load_split(n)[0]} for n in ["train", "valid", "test"]}
    assert not prompts["train"] & prompts["test"] and not prompts["train"] & prompts["valid"]


def test_targets_only_use_numbers_from_facts():
    train, _ = load_split("train")
    for r in train:
        target = json.loads(r["messages"][2]["content"])
        s = score(r["messages"][1]["content"], target, r["messages"][2]["content"])
        assert s["grounded_numbers"] == 1.0, target["reply"]


def test_all_required_categories_present():
    _, meta = load_split("train")
    cats = {m["category"] for m in meta}
    assert {"recommendation", "discovery", "objection", "inventory", "refusal", "comparison", "unsuitable"} <= cats


def test_scorer_rewards_gold_and_penalises_invented_prices():
    rows, _ = load_split("test")
    gold = [score(r["messages"][1]["content"], json.loads(r["messages"][2]["content"]), r["messages"][2]["content"]) for r in rows]
    assert aggregate(gold)["overall"] == 1.0
    bad = score(rows[0]["messages"][1]["content"], json.loads(rows[0]["messages"][2]["content"]),
                '{"intent": "recommendation_request", "reply": "Only $1 per m2!"}')
    assert bad["grounded_numbers"] == 0.0


def test_generation_is_reproducible():
    a = [e["messages"][1]["content"] for e in build(20, "train", seed=7)]
    b = [e["messages"][1]["content"] for e in build(20, "train", seed=7)]
    assert a == b
