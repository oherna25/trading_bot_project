from __future__ import annotations

import os
from typing import Mapping, Optional

from bot.profile import suggest_params
from bot.shapes import OperatorConfig, OperatorProfile

DOTENV_PATH = ".env"
AUTH_TOKEN_ENV = "MUSE_AUTH_TOKEN"


def read_dotenv(path: str = DOTENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path) as handle:
            lines = handle.readlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            values.setdefault(key, value)
    return values


def env_value(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    if env is not None:
        return (env.get(name, "") or "").strip() or default
    file_values = read_dotenv()
    return (os.environ.get(name, file_values.get(name, "")) or "").strip() or default


def auth_token(env: Mapping[str, str] | None = None) -> str:
    return env_value(AUTH_TOKEN_ENV, "", env)


def operator_config(
    profile: Optional[OperatorProfile] = None,
    day_profit_target: float = 500.0,
) -> OperatorConfig:
    max_day_loss = 1000.0
    max_trade_size = 5000.0
    if profile is not None:
        params = suggest_params(profile)
        max_day_loss = params["max_day_loss"]
        max_trade_size = params["max_trade_size"]
    return OperatorConfig(
        max_day_loss=max_day_loss,
        day_profit_target=day_profit_target,
        max_trade_size=max_trade_size,
    )
