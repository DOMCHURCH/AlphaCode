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
    veil_borne_text,
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


@pytest.mark.parametrize("label,value,deterministic", veil_borne_text())
def test_the_footer_and_form_labels_pass_aa(label, value, deterministic):
    """These measured 1.88:1 and 1.03:1 with nothing but a `text-shadow` under
    them, at the foot of a phone screen where the veil thins to a=0.16."""
    assert deterministic, f"{label} must not depend on the film"
    assert value >= AA, f"{label} is {value:.2f}:1"


def test_the_footer_would_still_fail_without_its_ground():
    """The property, not the number.

    Asserting that the OLD situation still fails is what stops somebody
    deleting `footer { background: var(--well) }` and reading the passing
    ratios above as though they still applied. That is exactly how the tab
    strip shipped with a comment claiming a fix it had not made.
    """
    from scripts.check_contrast import DIM

    mobile_veil = over((10, 10, 10), 0.16, CLOUD)
    assert ratio(INK2, mobile_veil) < AA, "footer body on the bare veil"
    assert ratio(DIM, mobile_veil) < AA, "the disclaimer line on the bare veil"
    assert ratio(INK, mobile_veil) < AA, "field labels on the bare veil"

    assert ratio(INK2, WELL) >= AA
    assert ratio(DIM, WELL) >= AA
    assert ratio(INK, WELL) >= AA


def test_a_text_shadow_is_not_counted_as_contrast():
    """The mitigation that was there before, stated as a rule.

    A shadow changes how text LOOKS against a bright frame and changes nothing
    about the measured ratio, because WCAG measures foreground against
    background. If a stylesheet ever leans on one again, this is the note
    saying why it does not count.
    """
    mobile_veil = over((10, 10, 10), 0.16, CLOUD)
    with_shadow_still = ratio(INK, mobile_veil)
    assert with_shadow_still == pytest.approx(1.88, abs=0.02)
    assert with_shadow_still < AA


def test_the_bare_veil_numbers_stay_on_the_report():
    """Not a failure -- the default any new element inherits on this site.

    Kept visible so the next person putting text on the page without a surface
    under it sees what that costs before shipping it.
    """
    rows = on_the_veil()
    assert rows, "the bare-veil reference must not quietly disappear"
    assert all(not deterministic for _, _, deterministic in rows)
    assert any(value < AA for _, value, _ in rows)
