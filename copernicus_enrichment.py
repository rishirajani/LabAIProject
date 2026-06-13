"""
copernicus_enrichment.py
Enriches RDF zones with per-zone imperviousness and tree-cover fractions
derived from Copernicus HRL rasters at 10 m resolution.

Data sources
------------
IMD  – Imperviousness Degree 2024, 10 m, EPSG:3035
       Imperviousness/CLMS_NVLCC_IMD_S2024_R10m_E42N28_03035_V01_R01.zip
TCD  – Tree Cover Density 2023, 10 m, EPSG:3035
       TreeCover/Tree Cover Density 2018-present (raster 10 m), Europe, yearly/
       20230101/CLMS_HRLVLCC_TCD_DE111.tif

Replaces (via g.set)
--------------------
uhi:hasImperviousSurfaceFraction  – was a constant 0.80 for all zones
uhi:hasVegetationFraction         – was an OSM polygon-area estimate

OSM enrichment still runs first and provides tree count / dominant type;
this script overwrites only the two fraction values with satellite measurements.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import from_bounds
from rdflib import Graph, Literal
from rdflib.namespace import XSD

from namespaces import UHI, EX, bind_all

BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

IMD_ZIP = BASE_DIR / "Imperviousness" / "CLMS_NVLCC_IMD_S2024_R10m_E42N28_03035_V01_R01.zip"
TCD_TIF = (BASE_DIR / "TreeCover"
           / "Tree Cover Density 2018-present (raster 10 m), Europe, yearly"
           / "20230101" / "CLMS_HRLVLCC_TCD_DE111.tif")

# Zone bounding boxes in EPSG:25832 (UTM32N): (easting_min, northing_min, easting_max, northing_max)
ZONE_BOUNDS_UTM = {
    EX.Zone_513_5402: (513000, 5402000, 514000, 5403000),
    EX.Zone_513_5403: (513000, 5403000, 514000, 5404000),
    EX.Zone_514_5402: (514000, 5402000, 515000, 5403000),
    EX.Zone_514_5403: (514000, 5403000, 515000, 5404000),
}

# Both rasters are EPSG:3035 (LAEA Europe)
_tf = Transformer.from_crs("EPSG:25832", "EPSG:3035", always_xy=True)


def _utm_to_laea_bbox(e_min: float, n_min: float, e_max: float, n_max: float) -> tuple[float, float, float, float]:
    """Transform a UTM32N bbox to a conservative LAEA bounding box."""
    corners = [
        _tf.transform(e_min, n_min),
        _tf.transform(e_max, n_min),
        _tf.transform(e_max, n_max),
        _tf.transform(e_min, n_max),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _zone_mean(ds: rasterio.DatasetReader, laea_bbox: tuple) -> float:
    """Read the raster window for a zone and return mean value (0–100 scale)."""
    x_min, y_min, x_max, y_max = laea_bbox
    win = from_bounds(x_min, y_min, x_max, y_max, ds.transform)
    arr = ds.read(1, window=win)
    return float(arr.mean())


def load_imd_dataset() -> tuple[rasterio.DatasetReader, bytes]:
    """Extract the IMD tif from the zip and return an open rasterio dataset."""
    with zipfile.ZipFile(IMD_ZIP) as z:
        tif_name = next(n for n in z.namelist() if n.endswith(".tif"))
        data = z.read(tif_name)
    return data


def main() -> None:
    for path, label in [(IMD_ZIP, "IMD zip"), (TCD_TIF, "TCD tif")]:
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")

    print("Loading RDF graph ...")
    g = Graph()
    bind_all(g)
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    # Pre-compute LAEA bounding boxes for each zone
    laea_bboxes = {
        zone: _utm_to_laea_bbox(*bounds)
        for zone, bounds in ZONE_BOUNDS_UTM.items()
    }

    print("\nSampling Copernicus rasters ...")
    results: dict = {}

    imd_bytes = load_imd_dataset()
    with rasterio.open(io.BytesIO(imd_bytes)) as imd_ds:
        with rasterio.open(str(TCD_TIF)) as tcd_ds:
            for zone_uri, bbox in laea_bboxes.items():
                zone_name = str(zone_uri).rsplit("/", 1)[-1]

                imd_mean = _zone_mean(imd_ds, bbox)
                tcd_mean = _zone_mean(tcd_ds, bbox)

                imp_frac = round(imd_mean / 100.0, 4)
                veg_frac = round(tcd_mean / 100.0, 4)

                results[zone_name] = {"imp": imp_frac, "veg": veg_frac}

                # Read existing OSM vegetation fraction for comparison
                old_veg = g.value(zone_uri, UHI.hasVegetationFraction)
                old_veg_str = f"{float(old_veg):.3f}" if old_veg else "none"

                g.set((zone_uri, UHI.hasImperviousSurfaceFraction,
                       Literal(imp_frac, datatype=XSD.decimal)))
                g.set((zone_uri, UHI.hasVegetationFraction,
                       Literal(veg_frac, datatype=XSD.decimal)))
                g.set((zone_uri, UHI.vegetationDataSource,
                       Literal("Copernicus HRL TCD 2023, 10 m", datatype=XSD.string)))
                g.set((zone_uri, UHI.imperviousnessDataSource,
                       Literal("Copernicus HRL IMD 2024, 10 m", datatype=XSD.string)))

                print(f"  {zone_name:<20}  "
                      f"imperviousness={imp_frac:.3f}  "
                      f"veg_fraction={veg_frac:.3f}  "
                      f"(OSM veg was {old_veg_str})")

    g.serialize(destination=str(TTL_FILE), format="turtle")
    print(f"\nTriples added : {len(g) - triples_before}")
    print(f"Triples total : {len(g)}")


if __name__ == "__main__":
    main()
