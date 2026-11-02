import os

# Tests must never call the paid API or depend on the network.
os.environ["LLM_PROVIDER"] = "mock"
os.environ.pop("SIMULATE_FLAKY", None)
os.environ["RAG_DENSE"] = "0"     # keyword retrieval only in tests: no model download needed
