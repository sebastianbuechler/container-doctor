"""Take the docs' screenshots and record the interactive demo.

    python -m demo.record

Starts the demo (demo/serve.py), waits until the doctor has polled,
diagnosed and written its briefing, then runs demo/record.cjs, a Playwright
tour, against it. Writes:

    docs/src/assets/screenshots/*.webp   the screenshots, light and dark
    docs/demo/api.json                   every API answer the tour read

Needs Playwright with Chromium (`npm i -g playwright`, then
`npx playwright install chromium`) and the docs' dependencies (`cd docs &&
npm ci`), whose sharp turns the screenshots into WebP.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8090
BASE = f"http://127.0.0.1:{PORT}"
AUTH = "Basic " + base64.b64encode(b"doctor:demo").decode()


def get(path: str):
    request = urllib.request.Request(BASE + path, headers={"Authorization": AUTH})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def ready() -> bool:
    """Polled, diagnosed, checked for updates and briefed."""
    try:
        get("/api/briefing")  # asking is what starts the briefing
        state = get("/api/state")
        incidents = get("/api/incidents?limit=50")["incidents"]
        briefing = get("/api/briefing")
    except OSError:
        return False
    kinds = {i["kind"] for i in incidents}
    return (bool(state.get("image_updates", {}).get("checked_at"))
            and {"restart_loop", "unhealthy", "log_errors", "disk_filling"} <= kinds
            and all(i.get("diagnosis") for i in incidents)
            and briefing.get("overall") is not None and not briefing.get("generating"))


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="doctor-record-"))
    server = subprocess.Popen(
        [sys.executable, "-m", "demo.serve", "--port", str(PORT), "--data", str(work / "run")],
        cwd=ROOT)
    try:
        deadline = time.monotonic() + 180
        while not ready():
            if time.monotonic() > deadline or server.poll() is not None:
                sys.exit("the demo did not get ready in three minutes")
            time.sleep(3)
        while get("/api/state").get("poll_interval_seconds", 0) < 900:
            time.sleep(2)  # until the doctor has slowed down; see serve.py
        time.sleep(3)
        node_path = subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()
        out = work / "out"
        subprocess.run(["node", str(ROOT / "demo" / "record.cjs"), BASE, str(out)], check=True,
                       env={**os.environ, "NODE_PATH": node_path})
    finally:
        server.terminate()
        server.wait(timeout=20)

    shots = ROOT / "docs" / "src" / "assets" / "screenshots"
    shutil.rmtree(shots, ignore_errors=True)
    shots.mkdir(parents=True)
    subprocess.run(["node", "-e", WEBP, str(out / "screenshots"), str(shots)],
                   cwd=ROOT / "docs", check=True)
    (ROOT / "docs" / "demo").mkdir(exist_ok=True)
    shutil.copy(out / "api.json", ROOT / "docs" / "demo" / "api.json")
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {shots.relative_to(ROOT)} and docs/demo/api.json")


WEBP = """
const fs = require("fs"), path = require("path"), sharp = require("sharp");
const [from, to] = process.argv.slice(1);
Promise.all(fs.readdirSync(from).filter((f) => f.endsWith(".png")).map((f) =>
  sharp(path.join(from, f)).webp({ quality: 82 })
    .toFile(path.join(to, f.replace(/\\.png$/, ".webp")))));
"""


if __name__ == "__main__":
    main()
