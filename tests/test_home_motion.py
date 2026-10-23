"""The home page's motion layer (2026-10-19).

Three promises, each pinned here:

1. Reduced motion and no script both get the finished page. The server never
   sends the `motion` class; the head line adds it only when the reader has not
   asked for reduced motion (or has turned motion on with the footer toggle);
   and every animation, transition and hover movement in home-light.css is
   scoped under `.motion`.
2. No animation can show a wrong number. Every figure is the server's final
   value in the HTML, the script never writes text (the toggle's own label
   aside), and the figure animation moves and fades the glyphs only.
3. Nothing needs script to be read: the headline, the response card, the
   story's three drawings are complete in the HTML.
4. The scroll story (home-story.js: GSAP, ScrollTrigger, Lenis, vendored and
   home-only) writes no text, moves and fades only, and tears itself down
   when motion is switched off.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_home_and_search import _drawable, _seed, client, db  # noqa: F401

STATIC = Path(__file__).resolve().parents[1] / "src" / "report" / "static"
CSS = (STATIC / "home-light.css").read_text(encoding="utf-8")
JS = (STATIC / "home-motion.js").read_text(encoding="utf-8")
# The scroll story (GSAP + ScrollTrigger + Lenis). Same rule as home-motion.js,
# with no exception at all: it never sets any text.
STORY_JS = (STATIC / "home-story.js").read_text(encoding="utf-8")
# The drifting gradient layer (raw WebGL). Same rule: it sets no text.
BG_JS = (STATIC / "home-bg.js").read_text(encoding="utf-8")
WRITES = re.compile(r"\.(textContent|innerHTML|innerText|outerHTML|nodeValue)\s*=(?!=)")


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, body) for every ordinary rule, keyframes and @media unwrapped."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@keyframes[^{]+\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", css)
    out = []
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        sel = sel.strip()
        if sel.startswith("@media"):
            sel = sel.split(")", 1)[-1].strip()
        out.append((sel, body))
    return out


def test_the_server_never_sends_motion_on():
    """The class is the switch, and it is never in the served HTML."""
    from src.report.home_page import _shell

    html = _shell("t", "<main></main>", film="none", theme="light")
    assert re.search(r'<html[^>]*class="[^"]*motion', html) is None
    # The head line turns it on unless the reader turned it off with the toggle
    # (2026-10-23: the OS reduce-motion setting is no longer read; Windows
    # reports it for anyone with animation effects off).
    head = html.split("</head>", 1)[0]
    assert "p!=='off'" in head
    assert "p==='on'||(p!=='off'&&!r)" not in head


def test_every_animation_transition_and_hover_move_is_scoped_under_motion():
    # Movement is an animation, a transition, or a transform applied on hover.
    # A static transform (the inspector's tick mark is a rotated border) places
    # a shape and moves nothing.
    moving = re.compile(r"\b(animation|transition)\s*:")
    offenders = []
    for sel, body in _rules(CSS):
        hover_move = ":hover" in sel and re.search(r"transform\s*:", body)
        if not (moving.search(body) or hover_move):
            continue
        for part in sel.split(","):
            if not part.strip().startswith(".motion"):
                offenders.append(part.strip())
    # The static half may still place things with transform (none does today).
    assert offenders == [], offenders


def test_nothing_is_hidden_outside_motion():
    """`.pre` (set aside before a reveal) exists only under `.motion`, so a
    reader without motion never meets a hidden element."""
    for sel, body in _rules(CSS):
        if ".pre" in sel or "opacity: 0" in body.replace("opacity:0", "opacity: 0"):
            for part in sel.split(","):
                part = part.strip()
                if ".pre" in part or "opacity: 0" in body:
                    # The decorative ring and the loop's second copy are the two
                    # static exceptions: neither carries content a reader needs.
                    # The pill tabs' radio inputs are visually hidden by design
                    # (the label is the control), not hidden content.
                    if part in (".lhome .illo .iring", ".lhome .uc-in"):
                        continue
                    assert part.startswith(".motion"), part


def test_the_script_never_writes_a_figure_or_any_page_text():
    writes = re.findall(r"\.(textContent|innerHTML|innerText|outerHTML|nodeValue)\s*=(?!=)", JS)
    assert writes == ["textContent"], writes
    line = next(ln for ln in JS.splitlines() if re.search(r"\.textContent\s*=(?!=)", ln))
    assert '"Motion: on"' in line and '"Motion: off"' in line, "only the toggle's own label"
    assert "insertAdjacentHTML" not in JS and "document.write" not in JS
    assert "createTextNode" not in JS


def test_the_story_script_never_writes_text_and_only_moves_and_fades():
    assert WRITES.findall(STORY_JS) == []
    for banned in ("insertAdjacentHTML", "document.write", "createTextNode",
                   ".textContent", ".innerHTML", ".innerText", "snap:", "SplitText"):
        assert banned not in STORY_JS, banned
    # GSAP may only touch position, scale, opacity and visibility: the keys of
    # every tween vars object, apart from timing and trigger settings.
    allowed = {"opacity", "autoAlpha", "x", "y", "yPercent", "scaleX", "scaleY",
               "transformOrigin", "duration", "ease", "stagger", "scrollTrigger",
               "clearProps", "defaults"}
    found = (re.findall(r"gsap\.(?:to|from|fromTo|set)\([^{]*(\{[^{}]*\})", STORY_JS)
             + re.findall(r"tl\.(?:to|from|fromTo)\([^{]*(\{[^{}]*\})", STORY_JS))
    assert len(found) >= 10, "the pattern still finds the tweens"
    for vars_ in found:
        keys = set(re.findall(r"([A-Za-z]+)\s*:", vars_))
        assert keys <= allowed, keys - allowed


def test_the_story_runs_only_with_motion_and_tears_down_when_it_goes():
    # Starts only when <html> has `motion`, and listens for the toggle.
    assert 'root.classList.contains("motion")' in STORY_JS
    assert '"bp-motion"' in STORY_JS and "dispatchEvent" in JS
    # Teardown: every trigger killed, every tween reverted, Lenis destroyed,
    # the ticker callback removed, inline transform/opacity cleared.
    down = STORY_JS.split("function down()", 1)[1].split("function start()", 1)[0]
    for step in ("mm.revert()", ".kill()", "lenis.destroy()", "ticker.remove(",
                 "clearProps", 'classList.remove("rail-on", "how-on")'):
        assert step in down, step
    # Never clears every inline style: band heights are the drawing's scale.
    assert 'clearProps: "all"' not in STORY_JS and "clearProps:'all'" not in STORY_JS
    # The stage's tall track and sticky layout exist only while the story runs.
    for sel, _body in _rules(CSS):
        if any(k in sel for k in ("rail-pin", "rail-stage", "hlayer", "hstep", "hstory")):
            if re.search(r"height:\s*\d+vh|position:\s*sticky", _body):
                assert all(p.strip().startswith((".motion.rail-on", ".motion.how-on"))
                           for p in sel.split(",")), sel


def test_the_story_libraries_are_vendored_and_loaded_on_the_home_page_only(client):
    _seed("JPM", _drawable(), sector="Financials")
    home = client.get("/").text
    for f in ("gsap.min.js", "ScrollTrigger.min.js", "lenis.min.js"):
        assert (STATIC / "vendor" / f).exists(), f
        assert f'src="/static/vendor/{f}?v=' in home, f
    assert 'src="/static/home-story.js?v=' in home
    # No third-party script or stylesheet on the home page.
    scripts = re.findall(r'<script[^>]+src="([^"]+)"', home)
    sheets = re.findall(r'<link[^>]+rel="(?:stylesheet|preload)"[^>]+href="([^"]+)"', home)
    assert [s for s in scripts + sheets if re.match(r"(https?:)?//", s)] == []
    other = client.get("/pricing").text
    assert "/static/vendor/" not in other and "home-story.js" not in other


def test_the_figure_animation_moves_and_fades_only():
    frames = re.search(r"@keyframes bp-num\s*\{(.*?)\}\s*\}", CSS, re.S).group(1)
    props = set(re.findall(r"([a-z-]+)\s*:", frames))
    assert props <= {"opacity", "transform"}, props
    assert "counter-increment" not in CSS and "counter(" not in CSS
    assert "content: attr(" not in CSS


def test_the_figures_in_the_html_are_the_final_counts(client):
    from src.company.stats import counts, identity_breakdown, reset_breakdown_cache

    reset_breakdown_cache()
    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(), sector="Information Technology")
    body = client.get("/").text
    nums = re.findall(r'<span class="num">([^<]+)</span>', body)
    c = counts()
    bd = identity_breakdown(max_age_s=0)
    expected = {f"{c['companies']:,}", f"{bd['reconciled']:,}", f"{bd['flagged']:,}",
                f"{bd['reconciled'] + bd['flagged']:,}"}
    assert expected <= set(nums), (expected, nums)


def test_the_page_reads_completely_without_script(client):
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    no_script = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    text = " ".join(re.sub(r"<[^>]+>", " ", no_script).split())
    assert "Balance-checked SEC financials for production data pipelines." in text
    assert '"identity_status": "balanced"' in text.replace("&quot;", '"')
    assert "Built for quants, developers and AI-finance teams." in text
    # The toggle needs script, so it ships hidden.
    toggle = re.search(r'<button[^>]*id="motion-toggle"[^>]*>', body).group(0)
    assert " hidden" in toggle


def test_the_marquee_is_gone_and_no_ticker_loop_returns(client):
    """The ticker marquee was removed as decoration (story pass, 2026-10-19).
    Its old guard (every listed status came from the data) has nothing left to
    check, so this pins the removal instead."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    assert 'class="marquee"' not in body and "mq-item" not in body
    assert "bp-marquee" not in CSS


def test_the_story_leads_with_a_multi_line_filing_that_balances(client):
    """MSFT (or AAPL) leads the story when it is among the drawable
    suggestions and balances; otherwise the first drawable one, as before."""
    _seed("JPM", _drawable(), sector="Financials")
    _seed("MSFT", _drawable(), sector="Information Technology")
    body = client.get("/").text
    stage = body.split("hero-bs", 1)[1].split("</section>", 1)[0]
    if 'href="/company/MSFT"' not in body:
        return  # MSFT was not offered as a suggestion in this database
    ok = stage.split('class="bs-panel ok"', 1)[1].split("</figure>", 1)[0]
    assert "MSFT" in ok and 'href="/company/MSFT"' in stage


def test_the_not_testable_step_states_the_rule_and_invents_no_figure(client):
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    nt = body.split('class="ntpanel"', 1)[1].split("</figure>", 1)[0]
    assert "What the check needs" in nt and "<code>not_testable</code>" in nt
    assert "Total assets" in nt and "stated total" in nt
    # No money figure in the panel: the only number allowed is the live count.
    assert "$" not in nt
    assert re.sub(r'<span class="num">[^<]+</span>', "", nt).count("could not be tested") <= 1


def test_the_background_layer_writes_no_text_and_falls_back_to_a_static_gradient():
    """No-WebGL and no-script fallback: the canvas is hidden unless the GL
    layer has drawn (`gl-on`, motion only), and every band keeps a static CSS
    gradient underneath. The script returns quietly when WebGL is missing."""
    assert WRITES.findall(BG_JS) == []
    assert 'getContext("webgl"' in BG_JS and "if (!gl) return null" in BG_JS
    assert "visibilitychange" in BG_JS and "IntersectionObserver" in BG_JS, "paused off screen"
    assert 'classList.contains("motion")' in BG_JS and '"bp-motion"' in BG_JS
    rules = dict(_rules(CSS))
    assert "visibility: hidden" in rules[".lhome .bg-gl"]
    assert "radial-gradient" in rules[".lhome .bg"], "the static gradient"
    shown = [sel for sel, body in _rules(CSS) if "bg-gl" in sel and "visible" in body]
    assert shown and all(sel.startswith(".motion") for sel in shown)


def test_the_held_sections_and_the_typing_card_need_their_script_to_engage(client):
    """Fallback when the animation library fails: the layout classes that hold
    sections or hide lines are added only by script, never by the server, so
    without it the rail is a row, the method is four steps with pictures, and
    the response card shows every line."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    for cls in ("rail-on", "how-on", "armed", "typed", "gl-on", " cur", "sda"):
        assert f'class="{cls.strip()}' not in body and f' {cls.strip()}"' not in body, cls
    assert "ready()" in STORY_JS and "window.gsap && window.ScrollTrigger && window.Lenis" in STORY_JS
    # The lines are held only once the observer is armed, and only under motion.
    held = [sel for sel, b in _rules(CSS) if ".jcard" in sel and "clip-path" in b]
    assert held and all(sel.startswith(".motion .jcard.armed") for sel in held), held
    assert 'classList.add("armed")' in JS and 'classList.add("typed")' in JS
    # The typing reveals lines that are already in the HTML: nothing is written.
    card = body.split('class="jcard"', 1)[1].split("</figure>", 1)[0]
    assert '"identity_basis": "as_filed"' in card.replace("&quot;", '"')


def test_reduced_motion_gets_the_final_state_of_every_new_motion():
    """Every scroll-driven animation, held layout and card effect is under
    `.motion`; the head line never adds it under prefers-reduced-motion."""
    for sel, body in _rules(CSS):
        if re.search(r"animation-timeline|view-timeline|animation\s*:|animation-name", body):
            for part in sel.split(","):
                assert part.strip().startswith(".motion"), part


def test_the_method_story_uses_the_real_figures(client):
    """Step pictures are the filings' own numbers: JPM's lines and totals, and
    the flagged filing's gap, in the same money format as the drawings."""
    from tests.test_home_and_search import _seed_flagged_blank_cheque

    _seed("JPM", _drawable(), sector="Financials")
    _seed_flagged_blank_cheque()
    body = client.get("/").text
    how = body.split('id="how"', 1)[1].split("</section>", 1)[0]
    assert 'data-layer="read"' in how and 'data-layer="test"' in how and 'data-layer="flag"' in how
    assert '<span class="hv-tk">JPM</span>' in how and '<span class="hv-tk">AACB</span>' in how
    assert "Not read from this filing" in how and "flagged: a line item did not reach us" in how
    assert "%" not in re.sub(r"<[^>]+>", "", how)


# ------------------------------------------------ the climb (layered hero scene)
HERO_MEDIA = STATIC / "media" / "hero"
# v3 layer set (2026-10-20): the sky, the ridges, the towers under the brass
# beam, a dark block at each edge, two distinct clouds, and the still (the
# composite of the opening frame that every non-motion reader gets).
HERO_FILES = ("sky-1600", "sky-640", "ridges-v3-2560", "ridges-v3-1280",
              "towers-1639", "towers-1024", "fg-left-824", "fg-right-825",
              "cloud-a-1600", "cloud-a-1280", "cloud-b-1600", "cloud-b-1280",
              "still-2560", "still-1280")
SCENE_LAYERS = ("climb-sky", "climb-ridges", "climb-towers", "climb-fg-l", "climb-fg-r",
                "climb-cloud-a", "climb-cloud-b")


def test_the_climb_reads_as_one_finished_still_without_script(client):
    """No script: the scene is in the HTML as one still with its copy and the
    bubble, no figure, and nothing in the static CSS hides or pins it."""
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    no_script = re.sub(r"<script.*?</script>", "", body, flags=re.S)
    assert 'class="climb"' in no_script and 'class="climb-still"' in no_script
    assert "climb-fig" not in body and "<svg" not in body.split('class="climb"', 1)[1].split("</section>", 1)[0]
    text = " ".join(re.sub(r"<[^>]+>", " ", no_script).split()).replace("&rsquo;", "'")
    for line in ("Every number has to hold its ground.",
                 "Up here, assets equal liabilities plus equity, or we tell you why not.",
                 "Climb past the noise.",
                 "Thousands of filings climb onto the SEC each quarter.",
                 "Balance-checked SEC financials, delivered as an API.",
                 "Here's how it works."):
        assert line in text, line
    # The scene sits before the hero, so the hero's own order is untouched.
    assert body.index('class="climb"') < body.index('class="htitle"')
    # Track height, sticky stage and hidden start states are motion-only.
    for sel, body_ in _rules(CSS):
        if "climb" not in sel:
            continue
        if re.search(r"height:\s*\d+vh|position:\s*sticky|opacity:\s*0\b", body_):
            assert all(p.strip().startswith(".motion") for p in sel.split(",")), sel
    # Without motion the layered scene is display: none (its lazy images are
    # never fetched) and the still shows; only .motion swaps them.
    rules = dict(_rules(CSS))
    assert "display: none" in rules[".lhome .climb-scene"]
    assert "display: none" in rules[".motion .climb-still"]
    assert "display: block" in rules[".motion .climb-scene"]


def test_the_climb_display_line_is_not_a_second_h1(client):
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    assert body.count("<h1") == 1
    assert re.search(r'<p class="climb-line"[^>]*>Every number has to hold its ground\.</p>', body)


def test_the_climb_images_exist_are_small_and_are_referenced(client):
    _seed("JPM", _drawable(), sector="Financials")
    body = client.get("/").text
    for name in HERO_FILES:
        f = HERO_MEDIA / f"{name}.webp"
        assert f.exists(), f
        assert f.stat().st_size < 250 * 1024, (name, f.stat().st_size)
        assert re.search(rf'/static/media/hero/{re.escape(name)}\.webp\?v=\d+', body), name
    # Nothing else in the folder: the old candlestick plates are gone.
    assert sorted(p.stem for p in HERO_MEDIA.glob("*")) == sorted(HERO_FILES)
    scene = body.split('class="climb-scene"', 1)[1].split('class="climb-bubble"', 1)[0]
    for layer in SCENE_LAYERS:
        assert f"{layer}" in scene, layer
    # Every layer image is lazy, so a hidden one is never fetched: phones and
    # the still never pull the scene, the climb never pulls the still. The
    # first frame comes from <head> instead, gated by the same screen test.
    climb = body.split('class="climb"', 1)[1].split("</section>", 1)[0]
    for tag in re.findall(r"<img[^>]*>", climb):
        assert 'loading="lazy"' in tag, tag
    head = body.split("</head>", 1)[0]
    pre = re.findall(r'<link rel="preload" as="image"[^>]*>', head)
    assert len(pre) == 2
    towers, still = pre
    assert "towers-1024.webp" in towers and "(min-width: 900px) and (min-height: 700px)" in towers
    assert "still-1280.webp" in still and "(max-width: 899px)" in still
    # The preload's candidates and sizes match the <img> exactly (else the
    # browser fetches twice).
    for pl, cls in ((towers, 'climb-towers"><span class="climb-glow"></span><img'),
                    (still, '<img class="climb-still"')):
        img = re.search(re.escape(cls) + r"[^>]*>", climb).group(0)
        assert re.search(r'imagesrcset="([^"]+)"', pl).group(1) == re.search(r' srcset="([^"]+)"', img).group(1)
        assert re.search(r'imagesizes="([^"]+)"', pl).group(1) == re.search(r' sizes="([^"]+)"', img).group(1)


def test_the_climb_lag_fix_holds_for_every_image_layer():
    """A layer holding only an <img> that the timeline scales or moves gets a
    near-transparent fill (so Chrome keeps its raster) and will-change."""
    rules = _rules(CSS)
    fill = [s for s, b in rules if "rgba(0, 0, 0, .004)" in b]
    assert fill, "the near-transparent fill rule"
    for part in (".motion .climb-ridges", ".motion .climb-fg", ".motion .climb-cloud"):
        assert any(part in s for s in fill), part
    wc = [s for s, b in rules if "will-change: transform, opacity" in b]
    for part in (".motion .climb-cam", ".motion .climb-plate", ".motion .climb-fg",
                 ".motion .climb-cloud", ".motion .climb-sky", ".motion .climb-bubble"):
        assert any(part in s for s in wc), part


def test_the_climb_motion_writes_no_text_and_tears_down():
    """The climb lives in home-story.js (no new script file): it inherits the
    no-text-write and tween-key checks above; here, its own pieces."""
    climb = STORY_JS.split("function climb()", 1)[1].split("function parallax()", 1)[0]
    assert WRITES.findall(climb) == [] and "innerHTML" not in climb
    assert 'scrollTrigger' in climb and 'scrub' in climb and "pin:" not in climb
    # The two clouds are separate layers on their own tweens (two speeds).
    assert '".climb-cloud-a"' in climb and '".climb-cloud-b"' in climb
    # The lazy layers are fetched and decoded before they scroll in.
    assert 'img.loading = "eager"' in climb and "decode()" in climb
    down = STORY_JS.split("function down()", 1)[1].split("function start()", 1)[0]
    for sel in (".climb-cam", ".climb-fg", ".climb-sky", ".climb-bubble", ".climb-cloud"):
        assert sel in down, sel
    assert ".climb-blue" not in STORY_JS
    # Every script the home page loads is one this file already checks.
    assert sorted(p.name for p in STATIC.glob("home*.js")) == [
        "home-bg.js", "home-motion.js", "home-story.js", "home.js"]
