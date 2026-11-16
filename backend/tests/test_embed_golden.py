"""Golden parity (#126): the real bge-small-jobfinder-v2 reproduces the model repo's golden file.

`golden_examples.json` is docs/golden_examples.json from jobfinder-embedding-model, copied unchanged:
made-up inputs with the texts and cosines the model gave in training. Texts must match exactly and
cosines within 1e-3; a drift means the text building, the prompts or the model files differ.

Needs the `embed` extra and EMBED_MODEL_DIR pointing at the model directory (worker-embed's image has
both); everywhere else it skips. Run it with `pytest -m embed_model`.
"""
import json
from pathlib import Path

import pytest

pytest.importorskip("fastembed")  # before numpy, which only the extra brings

import numpy as np

from jobfinder.core.config import settings
from jobfinder.services.matching import embed_text

GOLDEN = Path(__file__).parent / "golden_examples.json"

pytestmark = pytest.mark.embed_model


@pytest.fixture(scope="module")
def embedder():
    if not (Path(settings.embed_model_dir) / "model.onnx").is_file():
        pytest.skip(f"no model.onnx in EMBED_MODEL_DIR={settings.embed_model_dir}")
    from jobfinder.integrations.embed import get_embedder

    return get_embedder()


def test_contract_agrees_with_the_text_builders_and_golden(embedder):
    """check_golden embeds with embed_text.PROMPTS; production uses the contract's prompts. They must
    be the same, and the judge thresholds must be the ones the golden statuses were made with."""
    c, g = embedder.contract, json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert vars(c.prompts) == embed_text.PROMPTS
    assert (c.name, c.t_met, c.t_partial) == (g["model"], g["T_MET"], g["T_PARTIAL"])


def test_golden_texts_and_judge_cosines(embedder):
    embed_text.check_golden(str(GOLDEN), embed=lambda texts, prompt: np.array(embedder.embed(texts, prompt)))


def test_golden_retrieval_sides(embedder):
    """The two methods retrieval calls add the right prompts: a posting vector and the query-vs-posting
    cosines match the golden file."""
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    doc = np.array(embedder.embed_documents([g["expected"]["job_doc"]]))
    queries = np.array(embedder.embed_queries(g["expected"]["profile_queries"]))
    assert doc.shape == (1, 384)
    assert np.allclose(np.linalg.norm(doc, axis=1), 1.0, atol=1e-4)
    assert np.allclose(doc[0, :8], g["expected"]["job_doc_vector_first8"], atol=1e-3)
    assert np.allclose(queries @ doc[0], g["expected"]["profile_query_vs_job_cos"], atol=1e-3)


def test_one_instance_per_process(embedder):
    from jobfinder.integrations.embed import get_embedder

    assert get_embedder() is embedder
    assert (embedder.model, embedder.dim) == (settings.embed_model, 384)
