#!/usr/bin/env python3
"""
diagnose_cell_canopy.py — Why does CLMS TCD read 0.0% for a given 100 m cell?

For a target cell (default: Cell_513_5402_02_09) this script:

1. Prints the raw TCD pixel histogram inside the cell bbox
   (settles: is the raster genuinely all-zero here?).
2. Repeats with all_touched=True to include boundary-straddling pixels
   (settles: are edge pixels being excluded by the center-in rule?).
3. Prints the same histogram for the 8 neighboring cells
   (settles: is the tree row being counted in the cell to the south?).
4. Cross-checks the graph: stored hasTreeCanopyCoverage for the target
   cell and neighbors vs. freshly computed raster means.

Run from the project directory (same place as subzone_grid.py):

    python diagnose_cell_canopy.py
    python diagnose_cell_canopy.py --cell Cell_513_5403_07_01
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom
from rdflib import Graph
from shapely.geometry import box, mapping

from namespaces import UHI, EX

BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"
CLMS_DIR = BASE_DIR / "clms_landcover"

CELL_SIZE = 100.0
VALID_MAX = 100

ZONE_BBOXES = {
    "Zone_513_5402": (513000, 5402000, 514000, 5403000),
    "Zone_513_5403": (513000, 5403000, 514000, 5404000),
    "Zone_514_5402": (514000, 5402000, 515000, 5403000),
    "Zone_514_5403": (514000, 5403000, 515000, 5404000),
}

CELL_ID_PATTERN = re.compile(
    r"^Cell_(\d{3}_\d{4})_(\d{2})_(\d{2})$"
)


def cell_bbox(cell_id: str) -> tuple[float, float, float, float]:
    match = CELL_ID_PATTERN.match(cell_id)
    if not match:
        raise ValueError(
            f"Cell id {cell_id!r} does not match Cell_<zone>_<gi>_<gj>."
        )
    zone_key = f"Zone_{match.group(1)}"
    gi, gj = int(match.group(2)), int(match.group(3))
    xmin, ymin, _, _ = ZONE_BBOXES[zone_key]
    x0 = xmin + gi * CELL_SIZE
    y0 = ymin + gj * CELL_SIZE
    return (x0, y0, x0 + CELL_SIZE, y0 + CELL_SIZE)


def neighbor_id(cell_id: str, di: int, dj: int) -> str | None:
    """Neighbor within the same zone; returns None if it falls outside."""
    match = CELL_ID_PATTERN.match(cell_id)
    zone, gi, gj = match.group(1), int(match.group(2)), int(match.group(3))
    ni, nj = gi + di, gj + dj
    if not (0 <= ni <= 9 and 0 <= nj <= 9):
        return None
    return f"Cell_{zone}_{ni:02d}_{nj:02d}"


def tcd_pixels(
    raster_path: Path,
    bbox: tuple[float, float, float, float],
    all_touched: bool,
) -> np.ndarray:
    geom = mapping(box(*bbox))
    with rasterio.open(raster_path) as src:
        geom_rc = transform_geom("EPSG:25832", src.crs, geom)
        arr, _ = mask(
            src, [geom_rc], crop=True, filled=False, all_touched=all_touched
        )
    band = arr[0]
    valid = (~band.mask) & (band.data <= VALID_MAX)
    return band.data[valid]


def describe(vals: np.ndarray) -> str:
    if vals.size == 0:
        return "no valid pixels"
    mean_frac = float(vals.mean()) / 100.0
    nonzero = int((vals > 0).sum())
    parts = [f"n={vals.size}", f"mean={mean_frac:.4f}", f"nonzero_px={nonzero}"]
    if nonzero:
        uniq, counts = np.unique(vals[vals > 0], return_counts=True)
        hist = ", ".join(f"{int(u)}%×{int(c)}" for u, c in zip(uniq, counts))
        parts.append(f"values: {hist}")
    return "  ".join(parts)


def stored_canopy(graph: Graph, cell_id: str) -> float | None:
    value = graph.value(EX[cell_id], UHI.hasTreeCanopyCoverage)
    return float(value) if value is not None else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cell", default="Cell_513_5402_02_09")
    args = parser.parse_args()

    tcd_candidates = sorted(CLMS_DIR.glob("*TCD*.tif"))
    if not tcd_candidates:
        raise FileNotFoundError(f"No *TCD*.tif under {CLMS_DIR}")
    tcd_path = tcd_candidates[0]
    print(f"TCD raster: {tcd_path.name}")

    print("Loading graph (for stored-value cross-check) ...")
    graph = Graph()
    graph.parse(str(TTL_FILE), format="turtle")
    print(f"  {len(graph)} triples\n")

    target = args.cell
    bbox = cell_bbox(target)
    print(f"Target cell {target}  bbox={bbox}")

    # --- 1. raw pixels, center-in rule (matches subzone_grid.py) ---
    vals = tcd_pixels(tcd_path, bbox, all_touched=False)
    print(f"\n[1] center-in pixels : {describe(vals)}")

    # --- 2. all_touched comparison ---
    vals_touched = tcd_pixels(tcd_path, bbox, all_touched=True)
    print(f"[2] all_touched=True : {describe(vals_touched)}")
    extra = vals_touched.size - vals.size
    print(
        f"    boundary-straddling pixels added: {extra} "
        "(nonzero here + zero in [1] => edge-assignment explains part of it)"
    )

    # --- 3. neighbors ---
    print("\n[3] Neighbor cells (center-in rule), stored vs fresh:")
    offsets = [(-1, 1), (0, 1), (1, 1),
               (-1, 0),          (1, 0),
               (-1, -1), (0, -1), (1, -1)]
    labels = ["NW", "N", "NE", "W", "E", "SW", "S", "SE"]
    for (di, dj), label in zip(offsets, labels):
        nid = neighbor_id(target, di, dj)
        if nid is None:
            print(f"  {label:>2} —          (outside zone grid)")
            continue
        nvals = tcd_pixels(tcd_path, cell_bbox(nid), all_touched=False)
        fresh = float(nvals.mean()) / 100.0 if nvals.size else float("nan")
        stored = stored_canopy(graph, nid)
        stored_txt = f"{stored:.4f}" if stored is not None else "  n/a"
        flag = "  <-- check" if fresh >= 0.02 else ""
        print(
            f"  {label:>2} {nid}: stored={stored_txt} fresh={fresh:.4f} "
            f"nonzero_px={int((nvals > 0).sum())}{flag}"
        )

    # --- 4. target stored vs fresh ---
    stored = stored_canopy(graph, target)
    fresh = float(vals.mean()) / 100.0 if vals.size else float("nan")
    print(
        f"\n[4] Target stored={stored} fresh={fresh:.4f} "
        f"(should match to 4 dp; mismatch => graph is stale vs raster)"
    )

    print(
        "\nInterpretation guide:\n"
        "  [1] all zeros AND [2] adds nothing nonzero AND [3] south "
        "neighbor ~0\n"
        "      => TCD genuinely blind to these trees (product limitation).\n"
        "  [1] zeros but [2] adds nonzero pixels, or S/SW/SE neighbor "
        "clearly nonzero\n"
        "      => edge assignment: the tree row's pixels belong to the "
        "boundary/neighbor.\n"
        "  [4] mismatch => rerun subzone_grid.py; the graph predates the "
        "current raster."
    )


if __name__ == "__main__":
    main()
    