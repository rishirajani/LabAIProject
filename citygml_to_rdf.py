import xml.etree.ElementTree as ET
from pathlib import Path

from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, XSD

from namespaces import UHI, BOT, GEO, ALKIS, EX, bind_all


# Repository-local paths. Keep the GML folder beside this script.
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "LoD2_32_513_5402_2_bw"
ONTOLOGY_FILE = BASE_DIR / "uhi_ontology.ttl"
OUT_FILE = BASE_DIR / "stuttgart_buildings.ttl"

NS = {
    "core": "http://www.opengis.net/citygml/1.0",
    "bldg": "http://www.opengis.net/citygml/building/1.0",
    "gml":  "http://www.opengis.net/gml",
    "gen":  "http://www.opengis.net/citygml/generics/1.0",
}

ROOF_MAP = {
    "1000": ALKIS.Flachdach,
    "2100": ALKIS.Pultdach,
    "2200": ALKIS.Satteldach,
    "2300": ALKIS.Walmdach,
    "2400": ALKIS.Krueppelwalmdach,
    "3100": ALKIS.Tonnendach,
    "3200": ALKIS.Kuppeldach,
    "3500": ALKIS.Mansarddach,
    "4000": ALKIS.Zeltdach,
    "5000": ALKIS.Mischform,
    "9999": ALKIS.UnbekannterDachtyp,
}

ZONE_MAP = {
    "LoD2_32_513_5402_1_BW": (EX.Zone_513_5402, "Stuttgart tile 513/5402 (SW)"),
    "LoD2_32_513_5403_1_BW": (EX.Zone_513_5403, "Stuttgart tile 513/5403 (NW)"),
    "LoD2_32_514_5402_1_BW": (EX.Zone_514_5402, "Stuttgart tile 514/5402 (SE)"),
    "LoD2_32_514_5403_1_BW": (EX.Zone_514_5403, "Stuttgart tile 514/5403 (NE)"),
}


def alkis_function_uri(code: str) -> URIRef:
    if not code:
        return ALKIS.UnbekannteFunktion
    prefix = code[:5]
    if prefix == "31001":
        return ALKIS.Wohngebaeude
    if prefix.startswith("51"):
        return ALKIS.GewerblichesGebaeude
    if prefix.startswith("36") or prefix.startswith("35"):
        return ALKIS.OeffentlichesGebaeude
    return ALKIS.UnbekannteFunktion


def shoelace_area(coords: list[tuple[float, float]]) -> float:
    """Return polygon area in square metres.

    Assumes coordinates are projected EPSG:25832 / UTM metres.
    Do not use this function for WGS84 longitude/latitude coordinates.
    """
    n = len(coords)
    if n < 3:
        return 0.0
    area = sum(
        coords[i][0] * coords[(i + 1) % n][1] -
        coords[(i + 1) % n][0] * coords[i][1]
        for i in range(n)
    )
    return abs(area) / 2.0


def parse_pos_list(text: str) -> tuple[list[tuple[float, float]], tuple[float, float]]:
    """Parse a gml:posList (X Y Z triples) into 2-D coords and their centroid."""
    vals = list(map(float, text.split()))
    coords = [(vals[i], vals[i + 1]) for i in range(0, len(vals) - 2, 3)]
    if not coords:
        return coords, (0.0, 0.0)
    cx = sum(c[0] for c in coords) / len(coords)
    cy = sum(c[1] for c in coords) / len(coords)
    return coords, (cx, cy)


def extract_ground_surface(building_el) -> tuple[float, float, float]:
    """Return (footprint_area_m2, centroid_x, centroid_y) from GroundSurface polygons."""
    total_area = 0.0
    sum_cx, sum_cy, n_polys = 0.0, 0.0, 0

    for gs in building_el.findall(".//bldg:GroundSurface", NS):
        for pos_el in gs.findall(".//gml:posList", NS):
            if pos_el.text:
                coords, (cx, cy) = parse_pos_list(pos_el.text)
                total_area += shoelace_area(coords)
                sum_cx += cx
                sum_cy += cy
                n_polys += 1

    if n_polys == 0:
        return 0.0, 0.0, 0.0
    return total_area, sum_cx / n_polys, sum_cy / n_polys


def convert_tile(path: Path, g: Graph, stats: dict) -> None:
    print(f"  Parsing {path.name} ...")
    root = ET.parse(path).getroot()
    if path.stem not in ZONE_MAP:
        print(f"  Skipping unknown tile: {path.name}")
        return

    zone_uri, zone_label = ZONE_MAP[path.stem]

    g.add((zone_uri, RDF.type, UHI.LoD2Tile))
    g.add((zone_uri, RDF.type, UHI.AnalysisZone))
    g.add((zone_uri, RDF.type, BOT.Zone))
    g.add((zone_uri, RDFS.label, Literal(zone_label, lang="en")))

    for bldg_el in root.findall(".//bldg:Building", NS):
        gml_id = bldg_el.get("{http://www.opengis.net/gml}id", "")
        if not gml_id:
            continue

        height_el = bldg_el.find("bldg:measuredHeight", NS)
        roof_el   = bldg_el.find("bldg:roofType", NS)
        func_el   = bldg_el.find("bldg:function", NS)

        height_val = float(height_el.text) if height_el is not None and height_el.text else None
        roof_code  = roof_el.text.strip() if roof_el is not None and roof_el.text else None
        func_code  = func_el.text.strip() if func_el is not None and func_el.text else None

        # 832 buildings in this dataset have neither height nor roof type — skip them
        if height_val is None or roof_code is None:
            stats["skipped_incomplete"] += 1
            continue

        footprint_m2, cx, cy = extract_ground_surface(bldg_el)
        if footprint_m2 == 0.0:
            stats["skipped_no_geom"] += 1
            continue

        safe_id  = gml_id.replace(":", "_").replace("/", "_")
        bldg_uri = EX[safe_id]
        geom_uri = EX[safe_id + "_geom"]

        g.add((bldg_uri, RDF.type,           BOT.Building))
        g.add((bldg_uri, RDF.type,           GEO.Feature))
        g.add((bldg_uri, UHI.hasAlkisId,        Literal(gml_id, datatype=XSD.string)))
        g.add((bldg_uri, UHI.hasMeasuredHeight, Literal(round(height_val, 3), datatype=XSD.decimal)))
        g.add((bldg_uri, UHI.hasFootprintArea,  Literal(round(footprint_m2, 2), datatype=XSD.decimal)))
        g.add((bldg_uri, UHI.inAnalysisZone,    zone_uri))

        # CRS annotation required for GeoSPARQL spatial queries
        wkt = f"<http://www.opengis.net/def/crs/EPSG/0/25832> POINT({cx:.3f} {cy:.3f})"
        g.add((bldg_uri, GEO.hasGeometry, geom_uri))
        g.add((geom_uri, RDF.type,         GEO.Geometry))
        g.add((geom_uri, GEO.asWKT,        Literal(wkt, datatype=GEO.wktLiteral)))

        roof_uri = ROOF_MAP.get(roof_code, ALKIS.UnbekannterDachtyp)
        g.add((bldg_uri, UHI.hasRoofType, roof_uri))
        stats["roof_types"][roof_code] = stats["roof_types"].get(roof_code, 0) + 1

        g.add((bldg_uri, UHI.hasBuildingFunction, alkis_function_uri(func_code or "")))

        stats["total_converted"] += 1


def build_graph() -> Graph:
    g = Graph()
    bind_all(g)

    g.parse(str(ONTOLOGY_FILE), format="turtle")
    print(f"  Ontology loaded: {len(g)} triples")

    return g


def main():
    if not ONTOLOGY_FILE.exists():
        raise FileNotFoundError(f"Ontology file not found: {ONTOLOGY_FILE}")
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"CityGML data directory not found: {DATA_DIR}")

    TILES = sorted(DATA_DIR.glob("*.gml"))
    if not TILES:
        raise FileNotFoundError(f"No .gml files found in {DATA_DIR}")

    stats = {
        "total_converted":    0,
        "skipped_incomplete": 0,
        "skipped_no_geom":    0,
        "roof_types":         {},
    }

    print("Building RDF graph ...")
    g = build_graph()

    print(f"\nConverting {len(TILES)} tile(s) ...")
    for tile in TILES:
        convert_tile(tile, g, stats)

    print(f"\nSerialising to {OUT_FILE.name} ...")
    g.serialize(destination=str(OUT_FILE), format="turtle")

    print(f"\nBuildings converted  : {stats['total_converted']}")
    print(f"Skipped (incomplete) : {stats['skipped_incomplete']}")
    print(f"Skipped (no geometry): {stats['skipped_no_geom']}")
    print(f"Total triples        : {len(g)}")
    print(f"Building geometry and attributes exported for score-based assessment.")


if __name__ == "__main__":
    main()
