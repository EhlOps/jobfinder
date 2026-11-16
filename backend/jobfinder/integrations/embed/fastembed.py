"""The real `Embedder`: the model directory's fp32 `model.onnx` run by fastembed on CPU.

Loading follows the model repo's docs/jobfinder-integration.md exactly: register the directory as a
custom fastembed model (CLS pooling, L2 normalisation, 384-d) and load it from that path, so nothing is
downloaded. fastembed does not add prompts, so each side prepends its contract prompt here.

fastembed and onnxruntime are imported inside `load`, so this module imports without the `embed` extra.
"""
import threading
from collections.abc import Sequence

from jobfinder.integrations.embed.contract import Contract, ContractError

MODEL_FILE = "model.onnx"  # fp32; the int8 export changes about 11% of the judge's decisions


class FastEmbedder:
    """`integrations.embed.Embedder` over a fastembed `TextEmbedding`. Calls are serialised: the HF
    tokenizer object is not safe to share across threads, and each ONNX run already uses `threads`
    cores, so running two at once would only oversubscribe the CPU."""

    def __init__(self, model, contract: Contract, batch: int):
        self._model, self.contract, self._batch = model, contract, batch
        self.model, self.dim = contract.name, contract.dim
        self._lock = threading.Lock()

    @classmethod
    def load(cls, model_dir: str, contract: Contract, *, threads: int, batch: int) -> "FastEmbedder":
        """Register `model_dir` with fastembed under the contract's name and open an ONNX session."""
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        name = f"jobfinder/{contract.name}"
        if not any(m["model"].lower() == name.lower() for m in TextEmbedding.list_supported_models()):
            TextEmbedding.add_custom_model(model=name, pooling=PoolingType.CLS, normalization=True,
                                           sources=ModelSource(hf=name), dim=contract.dim,
                                           model_file=MODEL_FILE)
        embedder = cls(TextEmbedding(name, specific_model_path=model_dir, threads=threads), contract, batch)
        width = len(embedder.embed(["probe"], "")[0])
        if width != contract.dim:  # the contract next to a different export
            raise ContractError(f"{model_dir}/{MODEL_FILE} outputs {width}-d vectors, the contract says "
                                f"{contract.dim}")
        return embedder

    def embed(self, texts: Sequence[str], prompt: str) -> list[list[float]]:
        """`prompt + text` for each text, in batches of `batch`. Callers use the two sides below; this
        is public for the golden parity test, which also embeds requirements and evidence lines."""
        if not texts:
            return []
        with self._lock:
            vectors = self._model.embed([prompt + t for t in texts], batch_size=self._batch)
            return [v.tolist() for v in vectors]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts, self.contract.prompts.posting)

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts, self.contract.prompts.profile_query)
