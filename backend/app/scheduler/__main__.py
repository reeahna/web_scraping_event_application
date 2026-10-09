"""Entry point for the dedicated scheduler process: ``python -m app.scheduler``.

Runs exactly one scheduler. Installs signal handlers so SIGINT/SIGTERM trigger
a graceful shutdown (in-flight runs finish; no new work starts). This process
is separate from the web server on purpose — see app.scheduler.__init__.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.scheduler.runtime import SchedulerRuntime

logger = get_logger("scheduler.main")


async def _run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = SchedulerRuntime()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler is unavailable on Windows event loops; there we
        # fall back to the default KeyboardInterrupt handling for SIGINT.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    runtime.start()
    logger.info("scheduler process running; waiting for shutdown signal")
    try:
        await stop.wait()
    finally:
        await runtime.shutdown()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
