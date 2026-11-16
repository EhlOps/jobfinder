"""The model directory's `jobfinder_contract.json`, read and checked before any vector is made.

The contract is written by the jobfinder-embedding-model repo next to the ONNX file. jobfinder reads the
parts it relies on: the run name (stored next to every vector), the width, CLS pooling with L2
normalisation, the prompt per side, and the judge thresholds (kept for the quick matcher; retrieval does
not use them). A contract that does not fit fails loudly here instead of writing vectors no query can use.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from jobfinder.models.embedding import EMBED_DIM

CONTRACT_FILE = "jobfinder_contract.json"


class ContractError(ValueError):
    """The model directory's contract is missing, malformed, or does not fit this deployment."""


@dataclass(frozen=True)
class Prompts:
    """Prepended to each text before tokenizing. Postings and evidence lines have none (v2)."""

    profile_query: str
    requirement: str
    posting: str
    evidence: str


@dataclass(frozen=True)
class Contract:
    name: str
    dim: int
    pooling: str
    normalize: bool
    prompts: Prompts
    t_met: float  # judge: max cosine over the evidence >= t_met is met,
    t_partial: float  # >= t_partial is partial, else unknown


def _int(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"dim must be an integer, got {value!r}")
    return value


def load_contract(model_dir: str | Path, expected_name: str) -> Contract:
    """Read `<model_dir>/jobfinder_contract.json` and check it fits: CLS pooling, normalised, EMBED_DIM
    wide, and named `expected_name` (settings.embed_model), since vectors are stored under that name."""
    path = Path(model_dir) / CONTRACT_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        model, prompts, judge = raw["model"], raw["prompts"], raw["judge"]
        contract = Contract(
            name=str(model["name"]), dim=_int(model["dim"]), pooling=str(model["pooling"]),
            normalize=model["normalize"] is True,
            prompts=Prompts(**{k: str(prompts[k])
                               for k in ("profile_query", "requirement", "posting", "evidence")}),
            t_met=float(judge["T_MET"]), t_partial=float(judge["T_PARTIAL"]),
        )
    except OSError as e:
        raise ContractError(f"cannot read {path}: {e}") from e
    except (ValueError, KeyError, TypeError) as e:
        raise ContractError(f"{path} is malformed: {e!r}") from e

    if contract.name != expected_name:
        raise ContractError(
            f"{path} is for model {contract.name!r} but EMBED_MODEL is {expected_name!r}; vectors are "
            f"stored under the model name, so point EMBED_MODEL_DIR at {expected_name!r} or change "
            "EMBED_MODEL (and reindex)")
    if contract.pooling != "cls":
        raise ContractError(f"{path}: pooling {contract.pooling!r} is not supported (only 'cls')")
    if not contract.normalize:
        raise ContractError(f"{path}: normalize must be true (cosine is computed as a dot product)")
    if not 0 < contract.t_partial < contract.t_met < 1:
        raise ContractError(f"{path}: judge thresholds need 0 < T_PARTIAL < T_MET < 1, got "
                            f"{contract.t_partial} and {contract.t_met}")
    if contract.dim != EMBED_DIM:
        raise ContractError(f"{path}: dim {contract.dim} does not match the vector columns "
                            f"({EMBED_DIM}); a new width needs a migration")
    return contract
