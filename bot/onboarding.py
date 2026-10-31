from __future__ import annotations

from datetime import datetime

from bot.journal import Journal
from bot.profile import (
    KNOWLEDGE_LEVELS,
    OPTIONS_FLAGS,
    QUESTION_TABLE,
    RISK_PER_TRADE,
    build_profile,
    format_summary,
    suggest_params,
)
from bot.store import SnapshotStore

START_WORDS = ("start", "start onboarding", "begin onboarding")


def _last_index(journal: Journal, kind: str) -> int | None:
    last = None
    for index, entry in enumerate(journal.entries()):
        if entry.kind == kind:
            last = index
    return last


def staged_answers(journal: Journal) -> dict[str, str]:
    started = _last_index(journal, "onboarding_started")
    if started is None:
        return {}
    accepted = _last_index(journal, "profile_accepted")
    if accepted is not None and accepted > started:
        return {}
    out: dict[str, str] = {}
    for entry in journal.entries()[started + 1:]:
        if entry.kind != "onboarding_answer":
            continue
        key = str(entry.payload.get("id", ""))
        value = str(entry.payload.get("value", ""))
        if key:
            out[key] = value
    return out


def onboarding_in_progress(journal: Journal) -> bool:
    started = _last_index(journal, "onboarding_started")
    if started is None:
        return False
    accepted = _last_index(journal, "profile_accepted")
    if accepted is not None and accepted > started:
        return False
    return len(staged_answers(journal)) < len(QUESTION_TABLE)


def _hint_for(question_id: str) -> str:
    if question_id == "risk_tolerance":
        return " -- next: reply moderate (conservative/moderate/aggressive)"
    if question_id == "portfolio_size":
        return " -- next: reply 50000"
    if question_id == "knowledge":
        return " -- next: reply intermediate (beginner/intermediate/advanced)"
    return " -- next: reply yes (yes/no)"


def format_question_prompt(question, index: int) -> str:
    total = len(QUESTION_TABLE)
    head = f"Question {index + 1}/{total}: {question.id}: {question.prompt}"
    if question.options:
        head += f" ({'/'.join(question.options)})"
    return head + _hint_for(question.id)


def start_onboarding(journal: Journal, now: datetime) -> str:
    journal.append(now, "onboarding_started", {})
    return format_question_prompt(QUESTION_TABLE[0], 0)


def validate_answer(question, raw: str) -> str:
    text = (raw or "").strip()
    if question.id == "risk_tolerance":
        value = text.lower()
        if value not in RISK_PER_TRADE:
            raise ValueError(
                f"risk_tolerance must be one of conservative, moderate, aggressive; got {text!r}"
                " -- next: reply moderate"
            )
        return value
    if question.id == "portfolio_size":
        try:
            size = float(text)
        except (TypeError, ValueError):
            raise ValueError(
                f"portfolio_size must be a positive number; got {text!r} -- next: reply 50000"
            ) from None
        if size <= 0:
            raise ValueError(
                f"portfolio_size must be a positive number; got {text!r} -- next: reply 50000"
            )
        return text
    if question.id == "knowledge":
        value = text.lower()
        if value not in KNOWLEDGE_LEVELS:
            raise ValueError(
                f"knowledge must be one of {', '.join(KNOWLEDGE_LEVELS)}; got {text!r}"
                " -- next: reply intermediate"
            )
        return value
    value = text.lower()
    if value not in OPTIONS_FLAGS:
        raise ValueError(
            f"options_ok must be one of yes, no, true, false; got {text!r} -- next: reply yes"
        )
    return value


def answer_onboarding(
    text: str, underlying: str, store: SnapshotStore, journal: Journal, now: datetime
) -> str:
    if not onboarding_in_progress(journal):
        return "no onboarding in progress -- next: chat start to begin"
    answers = staged_answers(journal)
    question = QUESTION_TABLE[len(answers)]
    try:
        normalized = validate_answer(question, text)
    except ValueError as exc:
        return f"could not save answer: {exc}\n{format_question_prompt(question, len(answers))}"
    journal.append(now, "onboarding_answer", {"id": question.id, "value": normalized})
    answers[question.id] = normalized
    if len(answers) < len(QUESTION_TABLE):
        followed = QUESTION_TABLE[len(answers)]
        return f"saved {question.id}={normalized}.\n{format_question_prompt(followed, len(answers))}"
    profile = build_profile(answers)
    store.save_profile(profile)
    params = suggest_params(profile)
    journal.append(
        now,
        "profile_accepted",
        {
            "risk_tolerance": profile.risk_tolerance,
            "portfolio_size": profile.portfolio_size,
            "knowledge": profile.knowledge,
            "options_ok": profile.options_ok,
            **params,
        },
    )
    summary = format_summary(profile, params)
    try:
        from bot.tradecards import cards_response

        cards = cards_response("trade cards", underlying, store, journal, now)
    except ValueError as exc:
        cards = str(exc)
    return (
        f"{summary}\n\nfirst sized card:\n{cards}\n"
        f"-- next: chat explain my {underlying} to hear your position"
    )


def is_start(text: str) -> bool:
    return text.strip().lower() in START_WORDS


def onboarding_tool(
    answer: str | None, underlying: str, store: SnapshotStore, journal: Journal, now: datetime
) -> str:
    if answer is None or not str(answer).strip():
        if onboarding_in_progress(journal):
            answers = staged_answers(journal)
            question = QUESTION_TABLE[len(answers)]
            return format_question_prompt(question, len(answers))
        return start_onboarding(journal, now)
    return answer_onboarding(str(answer), underlying, store, journal, now)
