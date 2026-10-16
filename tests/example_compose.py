"""compose.example.yaml as the tests read it: the doctor's environment lines,
and what config.passed_env makes of a container started from it."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.config import passed_env

EXAMPLE = Path(__file__).resolve().parents[1] / "compose.example.yaml"
_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=\$\{([A-Z][A-Z0-9_]*)(?::?-([^}]*))?\}$")


def doctor_env(path: Path = EXAMPLE) -> dict[str, tuple[str, str]]:
    """{container variable: (stack variable, default)} for the doctor's
    `- NAME=${STACK:-default}` lines."""
    service = yaml.safe_load(path.read_text())["services"]["container-doctor"]
    found = {}
    for line in service["environment"]:
        match = _LINE.match(str(line).strip())
        if match:
            found[match.group(1)] = (match.group(2), match.group(3) or "")
    return found


def passed(path: Path = EXAMPLE) -> frozenset[str] | None:
    """What the doctor would read as passed, started from the example with
    nothing set: every variable there, empty but for the file's defaults."""
    return passed_env({name: default for name, (_, default) in doctor_env(path).items()})
