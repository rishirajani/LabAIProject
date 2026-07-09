"""Compute per-zone tree canopy coverage and impervious surface fraction from
Copernicus Land Monitoring Service High Resolution Layers, and write them
back onto each AnalysisZone in the knowledge graph.

Inputs
------
clms_landcover/CLMS_HRLVLCC_TCD_DE111.tif
    HRL Tree Cover Density 2023, 10 m raster, clipped to NUTS-3 DE111
    (Stuttgart, Stadtkreis). Values 0..100 = % canopy cover per pixel.
    European Union, Copernicus Land Monitoring Service (2023). Available at
    https://land.copernicus.eu/en/products/high-resolution-layer-forests-and-tree-cover

clms_landcover/CLMS_NVLCC_IMD_S2024_R10m_E42N28_03035_V01_R01.tif
    Non-Vegetated Land Cover Characteristics - Imperviousness Density 2024,
    10 m raster, EEA tile E42N28 (covers Stuttgart). Values 0..100 = % sealed
    surface per pixel.
    European Union, Copernicus Land Monitoring Service (2024). Available at
    https://land.copernicus.eu/en/products/high-resolution-layer-imperviousness

Method
------
For each of the four 1 km x 1 km analysis zones in Stuttgart-Mitte, the UTM32
zone polygon is reprojected to EPSG:3035 (ETRS89 / LAEA Europe, the native
CRS of both rasters), and the rasters are masked to that polygon. The mean
of valid pixels (values 0..100) is converted to a fraction in [0, 1] and
written to:
    uhi:hasTreeCanopyCoverage           - from CLMS HRL TCD 2023
    uhi:hasImperviousSurfaceFraction    - from CLMS HRL IMD 2024 (replaces
                                          the previous hardcoded 0.80 fallback)

The OSM-derived uhi:hasVegetationFraction is left in place: it measures
explicit OSM land-use polygons (parks, forests, grass, scrub) and answers a
different question than CLMS TCD, which is satellite-derived tree canopy.

License
-------
This publication has been prepared using European Union's Copernicus Land
Monitoring Service information. Copernicus CLMS data are free, full, open
under the Copernicus license; attribution is required but no fee.

References
----------
- Copernicus Land Monitoring Service (2023). Product User Manual - High
  Resolution Layer Tree Cover and Forests 2018-present. EEA.
- Cecilia, A., Casasanta, G., Petenko, I., Conidi, A., & Argentini, S.
  (2022). Measuring the urban heat island of Rome through a dense weather
  station network and imperviousness Copernicus Land Monitoring Service
  data. 17th Plinius Conference, Plinius17-52.
  https://doi.org/10.5194/egusphere-plinius17-52
- Polrolniczak, M., Kolendowicz, L., & Tomczyk, A. M. (2024). Urban growth's
  implications on land surface temperature based on LCZ classification.
  Scientific Reports. (Quantifies LST/IMD relationship at ~0.14 K per 10 %
  increase in IMD.)
"""

from __future__ import annotations

from pathlib import Path

import rasterio
from rasterio.mask import mask
from rasterio.warp import transform_geom
from shapely.geometry import box, mapping
from rdflib import Graph, Literal
from rdflib.namespace import XSD

from namespaces import UHI, EX, bind_all

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "clms_landcover"
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

# Raster identification by filename pattern (the CLMS portal naming is stable).
TCD_GLOB = "*TCD*.tif"
IMD_GLOB = "*IMD*.tif"

# Per-zone tile boxes in EPSG:25832 (matches citygml_to_rdf.py and terrain_dgm.py).
ZONES = {
    EX.Zone_513_5402: (513000, 5402000, 514000, 5403000),
    EX.Zone_513_5403: (513000, 5403000, 514000, 5404000),
    EX.Zone_514_5402: (514000, 5402000, 515000, 5403000),
    EX.Zone_514_5403: (514000, 5403000, 515000, 5404000),
}

# CLMS HRL convention: values 0..100 = valid percentage; > 100 = nodata/outside.
VALID_MAX = 100


def find_raster(pattern: str) -> Path:
    """Return the single raster matching the pattern in DATA_DIR; raise if not found or ambiguous."""
    matches = sorted(DATA_DIR.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No raster matching {pattern!r} in {DATA_DIR}")
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple rasters match {pattern!r} in {DATA_DIR}: {[p.name for p in matches]}. "
            "Keep only the one for the desired reference year."
        )
    return matches[0]


def zone_mean_fraction(raster_path: Path, utm32_bbox: tuple[float, float, float, float]) -> tuple[float, int]:
    """Mean of valid CLMS pixels (0..100) inside the UTM32 bbox, returned as a fraction in [0, 1]."""
    geom_utm = mapping(box(*utm32_bbox))
    with rasterio.open(raster_path) as src:
        geom_raster_crs = transform_geom("EPSG:25832", src.crs, geom_utm)
        arr, _ = mask(src, [geom_raster_crs], crop=True, filled=False)
        band = arr[0]
        # Valid = inside polygon mask AND value <= 100 (filter out nodata sentinels).
        valid = (~band.mask) & (band.data <= VALID_MAX)
        vals = band.data[valid]
        if vals.size == 0:
            raise RuntimeError(f"No valid pixels for {raster_path.name} inside {utm32_bbox}")
        return float(vals.mean()) / 100.0, int(vals.size)


def main() -> None:
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"CLMS data directory not found: {DATA_DIR}")
    if not TTL_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {TTL_FILE}. Run citygml_to_rdf.py first."
        )

    tcd_path = find_raster(TCD_GLOB)
    imd_path = find_raster(IMD_GLOB)
    print(f"TCD raster : {tcd_path.name}")
    print(f"IMD raster : {imd_path.name}")

    print("\nLoading graph ...")
    g = Graph()
    bind_all(g)
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    print(f"\nPer-zone CLMS HRL values:")
    print(f"  {'Zone':<18} {'TCD':>8} {'TCD_px':>8} {'IMD':>8} {'IMD_px':>8}")

    for zone_uri, bbox in ZONES.items():
        tcd_frac, tcd_n = zone_mean_fraction(tcd_path, bbox)
        imd_frac, imd_n = zone_mean_fraction(imd_path, bbox)

        # set() so re-running replaces prior values instead of appending duplicates.
        g.set((zone_uri, UHI.hasTreeCanopyCoverage,
               Literal(round(tcd_frac, 4), datatype=XSD.decimal)))
        g.set((zone_uri, UHI.hasImperviousSurfaceFraction,
               Literal(round(imd_frac, 4), datatype=XSD.decimal)))

        zone_id = str(zone_uri).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        print(f"  {zone_id:<18} {tcd_frac:>8.4f} {tcd_n:>8d} {imd_frac:>8.4f} {imd_n:>8d}")

    g.serialize(destination=str(TTL_FILE), format="turtle")
    print(f"\nTriples added : {len(g) - triples_before}")
    print(f"Triples total : {len(g)}")
    print(
        "\nAttribution: This publication has been prepared using European Union's "
        "Copernicus Land Monitoring Service information."
    )


if __name__ == "__main__":
    main()
    