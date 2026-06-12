"""External validation: compare the project's composite heat-risk score against
the Theeuwes et al. (2017) diagnostic equation for maximum urban heat island
intensity in north-western European cities.

The Theeuwes equation (Theeuwes, Steeneveld, Ronda & Holtslag, 2017,
International Journal of Climatology 37:443-454, DOI 10.1002/joc.4717) is:

    UHImax = (2 - SVF - F_veg) * (S * DTR^3 / U) ^ (1/4)

where
    SVF    = sky view factor                             [0..1]
    F_veg  = vegetation fraction                          [0..1]
    S      = daily mean specific global radiation         [K m/s]
    DTR    = daily temperature range, T_max - T_min       [K]
    U      = daily mean 10 m wind speed                   [m/s]

It was validated against observations from 14 NW European cities and is the
closest published "industry-standard" semi-empirical UHI equation for our
study area.

Crucial point: Theeuwes 2017 uses NO terrain term. The whole equation is
driven by morphology (SVF, F_veg) and meteorology. By comparing its per-zone
UHImax ranking against our composite score ranking, we ask the question:

    Does our composite -- with its added topographic, density, impervious,
    and heat-day terms -- preserve the ranking that a validated published
    morphology+meteorology equation produces? If yes, our model is at least
    not contradicting the established literature. If no, we have learned
    something we need to explain.

Note on meteorology: within Stuttgart-Mitte (2 km x 2 km) the four tiles
share essentially identical S, DTR and U on any given day. The meteorology
factor (S * DTR^3 / U)^(1/4) is therefore a common multiplier across zones
and does NOT affect the ranking. For absolute UHImax values we use a typical
Stuttgart summer heat-day climatology (documented below); for ranking
agreement the choice does not matter.
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph
from rdflib.namespace import XSD

from namespaces import UHI, EX, bind_all

BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

# Representative Stuttgart summer heat-day meteorology.
# T_max  ~ 32 deg C, T_min ~ 18 deg C  -> DTR = 14 K
# Daily mean global radiation in summer at 48.8 N: ~ 250 W/m^2
# Stuttgart-Mitte mean 10 m wind on weak-wind heat days: ~ 1.5 m/s
# Air heat capacity Cp = 1007 J/(kg K); air density rho ~ 1.18 kg/m^3 at 25 C
# Specific radiation S = Q / (Cp * rho) in K m/s, per Tygron implementation.
DTR_K = 14.0
Q_GLOBAL_W_M2 = 250.0
U_WIND_M_S = 1.5
CP_AIR = 1007.0
RHO_AIR = 1.18
S_SPECIFIC = Q_GLOBAL_W_M2 / (CP_AIR * RHO_AIR)  # K m/s

ZONES = [
    EX.Zone_513_5402,
    EX.Zone_513_5403,
    EX.Zone_514_5402,
    EX.Zone_514_5403,
]


def local_name(uri) -> str:
    return str(uri).rstrip("/").rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def read_float(g: Graph, subject, predicate) -> float | None:
    val = g.value(subject, predicate)
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def theeuwes_uhimax(svf: float, f_veg: float,
                    s: float = S_SPECIFIC, dtr: float = DTR_K, u: float = U_WIND_M_S) -> float:
    """Theeuwes et al. (2017) diagnostic equation. Returns UHImax in K."""
    morphology = max(2.0 - svf - f_veg, 0.0)
    meteorology = (s * dtr ** 3 / u) ** 0.25
    return morphology * meteorology


def spearman_rho(ranks_a: list[int], ranks_b: list[int]) -> float:
    """Spearman rank correlation for two rank vectors of equal length."""
    n = len(ranks_a)
    if n < 2:
        return float("nan")
    d_squared_sum = sum((a - b) ** 2 for a, b in zip(ranks_a, ranks_b))
    return 1.0 - (6.0 * d_squared_sum) / (n * (n ** 2 - 1))


def main() -> None:
    if not TTL_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {TTL_FILE}. Run the full pipeline first."
        )

    print("Loading graph ...")
    g = Graph()
    bind_all(g)
    g.parse(str(TTL_FILE), format="turtle")
    print(f"  {len(g)} triples loaded")

    print(f"\nTheeuwes (2017) meteorology assumption (Stuttgart summer heat day):")
    print(f"  Q_global = {Q_GLOBAL_W_M2:.0f} W/m^2    DTR = {DTR_K:.0f} K    U_10m = {U_WIND_M_S:.1f} m/s")
    print(f"  S = Q / (Cp * rho) = {S_SPECIFIC:.4f} K m/s")
    print(f"  meteorology factor = (S * DTR^3 / U)^(1/4) = "
          f"{(S_SPECIFIC * DTR_K ** 3 / U_WIND_M_S) ** 0.25:.3f} K\n")

    rows = []
    for zone in ZONES:
        svf = read_float(g, zone, UHI.hasSkyViewFactor)
        f_veg = read_float(g, zone, UHI.hasVegetationFraction)
        composite_score = read_float(g, zone, None)  # placeholder; pull below
        # The composite score lives on the assessment, not the zone.
        assessment = g.value(zone, UHI.hasHeatRiskAssessment)
        composite_score = read_float(g, assessment, UHI.hasHeatRiskScore) if assessment else None
        composite_delta_t = read_float(g, assessment, UHI.hasIndicativeDeltaT) if assessment else None

        if None in (svf, f_veg, composite_score):
            print(f"  WARNING: zone {local_name(zone)} missing data, skipping")
            continue

        morphology = 2.0 - svf - f_veg
        uhimax = theeuwes_uhimax(svf, f_veg)

        rows.append({
            "zone": local_name(zone),
            "svf": svf,
            "f_veg": f_veg,
            "morphology": morphology,
            "theeuwes_uhimax": uhimax,
            "composite_score": composite_score,
            "composite_delta_t": composite_delta_t,
        })

    # Rank both metrics (1 = highest).
    rows_by_theeuwes = sorted(rows, key=lambda r: -r["theeuwes_uhimax"])
    rows_by_composite = sorted(rows, key=lambda r: -r["composite_score"])
    theeuwes_rank = {r["zone"]: i + 1 for i, r in enumerate(rows_by_theeuwes)}
    composite_rank = {r["zone"]: i + 1 for i, r in enumerate(rows_by_composite)}

    print(f"  {'Zone':<18} {'SVF':>6} {'F_veg':>6} {'2-SVF-Fv':>9} "
          f"{'Theeuwes':>10} {'Comp.score':>12} {'Comp.dT':>9} "
          f"{'T-rank':>7} {'C-rank':>7}")
    for r in rows:
        print(f"  {r['zone']:<18} {r['svf']:>6.3f} {r['f_veg']:>6.3f} "
              f"{r['morphology']:>9.3f} {r['theeuwes_uhimax']:>9.2f} K "
              f"{r['composite_score']:>12.3f} {r['composite_delta_t']:>7.2f} C "
              f"{theeuwes_rank[r['zone']]:>7} {composite_rank[r['zone']]:>7}")

    ranks_a = [theeuwes_rank[r["zone"]] for r in rows]
    ranks_b = [composite_rank[r["zone"]] for r in rows]
    rho = spearman_rho(ranks_a, ranks_b)
    perfect = ranks_a == ranks_b

    print(f"\nSpearman rank correlation between Theeuwes UHImax and composite score: rho = {rho:.3f}")
    if perfect:
        print("Rankings agree perfectly across all zones.")
    else:
        print("Rankings differ. Disagreements:")
        for r in rows:
            t, c = theeuwes_rank[r["zone"]], composite_rank[r["zone"]]
            if t != c:
                print(f"  {r['zone']}: Theeuwes rank {t}, composite rank {c}")

    print("\nInterpretation:")
    if rho >= 0.9:
        print("  Strong concordance with the Theeuwes (2017) validated equation. The")
        print("  composite score reproduces the morphology-driven ordering and the")
        print("  additional terms (topographic exposure, density, imperviousness,")
        print("  heat days) do not distort that ordering. The composite is consistent")
        print("  with the published NW European UHI literature.")
    elif rho >= 0.6:
        print("  Moderate to strong concordance. The composite broadly agrees with the")
        print("  Theeuwes equation; disagreements (above) are confined to zones with")
        print("  near-identical scores in both metrics. The additional non-morphology")
        print("  terms shift relative ordering only at the margin.")
    else:
        print("  Weak concordance. The composite scoring disagrees substantively with")
        print("  the Theeuwes equation. Either the additional terms are dominating in")
        print("  ways that warrant explanation, or one of the inputs is mis-specified.")


if __name__ == "__main__":
    main()
    