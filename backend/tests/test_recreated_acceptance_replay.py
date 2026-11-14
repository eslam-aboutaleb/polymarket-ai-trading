"""A replay must survive a fresh process and reject tampering or hindsight."""

import base64
import gzip
import importlib.util
import json
from decimal import localcontext
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
DATA = ROOT / "user guide/acceptance-replay-2026-11-13/dataset.json.gz"


def module():
    spec = importlib.util.spec_from_file_location(
        "acceptance_report", ROOT / "scripts/acceptance-replay-report.py"
    )
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_public_replay_matches_committed_report_at_different_decimal_precision():
    expected = json.loads(DATA.with_name("report.json").read_text())
    with localcontext() as context:
        context.prec = 12
        result = module().analyze(DATA)
    assert result == expected
    assert result["out_of_sample_count"] == 5
    assert len(result["oracle_counterexamples"]) == 8
    assert result["oracle_verified"] is False
    assert all(not point["actual_fill"] for point in result["forecasts"])


def test_modified_raw_data_or_backdated_training_fails(tmp_path):
    original = json.loads(gzip.decompress(DATA.read_bytes()))
    changed = json.loads(json.dumps(original))
    digest = next(iter(changed["raw"]))
    changed["raw"][digest] = base64.b64encode(b'{"forged":true}').decode()
    target = tmp_path / "tampered.json.gz"
    target.write_bytes(gzip.compress(json.dumps(changed).encode()))
    with pytest.raises(ValueError, match="checksum mismatch"):
        module().analyze(target)
    changed = json.loads(json.dumps(original))
    target_hashes = changed["frozen_model"]["training_receipt_hashes"]
    for row in changed["requests"]:
        if row["sha256"] in target_hashes:
            row["received_at"] = "2027-01-01T00:00:00Z"
    target.write_bytes(gzip.compress(json.dumps(changed).encode()))
    with pytest.raises(ValueError, match="Training label unavailable"):
        module().analyze(target)
