"""Modal entry point: scheduled brief plus persistent store on a Volume.

Deploy with ``modal deploy modal_app.py`` after ``modal setup``. The scheduled
function ingests chains plus bars, writes the brief to the journal log, and
persists the store bundle under the mounted volume. Without the ``modal``
package installed this module still imports and exposes the pure runner.
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    import modal
except ImportError:  # pragma: no cover - optional deploy dependency
    modal = None  # type: ignore[assignment]

STORE_DIR = "/data/trading-bot"
SCHEDULE = "30 13 * * 1-5"


def run(underlying: str = "SPY", store_dir: str = STORE_DIR) -> str:
    from bot.schedule import run_scheduled_session

    return run_scheduled_session(underlying, store_dir, datetime.now(timezone.utc))


if modal is not None:  # pragma: no cover - needs modal deploy
    app = modal.App("trading-bot")
    volume = modal.Volume.from_name("trading-store", create_if_missing=True)

    @app.function(
        schedule=modal.Cron(SCHEDULE),
        volumes={STORE_DIR: volume},
        secrets=[modal.Secret.from_dotenv()],
    )
    def scheduled_session(underlying: str = "SPY") -> str:
        brief = run(underlying)
        volume.commit()
        return brief
