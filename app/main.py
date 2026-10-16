"""Entrypoint: start the supervisor, then serve the dashboard."""

from __future__ import annotations

import contextlib
import logging
import os

import uvicorn

from .config import load_settings, settings
from .supervisor import Supervisor
from .web.api import build_app


def configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # docker-py is chatty about every urllib3 connection it opens.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("docker.utils.config").setLevel(logging.WARNING)
    # httpx logs every request at INFO, which would echo each LLM and probe call.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def create_app():
    configure_logging()
    os.makedirs(settings.data_dir, exist_ok=True)
    # `settings` is the environment alone - enough to log, find the database
    # and serve. What runs also reads what the Settings page saved.
    resolved = load_settings()
    supervisor = Supervisor(resolved)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await supervisor.start()
        try:
            yield
        finally:
            await supervisor.stop()

    app = build_app(supervisor, resolved)
    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    uvicorn.run(
        create_app(),
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        access_log=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
