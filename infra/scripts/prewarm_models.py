"""One-shot model prewarm for the shared HuggingFace cache volume.

Runs as the `prewarm` service in infra/docker-compose.prod.yml (api image,
HF_HUB_OFFLINE unset) BEFORE the api/worker start. Downloads the three
inference models into HF_HOME (the `sermon-hf-cache` volume) so the runtime
containers — which run with HF_HUB_OFFLINE=1 — load deterministically with
zero network and never pay a multi-GB download on a user's first request.

Model ids MUST stay in sync with the in-process loaders:
  - BAAI/bge-large-en-v1.5            worker/embedding.py (+ chunking.py via
                                      llama-index; same on-disk hub snapshot)
  - cross-encoder/ms-marco-MiniLM-L-6-v2   api/rerank.py
  - BAAI/bge-m3                       api/highlight.py

Idempotent: warm cache entries are revalidated, not re-downloaded. Exits
non-zero on any failure so deploy.sh aborts before flipping traffic.
"""

import time

from sentence_transformers import CrossEncoder, SentenceTransformer

MODELS: list[tuple[str, type[CrossEncoder] | type[SentenceTransformer]]] = [
    ("BAAI/bge-large-en-v1.5", SentenceTransformer),
    ("cross-encoder/ms-marco-MiniLM-L-6-v2", CrossEncoder),
    ("BAAI/bge-m3", SentenceTransformer),
]


def main() -> None:
    for name, loader in MODELS:
        start = time.monotonic()
        print(f"prewarm: loading {name} …", flush=True)
        loader(name, device="cpu")
        print(f"prewarm: {name} ready in {time.monotonic() - start:.1f}s", flush=True)
    print("prewarm: all models cached", flush=True)


if __name__ == "__main__":
    main()
