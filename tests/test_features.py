"""Contract tests for the quality-focused product feature ledger."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FROZEN_CONTENT_SHA256 = "f39ee07d386978a08b6e8d578f94d8e51c9d19448c7d5fc797acd71caa2c248b"


def test_feature_ledger_shape_and_immutable_definition():
    data = json.loads((ROOT / "feature.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["features"]
    assert [stage["stage"] for stage in data["delivery_order"]] == [
        "执行数据沉淀优先", "叙事能力优先", "编排质量其次", "人工修订闭环最后"]
    assert data["features"][0]["id"] == "execution-data-corpus"
    assert "quality-benchmark" not in {feature["id"] for feature in data["features"]}
    assert len({feature["id"] for feature in data["features"]}) == len(data["features"])
    for feature in data["features"]:
        assert set(feature) == {"id", "category", "description", "steps", "ispass"}
        assert all(isinstance(feature[key], str) and feature[key].strip()
                   for key in ("id", "category", "description"))
        assert isinstance(feature["steps"], list) and feature["steps"]
        assert all(isinstance(step, str) and step.strip() for step in feature["steps"])
        assert isinstance(feature["ispass"], bool)

    # Completion is the sole mutable state. Normalizing it before hashing makes
    # false -> true legal while rejecting silent edits to the promised scope.
    frozen = json.loads(json.dumps(data, ensure_ascii=False))
    for feature in frozen["features"]:
        feature["ispass"] = False
    canonical = json.dumps(frozen, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(canonical).hexdigest() == FROZEN_CONTENT_SHA256
