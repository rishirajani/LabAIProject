"""Compute per-zone topographic exposure from the LGL Baden-Württemberg DGM1.

Pipeline step that turns the 1 m raw terrain model into a normalised UHI
indicator and writes it back onto each AnalysisZone in the knowledge graph.

The DEM loading and TPI computation live in the shared module terrain_tpi.py
so that subzone_grid.py can reuse exactly the same terrain analysis at 100 m
cell resolution. See terrain_tpi.py for the full method and citations.

Per tile this script computes:
    - mean / min / max elevation (raw, metres, stored for traceability)
    - mean depression depth = mean(max(-TPI, 0)) (raw, metres)
    - topographic exposure = clamp(mean_depression / REFERENCE_RELIEF, 0, 1)
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph, Literal
from rdflib.namespace import XSD

from namespaces import UHI, EX, bind_all
import terrain_tpi as tpi

BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

# Mapping from XYZ filename stem to the AnalysisZone URI used elsewhere.
TILE_TO_ZONE = {
    "dgm1_32_513_5402_1_bw_2023": EX.Zone_513_5402,
    "dgm1_32_513_5403_1_bw_2023": EX.Zone_513_5403,
    "dgm1_32_514_5402_1_bw_2023": EX.Zone_514_5402,
    "dgm1_32_514_5403_1_bw_2023": EX.Zone_514_5403,
}


def main() -> None:
    if not tpi.DATA_DIR.exists():
        raise FileNotFoundError(f"DGM1 data directory not found: {tpi.DATA_DIR}")
    if not TTL_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {TTL_FILE}. Run citygml_to_rdf.py first."
        )

    print("Loading DGM1 tiles ...")
    dem = tpi.load_merged_dem()
    print(f"  Merged DEM: {dem.shape}, "
          f"range {dem.min():.2f}..{dem.max():.2f} m")

    print(f"\nComputing TPI (window = {tpi.TPI_WINDOW} cells / "
          f"{tpi.TPI_RADIUS_M} m radius) ...")
    depression = tpi.compute_depression(dem)

    print("\nLoading graph ...")
    g = Graph()
    bind_all(g)
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    print(f"\nPer-zone topographic exposure "
          f"(REFERENCE_RELIEF = {tpi.REFERENCE_RELIEF_M} m):")
    print(f"  {'Zone':<18} {'mean_el':>8} {'min_el':>8} {'max_el':>8} "
          f"{'depr_m':>8} {'exposure':>9}")

    for stem, zone_uri in TILE_TO_ZONE.items():
        r_sl, c_sl = tpi.zone_slice(stem)
        tile_dem = dem[r_sl, c_sl]
        tile_depression = depression[r_sl, c_sl]

        mean_el = float(tile_dem.mean())
        min_el = float(tile_dem.min())
        max_el = float(tile_dem.max())
        mean_depression = float(tile_depression.mean())
        exposure = tpi.exposure_from_depression(mean_depression)

        # set() so re-runs replace prior values instead of accumulating duplicates.
        g.set((zone_uri, UHI.hasMeanElevation, Literal(round(mean_el, 2), datatype=XSD.decimal)))
        g.set((zone_uri, UHI.hasMinElevation,  Literal(round(min_el, 2),  datatype=XSD.decimal)))
        g.set((zone_uri, UHI.hasMaxElevation,  Literal(round(max_el, 2),  datatype=XSD.decimal)))
        g.set((zone_uri, UHI.hasMeanDepressionDepth,
               Literal(round(mean_depression, 3), datatype=XSD.decimal)))
        g.set((zone_uri, UHI.hasTopographicExposure,
               Literal(round(exposure, 4), datatype=XSD.decimal)))

        zone_id = str(zone_uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        print(f"  {zone_id:<18} {mean_el:>8.2f} {min_el:>8.2f} {max_el:>8.2f} "
              f"{mean_depression:>8.3f} {exposure:>9.4f}")

    g.serialize(destination=str(TTL_FILE), format="turtle")
    print(f"\nTriples added : {len(g) - triples_before}")
    print(f"Triples total : {len(g)}")


if __name__ == "__main__":
    main()
    