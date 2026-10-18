"""Generate the docs' environment variable reference from the code.

    python scripts/gen_docs.py          # write it
    python scripts/gen_docs.py --check  # exit 1 if it is out of date (CI)

Every setting is declared once, with setting() in app/config.py; this reads
those declarations, groups them as the Settings page does, and describes each
from its comment in .env.example - or, where it has none there, from the
comment above its declaration. Variables the compose file reads itself (the
image tag, the data directory, the proxy's switches) come from .env.example.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DOCTOR_DATA_DIR", tempfile.mkdtemp())

from app.config import ENV_FOR, SETTINGS, Settings  # noqa: E402

OUT = ROOT / "docs" / "src" / "content" / "docs" / "reference" / "environment-variables.md"
_VAR = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=(.*)$")
_DECL = re.compile(r'^\s+\w+: .*= setting\(\s*"([A-Z][A-Z0-9_]*)"')
_DECL_OPEN = re.compile(r"^\s+\w+: .*= setting\($")
_DECL_NAME = re.compile(r'^\s+"([A-Z][A-Z0-9_]*)"')


def prose(lines: list[str]) -> str:
    """Comment lines as Markdown: aligned `name - meaning` lines become a list,
    the rest one paragraph."""
    items = [re.match(r"^(\S+)\s+-\s+(.*)$", line) for line in lines]
    if len(lines) > 1 and all(items):
        return "\n".join(f"- `{m.group(1)}` - {m.group(2)}" for m in items)
    return " ".join(lines)


def env_example() -> tuple[dict[str, str], dict[str, str], list[str]]:
    """{variable: its own comment}, {variable: the comment of the group it
    follows without one of its own}, and the variables in file order."""
    described: dict[str, str] = {}
    shared: dict[str, str] = {}
    order: list[str] = []
    comment: list[str] = []
    group = ""
    for raw in (ROOT / ".env.example").read_text().splitlines():
        line = raw.strip()
        var = _VAR.match(line)
        if var:
            name = var.group(1)
            order.append(name)
            if comment:
                group = prose(comment)
                described.setdefault(name, group)
            elif group:
                shared.setdefault(name, group)
            comment = []
        elif line.startswith("# ---") or not line:
            comment, group = [], ""
        elif line.startswith("#"):
            comment.append(line.lstrip("#").strip())
    return described, shared, order


def code_comments() -> dict[str, str]:
    """{variable: the comment block right above its setting() declaration}."""
    found: dict[str, str] = {}
    comment: list[str] = []
    group = ""
    lines = (ROOT / "app" / "config.py").read_text().splitlines()
    for i, line in enumerate(lines):
        # setting("NAME", ...) on the line, or setting( with "NAME" on the next
        decl = _DECL.match(line) or (_DECL_OPEN.match(line) and i + 1 < len(lines)
                                     and _DECL_NAME.match(lines[i + 1]))
        if decl:
            if comment:
                group = prose(comment)
            if group:
                found[decl.group(1)] = group
            comment = []
        elif line.strip().startswith("#"):
            comment.append(line.strip().lstrip("#").strip())
        elif not line.strip():
            # A blank line ends a group; declarations on the lines right after
            # a commented one share its comment.
            comment, group = [], ""
        else:
            comment = []
    return found


def shown_default(spec) -> str:
    if spec.secret:
        return "unset"
    value = spec.default
    if spec.kind == "bool":
        return "on" if value else "off"
    if spec.kind == "list":
        return ", ".join(str(v) for v in value) or "none"
    if value in ("", None):
        return "unset"
    return f"{value:g}" if isinstance(value, float) else str(value)


def entry(name: str, description: str, facts: list[str]) -> str:
    text = description.strip() or "No description yet."
    text = text[0].upper() + text[1:]
    return f"### {name}\n\n{text}\n\n" + (" · ".join(facts) + "\n" if facts else "")


def render() -> str:
    described, shared, example_order = env_example()
    from_code = code_comments()

    # Every section the page can show - the GitHub one only appears with a token.
    sections: dict[str, list[str]] = {}
    notes: dict[str, str] = {}
    for section in Settings(github_token="x").overview():
        for label, _, note in section["items"]:
            for name in ENV_FOR.get((section["title"], label), ()):
                if note and name not in notes:
                    notes[name] = f"{label}: {note}."
                if not any(name in names for names in sections.values()):
                    sections.setdefault(section["title"], []).append(name)
    placed = {name for names in sections.values() for name in names}
    rest = sorted(name for name in SETTINGS if name not in placed)
    if rest:
        sections["Startup and runtime"] = rest

    out = [
        "---",
        "title: \"Environment variables\"",
        "description: \"Every setting Container Doctor reads, its default, and what it does - generated from the code.\"",
        "---",
        "",
        "<!-- Generated by scripts/gen_docs.py from app/config.py and .env.example. Do not edit by hand. -->",
        "",
        "Every setting is read from the environment - a `.env` file next to the",
        "compose file, or your Docker manager's stack environment. Most can also be",
        "changed on the dashboard's [Settings page](/container-doctor/guides/settings/); a variable",
        "set in the environment always wins. Empty is the same as unset.",
        "",
        "- **Secret**: never shown by value, and never saved from the Settings page.",
        "- **Environment only**: read before the database opens, so the Settings page",
        "  cannot change it.",
        "- **Applies on restart**: read once at start; a change takes effect when the",
        "  doctor restarts.",
        "- **`off` turns it off**: on by default; set `off` (or `none` for a list) to",
        "  disable it.",
        "",
    ]
    for title, names in sections.items():
        out += [f"## {title}", ""]
        for name in names:
            spec = SETTINGS[name]
            facts = [f"**Default:** `{shown_default(spec)}`"]
            if spec.choices is not None:
                values = ", ".join(f"`{c.value}`" for c in spec.choices)
                facts.append(f"**{'Any of' if spec.kind == 'list' else 'One of'}:** {values}")
            elif spec.kind in ("int", "float", "bool"):
                facts.append(f"**Type:** {dict(int='whole number', float='number', bool='on / off')[spec.kind]}")
            elif spec.kind == "list":
                facts.append("**Type:** comma-separated list")
            if spec.secret:
                facts.append("**Secret**")
            elif spec.env_only:
                facts.append("**Environment only**")
            if spec.restart and not spec.secret:
                facts.append("**Applies on restart**")
            if spec.can_be_off:
                facts.append("**`off` turns it off**")
            out.append(entry(name, described.get(name) or from_code.get(name)
                             or notes.get(name) or shared.get(name, ""), facts))

    compose_only = [n for n in example_order if n not in SETTINGS]
    if compose_only:
        out += ["## Read by the compose file", "",
                "These never reach the doctor: the reference compose file reads them itself.", ""]
        for name in dict.fromkeys(compose_only):
            out.append(entry(name, described.get(name) or shared.get(name, ""), []))
    return "\n".join(out).rstrip() + "\n"


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text() != text:
            print(f"{OUT.relative_to(ROOT)} is out of date: run python scripts/gen_docs.py")
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
