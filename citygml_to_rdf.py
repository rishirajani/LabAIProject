import xml.etree.ElementTree as ET
from pathlib import Path

from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, XSD

from namespaces import UHI, BOT, GEO, ALKIS, EX, bind_all

from shapely.geometry import Polygon
from shapely.ops import unary_union


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


def parse_pos_list(
    text: str,
    srs_dimension: int | None = None,
    default_dimension: int = 3,
) -> list[tuple[float, float]]:
    """Parse a gml:posList into 2D projected coordinates.

    Z and any additional dimensions are discarded. CityGML LoD2 geometry is
    normally three-dimensional, so dimension 3 is used when srsDimension is
    absent.
    """
    try:
        values = [float(value) for value in text.split()]
    except ValueError:
        return []

    dimension = srs_dimension or default_dimension

    if dimension < 2 or len(values) % dimension != 0:
        return []

    return [
        (values[index], values[index + 1])
        for index in range(0, len(values), dimension)
    ]


def normalize_ring(
    coordinates: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Remove consecutive duplicates and ensure that a ring is closed."""
    cleaned: list[tuple[float, float]] = []

    for coordinate in coordinates:
        if not cleaned or coordinate != cleaned[-1]:
            cleaned.append(coordinate)

    if len(set(cleaned)) < 3:
        return []

    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])

    return cleaned


def extract_ground_surface(
    building_el,
) -> tuple[float, float, float, float, float]:
    """Extract a merged GroundSurface footprint.

    Returns:
        (
            footprint_area_m2,
            centroid_x,
            centroid_y,
            representative_x,
            representative_y,
        )

    The centroid is the mathematical centroid of the merged footprint.
    The representative point is guaranteed to lie inside the footprint and is
    therefore better suited to map markers and spatial cell assignment.
    """
    polygons: list[Polygon] = []

    for ground_surface in building_el.findall(
        ".//bldg:GroundSurface",
        NS,
    ):
        for polygon_el in ground_surface.findall(
            ".//gml:Polygon",
            NS,
        ):
            exterior_el = polygon_el.find(
                "gml:exterior/gml:LinearRing/gml:posList",
                NS,
            )

            if exterior_el is None or not exterior_el.text:
                continue

            dimension_text = exterior_el.get("srsDimension")

            try:
                dimension = (
                    int(dimension_text)
                    if dimension_text is not None
                    else None
                )
            except ValueError:
                dimension = None

            shell = normalize_ring(
                parse_pos_list(
                    exterior_el.text,
                    srs_dimension=dimension,
                )
            )

            if not shell:
                continue

            holes: list[list[tuple[float, float]]] = []

            for interior_el in polygon_el.findall(
                "gml:interior/gml:LinearRing/gml:posList",
                NS,
            ):
                if not interior_el.text:
                    continue

                interior_dimension_text = interior_el.get(
                    "srsDimension"
                )

                try:
                    interior_dimension = (
                        int(interior_dimension_text)
                        if interior_dimension_text is not None
                        else dimension
                    )
                except ValueError:
                    interior_dimension = dimension

                hole = normalize_ring(
                    parse_pos_list(
                        interior_el.text,
                        srs_dimension=interior_dimension,
                    )
                )

                if hole:
                    holes.append(hole)

            try:
                polygon = Polygon(
                    shell,
                    holes or None,
                )
            except Exception:
                continue

            if not polygon.is_valid:
                polygon = polygon.buffer(0)

            if polygon.is_empty:
                continue

            if polygon.geom_type == "Polygon":
                polygons.append(polygon)

            elif polygon.geom_type == "MultiPolygon":
                polygons.extend(
                    part
                    for part in polygon.geoms
                    if not part.is_empty
                )

    if not polygons:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    merged = unary_union(polygons)

    if not merged.is_valid:
        merged = merged.buffer(0)

    if merged.is_empty or merged.geom_type not in {
        "Polygon",
        "MultiPolygon",
    }:
        return 0.0, 0.0, 0.0, 0.0, 0.0

    centroid = merged.centroid
    representative = merged.representative_point()

    return (
        float(merged.area),
        float(centroid.x),
        float(centroid.y),
        float(representative.x),
        float(representative.y),
    )


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

        (
            footprint_m2,
            centroid_x,
            centroid_y,
            representative_x,
            representative_y,
        ) = extract_ground_surface(bldg_el)
        
        if footprint_m2 == 0.0:
            stats["skipped_no_geom"] += 1
            continue

        centroid_offset = (
            (centroid_x - representative_x) ** 2
            + (centroid_y - representative_y) ** 2
        ) ** 0.5

        if centroid_offset > 1.0:
            stats["representative_differs"] += 1

        safe_id  = gml_id.replace(":", "_").replace("/", "_")
        bldg_uri = EX[safe_id]
        geom_uri = EX[safe_id + "_geom"]

        g.add((bldg_uri, RDF.type,           BOT.Building))
        g.add((bldg_uri, RDF.type,           GEO.Feature))
        g.add((bldg_uri, UHI.hasAlkisId,        Literal(gml_id, datatype=XSD.string)))
        g.add((
            bldg_uri,
            UHI.sourceGmlId,
            Literal(gml_id, datatype=XSD.string),
        ))
        g.add((bldg_uri, UHI.hasMeasuredHeight, Literal(round(height_val, 3), datatype=XSD.decimal)))
        g.add((bldg_uri, UHI.hasFootprintArea,  Literal(round(footprint_m2, 2), datatype=XSD.decimal)))
        g.add((bldg_uri, UHI.inAnalysisZone,    zone_uri))

        # CRS annotation required for GeoSPARQL spatial queries
        crs_prefix = (
            "<http://www.opengis.net/def/crs/EPSG/0/25832> "
        )

        centroid_wkt = (
            f"{crs_prefix}"
            f"POINT({centroid_x:.3f} {centroid_y:.3f})"
        )

        g.add((bldg_uri, UHI.hasCentroidGeometry, geom_uri))
        g.add((geom_uri, RDF.type, GEO.Geometry))
        g.add((
            geom_uri,
            GEO.asWKT,
            Literal(
                centroid_wkt,
                datatype=GEO.wktLiteral,
            ),
        ))

        representative_geom_uri = EX[
            safe_id + "_representative_point"
        ]

        representative_wkt = (
            f"{crs_prefix}"
            f"POINT({representative_x:.3f} {representative_y:.3f})"
        )

        g.add((
            bldg_uri,
            UHI.hasRepresentativePointGeometry,
            representative_geom_uri,
        ))
        g.add((
            representative_geom_uri,
            RDF.type,
            GEO.Geometry,
        ))
        g.add((
            representative_geom_uri,
            GEO.asWKT,
            Literal(
                representative_wkt,
                datatype=GEO.wktLiteral,
            ),
        ))

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
        "total_converted":          0,
        "skipped_incomplete":       0,
        "skipped_no_geom":          0,
        "representative_differs":   0,
        "roof_types":               {},
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
    print(
        "Centroid/representative >1 m apart: "
        f"{stats['representative_differs']}"
    )
    print(f"Total triples        : {len(g)}")
    print(f"Building geometry and attributes exported for score-based assessment.")


if __name__ == "__main__":
    main()
