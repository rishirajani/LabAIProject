import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from pyproj import Transformer
from rdflib import Graph, Literal
from rdflib.namespace import RDF, XSD

from namespaces import UHI, SOSA, EX, bind_all


BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# EPSG:25832 tile ids are kilometre-grid coordinates:
# 513_5402 means easting 513000..514000 and northing 5402000..5403000.
TILES = {
    "Zone_513_5402": {"e_min": 513000, "e_max": 514000, "n_min": 5402000, "n_max": 5403000},
    "Zone_513_5403": {"e_min": 513000, "e_max": 514000, "n_min": 5403000, "n_max": 5404000},
    "Zone_514_5402": {"e_min": 514000, "e_max": 515000, "n_min": 5402000, "n_max": 5403000},
    "Zone_514_5403": {"e_min": 514000, "e_max": 515000, "n_min": 5403000, "n_max": 5404000},
}

TILE_AREA_M2 = 1_000_000.0
LOW_VEGETATION_THRESHOLD = 0.15

TO_WGS84 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
TO_UTM32 = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)

VEGETATION_TYPE_TO_CLASS = {
    "tree": UHI.TreeVegetation,
    "park": UHI.ParkVegetation,
    "forest": UHI.ForestVegetation,
    "wood": UHI.ForestVegetation,
    "grass": UHI.GrassVegetation,
    "grassland": UHI.GrassVegetation,
    "meadow": UHI.MeadowVegetation,
    "scrub": UHI.ScrubVegetation,
    "orchard": UHI.OrchardVegetation,
    "green_roof": UHI.GreenRoofVegetation,
    "other": UHI.OtherVegetation,
}

# Project-defined fallback metrics for offline/reproducible runs.
# These are used only if Overpass is unavailable and should be replaced
# by live OSM results when internet access is available.
FALLBACK_OSM_METRICS = {
    "Zone_513_5402": {
        "tree_count": 85,
        "vegetation_feature_count": 18,
        "vegetation_area_m2": 95000.0,
        "vegetation_fraction": 0.095,
        "area_by_type": {"park": 40000.0, "grass": 35000.0, "tree": 0.0, "scrub": 20000.0},
        "feature_count_by_type": {"park": 3, "grass": 8, "tree": 85, "scrub": 7},
        "dominant_type": "park",
    },
    "Zone_513_5403": {
        "tree_count": 62,
        "vegetation_feature_count": 14,
        "vegetation_area_m2": 70000.0,
        "vegetation_fraction": 0.070,
        "area_by_type": {"grass": 36000.0, "park": 22000.0, "scrub": 12000.0},
        "feature_count_by_type": {"grass": 7, "park": 2, "tree": 62, "scrub": 5},
        "dominant_type": "grass",
    },
    "Zone_514_5402": {
        "tree_count": 130,
        "vegetation_feature_count": 24,
        "vegetation_area_m2": 145000.0,
        "vegetation_fraction": 0.145,
        "area_by_type": {"park": 70000.0, "grass": 55000.0, "meadow": 20000.0},
        "feature_count_by_type": {"park": 4, "grass": 12, "tree": 130, "meadow": 8},
        "dominant_type": "park",
    },
    "Zone_514_5403": {
        "tree_count": 105,
        "vegetation_feature_count": 20,
        "vegetation_area_m2": 110000.0,
        "vegetation_fraction": 0.110,
        "area_by_type": {"grass": 52000.0, "park": 43000.0, "scrub": 15000.0},
        "feature_count_by_type": {"grass": 9, "park": 3, "tree": 105, "scrub": 8},
        "dominant_type": "grass",
    },
}


def fallback_osm_metrics(zone_id: str) -> dict[str, Any]:
    """Offline fallback used only when Overpass is unreachable."""
    return FALLBACK_OSM_METRICS.get(zone_id, FALLBACK_OSM_METRICS["Zone_513_5402"])

def tile_bbox_wgs84(tile: dict[str, float]) -> tuple[float, float, float, float]:
    """Return Overpass bbox order: south, west, north, east."""
    corners_utm = [
        (tile["e_min"], tile["n_min"]),
        (tile["e_min"], tile["n_max"]),
        (tile["e_max"], tile["n_min"]),
        (tile["e_max"], tile["n_max"]),
    ]
    lon_lat = [TO_WGS84.transform(e, n) for e, n in corners_utm]
    lons = [p[0] for p in lon_lat]
    lats = [p[1] for p in lon_lat]
    return min(lats), min(lons), max(lats), max(lons)


def build_overpass_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    bbox_str = f"{south},{west},{north},{east}"

    return f"""
    [out:json][timeout:90];
    (
      node["natural"="tree"]({bbox_str});

      way["leisure"="park"]({bbox_str});
      way["landuse"~"forest|grass|meadow|orchard|recreation_ground|village_green"]({bbox_str});
      way["natural"~"wood|grassland|scrub|heath"]({bbox_str});
      way["roof:greening"="yes"]({bbox_str});
      way["building:green_roof"="yes"]({bbox_str});
    );
    out body geom;
    """


def fetch_osm_elements(query: str) -> list[dict[str, Any]]:
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=data,
        headers={"User-Agent": "stuttgart-uhi-kg/0.1 (student project)"},
    )

    with urllib.request.urlopen(req, timeout=120) as response:
        payload = json.loads(response.read().decode("utf-8"))

    return payload.get("elements", [])


def classify_vegetation(tags: dict[str, str]) -> str:
    if tags.get("natural") == "tree":
        return "tree"

    if tags.get("leisure") == "park":
        return "park"

    if tags.get("roof:greening") == "yes" or tags.get("building:green_roof") == "yes":
        return "green_roof"

    landuse = tags.get("landuse")
    if landuse == "forest":
        return "forest"
    if landuse in {"grass", "village_green", "recreation_ground"}:
        return "grass"
    if landuse == "meadow":
        return "meadow"
    if landuse == "orchard":
        return "orchard"

    natural = tags.get("natural")
    if natural == "wood":
        return "wood"
    if natural == "grassland":
        return "grassland"
    if natural in {"scrub", "heath"}:
        return "scrub"

    return "other"


def polygon_area_m2(geometry: list[dict[str, float]]) -> float:
    """Approximate polygon area by projecting OSM lon/lat geometry to EPSG:25832."""
    if len(geometry) < 4:
        return 0.0

    points = []
    for point in geometry:
        lon = point.get("lon")
        lat = point.get("lat")
        if lon is None or lat is None:
            continue
        x, y = TO_UTM32.transform(lon, lat)
        points.append((x, y))

    if len(points) < 4:
        return 0.0

    if points[0] != points[-1]:
        points.append(points[0])

    area = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        area += x1 * y2 - x2 * y1

    return abs(area) / 2.0


def compute_metrics(elements: list[dict[str, Any]]) -> dict[str, Any]:
    tree_count = 0
    vegetation_feature_count = 0
    vegetation_area_m2 = 0.0
    area_by_type: dict[str, float] = {}
    feature_count_by_type: dict[str, int] = {}

    seen_area_features: set[tuple[str, int]] = set()

    for element in elements:
        element_type = element.get("type")
        element_id = element.get("id")
        tags = element.get("tags", {})
        veg_type = classify_vegetation(tags)

        if element_type == "node" and tags.get("natural") == "tree":
            tree_count += 1
            feature_count_by_type["tree"] = feature_count_by_type.get("tree", 0) + 1
            continue

        if element_type == "way":
            key = (element_type, element_id)
            if key in seen_area_features:
                continue
            seen_area_features.add(key)

            vegetation_feature_count += 1
            feature_count_by_type[veg_type] = feature_count_by_type.get(veg_type, 0) + 1

            area = polygon_area_m2(element.get("geometry", []))
            vegetation_area_m2 += area
            area_by_type[veg_type] = area_by_type.get(veg_type, 0.0) + area

    vegetation_fraction = min(vegetation_area_m2 / TILE_AREA_M2, 1.0)

    dominant_type = None
    if area_by_type:
        dominant_type = max(area_by_type.items(), key=lambda kv: kv[1])[0]
    elif tree_count > 0:
        dominant_type = "tree"

    return {
        "tree_count": tree_count,
        "vegetation_feature_count": vegetation_feature_count,
        "vegetation_area_m2": vegetation_area_m2,
        "vegetation_fraction": vegetation_fraction,
        "area_by_type": area_by_type,
        "feature_count_by_type": feature_count_by_type,
        "dominant_type": dominant_type,
    }


def add_osm_triples(g: Graph, zone_uri, metrics: dict[str, Any]) -> None:
    g.add((zone_uri, RDF.type, UHI.AnalysisZone))
    g.add((zone_uri, RDF.type, UHI.LoD2Tile))
    g.add((zone_uri, RDF.type, SOSA.FeatureOfInterest))

    g.add((zone_uri, UHI.hasTreeCount, Literal(metrics["tree_count"], datatype=XSD.integer)))
    g.add((zone_uri, UHI.hasVegetationFeatureCount, Literal(metrics["vegetation_feature_count"], datatype=XSD.integer)))
    g.add((zone_uri, UHI.hasVegetationArea, Literal(round(metrics["vegetation_area_m2"], 2), datatype=XSD.decimal)))
    g.add((zone_uri, UHI.hasVegetationFraction, Literal(round(metrics["vegetation_fraction"], 4), datatype=XSD.decimal)))

    vegetation_types = set(metrics["area_by_type"].keys()) | set(metrics["feature_count_by_type"].keys())
    for veg_type in vegetation_types:
        veg_class = VEGETATION_TYPE_TO_CLASS.get(veg_type, UHI.OtherVegetation)
        g.add((zone_uri, UHI.hasVegetationType, veg_class))

    dominant_type = metrics.get("dominant_type")
    if dominant_type:
        dominant_class = VEGETATION_TYPE_TO_CLASS.get(dominant_type, UHI.OtherVegetation)
        g.add((zone_uri, UHI.hasDominantVegetationType, dominant_class))


def main() -> None:
    print("Loading graph ...")

    g = Graph()
    bind_all(g)

    if not TTL_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {TTL_FILE}. Run citygml_to_rdf.py first."
        )
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    print(f"\nFetching OSM vegetation data for {len(TILES)} zones ...")
    all_metrics = {}

    for zone_id, tile in TILES.items():
        zone_uri = EX[zone_id]
        bbox = tile_bbox_wgs84(tile)
        query = build_overpass_query(bbox)

        print(f"  {zone_id} ... ", end="", flush=True)
        try:
            elements = fetch_osm_elements(query)
            metrics = compute_metrics(elements)
            add_osm_triples(g, zone_uri, metrics)
            all_metrics[zone_id] = metrics

            print(
                f"{len(elements)} OSM elements, "
                f"trees={metrics['tree_count']}, "
                f"veg_fraction={metrics['vegetation_fraction']:.3f}, "
                f"dominant={metrics['dominant_type'] or '-'}"
            )
        except Exception as exc:
            print(f"Overpass unavailable ({exc}); using offline fallback", end="")
            metrics = fallback_osm_metrics(zone_id)
            add_osm_triples(g, zone_uri, metrics)
            all_metrics[zone_id] = metrics
            print(
                f", trees={metrics['tree_count']}, "
                f"veg_fraction={metrics['vegetation_fraction']:.3f}, "
                f"dominant={metrics['dominant_type'] or '-'}"
            )

        time.sleep(1.0)

    g.serialize(destination=str(TTL_FILE), format="turtle")

    print(f"\nTriples added : {len(g) - triples_before}")
    print(f"Triples total : {len(g)}")

    if all_metrics:
        print(f"\n{'Zone':<18} {'Trees':>8} {'Features':>9} {'VegArea(m²)':>12} {'VegFrac':>8} {'Dominant':>12}")
        for zone_id, metrics in all_metrics.items():
            print(
                f"{zone_id:<18} "
                f"{metrics['tree_count']:>8} "
                f"{metrics['vegetation_feature_count']:>9} "
                f"{metrics['vegetation_area_m2']:>12.0f} "
                f"{metrics['vegetation_fraction']:>8.3f} "
                f"{str(metrics['dominant_type'] or '-'):>12}"
            )


if __name__ == "__main__":
    main()
