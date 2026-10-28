from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from bot.shapes import OperatorProfile

RISK_PER_TRADE = {
    "conservative": 0.005,
    "moderate": 0.01,
    "aggressive": 0.02,
}

KNOWLEDGE_LEVELS = ("beginner", "intermediate", "advanced")

OPTIONS_FLAGS = {"yes": True, "true": True, "no": False, "false": False}


@dataclass(frozen=True)
class Question:
    id: str
    prompt: str
    kind: str
    options: tuple[str, ...] = ()


QUESTION_TABLE = (
    Question(
        id="risk_tolerance",
        prompt="What is your risk tolerance?",
        kind="choice",
        options=("conservative", "moderate", "aggressive"),
    ),
    Question(
        id="portfolio_size",
        prompt="What is your portfolio size in dollars?",
        kind="number",
        options=(),
    ),
    Question(
        id="knowledge",
        prompt="What is your trading experience level?",
        kind="choice",
        options=("beginner", "intermediate", "advanced"),
    ),
    Question(
        id="options_ok",
        prompt="Is options trading OK?",
        kind="bool",
        options=("yes", "no", "true", "false"),
    ),
)

QUESTION_IDS = tuple(question.id for question in QUESTION_TABLE)


def parse_answers(text: str) -> dict[str, str]:
    body = text.strip()
    if body.lower().startswith("profile:"):
        body = body[len("profile:"):]
    for separator in (";", "\n"):
        body = body.replace(separator, ",")
    answers: dict[str, str] = {}
    for part in body.split(","):
        part = part.strip()
        if not part:
            continue
        key, found, value = part.partition("=")
        if not found:
            key, found, value = part.partition(":")
        if not found:
            raise ValueError(f"could not parse {part!r}; use key=value pairs")
        name = key.strip().lower()
        if name not in QUESTION_IDS:
            raise ValueError(f"unknown answer {key.strip()!r}; expected one of {', '.join(QUESTION_IDS)}")
        if name in answers:
            raise ValueError(f"duplicate answer for {name!r}")
        answers[name] = value.strip()
    if not answers:
        raise ValueError(
            "no answers found; reply with profile: "
            "risk_tolerance=..., portfolio_size=..., knowledge=..., options_ok=..."
        )
    return answers


def build_profile(answers: Mapping[str, object]) -> OperatorProfile:
    missing = [qid for qid in QUESTION_IDS if qid not in answers]
    if missing:
        raise ValueError(f"missing answers for {', '.join(missing)}")
    risk = str(answers["risk_tolerance"]).strip().lower()
    if risk not in RISK_PER_TRADE:
        raise ValueError(
            "risk_tolerance must be one of conservative, moderate, aggressive; "
            f"got {answers['risk_tolerance']!r}"
        )
    try:
        size = float(str(answers["portfolio_size"]).strip())
    except (TypeError, ValueError):
        raise ValueError(f"portfolio_size must be a positive number; got {answers['portfolio_size']!r}")
    if size <= 0:
        raise ValueError(f"portfolio_size must be a positive number; got {answers['portfolio_size']!r}")
    knowledge = str(answers["knowledge"]).strip().lower()
    if knowledge not in KNOWLEDGE_LEVELS:
        raise ValueError(
            f"knowledge must be one of {', '.join(KNOWLEDGE_LEVELS)}; got {answers['knowledge']!r}"
        )
    raw_flag = answers["options_ok"]
    if isinstance(raw_flag, bool):
        options_ok = raw_flag
    else:
        flag = str(raw_flag).strip().lower()
        if flag not in OPTIONS_FLAGS:
            raise ValueError(
                f"options_ok must be one of yes, no, true, false; got {answers['options_ok']!r}"
            )
        options_ok = OPTIONS_FLAGS[flag]
    return OperatorProfile(
        risk_tolerance=risk,
        portfolio_size=size,
        knowledge=knowledge,
        options_ok=options_ok,
    )


def suggest_params(profile: OperatorProfile) -> dict[str, float]:
    return {
        "risk_per_trade": RISK_PER_TRADE[profile.risk_tolerance],
        "account_size": profile.portfolio_size,
        "max_day_loss": 0.02 * profile.portfolio_size,
        "max_trade_size": 0.05 * profile.portfolio_size,
    }


def format_questionnaire() -> str:
    lines = ["operator profile questionnaire; reply in one message as profile: key=value, ..."]
    for index, question in enumerate(QUESTION_TABLE, start=1):
        line = f"{index}. {question.id}: {question.prompt}"
        if question.options:
            line += f" ({'/'.join(question.options)})"
        lines.append(line)
    lines.append(
        "example: profile: risk_tolerance=moderate, portfolio_size=50000, "
        "knowledge=intermediate, options_ok=yes"
    )
    return "\n".join(lines)


def format_summary(profile: OperatorProfile, params: Mapping[str, float]) -> str:
    return "\n".join(
        [
            "profile saved: "
            f"risk_tolerance={profile.risk_tolerance}, "
            f"portfolio_size={profile.portfolio_size}, "
            f"knowledge={profile.knowledge}, "
            f"options_ok={profile.options_ok}",
            "suggested params: "
            f"risk_per_trade={params['risk_per_trade']}, "
            f"account_size={params['account_size']}, "
            f"max_day_loss={params['max_day_loss']}, "
            f"max_trade_size={params['max_trade_size']}",
            "options_ok gates options intents in review plus options ideas.",
        ]
    )
