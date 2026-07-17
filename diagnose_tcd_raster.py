#!/usr/bin/env python3
"""
diagnose_tcd_raster.py — Is the TCD raster itself sane, and is the
bbox -> raster sampling aligned?

Stage-2 diagnostic, to run after diagnose_cell_canopy.py returned all-zero
for a block of city-centre cells. Answers, in order:

[A] Raster metadata: CRS, bounds, resolution, nodata — for BOTH the TCD
    and IMD rasters, side by side. A CRS/bounds asymmetry between the two
    is the prime suspect for "IMD plausible, TCD all-zero".

[B] Global TCD histogram: how many valid pixels, how many nonzero, mean.
    Stuttgart DE111 includes large forested hillsides; a plausible clip
    should have a substantial nonzero share. All-zero here = broken clip.

[C] Coverage check: do the four 1 km zone bboxes (EPSG:25832), when
    reprojected to the raster CRS, actually fall INSIDE the raster bounds?
    Prints the reprojected corner coordinates vs raster bounds.

[D] Zone-level cross-check: recompute the per-zone TCD mean with the same
    mask logic as clms pipeline/subzone_grid and compare against the
    stored uhi:hasTreeCanopyCoverage on each zone in the graph. If fresh
    and stored agree AND are nonzero, the sampling path works at zone
    scale — so a nonzero signal must exist somewhere among the cells.

[E] Nonzero-cell map: for each zone, a 10x10 ASCII grid of per-cell TCD
    (center-in rule, same as subzone_grid.py), so you can see WHERE the
    canopy lands and compare it against where the parks actually are.
    Offset green = misalignment; green in the right place = TCD really
    does miss the Schlossplatz stands; no green anywhere = clip/CRS bug.

Run:
    python diagnose_tcd_raster.py
"""

from __future__ import annotations

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


def find_raster(pattern: str) -> Path:
    hits = sorted(CLMS_DIR.glob(pattern))
    if not hits:
        raise FileNotFoundError(f"No raster matching {pattern} in {CLMS_DIR}")
    return hits[0]


def mean_fraction(raster_path: Path, bbox_utm) -> tuple[float, int, int]:
    """(mean_fraction, n_valid, n_nonzero) with the pipeline's mask logic."""
    geom_utm = mapping(box(*bbox_utm))
    with rasterio.open(raster_path) as src:
        geom_rc = transform_geom("EPSG:25832", src.crs, geom_utm)
        try:
            arr, _ = mask(src, [geom_rc], crop=True, filled=False)
        except ValueError:
            return float("nan"), 0, 0
        band = arr[0]
        valid = (~band.mask) & (band.data <= VALID_MAX)
        vals = band.data[valid]
        if vals.size == 0:
            return float("nan"), 0, 0
        return float(vals.mean()) / 100.0, int(vals.size), int((vals > 0).sum())


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def main() -> None:
    tcd_path = find_raster("*TCD*.tif")
    imd_path = find_raster("*IMD*.tif")

    # ---------------- [A] metadata side by side ----------------
    section("[A] Raster metadata (TCD vs IMD)")
    for label, path in (("TCD", tcd_path), ("IMD", imd_path)):
        with rasterio.open(path) as src:
            print(f"{label}: {path.name}")
            print(f"    CRS       : {src.crs}")
            print(f"    bounds    : {tuple(round(b, 1) for b in src.bounds)}")
            print(f"    size      : {src.width} x {src.height} px")
            print(f"    resolution: {src.res}")
            print(f"    nodata    : {src.nodata}")
            print(f"    dtype     : {src.dtypes[0]}")

    # ---------------- [B] global TCD histogram ----------------
    section("[B] Global TCD histogram")
    with rasterio.open(tcd_path) as src:
        band = src.read(1)
    valid = band[band <= VALID_MAX]
    nonzero = valid[valid > 0]
    print(f"total px   : {band.size}")
    print(f"valid px   : {valid.size} ({valid.size / band.size:.1%} of total)")
    print(f"nonzero px : {nonzero.size} ({(nonzero.size / valid.size if valid.size else 0):.1%} of valid)")
    if valid.size:
        print(f"mean (valid): {float(valid.mean()) / 100.0:.4f}")
    if nonzero.size:
        print(f"mean (nonzero only): {float(nonzero.mean()) / 100.0:.4f}")
        deciles = np.percentile(nonzero, [10, 50, 90])
        print(f"nonzero deciles 10/50/90: {deciles[0]:.0f}% / {deciles[1]:.0f}% / {deciles[2]:.0f}%")
    else:
        print(">>> RASTER CONTAINS NO NONZERO CANOPY AT ALL — broken clip or "
              "wrong band; stop here, re-download / re-clip the TCD product.")

    # ---------------- [C] zone bboxes inside raster bounds? ----------------
    section("[C] Zone bboxes reprojected into raster CRS vs raster bounds")
    with rasterio.open(tcd_path) as src:
        rb = src.bounds
        print(f"TCD bounds: left={rb.left:.0f} bottom={rb.bottom:.0f} "
              f"right={rb.right:.0f} top={rb.top:.0f}  ({src.crs})")
        for zid, bbox in ZONE_BBOXES.items():
            geom = transform_geom("EPSG:25832", src.crs, mapping(box(*bbox)))
            xs = [pt[0] for ring in geom["coordinates"] for pt in ring]
            ys = [pt[1] for ring in geom["coordinates"] for pt in ring]
            inside = (min(xs) >= rb.left and max(xs) <= rb.right
                      and min(ys) >= rb.bottom and max(ys) <= rb.top)
            print(f"  {zid}: x [{min(xs):.0f}..{max(xs):.0f}] "
                  f"y [{min(ys):.0f}..{max(ys):.0f}]  inside={inside}"
                  f"{'' if inside else '  <-- OUTSIDE RASTER'}")

    # ---------------- [D] zone-level stored vs fresh ----------------
    section("[D] Zone-level TCD: stored in graph vs freshly recomputed")
    print("Loading graph ...")
    g = Graph()
    g.parse(str(TTL_FILE), format="turtle")
    print(f"  {len(g)} triples")
    print(f"  {'Zone':<16} {'stored':>8} {'fresh':>8} {'valid_px':>9} {'nonzero':>8}")
    for zid, bbox in ZONE_BBOXES.items():
        stored = g.value(EX[zid], UHI.hasTreeCanopyCoverage)
        stored_txt = f"{float(stored):.4f}" if stored is not None else "n/a"
        fresh, n_valid, n_nonzero = mean_fraction(tcd_path, bbox)
        print(f"  {zid:<16} {stored_txt:>8} {fresh:>8.4f} {n_valid:>9d} {n_nonzero:>8d}")

    # ---------------- [E] 10x10 per-cell ASCII maps ----------------
    section("[E] Per-cell TCD maps (rows = north to south, cols = west to east)")
    print("Legend: '.' = 0   digits = canopy in tens of percent (3 = 30-39%)\n"
          "Compare against your Folium map / aerial: does the green land where\n"
          "the parks are, or is it shifted / absent?")
    for zid, (xmin, ymin, xmax, ymax) in ZONE_BBOXES.items():
        print(f"\n  {zid}  (top row = gj=09 = north)")
        header = "      " + " ".join(f"{gi:02d}" for gi in range(10))
        print(header)
        for gj in range(9, -1, -1):
            row_chars = []
            for gi in range(10):
                cx0 = xmin + gi * CELL_SIZE
                cy0 = ymin + gj * CELL_SIZE
                frac, n_valid, _ = mean_fraction(
                    tcd_path, (cx0, cy0, cx0 + CELL_SIZE, cy0 + CELL_SIZE)
                )
                if n_valid == 0 or np.isnan(frac):
                    row_chars.append(" x")
                elif frac == 0.0:
                    row_chars.append(" .")
                else:
                    row_chars.append(f" {min(9, int(frac * 10))}")
            print(f"  gj{gj:02d}" + "".join(row_chars))
    print("\n  'x' = no valid pixels in cell (nodata / outside clip)")

    # ---------------- verdict guide ----------------
    section("Interpretation")
    print(
        "[B] no nonzero anywhere            -> broken TCD clip; re-download.\n"
        "[C] any zone outside raster bounds -> clip does not cover the AOI;\n"
        "                                      the mask silently returns only\n"
        "                                      partial/no pixels.\n"
        "[D] stored != fresh                -> graph predates raster; rerun\n"
        "                                      pipeline before trusting cells.\n"
        "[D] fresh nonzero but [E] shows the green offset from real parks\n"
        "                                   -> georeferencing/CRS bug in the\n"
        "                                      bbox -> raster path.\n"
        "[E] green exactly on the parks, '.' on Schlossplatz Nord\n"
        "                                   -> sampling is correct; TCD truly\n"
        "                                      does not map those tree stands;\n"
        "                                      write the underdetection caveat."
    )


if __name__ == "__main__":
    main()
    