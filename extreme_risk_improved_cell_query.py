#!/usr/bin/env python3
"""
extreme_risk_improved_cell_query.py

Select an ExtremeRisk 100 m grid cell that improves under the combined
greening counterfactual:

    +20 percentage points tree-canopy coverage
    -15 percentage points impervious-surface coverage

The script performs a first-stage physical-feasibility screening.

It does not claim that all non-building impervious area can actually be
removed. Roads, sidewalks, plazas, and parking areas must still be examined
using the basemap, OSM data, aerial imagery, or field knowledge.

Safety checks added in this revision
------------------------------------
1. Weight-consistency check: the locally recomputed baseline score must
   match the graph's stored baseline score for every cell. A mismatch
   means the hardcoded weights below have drifted from the calibrated
   weights used in risk_assessment pass 2, and the script aborts.
2. CRS validation: cell and footprint WKT must be EPSG:25832 (or carry
   no CRS prefix, which is warned about once and assumed to be 25832).
3. Duplicate-row detection: aborts if a cell appears more than once,
   which indicates stacked assessments (pass 1 + pass 2 both attached)
   or multiple hasHeatDayCount values on a zone.
4. Canopy open-area check (soft flag): reports whether the required new
   canopy fits within the non-building area of the cell. Street trees
   can legitimately overhang pavement, so this does not affect the
   pass/fail ranking, only the report.
5. Cell-area sanity check: median candidate cell area must be close to
   10,000 m2 (100 m grid), which would also catch a CRS mix-up.

Outputs
-------
1. A ranked list of ExtremeRisk cells that leave ExtremeRisk.
2. A detailed report for the best candidate.
3. selected_greening_cell.geojson for map visualization.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from pyproj import Transformer
from rdflib import Graph
from shapely import STRtree
from shapely import wkt as shapely_wkt
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shapely_transform
from shapely.ops import unary_union


# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"
DEFAULT_OUTPUT_FILE = BASE_DIR / "selected_greening_cell.geojson"


# ---------------------------------------------------------------------------
# MODEL SETTINGS
# Keep these synchronized with risk_assessment.py and subzone_grid.py.
#
# NOTE: risk_assessment runs twice (defaults, then calibrated). The graph
# stores pass-2 (calibrated) scores. The weight-consistency check in
# validate_weight_consistency() verifies at runtime that the values below
# actually reproduce the stored scores; if calibration changes the weights,
# this script refuses to run until they are updated here.
# ---------------------------------------------------------------------------

W_SVF = 0.35
W_DENSITY = 0.20
W_TOPO = 0.15
W_CANOPY = 0.10
W_IMPERVIOUS = 0.10
W_HEAT_DAYS = 0.10

MAX_HEAT_DAYS_FOR_NORMALISATION = 30.0

MEDIUM_RISK_MIN = 0.25
HIGH_RISK_MIN = 0.35
EXTREME_RISK_MIN = 0.50

CANOPY_INCREASE = 0.20
IMPERVIOUS_DECREASE = 0.15

WEIGHT_MATCH_TOLERANCE = 2e-4

# 100 m grid: expected cell area, with generous slack.
EXPECTED_CELL_AREA_M2 = 10_000.0
CELL_AREA_RELATIVE_SLACK = 0.05

EXPECTED_CRS_TOKEN = "25832"

TO_WGS84 = Transformer.from_crs(
    "EPSG:25832",
    "EPSG:4326",
    always_xy=True,
)


# ---------------------------------------------------------------------------
# SPARQL
# ---------------------------------------------------------------------------

CELL_QUERY = """
PREFIX uhi: <https://w3id.org/stuttgart-uhi#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>

SELECT DISTINCT
       ?cell
       ?zone
       ?wkt
       ?baselineScore
       ?baselineCategory
       ?svf
       ?density
       ?topo
       ?canopy
       ?impervious
       ?heatDays
WHERE {
    ?cell a uhi:GridCell ;
          uhi:hasParentZone ?zone ;
          geo:hasGeometry ?geometry ;
          uhi:hasSkyViewFactor ?svf ;
          uhi:hasUrbanDensity ?density ;
          uhi:hasTopographicExposure ?topo ;
          uhi:hasTreeCanopyCoverage ?canopy ;
          uhi:hasImperviousSurfaceFraction ?impervious ;
          uhi:hasHeatRiskAssessment ?assessment .

    ?geometry geo:asWKT ?wkt .

    ?assessment uhi:hasHeatRiskScore ?baselineScore ;
                uhi:hasRiskCategory ?baselineCategory .

    ?zone uhi:hasHeatDayCount ?heatDays .

    FILTER(?baselineCategory = uhi:ExtremeRisk)
}
"""


FOOTPRINT_QUERY = """
PREFIX uhi: <https://w3id.org/stuttgart-uhi#>
PREFIX bot: <https://w3id.org/bot#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>

SELECT DISTINCT ?building ?footprintWkt
WHERE {
    ?building a bot:Building ;
              uhi:hasFootprintGeometry ?footprintGeometry .

    ?footprintGeometry geo:asWKT ?footprintWkt .
}
"""


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def local_name(uri) -> str:
    value = str(uri)
    return value.rsplit("#", 1)[-1].rstrip("/").rsplit("/", 1)[-1]


def risk_category(score: float) -> str:
    if score >= EXTREME_RISK_MIN:
        return "ExtremeRisk"
    if score >= HIGH_RISK_MIN:
        return "HighRisk"
    if score >= MEDIUM_RISK_MIN:
        return "MediumRisk"
    return "LowRisk"


def calculate_score(
    svf: float,
    density: float,
    topo: float,
    canopy: float,
    impervious: float,
    heat_days: float,
) -> float:
    heat_normalized = clamp(heat_days / MAX_HEAT_DAYS_FOR_NORMALISATION)

    return clamp(
        W_SVF * (1.0 - svf)
        + W_DENSITY * density
        + W_TOPO * topo
        + W_CANOPY * (1.0 - canopy)
        + W_IMPERVIOUS * impervious
        + W_HEAT_DAYS * heat_normalized
    )


class CrsTracker:
    """Track CRS prefixes seen in WKT literals and enforce EPSG:25832."""

    def __init__(self) -> None:
        self.missing_prefix_count = 0

    def check(self, crs_uri: str | None, context: str) -> None:
        if crs_uri is None:
            self.missing_prefix_count += 1
            return

        if EXPECTED_CRS_TOKEN not in crs_uri:
            raise ValueError(
                f"Unexpected CRS in {context}: <{crs_uri}>. "
                "This script assumes EPSG:25832 (metric) geometry. "
                "WGS84 / CRS84 geometry would silently produce areas in "
                "square degrees, so this is a hard error."
            )

    def report(self) -> None:
        if self.missing_prefix_count:
            print(
                f"  WARNING: {self.missing_prefix_count} WKT literals had "
                "no CRS prefix; EPSG:25832 was assumed. The cell-area "
                "sanity check below guards against a wrong assumption."
            )


def parse_geosparql_wkt(
    value: str,
    crs_tracker: CrsTracker,
    context: str,
) -> BaseGeometry | None:
    """Parse (optionally CRS-prefixed) GeoSPARQL WKT and validate the CRS."""

    text = value.strip()
    crs_uri: str | None = None

    if text.startswith("<") and ">" in text:
        crs_uri, text = text[1:].split(">", 1)
        text = text.strip()

    crs_tracker.check(crs_uri, context)

    try:
        geometry = shapely_wkt.loads(text)
    except Exception:
        return None

    if geometry.is_empty:
        return None

    if not geometry.is_valid:
        geometry = geometry.buffer(0)

    if geometry.is_empty or not geometry.is_valid:
        return None

    return geometry


def building_area_inside_cell(
    cell_geometry: BaseGeometry,
    footprints: list[BaseGeometry],
    footprint_index: STRtree,
) -> float:
    """Union building-footprint intersections to avoid double counting.

    The STRtree returns only footprints whose bounding boxes intersect the
    cell, so no manual bbox pre-filter is needed.
    """

    intersections: list[BaseGeometry] = []

    for footprint_position in footprint_index.query(cell_geometry):
        footprint = footprints[int(footprint_position)]

        if not footprint.intersects(cell_geometry):
            continue

        intersection = footprint.intersection(cell_geometry)

        if not intersection.is_empty:
            intersections.append(intersection)

    if not intersections:
        return 0.0

    return float(unary_union(intersections).area)


# ---------------------------------------------------------------------------
# GRAPH READING
# ---------------------------------------------------------------------------

def load_footprints(
    graph: Graph,
    crs_tracker: CrsTracker,
) -> list[BaseGeometry]:
    footprints: list[BaseGeometry] = []

    for row in graph.query(FOOTPRINT_QUERY):
        geometry = parse_geosparql_wkt(
            str(row.footprintWkt),
            crs_tracker=crs_tracker,
            context=f"footprint of {local_name(row.building)}",
        )

        if geometry is not None:
            footprints.append(geometry)

    return footprints


def load_candidate_cells(
    graph: Graph,
    crs_tracker: CrsTracker,
) -> tuple[list[dict], int]:
    """Return (candidates that leave ExtremeRisk, total ExtremeRisk rows).

    The total row count is needed by the duplicate check: duplicates must
    be detected on all rows, not only on the improving subset.
    """

    candidates: list[dict] = []
    seen_cell_uris: dict[str, int] = {}
    total_rows = 0

    for row in graph.query(CELL_QUERY):
        total_rows += 1
        cell_uri = str(row.cell)
        seen_cell_uris[cell_uri] = seen_cell_uris.get(cell_uri, 0) + 1

        cell_geometry = parse_geosparql_wkt(
            str(row.wkt),
            crs_tracker=crs_tracker,
            context=f"geometry of {local_name(row.cell)}",
        )

        if cell_geometry is None:
            continue

        svf = float(row.svf)
        density = float(row.density)
        topo = float(row.topo)
        canopy = float(row.canopy)
        impervious = float(row.impervious)
        heat_days = float(row.heatDays)

        new_canopy = clamp(canopy + CANOPY_INCREASE)
        new_impervious = clamp(impervious - IMPERVIOUS_DECREASE)

        recomputed_baseline_score = calculate_score(
            svf=svf,
            density=density,
            topo=topo,
            canopy=canopy,
            impervious=impervious,
            heat_days=heat_days,
        )

        scenario_score = calculate_score(
            svf=svf,
            density=density,
            topo=topo,
            canopy=new_canopy,
            impervious=new_impervious,
            heat_days=heat_days,
        )

        scenario_category = risk_category(scenario_score)

        cell_area_m2 = float(cell_geometry.area)
        canopy_increase_actual = new_canopy - canopy
        impervious_reduction_actual = impervious - new_impervious

        candidate = {
            "cell_uri": cell_uri,
            "cell_id": local_name(row.cell),
            "zone_uri": str(row.zone),
            "zone_id": local_name(row.zone),
            "geometry": cell_geometry,
            "graph_baseline_score": float(row.baselineScore),
            "recomputed_baseline_score": recomputed_baseline_score,
            "baseline_category": local_name(row.baselineCategory),
            "scenario_score": scenario_score,
            "scenario_category": scenario_category,
            "score_reduction": recomputed_baseline_score - scenario_score,
            "svf": svf,
            "density": density,
            "topo": topo,
            "canopy": canopy,
            "new_canopy": new_canopy,
            "canopy_increase": canopy_increase_actual,
            "impervious": impervious,
            "new_impervious": new_impervious,
            "impervious_reduction": impervious_reduction_actual,
            "heat_days": heat_days,
            "cell_area_m2": cell_area_m2,
            "required_new_canopy_m2": canopy_increase_actual * cell_area_m2,
            "required_desealing_m2": (
                impervious_reduction_actual * cell_area_m2
            ),
        }

        # We only want cells that leave ExtremeRisk, but the duplicate
        # check above has already counted this row either way.
        if scenario_category != "ExtremeRisk":
            candidates.append(candidate)

    duplicates = {
        uri: count for uri, count in seen_cell_uris.items() if count > 1
    }

    if duplicates:
        sample = ", ".join(
            local_name(uri) for uri in list(duplicates)[:5]
        )
        raise RuntimeError(
            f"{len(duplicates)} ExtremeRisk cells returned multiple query "
            f"rows (e.g. {sample}). This usually means a cell carries more "
            "than one uhi:hasHeatRiskAssessment (pass 1 and pass 2 both "
            "attached instead of replaced) or a zone has multiple "
            "uhi:hasHeatDayCount values. Fix the graph before selecting "
            "an intervention cell, otherwise pass-1 (default-weight) "
            "scores can leak into the ranking."
        )

    return candidates, total_rows


# ---------------------------------------------------------------------------
# CONSISTENCY CHECKS
# ---------------------------------------------------------------------------

def validate_weight_consistency(
    candidates: list[dict],
    tolerance: float,
    skip: bool,
) -> None:
    """Ensure local weights reproduce the graph's stored baseline scores.

    The graph stores pass-2 (calibrated) scores. If the hardcoded weights
    here have drifted from the calibrated ones, every derived quantity
    (scenario score, score reduction, category transition) is computed in
    a different scoring space than the one that made these cells
    ExtremeRisk — so this is a hard error by default.
    """

    mismatches = [
        candidate
        for candidate in candidates
        if abs(
            candidate["recomputed_baseline_score"]
            - candidate["graph_baseline_score"]
        )
        > tolerance
    ]

    if not mismatches:
        print(
            "  Weight-consistency check passed: local weights reproduce "
            f"all {len(candidates)} stored baseline scores "
            f"(tolerance {tolerance:g})."
        )
        return

    worst = max(
        mismatches,
        key=lambda candidate: abs(
            candidate["recomputed_baseline_score"]
            - candidate["graph_baseline_score"]
        ),
    )

    message = (
        f"{len(mismatches)} of {len(candidates)} cells have a locally "
        "recomputed baseline score that does not match the stored graph "
        f"score (worst: {worst['cell_id']}, stored "
        f"{worst['graph_baseline_score']:.6f} vs recomputed "
        f"{worst['recomputed_baseline_score']:.6f}). The hardcoded "
        "weights in this script likely differ from the calibrated "
        "weights used by risk_assessment pass 2. Update the W_* "
        "constants (or the normalisation cap) to match uhi_calibration "
        "output before trusting any scenario numbers."
    )

    if skip:
        print(f"  WARNING (--skip-weight-check): {message}")
        return

    raise RuntimeError(message)


def validate_cell_areas(candidates: list[dict]) -> None:
    """Median candidate area must be ~10,000 m2 for a 100 m metric grid."""

    if not candidates:
        return

    median_area = statistics.median(
        candidate["cell_area_m2"] for candidate in candidates
    )

    lower = EXPECTED_CELL_AREA_M2 * (1.0 - CELL_AREA_RELATIVE_SLACK)
    upper = EXPECTED_CELL_AREA_M2 * (1.0 + CELL_AREA_RELATIVE_SLACK)

    if not (lower <= median_area <= upper):
        raise RuntimeError(
            f"Median candidate cell area is {median_area:.1f} m2, expected "
            f"about {EXPECTED_CELL_AREA_M2:.0f} m2 for the 100 m grid. "
            "This usually indicates a CRS problem (e.g. WGS84 WKT parsed "
            "as metric) or a grid-resolution change."
        )

    print(
        f"  Cell-area sanity check passed: median candidate area "
        f"{median_area:.0f} m2."
    )


# ---------------------------------------------------------------------------
# FEASIBILITY SCREENING
# ---------------------------------------------------------------------------

def add_feasibility_screening(
    candidates: list[dict],
    footprints: list[BaseGeometry],
) -> None:
    footprint_index = STRtree(footprints)

    for candidate in candidates:
        cell_geometry = candidate["geometry"]
        cell_area_m2 = candidate["cell_area_m2"]

        building_area_m2 = building_area_inside_cell(
            cell_geometry=cell_geometry,
            footprints=footprints,
            footprint_index=footprint_index,
        )

        impervious_area_m2 = candidate["impervious"] * cell_area_m2

        # Screening proxy:
        # impervious area minus actual building footprint area.
        #
        # This may represent roads, parking, pavements, plazas, and
        # courtyards, but not all of it is necessarily removable.
        non_building_impervious_proxy_m2 = max(
            0.0,
            impervious_area_m2 - building_area_m2,
        )

        required_desealing_m2 = candidate["required_desealing_m2"]

        desealing_margin_m2 = (
            non_building_impervious_proxy_m2 - required_desealing_m2
        )

        # Soft canopy check: does the required new canopy fit within the
        # non-building area of the cell? Street trees can legitimately
        # overhang pavement and even touch facades, so this is reported
        # but does not affect the pass/fail ranking.
        open_area_m2 = max(0.0, cell_area_m2 - building_area_m2)

        canopy_fits_open_area = (
            candidate["required_new_canopy_m2"] <= open_area_m2
        )

        candidate.update(
            {
                "building_area_m2": building_area_m2,
                "building_fraction": (
                    building_area_m2 / cell_area_m2 if cell_area_m2 else 0.0
                ),
                "impervious_area_m2": impervious_area_m2,
                "non_building_impervious_proxy_m2": (
                    non_building_impervious_proxy_m2
                ),
                "desealing_margin_m2": desealing_margin_m2,
                "passes_area_screening": desealing_margin_m2 >= 0.0,
                "open_area_m2": open_area_m2,
                "canopy_fits_open_area": canopy_fits_open_area,
            }
        )


def rank_candidates(candidates: list[dict]) -> list[dict]:
    return sorted(
        candidates,
        key=lambda candidate: (
            not candidate["passes_area_screening"],
            -candidate["desealing_margin_m2"],
            -candidate["score_reduction"],
        ),
    )


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def print_candidate_table(candidates: list[dict], limit: int) -> None:
    print()
    print(
        "ExtremeRisk cells leaving ExtremeRisk under the combined scenario"
    )
    print("-" * 126)

    print(
        f"{'Cell':<28}"
        f"{'Base':>7}"
        f"{'New':>7}"
        f"{'New category':>15}"
        f"{'Canopy':>10}"
        f"{'Imperv.':>10}"
        f"{'Open imperv.':>15}"
        f"{'Needed':>11}"
        f"{'Pass':>7}"
        f"{'CanopyOK':>10}"
    )

    for candidate in candidates[:limit]:
        print(
            f"{candidate['cell_id']:<28}"
            f"{candidate['graph_baseline_score']:>7.3f}"
            f"{candidate['scenario_score']:>7.3f}"
            f"{candidate['scenario_category']:>15}"
            f"{candidate['canopy']:>9.1%}"
            f"{candidate['impervious']:>10.1%}"
            f"{candidate['non_building_impervious_proxy_m2']:>13.0f} m²"
            f"{candidate['required_desealing_m2']:>9.0f} m²"
            f"{str(candidate['passes_area_screening']):>7}"
            f"{str(candidate['canopy_fits_open_area']):>10}"
        )


def print_selected_candidate(candidate: dict) -> None:
    centroid = candidate["geometry"].representative_point()
    lon, lat = TO_WGS84.transform(centroid.x, centroid.y)

    print()
    print("Selected cell")
    print("-------------")
    print(f"Cell: {candidate['cell_id']}")
    print(f"Zone: {candidate['zone_id']}")
    print(f"Map center: {lat:.6f}, {lon:.6f}")

    print()
    print("Risk result")
    print("-----------")
    print(
        "Baseline score: "
        f"{candidate['graph_baseline_score']:.3f} "
        f"({candidate['baseline_category']})"
    )
    print(
        "Scenario score: "
        f"{candidate['scenario_score']:.3f} "
        f"({candidate['scenario_category']})"
    )
    print(f"Score reduction: {candidate['score_reduction']:.3f}")

    print()
    print("Greening assumptions")
    print("--------------------")
    print(f"Current canopy: {candidate['canopy']:.1%}")
    print(f"Scenario canopy: {candidate['new_canopy']:.1%}")
    print(
        "Additional canopy required: "
        f"{candidate['required_new_canopy_m2']:.0f} m²"
    )
    print(
        "Fits within non-building area: "
        f"{candidate['canopy_fits_open_area']} "
        f"(open area {candidate['open_area_m2']:.0f} m²)"
    )

    print()
    print(f"Current imperviousness: {candidate['impervious']:.1%}")
    print(f"Scenario imperviousness: {candidate['new_impervious']:.1%}")
    print(
        "Surface requiring de-sealing: "
        f"{candidate['required_desealing_m2']:.0f} m²"
    )

    print()
    print("Physical screening")
    print("------------------")
    print(f"Cell area: {candidate['cell_area_m2']:.0f} m²")
    print(
        "Actual building footprint inside cell: "
        f"{candidate['building_area_m2']:.0f} m² "
        f"({candidate['building_fraction']:.1%})"
    )
    print(
        "Estimated total impervious area: "
        f"{candidate['impervious_area_m2']:.0f} m²"
    )
    print(
        "Non-building impervious-area proxy: "
        f"{candidate['non_building_impervious_proxy_m2']:.0f} m²"
    )
    print(
        "Area remaining after assumed de-sealing: "
        f"{candidate['desealing_margin_m2']:.0f} m²"
    )
    print(
        "Passes first-stage area screening: "
        f"{candidate['passes_area_screening']}"
    )

    print()
    print(
        "Important: passing this screening only means that the cell "
        "contains enough estimated non-building impervious area for "
        "de-sealing. The canopy check is a soft indicator only — street "
        "trees can overhang pavement, so canopy exceeding the open area "
        "is not automatically infeasible, and canopy fitting the open "
        "area does not guarantee plantable ground. The next map must "
        "verify whether the candidate surfaces are parking, roadway, "
        "pavement, plaza, or another surface that can realistically be "
        "converted."
    )


def write_selected_geojson(candidate: dict, destination: Path) -> None:
    geometry_wgs84 = shapely_transform(
        TO_WGS84.transform,
        candidate["geometry"],
    )

    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": mapping(geometry_wgs84),
                "properties": {
                    "cell_id": candidate["cell_id"],
                    "zone_id": candidate["zone_id"],
                    "baseline_score": round(
                        candidate["graph_baseline_score"], 4
                    ),
                    "baseline_category": candidate["baseline_category"],
                    "scenario_score": round(
                        candidate["scenario_score"], 4
                    ),
                    "scenario_category": candidate["scenario_category"],
                    "canopy_baseline": round(candidate["canopy"], 4),
                    "canopy_scenario": round(candidate["new_canopy"], 4),
                    "impervious_baseline": round(
                        candidate["impervious"], 4
                    ),
                    "impervious_scenario": round(
                        candidate["new_impervious"], 4
                    ),
                    "required_new_canopy_m2": round(
                        candidate["required_new_canopy_m2"], 1
                    ),
                    "required_desealing_m2": round(
                        candidate["required_desealing_m2"], 1
                    ),
                    "building_area_m2": round(
                        candidate["building_area_m2"], 1
                    ),
                    "non_building_impervious_proxy_m2": round(
                        candidate["non_building_impervious_proxy_m2"], 1
                    ),
                    "open_area_m2": round(candidate["open_area_m2"], 1),
                    "canopy_fits_open_area": candidate[
                        "canopy_fits_open_area"
                    ],
                    "passes_area_screening": candidate[
                        "passes_area_screening"
                    ],
                },
            }
        ],
    }

    destination.write_text(
        json.dumps(feature_collection, indent=2),
        encoding="utf-8",
    )

    print()
    print(f"Selected-cell GeoJSON written to: {destination}")


# ---------------------------------------------------------------------------
# COMMAND LINE
# ---------------------------------------------------------------------------

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Select an ExtremeRisk grid cell that leaves ExtremeRisk "
            "under the combined greening counterfactual."
        )
    )

    parser.add_argument(
        "--ttl",
        type=Path,
        default=DEFAULT_TTL_FILE,
        help=f"Input Turtle graph. Default: {DEFAULT_TTL_FILE}",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_FILE,
        help=(
            "GeoJSON output for the selected cell. "
            f"Default: {DEFAULT_OUTPUT_FILE}"
        ),
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of ranked candidates to print.",
    )

    parser.add_argument(
        "--cell",
        type=str,
        default=None,
        help=(
            "Select a specific improving cell by local ID, "
            "for example Cell_513_5402_04_07."
        ),
    )

    parser.add_argument(
        "--skip-weight-check",
        action="store_true",
        help=(
            "Downgrade the weight-consistency check from an error to a "
            "warning. Only use this deliberately, e.g. to inspect how "
            "far the weights have drifted."
        ),
    )

    parser.add_argument(
        "--weight-tolerance",
        type=float,
        default=WEIGHT_MATCH_TOLERANCE,
        help=(
            "Maximum allowed difference between stored and recomputed "
            f"baseline scores. Default: {WEIGHT_MATCH_TOLERANCE}"
        ),
    )

    return parser.parse_args()


def main() -> None:
    args = parse_arguments()

    ttl_file = args.ttl.resolve()
    output_file = args.output.resolve()

    if not ttl_file.exists():
        raise FileNotFoundError(
            f"{ttl_file} not found. Run the pipeline first."
        )

    print(f"Loading graph: {ttl_file}")

    graph = Graph()
    graph.parse(str(ttl_file), format="turtle")

    print(f"  {len(graph)} triples loaded")

    crs_tracker = CrsTracker()

    print("Reading real building footprints ...")

    footprints = load_footprints(graph, crs_tracker=crs_tracker)

    print(f"  {len(footprints)} footprint geometries loaded")

    print("Reading ExtremeRisk grid cells ...")

    candidates, total_extreme_rows = load_candidate_cells(
        graph,
        crs_tracker=crs_tracker,
    )

    crs_tracker.report()

    print(f"  ExtremeRisk query rows: {total_extreme_rows}")
    print(
        f"  ExtremeRisk cells leaving ExtremeRisk: {len(candidates)}"
    )

    if not candidates:
        raise RuntimeError(
            "No ExtremeRisk grid cell leaves ExtremeRisk under the "
            "configured greening scenario."
        )

    print("Running consistency checks ...")

    validate_weight_consistency(
        candidates=candidates,
        tolerance=args.weight_tolerance,
        skip=args.skip_weight_check,
    )

    validate_cell_areas(candidates)

    print(
        "Computing building-area intersections and "
        "first-stage physical screening ..."
    )

    add_feasibility_screening(
        candidates=candidates,
        footprints=footprints,
    )

    ranked_candidates = rank_candidates(candidates)

    print_candidate_table(
        candidates=ranked_candidates,
        limit=max(1, args.limit),
    )

    selected = None

    if args.cell:
        for candidate in ranked_candidates:
            if candidate["cell_id"] == args.cell:
                selected = candidate
                break

        if selected is None:
            raise ValueError(
                f"{args.cell} is not an ExtremeRisk cell that leaves "
                "ExtremeRisk under this scenario."
            )
    else:
        selected = ranked_candidates[0]

    print_selected_candidate(selected)

    write_selected_geojson(
        candidate=selected,
        destination=output_file,
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, FileNotFoundError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        sys.exit(1)
