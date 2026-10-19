"""Run the real doctor against the made-up host.

    python -m demo.serve [--port 8090] [--data DIR]

Docker, /proc and /sys, the SMART exporter, the registries, the network
checks and the model are stand-ins (see fakes.py and model.py); everything
else - detection, diagnosis, the decision, the store and the dashboard - is
the code in app/. It seeds two weeks of history first, so the dashboard has
something to show at once. Nothing it does reaches outside the process.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Long enough not to earn the dashboard's short-password notice.
PASSWORD = "demo-password-not-secret"
# Polls run this often while the scenario plays out, then stop.
QUICK_POLLS, QUICK_SECONDS = 16, 5


def environment(data: Path, port: int, fake_root: Path) -> dict[str, str]:
    return {
        "DOCTOR_DATA_DIR": str(data),
        "DOCTOR_WEB_HOST": "127.0.0.1",
        "DOCTOR_WEB_PORT": str(port),
        "DOCTOR_WEB_PASSWORD": PASSWORD,
        "DOCTOR_MODE": "suggest",
        "DOCTOR_HOST_NAME": "atlas",
        "DOCTOR_DOCKER_MANAGER": "Dockge",
        "DOCTOR_DEPLOY_METHOD": "",
        "DOCTOR_PROC_PATH": str(fake_root / "proc"),
        "DOCTOR_SYSFS_PATH": str(fake_root / "sys"),
        "DOCTOR_MDSTAT_PATH": str(fake_root / "proc" / "mdstat"),
        "DOCTOR_DISK_PATHS": "/srv/data,/",
        "DOCTOR_SMART_URL": f"http://127.0.0.1:{port}/demo-smart/metrics",
        "DOCTOR_NET_CHECK_SECONDS": "30",
        "DOCTOR_NET_DNS_SERVERS": "192.168.1.1",
        "DOCTOR_NET_HTTP_URLS": "https://github.com",
        "DOCTOR_IMAGE_CHECK_HOURS": "12",
        "DOCTOR_RELEASE_NOTES": "true",
        "ANTHROPIC_API_KEY": "demo",
        "DOCTOR_MODEL": "claude-opus-5-5",
        # The classifier, acting on its verdicts: its backend is faked at the
        # HTTP call (model.patch_classifier).
        "DOCTOR_CLASSIFIER": "on",
        "DOCTOR_CLASSIFIER_API_KEY": "demo",
        "DOCTOR_LOG_LEVEL": "INFO",
        "DOCKER_HOST": "tcp://doctor-socket-proxy:2375",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--data", default="")
    args = parser.parse_args()

    work = Path(args.data or tempfile.mkdtemp(prefix="doctor-demo-"))
    if work.exists():
        shutil.rmtree(work)
    data, fake_root = work / "data", work / "host"
    data.mkdir(parents=True)
    os.environ.update(environment(data, args.port, fake_root))

    # Imported only now: app.config reads the environment on import.
    from fastapi.responses import PlainTextResponse
    import uvicorn

    from app import connectivity, decision, diagnoser, preflight, versions
    from app import supervisor as supervisor_module
    from app import main as app_main
    from app import docker_client

    from . import fakes, model, seed

    settled = threading.Event()

    seed.seed(str(data / "doctor.db"))

    host = fakes.FakeHost(fake_root)
    host.run()

    class DemoDocker(docker_client.DockerClient):
        def __init__(self, base_url, env_key=b"", max_pool_size=10):
            self._base_url = base_url
            self._env_key = env_key
            self._client = fakes.FakeDockerPy()
            self._events_thread = None
            self._stop = threading.Event()

    supervisor_module.DockerClient = DemoDocker
    supervisor_module.disk_usage = fakes.disk_usage
    fakes.patch_registries(supervisor_module, versions)
    fakes.patch_connectivity(connectivity)
    model.patch(diagnoser)
    model.patch_classifier(decision)

    async def probe_endpoint(docker_host):
        return fakes.preflight_results(preflight)
    supervisor_module.probe_endpoint = probe_endpoint

    log = logging.getLogger("demo")

    async def poll_loop(self):
        """The scenario in a minute and a half: quick polls while paperless
        falls over and the rules catch it, then none, so nothing moves while
        the screenshots are taken - and the settings stay as configured."""
        for n in range(1, QUICK_POLLS + 1):
            fakes.evolve(n)
            self._polling = True
            self.broadcast("poll", {"polling": True})
            try:
                await self._poll_once()
            except Exception:
                log.exception("poll failed")
            if self._polling:
                self._polling = False
                self._poll_progress = None
                self.broadcast("poll", {"polling": False})
            await asyncio.sleep(QUICK_SECONDS)
        settled.set()
    supervisor_module.Supervisor._poll_loop = poll_loop

    original_start = supervisor_module.Supervisor.start

    async def start(self):
        if self.net is not None:
            fakes.backfill_connectivity(self.net, connectivity, connectivity.HISTORY_ROUNDS)
        # A day of CPU and memory per container, which the doctor keeps in
        # memory only and so would otherwise start empty.
        self.history.update(fakes.container_history(supervisor_module.HISTORY_POINTS))
        await original_start(self)
    supervisor_module.Supervisor.start = start

    app = app_main.create_app()

    @app.get("/demo-smart/metrics", include_in_schema=False)
    def smart_metrics():
        return PlainTextResponse(fakes.smart_metrics())

    @app.get("/demo-settled", include_in_schema=False)
    def demo_settled():
        """Whether the quick polls are over; the recorder waits for it."""
        return {"settled": settled.is_set()}

    # The routes the app registered first include a catch-all for the
    # dashboard; these two have to answer before it.
    for _ in range(2):
        app.router.routes.insert(0, app.router.routes.pop())

    # The password is PASSWORD above; it is not printed, so it does not end up in logs.
    print(f"demo: http://127.0.0.1:{args.port}, user doctor  (data in {work})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
