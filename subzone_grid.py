#!/usr/bin/env python3
"""
subzone_grid.py — Refine the zone-level heat-risk assessment to a 100 m grid.

Motivation
----------
The composite score is computed per 1 km² analysis zone, so the map shows four
flat colour blocks. Four of the six indicators, however, have much finer native
resolution than the zone:

    Tree canopy (CLMS TCD)      10 m raster
    Imperviousness (CLMS IMD)   10 m raster
    Topographic exposure (TPI)  1 m DGM1 raster
    Sky view factor             per building (geometric)

This script recovers that spatial detail by subdividing each zone into a regular
100 m grid (10 x 10 = 100 cells per zone; 400 cells total), re-sampling every
indicator per cell, and computing the *same* composite score per cell. All four
high-resolution indicators — tree canopy, imperviousness, sky view factor and
topographic exposure — are resolved per cell (topographic exposure via the shared
terrain_tpi module, using the same DGM1 DEM and 300 m TPI window as the zone-level
step). Heat-day count is the only indicator that stays at zone resolution, because
no finer climate observation exists. The result is a continuous gradient instead
of a per-zone constant, and it directly delivers the "sub-zone grid scoring"
roadmap item.

Each cell is written to the graph as a uhi:GridCell (already declared as an
uhi:AnalysisZone subclass), linked to its parent zone via uhi:hasParentZone, and
given its own uhi:GridCellHeatRiskAssessment. Because GridCell is an AnalysisZone,
it reuses all the existing indicator and assessment properties unchanged — the
ontology scales to finer granularity by adding triples, not by redesign.

100 m was chosen deliberately: it matches the effective resolution of Landsat
thermal imagery, so the grid can later be validated cell-for-cell against
satellite land-surface temperature (see lst_validation.py).

Run order
---------
Run AFTER risk_assessment.py, so buildings already carry geometric SVF and the
parent zones already carry all indicators:

    ... -> uhi_calibration.py -> risk_assessment.py -> subzone_grid.py -> queries_and_viz.py

Requires: rdflib, rasterio, shapely, pyproj, numpy (already in requirements.txt).
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom
from rdflib import Graph, Literal, Namespace, RDF, URIRef, XSD
from shapely.geometry import box, mapping, Point

import terrain_tpi as tpi
# Reuse the project's canonical namespace definitions so URIs match the rest of
# the graph exactly (risk_assessment.py, citygml_to_rdf.py, etc. all import these).
from namespaces import UHI, EX, bind_all

# GEO and BOT may or may not be exported by namespaces.py; define locally as a
# fallback but prefer the shared ones if present.
try:
    from namespaces import GEO  # type: ignore
except ImportError:
    GEO = Namespace("http://www.opengis.net/ont/geosparql#")
try:
    from namespaces import BOT  # type: ignore
except ImportError:
    BOT = Namespace("https://w3id.org/bot#")

# ---------------------------------------------------------------------------
# CONFIG — mirror the constants used elsewhere in the pipeline
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"
CLMS_DIR = BASE_DIR / "clms_landcover"
DGM_DIR = BASE_DIR                     # terrain_dgm writes exposure into graph already

CELL_SIZE = 100.0                      # metres — matches Landsat thermal resolution
VALID_MAX = 100                        # CLMS valid pixel ceiling (0..100 %)

# Score weights — identical to risk_assessment.py
W_SVF = 0.35
W_DENSITY = 0.20
W_TOPO = 0.15
W_VEGETATION = 0.10
W_IMPERVIOUS = 0.10
W_HEAT_DAYS = 0.10
MAX_HEAT_DAYS_FOR_NORMALISATION = 30.0

# Category thresholds — identical to risk_assessment.py
MEDIUM_RISK_MIN = 0.25
HIGH_RISK_MIN = 0.35
EXTREME_RISK_MIN = 0.50

# Zone bounding boxes in EPSG:25832 — identical to the rest of the pipeline
ZONE_BBOXES = {
    "Zone_513_5402": (513000, 5402000, 514000, 5403000),
    "Zone_513_5403": (513000, 5403000, 514000, 5404000),
    "Zone_514_5402": (514000, 5402000, 515000, 5403000),
    "Zone_514_5403": (514000, 5403000, 515000, 5404000),
}


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------
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


def find_raster(directory: Path, pattern: str) -> Path | None:
    hits = list(directory.glob(pattern))
    return hits[0] if hits else None


def raster_mean_fraction(raster_path: Path, bbox_utm) -> float | None:
    """Mean of valid CLMS pixels (0..100) inside a UTM32 bbox, as a fraction [0,1].
    Returns None if the cell contains no valid pixels (e.g. entirely nodata)."""
    geom_utm = mapping(box(*bbox_utm))
    with rasterio.open(raster_path) as src:
        geom_rc = transform_geom("EPSG:25832", src.crs, geom_utm)
        try:
            arr, _ = mask(src, [geom_rc], crop=True, filled=False)
        except ValueError:
            return None
        band = arr[0]
        valid = (~band.mask) & (band.data <= VALID_MAX)
        vals = band.data[valid]
        if vals.size == 0:
            return None
        return float(vals.mean()) / 100.0


def local_name(uri) -> str:
    text = str(uri)
    return text.rsplit("#", 1)[-1].rstrip("/").rsplit("/", 1)[-1]


# ---------------------------------------------------------------------------
# READ EXISTING GRAPH DATA
# ---------------------------------------------------------------------------
def _sparql_prefixes() -> str:
    """Build SPARQL PREFIX declarations from the imported Namespace objects so the
    queries always match the URIs actually used in the graph, regardless of what
    namespaces.py defines."""
    return (
        f"PREFIX uhi: <{str(UHI)}>\n"
        f"PREFIX geo: <{str(GEO)}>\n"
        f"PREFIX bot: <{str(BOT)}>\n"
    )


def read_zone_context(g: Graph):
    """Return {zone_id: {topo, canopy, imperv, heat_days, svf}} from the graph,
    used as fallbacks when a cell has no finer data of its own."""
    q = _sparql_prefixes() + """
    SELECT ?zone ?topo ?canopy ?imperv ?heat ?svf WHERE {
        ?zone a uhi:AnalysisZone .
        OPTIONAL { ?zone uhi:hasTopographicExposure       ?topo }
        OPTIONAL { ?zone uhi:hasTreeCanopyCoverage        ?canopy }
        OPTIONAL { ?zone uhi:hasImperviousSurfaceFraction ?imperv }
        OPTIONAL { ?zone uhi:hasHeatDayCount              ?heat }
        OPTIONAL { ?zone uhi:hasSkyViewFactor             ?svf }
    }
    """
    ctx = {}
    for r in g.query(q):
        zid = local_name(r.zone)
        if zid not in ZONE_BBOXES:
            continue
        ctx[zid] = {
            "topo": float(r.topo) if r.topo is not None else 0.35,
            "canopy": float(r.canopy) if r.canopy is not None else 0.0,
            "imperv": float(r.imperv) if r.imperv is not None else 0.5,
            "heat_days": float(r.heat) if r.heat is not None else 0.0,
            "svf": float(r.svf) if r.svf is not None else 0.5,
        }
    return ctx


def read_buildings(g: Graph):
    """Return list of (zone_id, easting, northing, svf, footprint_area)."""
    q = _sparql_prefixes() + """
    SELECT ?zone ?wkt ?svf ?foot WHERE {
        ?b a bot:Building ;
           uhi:inAnalysisZone ?zone ;
           uhi:hasFootprintArea ?foot ;
           geo:hasGeometry ?geom .
        ?geom geo:asWKT ?wkt .
        OPTIONAL { ?b uhi:hasSkyViewFactor ?svf }
    }
    """
    pt_re = re.compile(r"POINT\(\s*([-0-9.]+)\s+([-0-9.]+)\s*\)")
    out = []
    for r in g.query(q):
        m = pt_re.search(str(r.wkt))
        if not m:
            continue
        out.append((
            local_name(r.zone),
            float(m.group(1)), float(m.group(2)),
            float(r.svf) if r.svf is not None else None,
            float(r.foot),
        ))
    return out


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main() -> None:
    if not TTL_FILE.exists():
        raise FileNotFoundError(f"{TTL_FILE} not found. Run risk_assessment.py first.")

    print("Loading graph ...")
    g = Graph()
    bind_all(g)
    g.parse(TTL_FILE, format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    tcd_path = find_raster(CLMS_DIR, "*TCD*.tif")
    imd_path = find_raster(CLMS_DIR, "*IMD*.tif")
    if tcd_path is None or imd_path is None:
        print(f"  [!] CLMS rasters not found under {CLMS_DIR}; cells will fall back "
              f"to parent-zone canopy/imperviousness values.")

    zone_ctx = read_zone_context(g)
    buildings = read_buildings(g)
    print(f"  {len(buildings)} buildings, {len(zone_ctx)} zones in graph")

    # Bucket buildings by zone for fast per-cell lookup
    by_zone: dict[str, list] = {z: [] for z in ZONE_BBOXES}
    for zid, e, n, svf, foot in buildings:
        if zid in by_zone:
            by_zone[zid].append((e, n, svf, foot))

    n_per_side = int(1000 / CELL_SIZE)   # 10 cells per 1 km zone edge
    total_cells = 0
    cell_scores = []

    for zid, (xmin, ymin, xmax, ymax) in ZONE_BBOXES.items():
        ctx = zone_ctx.get(zid, {
            "topo": 0.35, "canopy": 0.0, "imperv": 0.5, "heat_days": 0.0, "svf": 0.5,
        })
        heat_norm = clamp(ctx["heat_days"] / MAX_HEAT_DAYS_FOR_NORMALISATION)

        for gi in range(n_per_side):        # column (easting)
            for gj in range(n_per_side):    # row (northing)
                cx0 = xmin + gi * CELL_SIZE
                cy0 = ymin + gj * CELL_SIZE
                cx1, cy1 = cx0 + CELL_SIZE, cy0 + CELL_SIZE
                cell_bbox = (cx0, cy0, cx1, cy1)

                # --- raster indicators per cell (fall back to zone mean) ---
                canopy = None
                imperv = None
                if tcd_path is not None:
                    canopy = raster_mean_fraction(tcd_path, cell_bbox)
                if imd_path is not None:
                    imperv = raster_mean_fraction(imd_path, cell_bbox)
                if canopy is None:
                    canopy = ctx["canopy"]
                if imperv is None:
                    imperv = ctx["imperv"]

                # --- topographic exposure: per-cell TPI from the shared module ---
                # Uses the same DGM1 DEM and 300 m TPI window as terrain_dgm.py,
                # sampled over this 100 m cell. Falls back to the parent-zone value
                # if the DGM1 tiles are unavailable (e.g. running without terrain data).
                topo = None
                try:
                    topo = tpi.cell_exposure(cx0, cy0, cx1, cy1)
                except FileNotFoundError:
                    topo = None
                if topo is None:
                    topo = ctx["topo"]

                # --- building-derived indicators per cell ---
                cell_buildings = [
                    (e, n, svf, foot) for (e, n, svf, foot) in by_zone[zid]
                    if cx0 <= e < cx1 and cy0 <= n < cy1
                ]
                if cell_buildings:
                    svf_vals = [b[2] for b in cell_buildings if b[2] is not None]
                    svf = float(np.mean(svf_vals)) if svf_vals else ctx["svf"]
                    built_area = sum(b[3] for b in cell_buildings)
                    density = clamp(built_area / (CELL_SIZE * CELL_SIZE))
                else:
                    # No buildings (park, water, open land): use zone SVF, zero density.
                    svf = ctx["svf"]
                    density = 0.0

                # --- composite score (identical formula) ---
                score = clamp(
                    W_SVF * (1.0 - svf) +
                    W_DENSITY * density +
                    W_TOPO * topo +
                    W_VEGETATION * (1.0 - canopy) +
                    W_IMPERVIOUS * imperv +
                    W_HEAT_DAYS * heat_norm
                )
                category = risk_category(score)

                # --- write GridCell + its assessment to the graph ---
                cell_id = f"Cell_{zid.replace('Zone_', '')}_{gi:02d}_{gj:02d}"
                cell_uri = EX[cell_id]
                geom_uri = EX[f"{cell_id}_geom"]
                assess_uri = EX[f"GridCellHeatRiskAssessment_{cell_id}"]

                ccx, ccy = cx0 + CELL_SIZE / 2, cy0 + CELL_SIZE / 2
                wkt = (f"<http://www.opengis.net/def/crs/EPSG/0/25832> "
                       f"POLYGON(({cx0} {cy0}, {cx1} {cy0}, {cx1} {cy1}, "
                       f"{cx0} {cy1}, {cx0} {cy0}))")

                g.add((cell_uri, RDF.type, UHI.GridCell))
                g.add((cell_uri, UHI.hasParentZone, EX[zid]))
                g.add((cell_uri, UHI.hasGridResolution,
                       Literal(int(CELL_SIZE), datatype=XSD.integer)))
                g.add((cell_uri, GEO.hasGeometry, geom_uri))
                g.add((geom_uri, RDF.type, GEO.Geometry))
                g.add((geom_uri, GEO.asWKT, Literal(wkt, datatype=GEO.wktLiteral)))
                # indicators (reuse AnalysisZone datatype properties)
                g.add((cell_uri, UHI.hasSkyViewFactor, Literal(round(svf, 4), datatype=XSD.decimal)))
                g.add((cell_uri, UHI.hasUrbanDensity, Literal(round(density, 4), datatype=XSD.decimal)))
                g.add((cell_uri, UHI.hasTopographicExposure, Literal(round(topo, 4), datatype=XSD.decimal)))
                g.add((cell_uri, UHI.hasTreeCanopyCoverage, Literal(round(canopy, 4), datatype=XSD.decimal)))
                g.add((cell_uri, UHI.hasImperviousSurfaceFraction, Literal(round(imperv, 4), datatype=XSD.decimal)))
                # assessment node
                g.add((cell_uri, UHI.hasHeatRiskAssessment, assess_uri))
                g.add((assess_uri, RDF.type, UHI.GridCellHeatRiskAssessment))
                g.add((assess_uri, UHI.hasHeatRiskScore, Literal(round(score, 4), datatype=XSD.decimal)))
                g.add((assess_uri, UHI.hasRiskCategory, category))

                cell_scores.append((cell_id, score, local_name(category)))
                total_cells += 1

    # --- report ---
    print(f"\nGrid cells written : {total_cells}")
    cats = {}
    for _, _, c in cell_scores:
        cats[c] = cats.get(c, 0) + 1
    print("Category distribution:")
    for c in ("ExtremeRisk", "HighRisk", "MediumRisk", "LowRisk"):
        print(f"  {c:<12}: {cats.get(c, 0)}")
    scores_only = [s for _, s, _ in cell_scores]
    print(f"Score range        : {min(scores_only):.3f} .. {max(scores_only):.3f}")
    print(f"Score mean/std     : {np.mean(scores_only):.3f} / {np.std(scores_only):.3f}")

    g.serialize(destination=str(TTL_FILE), format="turtle")
    print(f"\nTriples added      : {len(g) - triples_before}")
    print(f"Triples total      : {len(g)}")
    print(f"Updated graph written to {TTL_FILE.name}")


if __name__ == "__main__":
    main()
    