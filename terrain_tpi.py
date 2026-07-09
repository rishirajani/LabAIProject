"""Shared DGM1 terrain / TPI computation for the UHI pipeline.

Single source of truth for loading the LGL Baden-Württemberg DGM1 tiles and
computing the Topographic Position Index (TPI). Imported by:

    terrain_dgm.py   — writes per-zone topographic exposure to the graph
    subzone_grid.py  — samples per-cell topographic exposure for 100 m cells

Method
------
TPI is the difference between a cell's elevation and the mean elevation of its
surrounding neighbourhood (Weiss, 2001; De Reu et al., 2013). Negative TPI = a
local depression. In a basin city such as Stuttgart, depressions trap nocturnal
cold-air pools and reduce daytime ventilation (Oke, 1987; Baumüller et al.,
1996; Emeis et al., 2022), so mean depression depth is a physically meaningful
topographic component of UHI risk.

The derived indicator is:
    topographic exposure = clamp(mean_depression / REFERENCE_RELIEF, 0, 1)
where mean_depression = mean(max(-TPI, 0)) over the area of interest.

REFERENCE_RELIEF is fixed at 10 m (≈ 1 std of TPI at the 300 m radius over
Stuttgart-Mitte; the canonical "valley" threshold of De Reu et al. 2013). Keeping
it fixed rather than min-max normalising makes the indicator portable: adding
tiles outside the study area does not change existing scores. The same reference
is used at both zone and cell resolution so the two are directly comparable.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from scipy.ndimage import uniform_filter

# ---------------------------------------------------------------------------
# CONSTANTS (shared by both zone-level and cell-level scoring)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "dgm1_32_513_5402_2_bw"

TILE_SIZE_M = 1000
CELL_SIZE_M = 1.0

TPI_RADIUS_M = 300
TPI_WINDOW = int(2 * TPI_RADIUS_M / CELL_SIZE_M) + 1  # 601 cells

REFERENCE_RELIEF_M = 10.0

# Merged 2x2 km grid layout for Stuttgart-Mitte (4 tiles).
# Easting  513000..515000 -> 2000 columns (col 0 = westernmost)
# Northing 5402000..5404000 -> 2000 rows (row 0 = northernmost)
MERGED_E_MIN = 513000
MERGED_N_MAX = 5404000
MERGED_COLS = 2000
MERGED_ROWS = 2000

TILE_STEMS = (
    "dgm1_32_513_5402_1_bw_2023",
    "dgm1_32_513_5403_1_bw_2023",
    "dgm1_32_514_5402_1_bw_2023",
    "dgm1_32_514_5403_1_bw_2023",
)


# ---------------------------------------------------------------------------
# COORDINATE HELPERS
# ---------------------------------------------------------------------------
def tile_origin(stem: str) -> tuple[int, int]:
    """SW corner of a tile parsed from its filename stem, e.g.
    'dgm1_32_513_5402_1_bw_2023' -> (513000, 5402000)."""
    parts = stem.split("_")
    return int(parts[2]) * 1000, int(parts[3]) * 1000


def zone_slice(stem: str) -> tuple[slice, slice]:
    """(row_slice, col_slice) carving one tile out of the merged grid."""
    e_origin, n_origin = tile_origin(stem)
    col_start = e_origin - MERGED_E_MIN
    row_start = MERGED_N_MAX - (n_origin + TILE_SIZE_M)
    return (slice(row_start, row_start + TILE_SIZE_M),
            slice(col_start, col_start + TILE_SIZE_M))


def bbox_slice(xmin: float, ymin: float, xmax: float, ymax: float) -> tuple[slice, slice]:
    """(row_slice, col_slice) for an arbitrary UTM32 bbox within the merged grid.

    Used to carve out a 100 m cell (or any sub-tile window). Rows increase
    southward, so ymax maps to the smaller row index.
    """
    col_start = int(round(xmin - MERGED_E_MIN))
    col_stop = int(round(xmax - MERGED_E_MIN))
    row_start = int(round(MERGED_N_MAX - ymax))
    row_stop = int(round(MERGED_N_MAX - ymin))
    # Clamp to grid bounds defensively.
    col_start = max(0, min(MERGED_COLS, col_start))
    col_stop = max(0, min(MERGED_COLS, col_stop))
    row_start = max(0, min(MERGED_ROWS, row_start))
    row_stop = max(0, min(MERGED_ROWS, row_stop))
    return slice(row_start, row_stop), slice(col_start, col_stop)


# ---------------------------------------------------------------------------
# DEM + TPI
# ---------------------------------------------------------------------------
def load_merged_dem(data_dir: Path | None = None) -> np.ndarray:
    """Read all 4 XYZ tiles into one 2000x2000 elevation array.

    Row 0 = northernmost, column 0 = westernmost. Cells addressed by centre
    coordinates (x.5, y.5)."""
    d = data_dir or DATA_DIR
    dem = np.full((MERGED_ROWS, MERGED_COLS), np.nan, dtype=np.float32)

    for stem in TILE_STEMS:
        path = d / f"{stem}.xyz"
        if not path.exists():
            raise FileNotFoundError(f"Missing DGM1 tile: {path}")
        print(f"  Loading {path.name} ...")
        xyz = np.loadtxt(path, dtype=np.float64)
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


def compute_depression(dem: np.ndarray) -> np.ndarray:
    """Per-cell depression depth = max(-TPI, 0). Only cells below their
    neighbourhood contribute; ridges contribute zero."""
    return np.maximum(-compute_tpi(dem), 0.0)


def exposure_from_depression(mean_depression: float) -> float:
    """Normalise a mean depression depth (metres) to exposure in [0, 1]."""
    return min(max(mean_depression / REFERENCE_RELIEF_M, 0.0), 1.0)


# ---------------------------------------------------------------------------
# CACHED ACCESS (so subzone_grid does not recompute TPI for every cell)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def get_depression_grid(data_dir_str: str | None = None) -> np.ndarray:
    """Load the DEM and return the full 2000x2000 depression-depth grid, cached.

    lru_cache means the (expensive) DEM read + 601-window filter runs once per
    process even when called for hundreds of cells. Pass the DATA_DIR as a string
    so the cache key is hashable; None uses the module default.
    """
    d = Path(data_dir_str) if data_dir_str else DATA_DIR
    dem = load_merged_dem(d)
    return compute_depression(dem)


def cell_exposure(xmin: float, ymin: float, xmax: float, ymax: float,
                  data_dir: Path | None = None) -> float | None:
    """Mean topographic exposure over a UTM32 bbox (e.g. one 100 m grid cell).

    Returns None if the bbox falls outside the merged grid (no data).
    """
    d = str(data_dir) if data_dir else None
    depression = get_depression_grid(d)
    r_sl, c_sl = bbox_slice(xmin, ymin, xmax, ymax)
    sub = depression[r_sl, c_sl]
    if sub.size == 0:
        return None
    return exposure_from_depression(float(sub.mean()))