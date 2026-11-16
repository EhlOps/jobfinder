"""The model directory's contract is checked before any vector is made (#126): integrations/embed/contract.py.
Needs neither the `embed` extra nor the model."""
import json

import pytest

from jobfinder.integrations.embed import ContractError, load_contract

NAME = "bge-small-jobfinder-v2"


def _write(tmp_path, judge=None, **model_overrides):
    contract = {
        "model": {"name": NAME, "dim": 384, "pooling": "cls", "normalize": True} | model_overrides,
        "prompts": {"profile_query": "Represent this job seeker's target role: ", "requirement": "Req: ",
                    "posting": "", "evidence": ""},
        "judge": judge or {"T_MET": 0.42, "T_PARTIAL": 0.22},
    }
    (tmp_path / "jobfinder_contract.json").write_text(json.dumps(contract), encoding="utf-8")
    return tmp_path


def test_good_contract(tmp_path):
    c = load_contract(_write(tmp_path), NAME)
    assert (c.name, c.dim, c.pooling, c.normalize) == (NAME, 384, "cls", True)
    assert c.prompts.profile_query == "Represent this job seeker's target role: "
    assert c.prompts.posting == ""
    assert (c.t_met, c.t_partial) == (0.42, 0.22)
    with pytest.raises(AttributeError):
        c.name = "other"  # frozen


def test_wrong_name_names_both(tmp_path):
    with pytest.raises(ContractError, match="'bge-small-jobfinder-v1'.*EMBED_MODEL is 'bge-small-jobfinder-v2'"):
        load_contract(_write(tmp_path, name="bge-small-jobfinder-v1"), NAME)


def test_wrong_pooling(tmp_path):
    with pytest.raises(ContractError, match="pooling 'mean'"):
        load_contract(_write(tmp_path, pooling="mean"), NAME)


def test_not_normalised(tmp_path):
    with pytest.raises(ContractError, match="normalize"):
        load_contract(_write(tmp_path, normalize=False), NAME)


def test_wrong_dim(tmp_path):
    with pytest.raises(ContractError, match="dim 768"):
        load_contract(_write(tmp_path, dim=768), NAME)


def test_dim_must_be_an_integer(tmp_path):
    with pytest.raises(ContractError, match="malformed.*integer"):
        load_contract(_write(tmp_path, dim=384.5), NAME)


def test_thresholds_out_of_order(tmp_path):
    with pytest.raises(ContractError, match="T_PARTIAL < T_MET"):
        load_contract(_write(tmp_path, judge={"T_MET": 0.2, "T_PARTIAL": 0.4}), NAME)


def test_missing_or_malformed(tmp_path):
    with pytest.raises(ContractError, match="cannot read"):
        load_contract(tmp_path, NAME)
    (tmp_path / "jobfinder_contract.json").write_text('{"model": {}}', encoding="utf-8")
    with pytest.raises(ContractError, match="malformed"):
        load_contract(tmp_path, NAME)
