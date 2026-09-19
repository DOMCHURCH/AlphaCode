"""View 1: the balance sheet drawn to scale.

Two columns of the same height, because assets equal liabilities plus equity.
The proportions are the information -- a bank, an airline and a software company
have completely different shapes, and you see it before you read anything.

Composition rules, all of which exist to keep the drawing honest:

  * A missing component is ABSENT and named, never imputed and never silently
    folded into "other".
  * "Other" is always total minus the components we actually have, and it says
    so. Where components are missing it also says how many, because otherwise
    "other" would quietly absorb them and look like a real line item.
  * Where only the totals exist, the simplified three-block version renders.
    That covers ~89% of filers and is a real answer, not a degraded one.
  * Negative equity is drawn below the baseline rather than hidden. A company
    that owes more than it owns should look like one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Asset components, top to bottom, most liquid first. The order is the story:
# cash at the top, the things hardest to turn back into cash at the bottom.
ASSET_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("cash", "Cash"),
    ("receivables", "Receivables"),
    ("inventory", "Inventory"),
    ("property_plant_equipment", "Property & equipment"),
    ("goodwill", "Goodwill"),
    ("intangibles", "Intangibles"),
)

# Claim components, most senior first: trade creditors, then lenders, then the
# owners' residual last -- which is the order they get paid in.
LIABILITY_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("accounts_payable", "Accounts payable"),
    ("long_term_debt", "Long-term debt"),
)

# A bank files a different balance sheet. It reports no InventoryNet and no
# AccountsPayableCurrent, so against the general set it renders as one
# undifferentiated block -- accurate and useless. Its money is in loans and
# securities, and it is funded by deposits, so those are its line items.
BANK_ASSET_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("cash", "Cash"),
    ("interbank_deposits", "Deposits with banks"),
    ("trading_securities", "Trading securities"),
    ("investment_securities", "Investment securities"),
    ("loans", "Loans"),
    ("goodwill", "Goodwill"),
    ("intangibles", "Intangibles"),
)
BANK_LIABILITY_COMPONENTS: tuple[tuple[str, str], ...] = (
    ("deposits", "Customer deposits"),
    ("short_term_borrowings", "Short-term borrowings"),
    ("long_term_debt", "Long-term debt"),
)

# Sector names that file a bank-shaped balance sheet. Matched loosely because
# the sector map mixes GICS-style and SIC-derived labels.
_BANKISH = ("financial", "bank")


def component_sets(
    sector: str | None,
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    """The line items to look for, chosen by what this kind of filer reports."""
    if sector and any(word in sector.lower() for word in _BANKISH):
        return BANK_ASSET_COMPONENTS, BANK_LIABILITY_COMPONENTS
    return ASSET_COMPONENTS, LIABILITY_COMPONENTS

# Label tiers, in share of total assets. A band is 3px per percent, so:
#   >= 9%  (27px+) fits the name and the value on two lines
#   >= 4.5% (13px+) fits one compact line
#   below that, only the legend carries it
# A label must never be taller than the band it sits in -- clipped text reads as
# a rendering bug and undermines trust in the numbers next to it.
FULL_LABEL_MIN_PCT = 9.0
COMPACT_LABEL_MIN_PCT = 4.5


@dataclass
class Block:
    """One drawn band."""

    key: str
    label: str
    value: float
    pct: float           # of total assets, so both columns share one scale
    kind: str            # asset | liability | equity
    tone: int = 0        # 0..5, position within its colour family
    is_remainder: bool = False
    note: str | None = None

    @property
    def label_style(self) -> str:
        if self.pct >= FULL_LABEL_MIN_PCT:
            return "full"
        if self.pct >= COMPACT_LABEL_MIN_PCT:
            return "compact"
        return "none"


@dataclass
class View1:
    ticker: str
    company_name: str | None
    sector: str | None
    period_end: dt.date
    filing_date: dt.date
    mode: str                       # detailed | totals_only
    assets: list[Block] = field(default_factory=list)
    claims: list[Block] = field(default_factory=list)
    total_assets: float = 0.0
    total_liabilities: float | None = None
    total_equity: float | None = None
    missing_components: list[str] = field(default_factory=list)
    # SEC's previous name for this registrant, and when it stopped applying.
    # Defaulted because every construction predates them and a missing former
    # name is the ordinary case, not an error.
    former_name: str | None = None
    former_name_until: dt.date | None = None
    # Set when total liabilities was computed from the identity rather than
    # read off the filing. Never left implicit.
    liabilities_derived_from: str | None = None
    # The filer's own stated `LiabilitiesAndStockholdersEquity`, as filed, or
    # None when they published no such total. Carried on the view so the page
    # can referee a failed identity against the filer's own arithmetic instead
    # of asserting the filing is wrong -- which, for the overwhelming majority
    # of failures, it is not.
    stated_rhs: float | None = None
    negative_equity: bool = False
    # Height of the claims column relative to assets. Exceeds 100 only when
    # equity is negative, which is exactly when it should.
    claims_span_pct: float = 100.0
    notes: list[str] = field(default_factory=list)
    # Whether A = L + E actually holds on this filing, within tolerance.
    #
    # Nothing on the render path used to ask. The identity is checked at
    # INGEST, but the result goes to an in-memory report keyed by quarter and
    # never to the row -- so the drawing layer had nothing to consult, and a
    # filing where assets were 1000 against claims of 1800 rendered happily
    # with the claims column at 180% of the assets column, no note, no flag.
    # On a site whose entire argument is that both columns are the same money
    # counted twice, that is the one picture that must never be drawn without
    # saying so.
    balances: bool = True
    # How far out it is, as a share of assets. Reported so the page can say
    # "by 3%" rather than only "it does not balance".
    imbalance_pct: float = 0.0
    # WHY it balances, when the plain sum did not: "" for the ordinary case,
    # "nci" when the noncontrolling interest had to be added to close it.
    #
    # A = L + E is an identity, so a filing that misses it has either been
    # mis-parsed or is not being read on the terms it was written on. The
    # commonest instance of the second is a consolidated filer who reports
    # PARENT equity and the noncontrolling interest as separate lines and no
    # combined total: the real identity for that filing is A = L + E + NCI, and
    # testing it without the NCI flags a correct filing as broken. Recorded
    # rather than silently applied, because "this balances once you include the
    # minority interest" is a different statement from "this balances" and the
    # page says which one it means.
    identity_basis: str = ""
    # The noncontrolling interest, where the filer reported one. Carried so the
    # note can name the figure that closed the gap.
    minority_interest: float | None = None
    # Mezzanine (temporary) equity, where the filer reported it. Same purpose:
    # the note names the figure rather than the drawing absorbing it silently.
    mezzanine: float | None = None

    def as_dict(self) -> dict[str, Any]:
        def blocks(bs: list[Block]) -> list[dict[str, Any]]:
            return [
                {
                    "key": b.key, "label": b.label, "value": b.value,
                    "pct": round(b.pct, 3), "kind": b.kind, "tone": b.tone,
                    "is_remainder": b.is_remainder, "note": b.note,
                    "label_style": b.label_style,
                }
                for b in bs
            ]

        return {
            "ticker": self.ticker,
            "company_name": self.company_name,
            "former_name": self.former_name,
            "former_name_until": (
                self.former_name_until.isoformat()
                if self.former_name_until else None
            ),
            "sector": self.sector,
            "period_end": self.period_end.isoformat(),
            "filing_date": self.filing_date.isoformat(),
            "mode": self.mode,
            "assets": blocks(self.assets),
            "claims": blocks(self.claims),
            "total_assets": self.total_assets,
            "total_liabilities": self.total_liabilities,
            "total_equity": self.total_equity,
            "missing_components": self.missing_components,
            "liabilities_derived_from": self.liabilities_derived_from,
            "balances": self.balances,
            # SERIALISED as an explicit value, never as "".
            #
            # The field stays "" internally because `page_extras` branches on
            # `not basis` to mean "closed on the plain sum", and several
            # thousand stored rows already carry "". But over JSON an empty
            # string is indistinguishable from a missing value, and the API
            # and the MCP tool both advertise this key as "the basis used" --
            # so a consumer reading "" cannot tell "nothing had to be added"
            # from "we did not say". "as_filed" says the first one out loud.
            "identity_basis": self.identity_basis or "as_filed",
            "imbalance_pct": round(self.imbalance_pct, 3),
            "negative_equity": self.negative_equity,
            "claims_span_pct": round(self.claims_span_pct, 3),
            "notes": self.notes,
        }


def _val(group: dict[str, Any], key: str) -> float | None:
    cell = group.get(key)
    if cell is None or cell.missing or cell.value is None:
        return None
    return float(cell.value)


def build_view1(ticker: str, as_of: dt.date | None = None) -> View1 | None:
    """Compose a scale drawing of the most recent balance sheet.

    Returns None when the ticker has no usable fundamentals -- the caller says
    so to the reader rather than rendering an empty frame.
    """
    from src.company.balancesheet import get_balance_sheet

    bs = get_balance_sheet(ticker, as_of=as_of)
    if bs is None:
        return None

    total_assets = _val(bs.assets, "total_assets")
    if not total_assets or total_assets <= 0:
        # Nothing can be drawn to scale without a positive total to scale to.
        log.info("view1_no_total_assets", ticker=ticker)
        return None

    total_liabilities = _val(bs.liabilities, "total_liabilities")
    equity_incl = _val(bs.equity, "total_equity_incl_nci")
    equity_parent = _val(bs.equity, "shareholders_equity")
    total_equity = equity_incl if equity_incl is not None else equity_parent
    # Only relevant when the filer did NOT publish a combined total: with
    # `total_equity_incl_nci` in hand the identity already closes. This is the
    # other shape -- parent equity and the minority interest as two lines --
    # and it is the single largest category of filings that fail A = L + E
    # while being entirely correct.
    # Offered ALWAYS now, not only when the combined total is absent. The old
    # rule switched the correction off whenever `total_equity_incl_nci` existed,
    # on the assumption that such a figure already absorbs the NCI -- which is
    # exactly wrong for a filer who has the two tags swapped, where it is the
    # parent-only figure. `resolve_identity` decides by arithmetic instead.
    nci = _val(bs.equity, "minority_interest")
    # Read UNCONDITIONALLY, unlike the NCI. `total_equity_incl_nci` is a total
    # of PERMANENT equity: it absorbs the noncontrolling interest and it does
    # not absorb the mezzanine block, which is presented outside permanent
    # equity entirely. So a filer publishing the combined equity total can
    # still be missing this term.
    mezzanine = _mezzanine(bs)
    mezzanine_parts = _mezzanine_parts(bs)
    # The equity figure NOT chosen above, offered to `resolve_identity` so the
    # identity can pick between them rather than the tag name deciding.
    equity_alt = equity_parent if equity_incl is not None else None

    # Deriving liabilities from the identity is arithmetic, not imputation. The
    # filer stated two of the three terms; the third is exactly determined, not
    # guessed from peers or averages. Leaving half the picture blank when the
    # number is recoverable would be the worse answer -- but it is labelled, so
    # a derived figure never passes as one read off the filing.
    # The filer's OWN stated total for the credit side, read unconditionally
    # rather than only when liabilities need deriving. It is what referees a
    # failed identity: if this equals total assets, the filing balances against
    # its own arithmetic and the gap is in what we read, not in what they
    # filed. The page cannot say which side is at fault without it, and for
    # 206 of 215 failures the answer is "ours".
    stated_rhs = _val(bs.liabilities, "liabilities_and_equity")

    liabilities_derived_from: str | None = None
    if total_liabilities is None and total_equity is not None:
        # WHICH equity, when the filer published two and the tags disagree
        # with the figures. Deriving is arithmetic, but it is arithmetic on a
        # chosen input, and choosing by tag name here is the same mistake
        # `resolve_identity` exists to stop -- made somewhere the identity
        # check cannot catch it, because a derived liability makes the sum
        # balance BY CONSTRUCTION and `_check_identity` therefore skips it.
        #
        # Agilent's shape without a stated liabilities line would land exactly
        # here: derive from -$233M instead of $7.363B and the drawing shows
        # $14.2B of liabilities against $14.0B of assets, silently, with no
        # flag and nothing to check it against.
        #
        # There is no identity to rank candidates by, so the test is the only
        # one available: a derived liability must be a possible one. Negative
        # liabilities are impossible. Both figures are the filer's own.
        equity_for_derivation = total_equity
        if equity_alt is not None and stated_rhs is not None:
            primary = stated_rhs - total_equity
            alternate = stated_rhs - equity_alt
            if primary < 0 <= alternate:
                equity_for_derivation = equity_alt

        if stated_rhs is not None:
            total_liabilities = stated_rhs - equity_for_derivation
            liabilities_derived_from = "liabilities and equity, less equity"
        else:
            total_liabilities = total_assets - equity_for_derivation
            liabilities_derived_from = "total assets, less equity"
        total_equity = equity_for_derivation

    sector = _sector_for(ticker)
    view = View1(
        ticker=bs.ticker,
        company_name=bs.company_name,
        former_name=bs.former_name,
        former_name_until=bs.former_name_until,
        sector=sector,
        period_end=bs.period_end,
        filing_date=bs.filing_date,
        mode="detailed",
        total_assets=total_assets,
        total_liabilities=total_liabilities,
        stated_rhs=stated_rhs,
        total_equity=total_equity,
        liabilities_derived_from=liabilities_derived_from,
    )

    pct = lambda v: v / total_assets * 100.0  # noqa: E731
    asset_components, liability_components = component_sets(sector)

    # ---- left column: what it owns ----
    named_total = 0.0
    missing: list[str] = []
    for tone, (key, label) in enumerate(asset_components):
        v = _val(bs.assets, key)
        if v is None:
            missing.append(label)
            continue
        if v <= 0:
            continue
        named_total += v
        view.assets.append(
            Block(key=key, label=label, value=v, pct=pct(v), kind="asset", tone=tone)
        )

    remainder = total_assets - named_total
    if remainder > 0:
        note = None
        if missing:
            # Say it. Otherwise "other" quietly absorbs the unreported items and
            # reads as a real line item the filer stated.
            note = f"includes {len(missing)} line item{'s' if len(missing) > 1 else ''} this filer does not report separately"
        view.assets.append(
            Block(
                key="other_assets", label="Other assets", value=remainder,
                pct=pct(remainder), kind="asset", tone=6, is_remainder=True, note=note,
            )
        )
    elif remainder < 0:
        # Components exceeding the total should be impossible after validation,
        # so if it happens the drawing is not trustworthy and must say so.
        view.notes.append(
            "Reported components exceed total assets; the drawing is clipped."
        )

    view.missing_components = missing

    # ---- right column: who has a claim on it ----
    if total_liabilities is not None and total_equity is not None:
        named_liab = 0.0
        missing_liab: list[str] = []
        for tone, (key, label) in enumerate(liability_components):
            v = _val(bs.liabilities, key)
            if v is None:
                missing_liab.append(label)
                continue
            if v <= 0:
                continue
            named_liab += v
            view.claims.append(
                Block(key=key, label=label, value=v, pct=pct(v),
                      kind="liability", tone=tone)
            )

        liab_remainder = total_liabilities - named_liab
        if liab_remainder > 0:
            note = None
            if missing_liab:
                note = f"includes {len(missing_liab)} line item{'s' if len(missing_liab) > 1 else ''} this filer does not report separately"
            view.claims.append(
                Block(
                    key="other_liabilities", label="Other liabilities",
                    value=liab_remainder, pct=pct(liab_remainder),
                    kind="liability", tone=3, is_remainder=True, note=note,
                )
            )
        view.missing_components += missing_liab

        view.claims.append(
            Block(key="equity", label="Shareholders' equity", value=total_equity,
                  pct=pct(abs(total_equity)), kind="equity")
        )
        if total_equity < 0:
            view.negative_equity = True
            view.claims_span_pct = pct(total_liabilities)
            view.notes.append(
                "Liabilities exceed total assets, so equity is negative. It is "
                "drawn below the baseline."
            )
    else:
        # Totals-only: still a real answer, and it is the common case.
        view.mode = "totals_only"
        if total_liabilities is None:
            view.notes.append("This filer does not report a total for liabilities.")
        if total_equity is None:
            view.notes.append("This filer does not report a total for equity.")

    # "Simplified" means NO component was broken out on either side. A company
    # with four rendered components is not simplified, whichever totals it
    # happened to report.
    named_blocks = [
        b for b in (view.assets + view.claims)
        if not b.is_remainder and b.kind != "equity"
    ]
    view.mode = "detailed" if named_blocks else "totals_only"

    view.minority_interest = nci
    view.mezzanine = mezzanine
    _check_identity(
        view, total_assets, total_liabilities, total_equity, nci, mezzanine,
        equity_alt=equity_alt, mezzanine_parts=mezzanine_parts,
    )
    return view


# How far A = L + E may miss before the drawing says so, as a share of assets.
# 0.5% absorbs rounding and a filer's own presentation slack; anything past it
# is a disagreement the reader should be told about rather than shown as a
# column drawn off the top of the other one.
IDENTITY_TOLERANCE = 0.005


def _mezzanine(bs: Any) -> float | None:
    """The mezzanine (temporary) equity block, where the filer published one.

    PREFERRED, not summed. `temporary_equity` is the total of the section and
    the other two are components of it, so a filer who tags both a component
    and the total would be counted twice by an addition -- turning a filing
    that balances into one that overshoots, which is the same class of error
    as not reading the block at all.

    The cost of preferring is that a filer who publishes two COMPONENTS and no
    total is under-counted. That is the safer direction: an under-count leaves
    the filing looking unbalanced and reported as such, where an over-count
    would quietly invent a balance that the filing does not have.
    """
    for key in (
        "temporary_equity",
        "redeemable_preferred_stock",
        "redeemable_noncontrolling_interest",
        "minority_interest_operating_partnership",
    ):
        value = _val(bs.equity, key)
        if value:
            return value
    return None


def _mezzanine_parts(bs: Any) -> tuple[float | None, ...]:
    """Every mezzanine COMPONENT the filer published, excluding the section
    total.

    `_mezzanine` prefers the total and stops. That is right when a total
    exists, and under-counts a filer who published two components and no
    total -- Brookfield reports redeemable NCI as a preferred carrying amount
    AND an other carrying amount, and only their sum closes the identity.
    These are handed over separately so `resolve_identity` can try the sum
    WITHOUT ever adding it to the total and double-counting.
    """
    if _val(bs.equity, "temporary_equity"):
        return ()
    return tuple(
        _val(bs.equity, key)
        for key in (
            "redeemable_preferred_stock",
            "redeemable_noncontrolling_interest",
            "minority_interest_operating_partnership",
        )
    )


# The terms that may be ADDED to L + E to close a filing, in the order they are
# tried, and the sentence each one puts on the drawing.
#
# Every entry is a line the filer actually published. That is the whole
# discipline: a term is admitted because it names a real block on the real
# balance sheet, never because it happens to close a gap. Adding a term you
# cannot name is how a test stops being a test and becomes a fudge factor.
_IDENTITY_TERMS: dict[str, str] = {
    "nci": (
        "Balances as A = L + E + noncontrolling interest. This filer "
        "reports the parent's equity and the noncontrolling interest "
        "separately rather than as one total, so the two are added "
        "here. Nothing is adjusted — both figures are as filed."
    ),
    "mezzanine": (
        "Balances as A = L + E + mezzanine equity. This filer presents "
        "redeemable instruments between liabilities and equity, where they "
        "belong to neither column, so that block is added here. Nothing is "
        "adjusted — every figure is as filed."
    ),
    "nci+mezzanine": (
        "Balances as A = L + E + mezzanine equity + noncontrolling interest. "
        "This filer reports the parent's equity, a mezzanine block and the "
        "noncontrolling interest as three separate lines, so all three are "
        "added here. Nothing is adjusted — every figure is as filed."
    ),
}


def resolve_identity(
    total_assets: float,
    total_liabilities: float,
    total_equity: float,
    nci: float | None = None,
    mezzanine: float | None = None,
    *,
    equity_alt: float | None = None,
    mezzanine_parts: tuple[float | None, ...] | None = None,
) -> tuple[bool, float, str | None, float]:
    """Does this filing balance, and on what basis? THE definition, for everyone.

    SELECTS BY THE IDENTITY, NOT BY TAG NAME. The caller used to pick one
    equity figure by preferring `total_equity_incl_nci` whenever it existed,
    and to switch the NCI correction OFF on the same condition. Both assume the
    tag is named truthfully, and hand-tracing found two filings where it is
    not: Agilent carries -$233M on the including-NCI tag when its equity is
    $7.363B, and iQSTEL's filer has the two tags swapped, so the "including
    NCI" figure is the parent-only one. In both, the filing balances to the
    dollar against a figure already in our table and we reported a failure --
    54% and 9% respectively. See docs/internal/mezzanine-trace.md.

    So both equity figures are offered here and the one that satisfies the
    identity wins. A filer's naming mistake stops being our reconciliation
    failure, and nothing is invented: every candidate is a line the filer
    published.

    Returns `(balances, imbalance_pct, basis, equity_used)`. `basis` is None
    when the plain A = L + E closed it, and otherwise a key of
    `_IDENTITY_TERMS` naming the term that had to be added.

    `equity_used` is the equity figure the winning candidate was built on, and
    it is RETURNED rather than inferred because the alternative is a hidden
    coupling. A caller can reason that "balances with basis None, after the
    plain sum already failed" must mean the other equity figure won -- and
    that reasoning silently stops being true the moment either side's
    tolerance or pre-check changes. The winner already knows which figure it
    used; saying so costs one tuple element and removes the trap.

    This is a module-level function rather than four lines inside
    `_check_identity` because it had already been reimplemented elsewhere and
    the copies did not agree. `scripts/identity_failures.py` tried the NCI and
    the mezzanine SEPARATELY and never the two together, and counted a filing
    that closed on either as a FAILURE -- so the script's headline pass rate
    was measuring something the drawing does not: it was the rate before the
    explanations, while the page reports the rate after them. Two numbers, both
    called the pass rate, on the same data.

    A term is admitted only where the filer published it, and only when the
    plain sum has already failed: adding a term to a filing that balances would
    break one that was right.

    The candidate that closes with the SMALLEST remaining gap wins, not the
    first one to clear the tolerance. Where two bases both close, the tighter
    one is the one the filing was actually written on; picking by order would
    let an accidental near-miss claim the drawing's explanation.
    """
    if total_assets <= 0:
        return False, 0.0, None

    tolerance = IDENTITY_TOLERANCE * 100.0

    def drift(equity: float, extra: float) -> float:
        gap = abs(total_assets - (total_liabilities + equity + extra))
        return gap / total_assets * 100.0

    # Every equity figure the filer published, not one chosen by tag name.
    # `equity_alt` is the other of the pair (parent / including-NCI). Order is
    # preserved so that an exact tie keeps the caller's preference.
    equities: list[float] = [total_equity]
    if equity_alt is not None and equity_alt != total_equity:
        equities.append(equity_alt)

    # Mezzanine: the section total where the filer gave one, otherwise the sum
    # of the components. Never both -- a filer who tags a component AND the
    # total would be counted twice, which invents a balance rather than
    # finding one.
    mezz_candidates: list[float] = []
    if mezzanine:
        mezz_candidates.append(mezzanine)
    if mezzanine_parts:
        parts = [p for p in mezzanine_parts if p]
        summed = sum(parts)
        if len(parts) > 1 and summed not in mezz_candidates:
            mezz_candidates.append(summed)

    candidates: list[tuple[int, str | None, float, float]] = []
    for equity in equities:
        candidates.append((0, None, equity, 0.0))
        if nci:
            candidates.append((1, "nci", equity, nci))
        for mezz in mezz_candidates:
            candidates.append((1, "mezzanine", equity, mezz))
            if nci:
                candidates.append((2, "nci+mezzanine", equity, nci + mezz))

    # NO TERM BEATS ANY TERM; among terms, the tightest wins.
    #
    # The first half is Occam: a filing that closes on a plain sum balances,
    # and reaching for an explanation it does not need would put a sentence on
    # the drawing about a term that was never the reason.
    #
    # The second half is deliberately NOT "fewest terms". Where NCI alone
    # clears the tolerance and NCI + mezzanine lands exactly, the exact one is
    # the identity the filing was written on, and ranking by count would let an
    # accidental near-miss claim the drawing's explanation. That is a decision
    # this module already made and `test_the_tightest_basis_wins_not_the_first_one_tried`
    # already guards; the fix here is about WHICH FIGURES are offered, not
    # about changing how a winner is chosen among them.
    closing = [c for c in candidates if drift(c[2], c[3]) <= tolerance]
    if closing:
        best = min(
            closing,
            key=lambda c: (0 if c[1] is None else 1, drift(c[2], c[3])),
        )
        return True, drift(best[2], best[3]), best[1], best[2]

    # Nothing closes. Report the smallest gap on the caller's own equity, so a
    # failure is described in the terms the rest of the view is drawn in.
    return False, drift(total_equity, 0.0), None, total_equity


def _adopt_equity(
    view: View1,
    equity: float,
    total_assets: float,
    total_liabilities: float,
) -> None:
    """Redraw the claims column on the equity figure that actually closed.

    Setting `view.total_equity` alone would not be enough and would be the
    more dangerous half-fix: the equity Block, `negative_equity`,
    `claims_span_pct` and the "drawn below the baseline" note are all built in
    `build_view1` BEFORE the identity is checked, from whichever figure the tag
    names happened to favour. Accepting the alternate figure without redrawing
    would leave a page that says the filing balances above a picture drawn from
    a number that does not balance -- moving the lie rather than removing it.

    Agilent is the case that makes this concrete. Its `total_equity_incl_nci`
    is -$233M, so the live page asserted "Equity is negative, so the claims
    against this company exceed what it owns... That is a real shape, not an
    error" about a company with $7.363B of positive equity. That sentence is a
    false statement about a real public company, and it was the drawing's
    doing, not the paragraph's.
    """
    view.total_equity = equity

    for block in view.claims:
        if block.kind == "equity":
            block.value = equity
            block.pct = (
                abs(equity) / total_assets * 100.0 if total_assets else 0.0
            )

    # Both of these were decided by the discarded figure and have to be
    # recomputed, not merely cleared: the replacement equity can legitimately
    # be negative too, and a filer whose real equity is below zero must keep
    # the note that says so.
    view.negative_equity = equity < 0
    view.notes = [n for n in view.notes if "drawn below the baseline" not in n]
    if equity < 0:
        view.claims_span_pct = (
            total_liabilities / total_assets * 100.0 if total_assets else 100.0
        )
        view.notes.append(
            "Liabilities exceed total assets, so equity is negative. It is "
            "drawn below the baseline."
        )
    else:
        view.claims_span_pct = 100.0

    view.notes.append(
        "This filer publishes two equity totals and the tags disagree with the "
        "figures. The one that satisfies A = L + E is drawn — both are as "
        "filed, and nothing is adjusted."
    )


def _check_identity(
    view: View1,
    total_assets: float | None,
    total_liabilities: float | None,
    total_equity: float | None,
    nci: float | None = None,
    mezzanine: float | None = None,
    equity_alt: float | None = None,
    mezzanine_parts: tuple[float | None, ...] | None = None,
) -> None:
    """Does this filing balance? Record it, and say so on the drawing.

    Skipped in two cases, both on purpose:

    * liabilities DERIVED from the identity, where the sum balances by
      construction and reporting that as a pass would be circular;
    * negative equity, which is a real shape this site draws deliberately
      below the baseline -- it balances, it just does not look like it.
    """
    if total_assets is None or total_liabilities is None or total_equity is None:
        return
    if total_assets <= 0:
        return

    if view.liabilities_derived_from:
        # A DERIVED liability makes A = L + E hold by construction, so testing
        # it would be circular and reporting the pass would be a lie of the
        # quietest kind. That is why this returned early.
        #
        # But only ONE of the two derivations is circular. Where liabilities
        # came from `total assets, less equity`, L = A - E and the identity is
        # a tautology -- nothing to check. Where they came from the filer's
        # own `liabilities and equity, less equity`, there is still a real and
        # non-circular question: does the filer's own stated total match the
        # filer's own total assets? Neither side of that comparison came from
        # us, and a filing that fails it is a filing whose own two totals
        # disagree.
        #
        # Skipping both meant a page could say nothing at all about a filer
        # whose arithmetic did not work, purely because we had to derive one
        # line -- silence that reads as assent.
        if view.liabilities_derived_from.startswith("liabilities and equity"):
            stated = view.stated_rhs
            if stated:
                own = abs(total_assets - stated) / total_assets * 100.0
                view.imbalance_pct = own
                view.balances = own <= IDENTITY_TOLERANCE * 100.0
                if not view.balances:
                    view.notes.append(
                        "This filer's own stated total for liabilities plus "
                        f"equity differs from their own total assets by "
                        f"{own:.1f}%. Their arithmetic, not ours. One line "
                        "here is derived from that stated total, so the two "
                        "columns are drawn to agree — the disagreement is in "
                        "the filing, and it is reported rather than hidden."
                    )
        return

    gap = abs(total_assets - (total_liabilities + total_equity))
    view.imbalance_pct = gap / total_assets * 100.0
    view.balances = view.imbalance_pct <= IDENTITY_TOLERANCE * 100.0
    if view.balances:
        return

    # It did not balance on the plain sum. Before calling a filing broken, try
    # the identity it was actually written on.
    #
    # Two things are presented outside `L + StockholdersEquity` and are neither
    # an error nor an adjustment:
    #
    # * the NONCONTROLLING INTEREST, where a consolidated filer reports parent
    #   equity and the NCI as separate lines with no combined total. `Assets`
    #   is consolidated; parent equity is not. `verify.py` and
    #   `universe_check.py` already preferred the NCI-inclusive basis, so the
    #   same filing could be sound to the internal checker and "does not
    #   balance" to a reader.
    # * MEZZANINE EQUITY, the block of redeemable instruments presented between
    #   the two columns. Neither tag was ingested at all until recently, which
    #   made every mezzanine filer look like identity drift -- our failure to
    #   read the filing, not their arithmetic. See
    #   docs/internal/identity-failures.md §2.
    #
    # Tried only when the plain sum FAILED. Adding a term to a filing that
    # already balances would break one that was right.
    #
    # The candidate that closes with the SMALLEST remaining gap wins, rather
    # than the first that clears the tolerance. Where two bases both close, the
    # tighter one is the one the filing was actually written on; picking by
    # order would let an accidental near-miss claim the drawing's explanation.
    balances, imbalance_pct, basis, equity_used = resolve_identity(
        total_assets, total_liabilities, total_equity, nci, mezzanine,
        equity_alt=equity_alt, mezzanine_parts=mezzanine_parts,
    )

    # THE ALTERNATE EQUITY FIGURE, which used to be thrown away here.
    #
    # `resolve_identity` was rewritten to offer both equity figures the filer
    # published and let the identity choose, precisely because Agilent carries
    # -$233M on `total_equity_incl_nci` when its equity is $7.363B, and
    # iQSTEL's filer has the two tags swapped. It found the right one. Then
    # this function threw the answer away, because the guard below read
    # `balances and basis is not None` -- and a close on the OTHER equity with
    # no extra term returns basis None, since no term was added.
    #
    # So the whole fix was invisible: Agilent went on reporting a 54.4% failure
    # against a filing that closes to the dollar, and -- worse -- went on
    # telling readers its equity was negative, because the drawing had already
    # been built from -$233M before this function ran.
    #
    # `stats.py` never had this guard (`if balances:` is all it asks), so
    # /methodology has been counting these as reconciled while the company page
    # called them failures. Same data, same function, two answers.
    if balances and basis is None and equity_used != total_equity:
        _adopt_equity(view, equity_used, total_assets, total_liabilities)
        view.balances = True
        view.imbalance_pct = imbalance_pct
        return

    if balances and basis is not None:
        if equity_used != total_equity:
            _adopt_equity(view, equity_used, total_assets, total_liabilities)
        view.balances = True
        view.imbalance_pct = imbalance_pct
        view.identity_basis = basis
        # Said out loud rather than applied silently. "This balances once you
        # include the minority interest" is a different statement from "this
        # balances", and a reader checking the drawing against the filing needs
        # to know which one they are being shown.
        view.notes.append(_IDENTITY_TERMS[basis])
        return

    # The claims column is no longer a proportion of anything meaningful, so
    # it is held at the assets column's height rather than drawn past it. The
    # figures underneath are untouched -- what is shown is still as reported.
    view.claims_span_pct = min(view.claims_span_pct, 100.0)

    # DRAW THE SHORTFALL WHERE THE FILER'S OWN TOTAL SAYS IT BELONGS.
    #
    # When the filer's stated liabilities-and-equity equals their total assets,
    # the filing balances against its own arithmetic and the gap is a component
    # we could not read. The paragraph now says so. The PICTURE did not: the
    # claims column was simply drawn short, so BLK rendered $111.3B + $57.8B
    # against $175.9B of assets and the image went on blaming the filer after
    # the text had stopped.
    #
    # This is the remainder the page already uses on both sides -- the same
    # device as "Other liabilities ... includes N line items this filer does
    # not report separately" -- pointed at our own coverage gap and labelled as
    # ours. Nothing is invented: the height comes from the filer's own stated
    # total, and the band says we could not read what fills it.
    stated = view.stated_rhs
    if stated and total_assets > 0:
        own_gap = abs(total_assets - stated) / total_assets * 100.0
        drawn = (total_liabilities or 0.0) + (total_equity or 0.0)
        unread = stated - drawn
        if own_gap <= IDENTITY_TOLERANCE * 100.0 and unread > 0:
            view.claims.append(Block(
                key="unread_components",
                label="Not read from this filing",
                value=unread,
                pct=unread / total_assets * 100.0,
                kind="liability",
                tone=3,
                is_remainder=True,
                note=(
                    "This filer's own stated total for liabilities plus equity "
                    "equals their total assets, so the filing balances. This "
                    "band is the part we could not read — a line reported "
                    "under a tag this site does not yet map. Ours, not theirs."
                ),
            ))

    view.notes.append(
        "This filing does not balance — data shown as reported. "
        f"Liabilities plus equity differ from total assets by "
        f"{view.imbalance_pct:.1f}%. Every figure here is as filed with the "
        "SEC; nothing has been adjusted to make the two columns agree."
    )


def _sector_for(ticker: str) -> str | None:
    from sqlalchemy import select

    from src.storage.db import session_scope
    from src.storage.models import SectorMap

    try:
        with session_scope() as session:
            return session.execute(
                select(SectorMap.sector).where(SectorMap.ticker == ticker.upper())
            ).scalar_one_or_none()
    except Exception:  # noqa: BLE001 - a missing sector must never break the page
        return None


def describe_shape(view: View1) -> list[str]:
    """Two or three plain sentences describing the shape.

    Description, not judgement: no score, no ranking, no investment language.
    Every clause is a restatement of a number already on the page.
    """
    out: list[str] = []
    ta = view.total_assets

    by_key = {b.key: b for b in view.assets}
    hard = sum(
        by_key[k].value for k in ("property_plant_equipment", "inventory")
        if k in by_key
    )
    soft = sum(
        by_key[k].value for k in ("cash", "receivables") if k in by_key
    )
    if hard or soft:
        if hard / ta >= 0.4:
            out.append(
                f"Most of what {view.ticker} owns is physical — property, "
                f"equipment and inventory account for {hard / ta * 100:.0f}% of "
                "total assets."
            )
        elif soft / ta >= 0.4:
            out.append(
                f"Most of what {view.ticker} owns is financial — cash and money "
                f"owed to it account for {soft / ta * 100:.0f}% of total assets."
            )

    if view.total_equity is not None and view.total_liabilities is not None:
        eq_share = view.total_equity / ta * 100.0
        if view.total_equity < 0:
            out.append(
                "Liabilities exceed total assets, so shareholders' equity is "
                f"negative at {_money(view.total_equity)}."
            )
        elif eq_share >= 60:
            out.append(
                f"It is mostly owner-funded: equity is {eq_share:.0f}% of the "
                "balance sheet, borrowings and other claims the rest."
            )
        elif eq_share >= 40:
            # A near-even split is neither, and calling it either would be
            # editorialising past what the number says.
            out.append(
                f"Funding is split roughly evenly: equity is {eq_share:.0f}% of "
                f"the balance sheet and liabilities {100 - eq_share:.0f}%."
            )
        else:
            out.append(
                f"It is mostly funded by others: equity is {eq_share:.0f}% of "
                "the balance sheet and liabilities are the remaining "
                f"{100 - eq_share:.0f}%."
            )

    if view.missing_components:
        out.append(
            "Not every line item is reported separately by this filer: "
            + ", ".join(view.missing_components[:4]).lower()
            + (" and others" if len(view.missing_components) > 4 else "")
            + " are inside the totals rather than broken out."
        )
    return out


def _money(v: float) -> str:
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e12:
        return f"{sign}${a / 1e12:,.2f} trillion"
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.1f} billion"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.1f} million"
    return f"{sign}${a:,.0f}"
