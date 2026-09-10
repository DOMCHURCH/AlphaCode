"""WCAG AA on the surfaces that are built out of alpha fills.

The site's look is translucent panels over a backdrop film, which makes
contrast a thing that cannot be checked by reading a hex value: the number that
matters is the COMPOSITE, and where the ground is a video it changes as the
video plays. A fix shipped once with a comment claiming those surfaces had been
made opaque when they had not, and it took compositing the stack by hand to
notice. This is that arithmetic, as assertions.

What is enforced is narrower than "everything passes AA": every DETERMINISTIC
ground must pass. A surface whose ratio depends on the frame is reported and
not enforced, because the honest fix there is an opaque surface rather than a
better colour.
"""

from __future__ import annotations

import pytest

from scripts.check_contrast import (
    AA,
    CLOUD,
    INK,
    INK2,
    WELL,
    on_the_veil,
    over,
    ratio,
    solid_text,
    tab_surfaces,
)


@pytest.mark.parametrize("label,value,deterministic", tab_surfaces())
def test_the_tab_strip_passes_aa(label, value, deterministic):
    """`.tab.on` measured 1.64:1 on a phone before `.tabs` got an opaque
    ground -- a selected tab label that is effectively invisible over the
    bright half of the backdrop."""
    assert deterministic, f"{label} must not depend on the film"
    assert value >= AA, f"{label} is {value:.2f}:1"


@pytest.mark.parametrize("label,value,deterministic", solid_text())
def test_text_on_solid_surfaces_passes_aa(label, value, deterministic):
    assert value >= AA, f"{label} is {value:.2f}:1"


def test_the_tab_strip_no_longer_depends_on_the_backdrop():
    """The property, not just the number.

    Composited over the brightest band of the backdrop instead of over the
    opaque strip, `.tab.on` would be 1.64:1 on mobile. Asserting that the old
    ground would still fail is what stops somebody restoring
    `.tabs { background: transparent }` and re-reading the passing numbers
    above as though they still applied.
    """
    mobile_veil = over((10, 10, 10), 0.16, CLOUD)
    if_transparent = over((255, 255, 255), 0.16, mobile_veil)
    assert ratio(INK, if_transparent) < AA

    on_opaque = over((255, 255, 255), 0.16, WELL)
    assert ratio(INK, on_opaque) >= AA


def test_the_dim_token_is_the_raised_one():
    """`--dim` was #71748A at 4.25:1 and is #8B8EA3 at 6.04:1. It is used on 32
    rules across the stylesheets, so a revert here is a site-wide regression."""
    from scripts.check_contrast import BG, DIM

    assert ratio(DIM, BG) >= AA
    assert ratio(INK2, BG) >= AA


def test_the_veil_cases_are_reported_rather_than_silently_passing():
    """These are the ones still outstanding, and the suite should say so rather
    than let them look like they were checked and found fine.

    Text with nothing opaque behind it -- field labels, the terms line -- sits
    on a `position: fixed` veil whose gradient reaches a=0.16 at the foot of
    the viewport. `--ink` measures 1.88:1 there. A `text-shadow` is the current
    mitigation and WCAG does not count one.
    """
    rows = on_the_veil()
    assert rows, "the veil cases must not quietly disappear from the report"
    assert all(not deterministic for _, _, deterministic in rows)
    assert any(value < AA for _, value, _ in rows), (
        "if these now pass, give them an opaque ground and enforce them"
    )
