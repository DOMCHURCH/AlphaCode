"""WCAG contrast for the surfaces that are built out of alpha fills.

Run it: `python scripts/check_contrast.py`

The site's look is translucent panels over a backdrop film, and that makes
contrast a thing you cannot check by looking at a hex value in a stylesheet.
The number that matters is the COMPOSITE -- the fill over whatever is behind
it -- and where "behind it" is a video, the ratio changes as the video plays.
That is how a fix shipped once already with a comment claiming it had made
those surfaces opaque when it had not.

So the rule this file enforces is narrower than "everything passes AA": every
piece of text must sit on a ground that is DETERMINISTIC. A surface whose
contrast depends on the frame cannot be measured once and trusted, and a number
measured on the dark half of a sunset is not a result.

`tests/test_contrast.py` runs the same checks as assertions.
"""

from __future__ import annotations


def _srgb(channel: float) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = rgb
    return 0.2126 * _srgb(r) + 0.7152 * _srgb(g) + 0.0722 * _srgb(b)


def ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    """WCAG 2.x contrast ratio. Order does not matter."""
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def over(
    top: tuple[float, float, float], alpha: float, under: tuple[float, float, float]
) -> tuple[float, float, float]:
    """`top` at `alpha` composited over `under`. Straight alpha, no gamma --
    which is what a browser does for `background: rgba(...)`."""
    return tuple(top[i] * alpha + under[i] * (1 - alpha) for i in range(3))


def hexc(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(float(int(value[i:i + 2], 16)) for i in (0, 2, 4))


# Tokens, from the `:root` block of glass.css. Kept here by hand rather than
# parsed: a parser would silently pass when it failed to find a token, which is
# the failure mode this whole file exists to avoid.
INK = hexc("F2F1F7")
INK2 = hexc("ADAFC0")
DIM = hexc("8B8EA3")
BG = hexc("0B0C12")
WELL = hexc("10121A")
PANEL_SOLID = hexc("14161F")

# The brightest band of static/media/backdrop.jpg -- the cloud layer at the
# horizon. The worst realistic case for anything drawn over the film.
CLOUD = (225.0, 205.0, 220.0)

AA = 4.5


def tab_surfaces() -> list[tuple[str, float, bool]]:
    """(label, ratio, deterministic) for the tab strip as it is now.

    `.tabs` carries an opaque ground, so each fill composites over a known
    colour rather than over the film.
    """
    tab = over((10, 11, 17), 0.62, WELL)
    hover = over((10, 11, 17), 0.78, WELL)
    on = over((255, 255, 255), 0.16, WELL)
    return [
        (".tab        (--ink2)", ratio(INK2, tab), True),
        (".tab:hover  (--ink)", ratio(INK, hover), True),
        (".tab.on     (--ink)", ratio(INK, on), True),
    ]


def solid_text() -> list[tuple[str, float, bool]]:
    out = []
    for name, fg in (("--ink", INK), ("--ink2", INK2), ("--dim", DIM)):
        for gname, bg in (("--bg", BG), ("--panel-solid", PANEL_SOLID), ("--well", WELL)):
            out.append((f"{name:<7} on {gname:<14}", ratio(fg, bg), True))
    return out


def veil_borne_text() -> list[tuple[str, float, bool]]:
    """Text that used to sit on the veil and now has an opaque ground.

    The footer disclaimer, the field labels and the terms line. Each one is
    `--well` now, so the ratio is a fixed number rather than a function of the
    frame -- which is the whole difference between a fix and a mitigation. The
    previous attempt was a `text-shadow`, and a shadow is not a contrast ratio:
    WCAG measures foreground against background and does not count one.
    """
    return [
        ("footer body   (--ink2 on --well)", ratio(INK2, WELL), True),
        ("footer disc   (--dim  on --well)", ratio(DIM, WELL), True),
        (".slabel       (--ink  on --well)", ratio(INK, WELL), True),
        (".accept label (--ink  on --well)", ratio(INK, WELL), True),
    ]


def on_the_veil() -> list[tuple[str, float, bool]]:
    """What text WOULD measure with nothing opaque behind it.

    Kept after the footer and the labels were given a ground, because it is the
    reason they needed one: these are the numbers any NEW element inherits by
    default on this site, and the check to run before putting text somewhere
    with no surface under it.

    Reported and not enforced. The desktop veil bottoms out near a=0.59 (a 0.30
    radial over a 0.42 linear); the mobile gradient reaches a=0.16 at the foot
    of the viewport, and the veil is `position: fixed`, so content scrolls
    through it.
    """
    out = []
    for vname, alpha in (("desktop veil a=0.59", 0.594), ("mobile veil a=0.16", 0.16)):
        ground = over((10, 10, 10), alpha, CLOUD)
        for name, fg in (("--ink", INK), ("--ink2", INK2), ("--dim", DIM)):
            out.append((f"{name:<7} on {vname}", ratio(fg, ground), False))
    return out


def main() -> int:
    failures = 0
    for heading, rows in (
        ("Tab strip (opaque ground -- fixed number)", tab_surfaces()),
        ("Footer and form labels (opaque ground)", veil_borne_text()),
        ("Text on solid surfaces", solid_text()),
        ("With NO opaque ground -- what the veil alone gives", on_the_veil()),
    ):
        print(f"\n{heading}")
        for label, value, deterministic in rows:
            ok = value >= AA
            mark = "PASS" if ok else "FAIL"
            note = "" if deterministic else "   <- not deterministic"
            print(f"  {label:<34} {value:6.2f}:1  {mark}{note}")
            if deterministic and not ok:
                failures += 1

    print(
        "\nThe last block is reported, not enforced: that ground is a video "
        "frame.\nIt is what any element with no surface under it inherits, and "
        "it is the\nreason the footer and the form labels were given one."
    )
    print("FAILURES (deterministic surfaces under 4.5:1):", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
