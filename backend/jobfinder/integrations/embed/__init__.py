"""embed: the local embedding model (#126), `bge-small-jobfinder-v2` by default, run on CPU in worker-embed.

The model ships as a directory (EMBED_MODEL_DIR) holding the ONNX file, its tokenizer and
`jobfinder_contract.json`, exported by the jobfinder-embedding-model repo. The contract is the spec:
the model name stored next to every vector, the prompt each side gets, and how each text is built
(services/matching/embed_text.py, copied verbatim from that repo). Vectors are unit length, so cosine
similarity is a dot product and pgvector's `<=>` is 1 - cosine.

Two sides, one space:
    embed_documents  postings (`job_doc`): the contract's posting prompt (none for v2)
    embed_queries    profile queries (`profile_queries`): the contract's profile_query prompt

`get_embedder()` checks the contract (contract.py: name equals EMBED_MODEL, CLS pooling, normalised,
384-d) and loads the model once per process (fastembed.py). The heavy imports (fastembed, onnxruntime)
happen inside it, so importing this package needs no extras; only worker-embed installs them (the
`embed` extra). Tasks call it and the embed methods from a thread (`asyncio.to_thread`): they block.
"""
import threading
from collections.abc import Sequence
from typing import Protocol

from jobfinder.core.config import settings
from jobfinder.integrations.embed.contract import Contract, ContractError, load_contract
from jobfinder.integrations.embed.fastembed import FastEmbedder

__all__ = ["Contract", "ContractError", "Embedder", "FastEmbedder", "get_embedder", "load_contract",
           "load_embedder"]


class Embedder(Protocol):
    """A loaded model. `model` is the run name written to every vector row; `dim` its width."""

    model: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Postings: the contract's posting prompt (empty for v2, so the texts as given) is prepended
        here. One unit-length vector per text, in order."""
        ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Profile queries: the contract's query prompt is prepended here, not by the caller."""
        ...


def load_embedder(*, threads: int | None = None) -> FastEmbedder:
    """A new model from EMBED_MODEL_DIR, its contract checked first. Use `get_embedder()`; this is for
    tools that need their own instance (scripts/embed_throughput.py overrides EMBED_THREADS)."""
    contract = load_contract(settings.embed_model_dir, settings.embed_model)
    return FastEmbedder.load(settings.embed_model_dir, contract,
                             threads=settings.embed_threads if threads is None else threads,
                             batch=settings.embed_batch)


_lock = threading.Lock()
_embedder: Embedder | None = None


def get_embedder() -> Embedder:
    """The process-wide model loaded from EMBED_MODEL_DIR (loaded once, on first call; thread-safe).

    Raises `ContractError` when the directory's contract does not fit, and ImportError without the
    `embed` extra. A failed load is not cached: the next call (the next task) tries again."""
    global _embedder
    if _embedder is None:
        with _lock:
            if _embedder is None:
                _embedder = load_embedder()
    return _embedder
