"""SIC -> GICS-equivalent sector buckets.

SEC filers carry a Standard Industrial Classification code (near-static, one per
company). GICS is a different taxonomy, so this is a deliberate approximation:
SIC major groups mapped to the eleven GICS sectors. It exists so that, without a
paid sector feed (FMP), the Stage-2 sector-neutral z-scores group like with like
instead of silently collapsing to universe-neutral -- which would let whichever
sector is hot dominate the top-10.

`sic_to_gics` returns None for codes with no defensible mapping (government,
unclassified); those names stay sector-less rather than being forced into a
bucket. Nothing is fabricated.
"""

from __future__ import annotations

# GICS sector names (canonical). Kept as plain strings -- the funnel only needs
# a consistent grouping key, not a GICS licence.
ENERGY = "Energy"
MATERIALS = "Materials"
INDUSTRIALS = "Industrials"
CONS_DISC = "Consumer Discretionary"
CONS_STAPLES = "Consumer Staples"
HEALTHCARE = "Health Care"
FINANCIALS = "Financials"
INFO_TECH = "Information Technology"
COMM_SVCS = "Communication Services"
UTILITIES = "Utilities"
REAL_ESTATE = "Real Estate"

# Ordered (low, high, sector) ranges, inclusive. NARROW OVERRIDES FIRST -- a
# specific range (e.g. 2830-2836 drugs) must precede its broad parent (2800-2899
# chemicals). First match wins.
_RANGES: list[tuple[int, int, str]] = [
    # Agriculture / mining / energy
    (100, 999, CONS_STAPLES),        # agriculture, forestry, fishing
    (1000, 1099, MATERIALS),         # metal mining
    (1100, 1299, ENERGY),            # coal mining
    (1300, 1399, ENERGY),            # oil & gas extraction
    (1400, 1499, MATERIALS),         # nonmetallic minerals
    (1500, 1799, INDUSTRIALS),       # construction
    # Food / staples / discretionary goods
    (2000, 2199, CONS_STAPLES),      # food + tobacco
    (2200, 2399, CONS_DISC),         # textiles / apparel
    (2400, 2499, MATERIALS),         # lumber & wood
    (2500, 2599, CONS_DISC),         # furniture
    (2600, 2699, MATERIALS),         # paper
    (2700, 2799, COMM_SVCS),         # printing / publishing
    # Chemicals (drugs override first)
    (2830, 2836, HEALTHCARE),        # drugs / biologicals
    (2800, 2899, MATERIALS),         # chemicals
    (2900, 2999, ENERGY),            # petroleum refining
    (3000, 3099, MATERIALS),         # rubber & plastics
    (3100, 3199, CONS_DISC),         # leather
    (3200, 3299, MATERIALS),         # stone / clay / glass
    (3300, 3399, MATERIALS),         # primary metals
    (3400, 3499, INDUSTRIALS),       # fabricated metal
    (3500, 3569, INDUSTRIALS),       # industrial machinery
    (3570, 3579, INFO_TECH),         # computers & office equipment
    (3580, 3599, INDUSTRIALS),
    (3600, 3669, INFO_TECH),         # electronic equipment
    (3670, 3699, INFO_TECH),         # semiconductors / components
    (3711, 3716, CONS_DISC),         # motor vehicles
    (3700, 3719, CONS_DISC),         # other transport goods
    (3720, 3799, INDUSTRIALS),       # aircraft / ships / rail equipment
    (3840, 3851, HEALTHCARE),        # medical / surgical instruments
    (3800, 3899, INFO_TECH),         # measuring / lab instruments
    (3900, 3999, CONS_DISC),         # misc manufacturing
    # Transportation / comms / utilities
    (4000, 4499, INDUSTRIALS),       # rail / transit / trucking / water
    (4500, 4599, INDUSTRIALS),       # air transport
    (4600, 4699, ENERGY),            # pipelines
    (4700, 4799, INDUSTRIALS),       # transportation services
    (4800, 4899, COMM_SVCS),         # communications
    (4900, 4999, UTILITIES),         # electric / gas / sanitary
    # Wholesale / retail
    (5000, 5199, CONS_DISC),         # wholesale
    (5400, 5499, CONS_STAPLES),      # food stores
    (5912, 5912, CONS_STAPLES),      # drug stores
    (5200, 5999, CONS_DISC),         # retail
    # Finance / insurance / real estate
    (6000, 6499, FINANCIALS),        # banks / brokers / insurance
    (6500, 6599, REAL_ESTATE),       # real estate
    (6798, 6798, REAL_ESTATE),       # REITs
    (6600, 6799, FINANCIALS),        # holding / investment offices
    # Services
    (7000, 7099, CONS_DISC),         # hotels
    (7200, 7299, CONS_DISC),         # personal services
    (7370, 7379, INFO_TECH),         # computer / software services
    (7300, 7399, INDUSTRIALS),       # business services
    (7800, 7899, COMM_SVCS),         # motion pictures
    (7900, 7999, CONS_DISC),         # amusement / recreation
    (7400, 7799, INDUSTRIALS),       # other services
    (8000, 8099, HEALTHCARE),        # health services
    (8300, 8399, HEALTHCARE),        # social services
    (8100, 8299, CONS_DISC),         # legal / educational
    (8400, 8999, INDUSTRIALS),       # engineering / research / mgmt services
]


def sic_to_gics(sic: int | str | None) -> str | None:
    """Map a SIC code to a GICS-equivalent sector, or None if unmappable."""
    if sic is None:
        return None
    try:
        code = int(str(sic).strip())
    except (TypeError, ValueError):
        return None
    if code <= 0:
        return None
    for lo, hi, sector in _RANGES:
        if lo <= code <= hi:
            return sector
    return None
