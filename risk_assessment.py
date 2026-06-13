from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, XSD

from namespaces import UHI, SOSA, EX, bind_all

BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

TILE_AREA_M2 = 1_000_000.0
MAX_HEAT_DAYS_FOR_NORMALISATION = 30.0

# Fallback assumption for dense Stuttgart-Mitte urban tiles when no explicit
# impervious-surface fraction is available.
DEFAULT_IMPERVIOUS_FRACTION = 0.80

# Approximate topographic basin-depth values for the four Stuttgart-Mitte tiles.
# Values are normalised 0..1 for the score model; they can be replaced by raster-derived values later.
BASIN_DEPTH_BY_ZONE = {
    "Zone_513_5402": 0.70,
    "Zone_513_5403": 0.85,
    "Zone_514_5402": 0.65,
    "Zone_514_5403": 0.75,
}

# Score thresholds. Tune after validation against external UHI studies or field data.

MEDIUM_RISK_MIN = 0.25
HIGH_RISK_MIN = 0.35
EXTREME_RISK_MIN = 0.50

SPARQL_PREFIXES = """
PREFIX uhi:  <https://w3id.org/stuttgart-uhi#>
PREFIX bot:  <https://w3id.org/bot#>
PREFIX sosa: <http://www.w3.org/ns/sosa/>
PREFIX geo:  <http://www.opengis.net/ont/geosparql#>
"""

# Project-defined heuristic weights.
# These should be calibrated or sensitivity-tested against measured UHI data in future work.
W_SVF = 0.35
W_DENSITY = 0.20
W_BASIN = 0.15
W_VEGETATION = 0.10
W_IMPERVIOUS = 0.10
W_HEAT_DAYS = 0.10

RISK_MODEL_FORMULA = (
    f"{W_SVF:.2f}*(1-SVF)+"
    f"{W_DENSITY:.2f}*density+"
    f"{W_BASIN:.2f}*basinDepth+"
    f"{W_VEGETATION:.2f}*(1-vegetationFraction)+"
    f"{W_IMPERVIOUS:.2f}*imperviousFraction+"
    f"{W_HEAT_DAYS:.2f}*heatDayNorm"
)


@dataclass
class ZoneIndicators:
    zone: URIRef
    building_count: int
    avg_height: float
    total_footprint: float
    urban_density: float
    impervious_fraction: float
    sky_view_factor: float
    basin_depth: float
    vegetation_fraction: float
    tree_count: int
    heat_day_count: int
    heat_day_norm: float
    score: float
    delta_t: float
    category: URIRef

def local_name(uri: URIRef) -> str:
    text = str(uri).rstrip("/")
    if "#" in text:
        return text.rsplit("#", 1)[-1]
    return text.rsplit("/", 1)[-1]


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def risk_category(score: float) -> URIRef:
    if score >= EXTREME_RISK_MIN:
        return UHI.ExtremeRisk
    if score >= HIGH_RISK_MIN:
        return UHI.HighRisk
    if score >= MEDIUM_RISK_MIN:
        return UHI.MediumRisk
    return UHI.LowRisk



def get_literal_float(g: Graph, subject: URIRef, predicate: URIRef, default: float | None = None) -> float | None:
    value = g.value(subject, predicate)
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def get_literal_int(g: Graph, subject: URIRef, predicate: URIRef, default: int = 0) -> int:
    value = g.value(subject, predicate)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def read_delta_t_coefficients(g: Graph) -> tuple[float, float]:
    """Read calibrated ΔT coefficients from the graph if available, else use defaults."""
    cal = g.value(predicate=RDF.type, object=UHI.CalibrationResult)
    if cal is not None:
        alpha = float(g.value(cal, UHI.hasCalibrationAlpha) or 0.0)
        beta  = float(g.value(cal, UHI.hasCalibrationBeta)  or 4.3969)
        return alpha, beta
    return 7.45, 3.97   # uncalibrated defaults


def compute_zone_indicators(g: Graph, dt_alpha: float = 7.45, dt_beta: float = 3.97) -> list[ZoneIndicators]:
    """Compute one indicator row per zone without duplicate aggregation.

    The function intentionally aggregates in Python instead of using a single
    SPARQL query with OPTIONAL heat-day observations, because a naive join would
    multiply building footprints by the number of heat-day observations.
    """
    zones = sorted(set(g.objects(None, UHI.inAnalysisZone)), key=str)
    indicators: list[ZoneIndicators] = []

    for zone in zones:
        buildings = sorted(set(g.subjects(UHI.inAnalysisZone, zone)), key=str)
        heights: list[float] = []
        footprints: list[float] = []
        for building in buildings:
            height = get_literal_float(g, building, UHI.hasMeasuredHeight, None)
            footprint = get_literal_float(g, building, UHI.hasFootprintArea, None)
            if height is not None:
                heights.append(height)
            if footprint is not None:
                footprints.append(footprint)

        building_count = len(buildings)
        avg_height = mean(heights) if heights else 0.0
        total_footprint = sum(footprints)
        heat_days = len(set(g.subjects(SOSA.hasFeatureOfInterest, zone)) & set(g.subjects(RDF.type, UHI.HeatDayObservation)))

        zone_id = local_name(zone)
        urban_density = clamp(total_footprint / TILE_AREA_M2)
        impervious_fraction = get_literal_float(g, zone, UHI.hasImperviousSurfaceFraction, DEFAULT_IMPERVIOUS_FRACTION)
        vegetation_fraction = get_literal_float(g, zone, UHI.hasVegetationFraction, 0.0)
        tree_count = get_literal_int(g, zone, UHI.hasTreeCount, 0)
        basin_depth = BASIN_DEPTH_BY_ZONE.get(zone_id, 0.70)

        # Preferred: use raster-derived SVF from the LoD2 SVF pipeline if available.
        # Fallback: approximate SVF from urban density and average building height.
        # This proxy is only used when uhi:hasSkyViewFactor is missing.
        existing_svf = get_literal_float(g, zone, UHI.hasSkyViewFactor, None)
        if existing_svf is None:
            height_factor = clamp(avg_height / 25.0)
            sky_view_factor = clamp(1.0 - (0.55 * urban_density + 0.35 * height_factor))
        else:
            sky_view_factor = clamp(existing_svf)

        heat_day_norm = clamp(heat_days / MAX_HEAT_DAYS_FOR_NORMALISATION)

        score = clamp(
            W_SVF * (1.0 - sky_view_factor) +
            W_DENSITY * urban_density +
            W_BASIN * basin_depth +
            W_VEGETATION * (1.0 - vegetation_fraction) +
            W_IMPERVIOUS * impervious_fraction +
            W_HEAT_DAYS * heat_day_norm
        )

        delta_t = round(dt_alpha + dt_beta * score, 2)
        category = risk_category(score)

        indicators.append(ZoneIndicators(
            zone=zone,
            building_count=building_count,
            avg_height=avg_height,
            total_footprint=total_footprint,
            urban_density=urban_density,
            impervious_fraction=impervious_fraction,
            sky_view_factor=sky_view_factor,
            basin_depth=basin_depth,
            vegetation_fraction=vegetation_fraction,
            tree_count=tree_count,
            heat_day_count=heat_days,
            heat_day_norm=heat_day_norm,
            score=score,
            delta_t=delta_t,
            category=category,
        ))

    return indicators

def add_zone_assessment(g: Graph, ind: ZoneIndicators) -> URIRef:
    zone_id = local_name(ind.zone)
    assessment = EX[f"ZoneHeatRiskAssessment_{zone_id}"]

    g.add((assessment, RDF.type, UHI.HeatRiskAssessment))
    g.add((assessment, RDF.type, UHI.ZoneHeatRiskAssessment))
    g.add((assessment, RDFS.label, Literal(f"Zone heat risk assessment for {zone_id}", lang="en")))
    g.add((assessment, UHI.assessesZone, ind.zone))
    g.set((ind.zone, UHI.hasHeatRiskAssessment, assessment))

    # Store indicators directly on the zone for simple queries and visualisation.
    zone_values = [
        (UHI.hasSkyViewFactor, ind.sky_view_factor, XSD.decimal),
        (UHI.hasAverageSkyViewFactor, ind.sky_view_factor, XSD.decimal),
        (UHI.hasUrbanDensity, ind.urban_density, XSD.decimal),
        (UHI.hasBasinDepth, ind.basin_depth, XSD.decimal),
        (UHI.hasHeatDayCount, ind.heat_day_count, XSD.integer),
        (UHI.hasImperviousSurfaceFraction, ind.impervious_fraction, XSD.decimal),
    ]
    for pred, value, dtype in zone_values:
        if dtype == XSD.integer:
            lit = Literal(int(value), datatype=dtype)
        else:
            lit = Literal(round(float(value), 4), datatype=dtype)
        g.set((ind.zone, pred, lit))


    assessment_values = [
        (UHI.hasHeatRiskScore, ind.score, XSD.decimal),
        (UHI.hasIndicativeDeltaT, ind.delta_t, XSD.decimal),
        (UHI.usesRiskModel, RISK_MODEL_FORMULA, XSD.string),
    ]
    for pred, value, dtype in assessment_values:
        if dtype == XSD.string:
            lit = Literal(value, datatype=dtype)
        else:
            lit = Literal(round(float(value), 4), datatype=dtype)
        g.set((assessment, pred, lit))

    g.set((assessment, UHI.hasRiskCategory, ind.category))
    return assessment


def add_building_assessments(g: Graph, zone_indicators: dict[URIRef, ZoneIndicators],
                              dt_alpha: float = 7.45, dt_beta: float = 3.97) -> int:
    query = SPARQL_PREFIXES + """
    SELECT ?building ?zone ?height ?footprint ?bsvf
    WHERE {
        ?building a bot:Building ;
                  uhi:inAnalysisZone ?zone ;
                  uhi:hasMeasuredHeight ?height ;
                  uhi:hasFootprintArea ?footprint .
        OPTIONAL { ?building uhi:hasSkyViewFactor ?bsvf . }
    }
    """

    # Clear stale VulnerableBuilding classifications from any previous run so
    # buildings that drop to MediumRisk or LowRisk don't retain the old type.
    for s, _, _ in list(g.triples((None, RDF.type, UHI.VulnerableBuilding))):
        g.remove((s, RDF.type, UHI.VulnerableBuilding))
    for s, _, _ in list(g.triples((None, UHI.classifiesAsVulnerableBuilding, None))):
        g.remove((s, UHI.classifiesAsVulnerableBuilding, None))

    vulnerable_count = 0
    for row in g.query(query):
        building = row.building
        zone = row.zone
        ind = zone_indicators.get(zone)
        if ind is None:
            continue

        building_id = local_name(building)
        assessment = EX[f"BuildingHeatRiskAssessment_{building_id}"]
        height = float(row.height or 0.0)
        footprint = float(row.footprint or 0.0)

        # Use per-building geometric SVF when available; fall back to zone average.
        # Replace only the SVF component of the zone score so all other zone-level
        # indicators (density, basin, vegetation, etc.) remain unchanged.
        building_svf = clamp(float(row.bsvf)) if row.bsvf is not None else ind.sky_view_factor
        svf_delta = W_SVF * (ind.sky_view_factor - building_svf)

        height_modifier = clamp((height - ind.avg_height) / 30.0, -0.05, 0.08)
        footprint_modifier = clamp((footprint - 500.0) / 5000.0, 0.0, 0.05)
        building_score = clamp(ind.score + svf_delta + height_modifier + footprint_modifier)

        building_delta_t = round(dt_alpha + dt_beta * building_score, 2)
        category = risk_category(building_score)

        g.add((assessment, RDF.type, UHI.HeatRiskAssessment))
        g.add((assessment, RDF.type, UHI.BuildingHeatRiskAssessment))
        g.add((assessment, RDFS.label, Literal(f"Building heat risk assessment for {building_id}", lang="en")))
        g.set((assessment, UHI.assessesBuilding, building))
        g.set((assessment, UHI.basedOnZone, zone))
        g.set((building, UHI.hasHeatRiskAssessment, assessment))
        g.set((assessment, UHI.hasHeatRiskScore, Literal(round(building_score, 4), datatype=XSD.decimal)))
        g.set((assessment, UHI.hasIndicativeDeltaT, Literal(building_delta_t, datatype=XSD.decimal)))
        g.set((assessment, UHI.hasRiskCategory, category))
        g.set((
            assessment,
            UHI.usesRiskModel,
            Literal(f"{RISK_MODEL_FORMULA} + building height/footprint exposure modifiers", datatype=XSD.string),
        ))

        if category in {UHI.HighRisk, UHI.ExtremeRisk}:
            g.add((building, RDF.type, UHI.VulnerableBuilding))
            g.add((assessment, UHI.classifiesAsVulnerableBuilding, UHI.VulnerableBuilding))
            vulnerable_count += 1

    return vulnerable_count


def main() -> None:
    print("Loading graph ...")
    g = Graph()
    bind_all(g)

    if not TTL_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {TTL_FILE}. Run citygml_to_rdf.py, climate_data.py, and osm_enrichment.py first."
        )
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    dt_alpha, dt_beta = read_delta_t_coefficients(g)
    indicators = compute_zone_indicators(g, dt_alpha, dt_beta)
    if not indicators:
        raise RuntimeError("No zones found. Run citygml_to_rdf.py and climate_data.py first.")

    print("\nComputing zone heat-risk assessments ...")
    zone_lookup = {}
    for ind in indicators:
        add_zone_assessment(g, ind)
        zone_lookup[ind.zone] = ind
        print(
            f"  {local_name(ind.zone):<15} score={ind.score:.3f} "
            f"category={local_name(ind.category):<11} "
            f"SVF={ind.sky_view_factor:.3f} density={ind.urban_density:.3f} "
            f"veg={ind.vegetation_fraction:.3f} heatDays={ind.heat_day_count}"
        )

    print("\nComputing building heat-risk assessments ...")
    vulnerable = add_building_assessments(g, zone_lookup, dt_alpha, dt_beta)

    g.serialize(destination=str(TTL_FILE), format="turtle")
    print(f"\nBuilding assessments classified as vulnerable: {vulnerable}")
    print(f"Triples added : {len(g) - triples_before}")
    print(f"Triples total : {len(g)}")


if __name__ == "__main__":
    main()
