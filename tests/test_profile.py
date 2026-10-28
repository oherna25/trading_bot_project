from datetime import datetime, timezone

import pytest

from bot.journal import Journal
from bot.profile import (
    QUESTION_TABLE,
    build_profile,
    format_questionnaire,
    parse_answers,
    suggest_params,
)
from bot.research import route_chat
from bot.shapes import OperatorConfig, OperatorProfile
from bot.store import SnapshotStore

NOW = datetime(2026, 10, 27, 14, 30, tzinfo=timezone.utc)

CONFIG = OperatorConfig(
    dashboard_base_url="https://ops.example.internal",
    dashboard_link_expiry_seconds=3600,
    max_day_loss=1000.0,
    day_profit_target=500.0,
    max_trade_size=5000.0,
)

ONE_SHOT = "profile: risk_tolerance=moderate, portfolio_size=50000, knowledge=intermediate, options_ok=yes"


def test_question_table_covers_all_profile_fields():
    assert tuple(question.id for question in QUESTION_TABLE) == (
        "risk_tolerance",
        "portfolio_size",
        "knowledge",
        "options_ok",
    )
    assert all(question.prompt and question.kind for question in QUESTION_TABLE)


def test_parse_answers_folds_one_shot_message():
    assert parse_answers(ONE_SHOT) == {
        "risk_tolerance": "moderate",
        "portfolio_size": "50000",
        "knowledge": "intermediate",
        "options_ok": "yes",
    }


def test_parse_answers_accepts_semicolons_and_newlines():
    text = "risk_tolerance=aggressive; portfolio_size=25000\nknowledge=advanced, options_ok=no"
    assert parse_answers(text) == {
        "risk_tolerance": "aggressive",
        "portfolio_size": "25000",
        "knowledge": "advanced",
        "options_ok": "no",
    }


def test_parse_answers_rejects_unknown_key():
    with pytest.raises(ValueError, match="unknown answer"):
        parse_answers("risk_tolerance=moderate, nickname=poteto")


def test_parse_answers_rejects_malformed_segment():
    with pytest.raises(ValueError, match="could not parse"):
        parse_answers("risk_tolerance moderate")


def test_parse_answers_rejects_duplicates():
    with pytest.raises(ValueError, match="duplicate answer"):
        parse_answers("risk_tolerance=moderate, risk_tolerance=aggressive")


def test_parse_answers_rejects_empty_message():
    with pytest.raises(ValueError, match="no answers found"):
        parse_answers("profile: ")


def test_build_profile_returns_frozen_shape():
    profile = build_profile(parse_answers(ONE_SHOT))
    assert profile == OperatorProfile(
        risk_tolerance="moderate",
        portfolio_size=50000.0,
        knowledge="intermediate",
        options_ok=True,
    )


def test_build_profile_normalizes_case():
    profile = build_profile(
        {
            "risk_tolerance": "Aggressive",
            "portfolio_size": "10000",
            "knowledge": "Beginner",
            "options_ok": "TRUE",
        }
    )
    assert profile.risk_tolerance == "aggressive"
    assert profile.knowledge == "beginner"
    assert profile.options_ok is True


def test_build_profile_accepts_false_variants():
    for flag in ("no", "false", "No", "FALSE"):
        profile = build_profile(
            {
                "risk_tolerance": "moderate",
                "portfolio_size": "10000",
                "knowledge": "beginner",
                "options_ok": flag,
            }
        )
        assert profile.options_ok is False


def test_build_profile_rejects_missing_answers():
    with pytest.raises(ValueError, match="missing answers for knowledge, options_ok"):
        build_profile({"risk_tolerance": "moderate", "portfolio_size": "10000"})


def test_build_profile_rejects_bad_values():
    base = {
        "risk_tolerance": "moderate",
        "portfolio_size": "10000",
        "knowledge": "beginner",
        "options_ok": "yes",
    }
    for field, bad in (
        ("risk_tolerance", "reckless"),
        ("portfolio_size", "zero"),
        ("portfolio_size", "0"),
        ("portfolio_size", "-5"),
        ("knowledge", "expert"),
        ("options_ok", "maybe"),
    ):
        answers = dict(base, **{field: bad})
        with pytest.raises(ValueError, match=field):
            build_profile(answers)


def test_suggest_params_maps_each_risk_level():
    for risk, expected in (("conservative", 0.005), ("moderate", 0.01), ("aggressive", 0.02)):
        profile = OperatorProfile(
            risk_tolerance=risk, portfolio_size=50000.0, knowledge="beginner", options_ok=True
        )
        params = suggest_params(profile)
        assert params["risk_per_trade"] == pytest.approx(expected)
        assert params["account_size"] == pytest.approx(50000.0)
        assert params["max_day_loss"] == pytest.approx(1000.0)
        assert params["max_trade_size"] == pytest.approx(2500.0)


def test_format_questionnaire_lists_every_question():
    text = format_questionnaire()
    for question in QUESTION_TABLE:
        assert question.id in text
        assert question.prompt in text


def test_route_chat_returns_questionnaire_for_profile_questions():
    reply = route_chat(
        "send me the profile questions", "TEST", SnapshotStore(), Journal(), CONFIG, NOW
    )
    assert reply == format_questionnaire()


def test_route_chat_accepts_one_shot_profile():
    journal = Journal()
    reply = route_chat(ONE_SHOT, "TEST", SnapshotStore(), journal, CONFIG, NOW)
    assert "risk_tolerance=moderate" in reply
    assert "portfolio_size=50000.0" in reply
    assert "knowledge=intermediate" in reply
    assert "options_ok=True" in reply
    assert "risk_per_trade=0.01" in reply
    assert "options_ok gates future options filtering." in reply
    entry = journal.entries()[-1]
    assert entry.kind == "profile_accepted"
    assert entry.payload["risk_tolerance"] == "moderate"
    assert entry.payload["portfolio_size"] == pytest.approx(50000.0)
    assert entry.payload["knowledge"] == "intermediate"
    assert entry.payload["options_ok"] is True
    assert entry.payload["risk_per_trade"] == pytest.approx(0.01)
    assert entry.payload["account_size"] == pytest.approx(50000.0)
    assert entry.payload["max_day_loss"] == pytest.approx(1000.0)
    assert entry.payload["max_trade_size"] == pytest.approx(2500.0)


def test_route_chat_rejects_bad_profile_with_usable_message():
    journal = Journal()
    reply = route_chat(
        "profile: risk_tolerance=reckless, portfolio_size=50000, knowledge=beginner, options_ok=yes",
        "TEST",
        SnapshotStore(),
        journal,
        CONFIG,
        NOW,
    )
    assert reply.startswith("could not save profile: ")
    assert "risk_tolerance" in reply
    assert [entry for entry in journal.entries() if entry.kind == "profile_accepted"] == []
