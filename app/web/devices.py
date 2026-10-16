"""Naming the device a session was signed in from: "Sebastian's Pixel 8 Pro".

Browsers do not reveal the name someone gave their device. The closest thing
is the model, which Chromium browsers report through User-Agent Client Hints
(`navigator.userAgentData`) and the page passes on; elsewhere the User-Agent
string still says what kind of device and browser it is. A name typed on the
profile page beats both.
"""

from __future__ import annotations

import re

# Most specific first: Edge and Opera also say Chrome, Chrome also says Safari.
_BROWSERS = (
    (re.compile(r"EdgA?/|EdgiOS/"), "Edge"),
    (re.compile(r"OPR/|Opera"), "Opera"),
    (re.compile(r"SamsungBrowser/"), "Samsung Internet"),
    (re.compile(r"Firefox/|FxiOS/"), "Firefox"),
    (re.compile(r"Chrome/|CriOS/"), "Chrome"),
    (re.compile(r"Safari/"), "Safari"),
)

# The device each platform most likely is, when nothing names the model.
_KINDS = {
    "Windows": "Windows PC",
    "macOS": "Mac",
    "iOS": "iPhone",
    "iPadOS": "iPad",
    "Android": "Android device",
    "ChromeOS": "Chromebook",
    "Linux": "Linux PC",
}


def browser(user_agent: str) -> str:
    for pattern, name in _BROWSERS:
        if pattern.search(user_agent or ""):
            return name
    return ""


def platform(user_agent: str) -> str:
    ua = user_agent or ""
    if "iPad" in ua:
        return "iPadOS"
    if "iPhone" in ua or "iPod" in ua:
        return "iOS"
    if "Android" in ua:
        return "Android"
    if "CrOS" in ua:
        return "ChromeOS"
    if "Windows" in ua:
        return "Windows"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "macOS"
    if "Linux" in ua:
        return "Linux"
    return ""


def first_name(claims: dict) -> str:
    """What to call the owner: the given name, else the first word of the name."""
    for key in ("given_name", "name", "preferred_username"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value.split()[0] if key == "name" else value
    return ""


def describe(*, user_agent: str = "", model: str = "", hinted_platform: str = "",
             owner: str = "", label: str = "") -> dict[str, str]:
    """The session's display name and what it is made of."""
    os_name = _normalise(hinted_platform) or platform(user_agent)
    device = model or _KINDS.get(os_name, "")
    if label:
        name = label
    elif device:
        name = f"{owner}'s {device}" if owner else device
    else:
        name = "Unknown device"
    return {
        "name": name,
        "device": device,
        "platform": os_name,
        "browser": browser(user_agent),
    }


def _normalise(hinted: str) -> str:
    """Client Hints say "macOS" and "Windows" already; a few need a nudge."""
    hinted = (hinted or "").strip()
    return {"Chrome OS": "ChromeOS", "Chromium OS": "ChromeOS"}.get(hinted, hinted)
