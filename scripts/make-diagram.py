"""Generate the README pipeline diagram in both themes from one source.

    python scripts/make-diagram.py

Re-run after editing and commit both SVGs. Stdlib only, no dependencies.

GitHub serves README images as isolated documents, so `currentColor` and CSS
media queries can't be relied on - the two themes are two files, swapped by a
<picture> element. Generating both from here keeps them from drifting.
"""
from pathlib import Path

LIGHT = dict(
    plane="#f9f9f7", surface="#ffffff", ink="#0b0b0b", ink2="#52514e",
    muted="#8a8880", hairline="#dcdbd4", accent="#2a78d6",
    accent_soft="#eef4fd", chip="#f2f1ed",
)
DARK = dict(
    plane="#0d0d0d", surface="#1c1c1a", ink="#ffffff", ink2="#c3c2b7",
    muted="#8e8c85", hairline="#34342f", accent="#5a9fee",
    accent_soft="#16243a", chip="#242421",
)

W, H = 980, 340
BAR_X, BAR_Y, BAR_W, BAR_H = 20, 16, 904, 46
BOUNDARY_Y = 86
STAGE_Y, STAGE_H, STAGE_W, GAP, STAGE_X0 = 108, 96, 178, 58, 20
BUS_Y = 244
CHIP_Y, CHIP_H, CHIP_W, CHIP_GAP = 272, 46, 250, 24
RETURN_X = 954

STAGES = [
    ("Detect", "rules", ["thresholds, exit codes,", "health checks"], None),
    ("Gather", "evidence", ["logs and stats,", "secrets stripped"], None),
    ("Diagnose", "the model", ["explains why it broke,"], "picks 1 of 5 actions"),
    ("Decide", "plain code", ["mode · confidence ·", "circuit breakers"], None),
]
ARROWS = [("incident", False), ("redacted", False), ("advisory", True)]
CHIPS = [
    ("Notify you", "nothing is touched"),
    ("Await approval", "you click, it acts"),
    ("Restart, then verify", "auto mode only"),
]


# Average glyph widths for the system sans, calibrated against the real
# rendered widths of this diagram's own labels. Only accurate to ~10%, which is
# why `fits` below keeps a margin - it exists to catch a copy edit that no
# longer fits its box, not to typeset.
_CHAR_W_REGULAR = 0.50
_CHAR_W_BOLD = 0.56


def text_width(text: str, size: float, bold: bool = False) -> float:
    per_char = _CHAR_W_BOLD if bold else _CHAR_W_REGULAR
    # The middle dot and spaces are narrower; capitals and digits wider.
    wide = sum(1 for ch in text if ch.isupper() or ch.isdigit())
    narrow = sum(1 for ch in text if ch in " \u00b7,.")
    return size * (per_char * len(text) + 0.14 * wide - 0.16 * narrow)


def check_fits(label: str, width: float, available: float, where: str) -> None:
    if width > available:
        raise SystemExit(
            f"diagram: {where} does not fit - {label!r} needs ~{width:.0f}px "
            f"but only {available:.0f}px is available. Shorten it, widen the box, "
            f"or drop the font size."
        )


def stage_x(i):
    return STAGE_X0 + i * (STAGE_W + GAP)


def chip_x(i):
    total = len(CHIPS) * CHIP_W + (len(CHIPS) - 1) * CHIP_GAP
    return (W - total) / 2 + i * (CHIP_W + CHIP_GAP)


def esc(t):
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def check_layout() -> None:
    last_edge = stage_x(len(STAGES) - 1) + STAGE_W
    if last_edge > RETURN_X - 12:
        raise SystemExit(
            f"diagram: the stage row ends at {last_edge:.0f} but the return path runs "
            f"at {RETURN_X} - they would cross."
        )
    if RETURN_X + 8 > W:
        raise SystemExit("diagram: the return path falls outside the viewBox.")


def build(c):
    o = []
    a = o.append
    a(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
      f'role="img" aria-label="How Container Doctor works. Rules watching the containers open an '
      f'incident; evidence is gathered and secrets stripped; a model explains the failure and '
      f'proposes one of five fixed actions, advisory only; a deterministic policy engine then '
      f'either notifies you, waits for your approval, or restarts the container and verifies it. '
      f'Everything crosses a socket proxy that blocks exec, volumes and secrets, and restarting '
      f'is the only write path back to the containers.">')
    a('<defs>')
    for ident, colour in (("a", c["muted"]), ("aa", c["accent"])):
        a(f'<marker id="{ident}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6" '
          f'markerHeight="6" orient="auto-start-reverse">'
          f'<path d="M0,1 L9,5 L0,9 z" fill="{colour}"/></marker>')
    a('</defs>')
    a(f'<rect width="{W}" height="{H}" fill="{c["plane"]}"/>')
    a('<g font-family="system-ui,-apple-system,Segoe UI,sans-serif">')

    # --- what is being watched ------------------------------------------
    a(f'<rect x="{BAR_X}" y="{BAR_Y}" width="{BAR_W}" height="{BAR_H}" rx="10" '
      f'fill="{c["surface"]}" stroke="{c["hairline"]}"/>')
    a(f'<text x="{BAR_X + 20}" y="{BAR_Y + 29}" font-size="15" font-weight="600" '
      f'fill="{c["ink"]}">Containers on the host</text>')
    a(f'<text x="{BAR_X + BAR_W - 20}" y="{BAR_Y + 29}" font-size="12" text-anchor="end" '
      f'fill="{c["muted"]}">Compose stacks, however deployed</text>')

    # --- the boundary both directions cross ------------------------------
    a(f'<line x1="18" y1="{BOUNDARY_Y}" x2="{RETURN_X + 8}" y2="{BOUNDARY_Y}" '
      f'stroke="{c["hairline"]}" stroke-width="1.5" stroke-dasharray="6 5"/>')
    a(f'<rect x="310" y="{BOUNDARY_Y - 11}" width="356" height="22" fill="{c["plane"]}"/>')
    a(f'<text x="488" y="{BOUNDARY_Y + 4}" font-size="11.5" text-anchor="middle" '
      f'fill="{c["muted"]}">socket proxy · no exec, volumes or secrets</text>')

    read_x = BAR_X + 76
    a(f'<path d="M{read_x},{BAR_Y + BAR_H} L{read_x},{STAGE_Y - 7}" stroke="{c["muted"]}" '
      f'stroke-width="1.5" fill="none" marker-end="url(#a)"/>')
    a(f'<text x="{read_x + 12}" y="{BOUNDARY_Y - 7}" font-size="11" fill="{c["muted"]}">'
      f'events · stats · logs</text>')

    # --- pipeline ---------------------------------------------------------
    for i, (title, kicker, subs, accent_sub) in enumerate(STAGES):
        x = stage_x(i)
        is_model = accent_sub is not None
        a(f'<rect x="{x}" y="{STAGE_Y}" width="{STAGE_W}" height="{STAGE_H}" rx="10" '
          f'fill="{c["accent_soft"] if is_model else c["surface"]}" '
          f'stroke="{c["accent"] if is_model else c["hairline"]}" '
          f'stroke-width="{1.75 if is_model else 1}"/>')
        a(f'<text x="{x + 16}" y="{STAGE_Y + 27}" font-size="15" font-weight="600" '
          f'fill="{c["ink"]}">{esc(title)}</text>')
        a(f'<text x="{x + STAGE_W - 16}" y="{STAGE_Y + 27}" font-size="11" text-anchor="end" '
          f'fill="{c["accent"] if is_model else c["muted"]}">{esc(kicker)}</text>')
        inner = STAGE_W - 32
        check_fits(title, text_width(title, 15, True) + text_width(kicker, 11) + 12,
                   inner, f"{title} box heading row")
        y = STAGE_Y + 50
        for line in subs:
            check_fits(line, text_width(line, 12), inner, f"{title} box")
            a(f'<text x="{x + 16}" y="{y}" font-size="12" fill="{c["ink2"]}">{esc(line)}</text>')
            y += 16
        if accent_sub:
            check_fits(accent_sub, text_width(accent_sub, 11.5, True), inner,
                       f"{title} box accent line")
            a(f'<text x="{x + 16}" y="{y + 2}" font-size="11.5" font-weight="600" '
              f'fill="{c["accent"]}">{esc(accent_sub)}</text>')

    for i, (label, is_model) in enumerate(ARROWS):
        x1, x2 = stage_x(i) + STAGE_W, stage_x(i + 1)
        mid, y = (x1 + x2) / 2, STAGE_Y + STAGE_H / 2
        colour = c["accent"] if is_model else c["muted"]
        # An arrow label wider than the gap sits on the neighbouring box borders.
        check_fits(label, text_width(label, 11), GAP - 8, f"arrow label after {STAGES[i][0]}")
        a(f'<path d="M{x1 + 5},{y} L{x2 - 6},{y}" stroke="{colour}" stroke-width="1.5" '
          f'fill="none" marker-end="url(#{"aa" if is_model else "a"})"/>')
        a(f'<text x="{mid}" y="{y - 10}" font-size="11" text-anchor="middle" '
          f'fill="{colour}">{esc(label)}</text>')

    # --- fan out to the three outcomes -----------------------------------
    decide_cx = stage_x(3) + STAGE_W / 2
    centres = [chip_x(i) + CHIP_W / 2 for i in range(len(CHIPS))]
    a(f'<path d="M{decide_cx},{STAGE_Y + STAGE_H} L{decide_cx},{BUS_Y}" '
      f'stroke="{c["muted"]}" stroke-width="1.5" fill="none"/>')
    # The bus must reach the drop-point as well as every chip, or it hangs in air.
    a(f'<path d="M{min(centres + [decide_cx])},{BUS_Y} L{max(centres + [decide_cx])},{BUS_Y}" '
      f'stroke="{c["muted"]}" stroke-width="1.5" fill="none"/>')

    for i, (title, sub) in enumerate(CHIPS):
        x, cx = chip_x(i), centres[i]
        check_fits(title, text_width(title, 13, True), CHIP_W - 32, f"{title} chip")
        check_fits(sub, text_width(sub, 11), CHIP_W - 32, f"{title} chip subtitle")
        a(f'<path d="M{cx},{BUS_Y} L{cx},{CHIP_Y - 7}" stroke="{c["muted"]}" '
          f'stroke-width="1.5" fill="none" marker-end="url(#a)"/>')
        a(f'<rect x="{x}" y="{CHIP_Y}" width="{CHIP_W}" height="{CHIP_H}" rx="23" '
          f'fill="{c["chip"]}" stroke="{c["hairline"]}"/>')
        a(f'<text x="{cx}" y="{CHIP_Y + 21}" font-size="13" font-weight="600" '
          f'text-anchor="middle" fill="{c["ink"]}">{esc(title)}</text>')
        a(f'<text x="{cx}" y="{CHIP_Y + 37}" font-size="11" text-anchor="middle" '
          f'fill="{c["muted"]}">{esc(sub)}</text>')

    # --- the write path, back across the boundary -------------------------
    right_edge = chip_x(len(CHIPS) - 1) + CHIP_W
    a(f'<path d="M{right_edge + 4},{CHIP_Y + CHIP_H / 2} L{RETURN_X},{CHIP_Y + CHIP_H / 2} '
      f'L{RETURN_X},{BAR_Y + BAR_H / 2} L{BAR_X + BAR_W + 6},{BAR_Y + BAR_H / 2}" '
      f'stroke="{c["muted"]}" stroke-width="1.5" fill="none" stroke-dasharray="5 4" '
      f'marker-end="url(#a)"/>')
    a(f'<text x="{RETURN_X - 12}" y="{BUS_Y + 18}" font-size="11" text-anchor="end" '
      f'fill="{c["muted"]}">the only write path</text>')

    a('</g></svg>')
    return "\n".join(o)


check_layout()
out = Path(__file__).resolve().parents[1] / "docs" / "src" / "assets"
(out / "how-it-works-light.svg").write_text(build(LIGHT))
(out / "how-it-works-dark.svg").write_text(build(DARK))
print("wrote both variants")
