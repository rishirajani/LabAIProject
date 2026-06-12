"""Compute per-zone topographic exposure from the LGL Baden-Württemberg DGM1.

Pipeline step that turns the 1 m raw terrain model into a normalised UHI
indicator and writes it back onto each AnalysisZone in the knowledge graph.

Method
------
The Topographic Position Index (TPI) is the difference between a cell's
elevation and the mean elevation of its surrounding neighbourhood
(Weiss, 2001; De Reu et al., 2013). Negative TPI = local depression.

In a basin city such as Stuttgart, depressions trap nocturnal cold-air pools
under weak-wind conditions and also accumulate radiative heat by day due to
reduced ventilation (Oke, 1987; Baumüller et al., 1996; Emeis et al., 2022).
Mean depression depth across a tile is therefore a physically meaningful
indicator of the topographic component of UHI risk.

Per tile this script computes:
    - mean / min / max elevation (raw, metres, stored for traceability)
    - mean depression depth = mean(max(-TPI, 0)) (raw, metres)
    - topographic exposure = clamp(mean_depression / REFERENCE_RELIEF, 0, 1)

REFERENCE_RELIEF is fixed at 10 m as an absolute reference rooted in the
standard TPI classification literature: De Reu et al. (2013) treat cells
with |TPI| > 1 standard deviation as ridges or valleys, and the empirical
std(TPI) over the Stuttgart-Mitte merged grid at the chosen 300 m radius
is ~9.4 m. A 10 m mean depression therefore corresponds to a tile whose
average cell sits at the "valley" threshold relative to its neighbourhood.
Keeping the reference fixed (rather than min-max normalising across the
4 tiles) makes the indicator portable: adding tiles outside the study
area does not change existing zone scores.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter
from rdflib import Graph, Literal
from rdflib.namespace import XSD

from namespaces import UHI, EX, bind_all

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "dgm1_32_513_5402_2_bw"
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

# Each DGM1 tile is 1 km x 1 km at 1 m resolution.
TILE_SIZE_M = 1000
CELL_SIZE_M = 1.0

# TPI neighbourhood radius. 300 m captures street-block to neighbourhood scale
# depressions, consistent with the small-scale TPI used in Weiss (2001) and
# the mid-range scale in De Reu et al. (2013).
TPI_RADIUS_M = 300
TPI_WINDOW = int(2 * TPI_RADIUS_M / CELL_SIZE_M) + 1  # 601 cells

# Absolute reference for normalisation (metres). Set to 1 standard deviation
# of TPI at the 300 m scale over Stuttgart-Mitte, which matches the canonical
# valley-cell threshold in De Reu et al. (2013). Kept as a project constant
# so adding tiles does not change existing zone scores.
REFERENCE_RELIEF_M = 10.0

# Merged 2x2 km grid layout for Stuttgart-Mitte (4 tiles).
# Easting:  513000..515000 -> 2000 columns
# Northing: 5402000..5404000 -> 2000 rows (row 0 = top = highest northing)
MERGED_E_MIN = 513000
MERGED_N_MAX = 5404000
MERGED_COLS = 2000
MERGED_ROWS = 2000

# Mapping from XYZ filename stem to the AnalysisZone URI used elsewhere.
TILE_TO_ZONE = {
    "dgm1_32_513_5402_1_bw_2023": EX.Zone_513_5402,
    "dgm1_32_513_5403_1_bw_2023": EX.Zone_513_5403,
    "dgm1_32_514_5402_1_bw_2023": EX.Zone_514_5402,
    "dgm1_32_514_5403_1_bw_2023": EX.Zone_514_5403,
}

# Tile origin (south-west corner) parsed from filename, e.g. "513_5402" -> (513000, 5402000).
def tile_origin(stem: str) -> tuple[int, int]:
    parts = stem.split("_")
    return int(parts[2]) * 1000, int(parts[3]) * 1000


def load_merged_dem() -> np.ndarray:
    """Read all 4 XYZ tiles into a single 2000x2000 array of elevations.

    Row 0 is the northernmost row (highest northing); column 0 is westernmost.
    Cells are addressed by their centre coordinates (x.5, y.5).
    """
    dem = np.full((MERGED_ROWS, MERGED_COLS), np.nan, dtype=np.float32)

    for stem in TILE_TO_ZONE:
        path = DATA_DIR / f"{stem}.xyz"
        if not path.exists():
            raise FileNotFoundError(f"Missing DGM1 tile: {path}")
        print(f"  Loading {path.name} ...")
        xyz = np.loadtxt(path, dtype=np.float64)

        # Convert UTM coordinates -> merged-grid row/col via cell centres.
        col = (xyz[:, 0] - MERGED_E_MIN - 0.5).astype(np.int32)
        row = (MERGED_N_MAX - xyz[:, 1] - 0.5).astype(np.int32)
        dem[row, col] = xyz[:, 2].astype(np.float32)

    missing = int(np.isnan(dem).sum())
    if missing:
        raise RuntimeError(f"Merged DEM has {missing} unfilled cells; check tile coverage.")
    return dem


def compute_tpi(dem: np.ndarray) -> np.ndarray:
    """TPI = elevation - local mean elevation (square window, edge reflection)."""
    local_mean = uniform_filter(dem, size=TPI_WINDOW, mode="reflect")
    return dem - local_mean


def zone_slice(stem: str) -> tuple[slice, slice]:
    """Return (row_slice, col_slice) carving a tile out of the merged grid."""
    e_origin, n_origin = tile_origin(stem)
    col_start = e_origin - MERGED_E_MIN
    row_start = MERGED_N_MAX - (n_origin + TILE_SIZE_M)
    return slice(row_start, row_start + TILE_SIZE_M), slice(col_start, col_start + TILE_SIZE_M)


def main() -> None:
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"DGM1 data directory not found: {DATA_DIR}")
    if not TTL_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {TTL_FILE}. Run citygml_to_rdf.py first."
        )

    print("Loading DGM1 tiles ...")
    dem = load_merged_dem()
    print(f"  Merged DEM: {dem.shape}, "
          f"range {dem.min():.2f}..{dem.max():.2f} m")

    print(f"\nComputing TPI (window = {TPI_WINDOW} cells / {TPI_RADIUS_M} m radius) ...")
    tpi = compute_tpi(dem)
    depression = np.maximum(-tpi, 0.0)  # only count cells below their neighbourhood

    print("\nLoading graph ...")
    g = Graph()
    bind_all(g)
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    print(f"\nPer-zone topographic exposure (REFERENCE_RELIEF = {REFERENCE_RELIEF_M} m):")
    print(f"  {'Zone':<18} {'mean_el':>8} {'min_el':>8} {'max_el':>8} "
          f"{'depr_m':>8} {'exposure':>9}")

    for stem, zone_uri in TILE_TO_ZONE.items():
        r_sl, c_sl = zone_slice(stem)
        tile_dem = dem[r_sl, c_sl]
        tile_depression = depression[r_sl, c_sl]

        mean_el = float(tile_dem.mean())
        min_el = float(tile_dem.min())
        max_el = float(tile_dem.max())
        mean_depression = float(tile_depression.mean())
        exposure = min(max(mean_depression / REFERENCE_RELIEF_M, 0.0), 1.0)

        # Write per-zone facts. We use set() so re-runs replace prior values
        # instead of accumulating duplicate triples.
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
    