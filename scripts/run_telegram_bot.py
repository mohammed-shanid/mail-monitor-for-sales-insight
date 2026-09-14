"""CLI entry point for the Telegram founder bot (V1 prototyping
channel -- see README; WhatsApp Business Cloud API is the eventual
founder-facing channel).

Runs a long-polling loop: receives founder messages on Telegram,
answers via app.router.intent.route_message() (deterministic SQL for
the fixed questions, Claude only for open-ended questions about a
specific enquiry already in the database), and sends the reply back.
No public HTTPS webhook needed.

Usage:
    python scripts/run_telegram_bot.py

Stop with Ctrl+C. One-time setup: see README.md (TELEGRAM_BOT_TOKEN).
"""

from __future__ import annotations

import logging
import os
import sys

# Allow running as `python scripts/run_telegram_bot.py` from anywhere
# without requiring the project to be pip-installed or invoked with -m.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import TELEGRAM_BOT_TOKEN  # noqa: E402
from app.telegram.bot import run_polling_loop  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set -- see .env.example")
        raise SystemExit(1)

    try:
        run_polling_loop()
    except KeyboardInterrupt:
        logger.info("Telegram bot stopped")


if __name__ == "__main__":
    main()
