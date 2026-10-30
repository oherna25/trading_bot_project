from datetime import datetime, timezone

from bot.journal import Journal
from bot.models import receipt_payload
from bot.shapes import ModelReceipt
from bot import tiers

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)
HOSTED = {"META_MODEL_API_KEY": "key-1", "MODEL_DAY_BUDGET_USD": "5.0"}


def _local_env(tmp_path) -> dict:
    weights = tmp_path / "glimmer-3b.gguf"
    weights.write_bytes(b"gguf-stub")
    return {"GLIMMER_GGUF_PATH": str(weights)}


def test_draft_prefers_local(tmp_path):
    journal = Journal()
    decision = tiers.select_model_tier("reason", journal, NOW, {**HOSTED, **_local_env(tmp_path)})
    assert (decision.tier, decision.role) == ("local", "local")
    assert decision.model_id == "muse-glimmer-3b"


def test_draft_falls_back_to_hosted():
    journal = Journal()
    decision = tiers.select_model_tier("reason", journal, NOW, dict(HOSTED))
    assert (decision.tier, decision.role) == ("model", "analyst")


def test_draft_without_anything_reports_hosted_reason():
    journal = Journal()
    decision = tiers.select_model_tier("reason", journal, NOW, {})
    assert decision.tier == "fallback"
    assert decision.reason == "missing META_MODEL_API_KEY"


def test_deep_prefers_hosted_over_local(tmp_path):
    journal = Journal()
    decision = tiers.select_model_tier("analyst", journal, NOW, {**HOSTED, **_local_env(tmp_path)})
    assert (decision.tier, decision.role) == ("model", "analyst")


def test_deep_uses_local_when_hosted_missing(tmp_path):
    journal = Journal()
    decision = tiers.select_model_tier("analyst", journal, NOW, _local_env(tmp_path))
    assert decision.tier == "local"


def test_premium_uses_reviewer():
    journal = Journal()
    decision = tiers.select_model_tier("coach", journal, NOW, dict(HOSTED))
    assert (decision.tier, decision.role) == ("model", "reviewer")


def test_spent_budget_blocks_hosted_but_keeps_local(tmp_path):
    journal = Journal()
    receipt = ModelReceipt(
        model_id="muse-spark-1.3", role_name="analyst", prompt_hash="h",
        tokens_in=10, tokens_out=10, cost_usd=5.0, latency_ms=1.0,
    )
    journal.append(NOW, "model_receipt", receipt_payload(receipt, ""))
    env = {**HOSTED, **_local_env(tmp_path)}
    assert tiers.select_model_tier("reason", journal, NOW, env).tier == "local"
    assert tiers.select_model_tier("analyst", journal, NOW, dict(HOSTED)).reason == "day budget spent"


def test_unknown_task_treated_as_deep():
    journal = Journal()
    decision = tiers.select_model_tier("something-new", journal, NOW, dict(HOSTED))
    assert (decision.tier, decision.role) == ("model", "analyst")


def test_selection_journals_tier(tmp_path):
    journal = Journal()
    tiers.select_model_tier("analyst", journal, NOW, {**HOSTED, **_local_env(tmp_path)})
    kinds = [entry.kind for entry in journal.entries()]
    assert "tier_selected" in kinds


def test_tier_status_reports_spend_and_local(tmp_path):
    journal = Journal()
    status = tiers.tier_status(journal, NOW, {**HOSTED, **_local_env(tmp_path)})
    assert status["spend_today_usd"] == 0.0
    assert status["day_budget_usd"] == 5.0
    assert status["local_available"] is True
    assert status["local_model"] == "muse-glimmer-3b"
    assert status["tasks"]["coach"] == "premium"
