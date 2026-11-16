"""embed: the local embedding model (#126), `bge-small-jobfinder-v2` by default, run on CPU in worker-embed.

The model ships as a directory (EMBED_MODEL_DIR) holding the ONNX file, its tokenizer and
`jobfinder_contract.json`, exported by the jobfinder-embedding-model repo. The contract is the spec:
the model name stored next to every vector, the prompt each side gets, and how each text is built
(services/matching/embed_text.py, copied verbatim from that repo). Vectors are unit length, so cosine
similarity is a dot product and pgvector's `<=>` is 1 - cosine.

Two sides, one space:
    embed_documents  postings (`job_doc`): no prompt
    embed_queries    profile queries (`profile_queries`): the contract's profile_query prompt

The heavy imports (fastembed, onnxruntime) happen inside `get_embedder()`, so importing this package
needs no extras; only worker-embed installs them (the `embed` extra).
"""
from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    """A loaded model. `model` is the run name written to every vector row; `dim` its width."""

    model: str
    dim: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Postings: the texts as given, no prompt. One unit-length vector per text, in order."""
        ...

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        """Profile queries: the contract's query prompt is prepended here, not by the caller."""
        ...


def get_embedder() -> Embedder:
    """The process-wide model loaded from EMBED_MODEL_DIR (loaded once, on first call)."""
    raise NotImplementedError("the fastembed-backed embedder lands in #126 unit 2")
