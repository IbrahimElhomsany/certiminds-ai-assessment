import os

# Tests must never call the paid API or depend on the network.
os.environ["LLM_PROVIDER"] = "mock"
os.environ.pop("SIMULATE_FLAKY", None)
os.environ["RAG_DENSE"] = "0"     # keyword retrieval only in tests: no model download needed
os.environ["DATABASE_URL"] = "sqlite://"   # in-memory database: tests never touch data/nova_studio.db
os.environ["JWT_SECRET"] = "test-secret-not-for-production-0123456789"
os.environ["BCRYPT_ROUNDS"] = "4"         # fast hashing in tests only
