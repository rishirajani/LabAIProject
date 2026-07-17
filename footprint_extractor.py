#!/usr/bin/env python3
"""
footprint_extractor.py

Extract true 2D building-footprint polygons from LoD2 CityGML files and write
them to an RDF knowledge graph as GeoSPARQL WKT geometries.

Purpose
-------
The knowledge graph may already store each building using:

    - a centroid POINT geometry linked through uhi:hasCentroidGeometry;
    - a representative POINT linked through uhi:hasRepresentativePointGeometry;
    - a scalar footprint area.

Those values are useful for analysis, but they do not preserve the actual
building outline. This script extracts real polygons from CityGML LoD2
GroundSurface geometry.

Extraction priority
-------------------
For each building:

1. Merge all bldg:GroundSurface exterior rings.
2. If no valid ground surface exists, merge projected bldg:RoofSurface rings.
3. Drop Z coordinates and retain the resulting 2D Polygon or MultiPolygon.
4. Preserve interior rings such as courtyards.
5. Skip buildings for which no valid polygonal geometry can be created.

RDF model
---------
Each footprint is linked using:

    uhi:hasFootprintGeometry

The footprint node is represented as:

    geo:Geometry
    uhi:FootprintGeometry

and serialized using:

    geo:asWKT "...WKT..."^^geo:wktLiteral

The script does not replace the existing centroid geometry.

Geometry semantics
------------------
Footprint geometry is linked only through uhi:hasFootprintGeometry. It is
intentionally not declared as a subproperty of geo:hasGeometry because each
building can have several geometries with distinct roles:

    - uhi:hasCentroidGeometry
    - uhi:hasRepresentativePointGeometry
    - uhi:hasFootprintGeometry

Using explicit predicates prevents centroid, representative-point, and
footprint geometries from being mixed by generic or reasoning-enabled queries.

Idempotency
-----------
Before writing new footprints, the script removes all existing triples linked
through uhi:hasFootprintGeometry. Therefore, rerunning the script replaces the
previous extraction instead of accumulating stale footprint resources.

Safety
------
The updated graph is written to a temporary file and moved into place
atomically. An optional backup can also be created.

Examples
--------
Default execution:

    python footprint_extractor.py

Preview without modifying the Turtle file:

    python footprint_extractor.py --dry-run

Create a backup before updating:

    python footprint_extractor.py --backup

Use explicit paths:

    python footprint_extractor.py \
        --ttl stuttgart_buildings.ttl \
        --gml-dir ./citygml

Increase WKT coordinate precision:

    python footprint_extractor.py --precision 3

Requirements
------------
    rdflib
    shapely
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Literal as TypingLiteral

from rdflib import Graph, Literal, Namespace, RDF, URIRef
from rdflib.namespace import OWL
from shapely.geometry import (
    GeometryCollection,
    MultiPolygon,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from namespaces import EX, GEO, UHI, bind_all


LOGGER = logging.getLogger("footprint_extractor")

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"
DEFAULT_GML_DIR = BASE_DIR

BOT = Namespace("https://w3id.org/bot#")

CRS_URI = "http://www.opengis.net/def/crs/EPSG/0/25832"
CRS_PREFIX = f"<{CRS_URI}> "

DEFAULT_SRS_DIMENSION = 3
DEFAULT_MIN_AREA_M2 = 1.0
DEFAULT_WKT_PRECISION = 2
MIN_DISTINCT_RING_POINTS = 3

SurfaceKind = TypingLiteral["ground", "roof"]
PolygonalGeometry = Polygon | MultiPolygon


@dataclass
class ExtractionStats:
    """Diagnostics collected while parsing CityGML files."""

    buildings_seen: int = 0
    building_parts_seen: int = 0
    polygons_seen: int = 0
    polygons_created: int = 0
    polygons_rejected: int = 0
    geometry_errors: int = 0
    xlink_references_seen: int = 0
    poslists_seen: int = 0
    individual_positions_seen: int = 0
    repaired_geometries: int = 0
    repair_area_change_warnings: int = 0
    unsupported_geometry_results: int = 0
    error_samples: list[str] = field(default_factory=list)

    def add_error(self, message: str, sample_limit: int = 10) -> None:
        self.geometry_errors += 1
        if len(self.error_samples) < sample_limit:
            self.error_samples.append(message)


@dataclass
class TileExtraction:
    """Raw polygon parts extracted from one CityGML tile."""

    ground: dict[str, list[PolygonalGeometry]] = field(
        default_factory=lambda: defaultdict(list)
    )
    roof: dict[str, list[PolygonalGeometry]] = field(
        default_factory=lambda: defaultdict(list)
    )
    stats: ExtractionStats = field(default_factory=ExtractionStats)


@dataclass
class FootprintRecord:
    """Final footprint and extraction provenance for one building."""

    geometry: PolygonalGeometry
    source_kind: SurfaceKind
    source_files: set[str] = field(default_factory=set)
    repaired: bool = False


@dataclass
class BuildingContext:
    """Tracks the enclosing CityGML Building or BuildingPart."""

    tag: str
    own_id: str | None
    root_building_id: str | None


@dataclass
class MatchResult:
    """Result of matching a CityGML building ID to a graph resource."""

    uri: URIRef | None
    method: str | None
    ambiguous_candidates: tuple[URIRef, ...] = ()


def lname(tag: str) -> str:
    """Return the namespace-independent local name of an XML tag."""

    return tag.rsplit("}", 1)[-1]


def attribute_by_local_name(
    elem: ET.Element,
    target_name: str,
) -> str | None:
    """Return an XML attribute value using its namespace-independent name."""

    for key, value in elem.attrib.items():
        if lname(key) == target_name:
            return value
    return None


def parse_dimension(value: str | None) -> int | None:
    """Parse a positive coordinate dimension from XML metadata."""

    if value is None:
        return None

    try:
        dimension = int(value)
    except ValueError:
        return None

    return dimension if dimension >= 2 else None


def parse_numeric_values(text: str) -> list[float]:
    """Parse whitespace-separated numeric coordinate values."""

    try:
        return [float(value) for value in text.split()]
    except ValueError:
        return []


def parse_poslist(
    text: str,
    srs_dimension: int | None,
    default_dimension: int,
) -> list[tuple[float, float]]:
    """
    Parse a gml:posList into two-dimensional coordinate tuples.

    Z and higher dimensions are dropped. The configured default dimension is
    used when the element does not explicitly provide srsDimension.
    """

    values = parse_numeric_values(text)
    if not values:
        return []

    dimension = srs_dimension or default_dimension

    if dimension < 2:
        return []

    if len(values) % dimension != 0:
        return []

    return [
        (values[index], values[index + 1])
        for index in range(0, len(values), dimension)
    ]


def parse_pos(
    text: str,
    srs_dimension: int | None,
    default_dimension: int,
) -> tuple[float, float] | None:
    """
    Parse one gml:pos value.

    Extra dimensions such as Z are ignored.
    """

    values = parse_numeric_values(text)
    if len(values) < 2:
        return None

    dimension = srs_dimension or default_dimension

    if dimension < 2 or len(values) < dimension:
        return None

    return values[0], values[1]


def remove_consecutive_duplicates(
    points: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Remove adjacent duplicate coordinates while preserving order."""

    cleaned: list[tuple[float, float]] = []

    for point in points:
        if not cleaned or point != cleaned[-1]:
            cleaned.append(point)

    return cleaned


def normalize_ring(
    points: Iterable[tuple[float, float]],
) -> list[tuple[float, float]]:
    """
    Normalize a polygon ring.

    The function:

    - removes consecutive duplicates;
    - verifies that at least three distinct points remain;
    - closes the ring if necessary.
    """

    cleaned = remove_consecutive_duplicates(points)

    if len(set(cleaned)) < MIN_DISTINCT_RING_POINTS:
        return []

    if cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])

    if len(cleaned) < 4:
        return []

    return cleaned


def polygonal_only(
    geometry: BaseGeometry,
) -> PolygonalGeometry | None:
    """
    Return only the polygonal portion of a Shapely geometry.

    GeometryCollection results can occur after union or repair operations.
    Non-polygonal members are discarded.
    """

    if geometry.is_empty:
        return None

    if isinstance(geometry, Polygon):
        return geometry

    if isinstance(geometry, MultiPolygon):
        return geometry

    if isinstance(geometry, GeometryCollection):
        polygon_parts: list[BaseGeometry] = []

        for part in geometry.geoms:
            if isinstance(part, (Polygon, MultiPolygon)) and not part.is_empty:
                polygon_parts.append(part)

        if not polygon_parts:
            return None

        merged = unary_union(polygon_parts)

        if isinstance(merged, Polygon):
            return merged

        if isinstance(merged, MultiPolygon):
            return merged

    return None


def repair_polygonal_geometry(
    geometry: BaseGeometry,
    stats: ExtractionStats | None = None,
    building_id: str | None = None,
    area_change_warning_ratio: float = 0.01,
) -> tuple[PolygonalGeometry | None, bool]:
    """
    Validate and repair polygonal geometry.

    Returns:
        (normalized geometry or None, whether a repair was performed)
    """

    polygonal = polygonal_only(geometry)

    if polygonal is None:
        if stats is not None:
            stats.unsupported_geometry_results += 1
        return None, False

    if polygonal.is_valid:
        return polygonal, False

    area_before = polygonal.area

    try:
        repaired_raw = polygonal.buffer(0)
    except Exception as exc:
        if stats is not None:
            stats.add_error(
                f"{building_id or 'unknown'}: geometry repair failed: {exc}"
            )
        return None, False

    repaired = polygonal_only(repaired_raw)

    if repaired is None or repaired.is_empty or not repaired.is_valid:
        if stats is not None:
            stats.add_error(
                f"{building_id or 'unknown'}: geometry remained invalid "
                "after buffer(0)"
            )
        return None, True

    if stats is not None:
        stats.repaired_geometries += 1

    area_after = repaired.area

    if area_before > 0:
        relative_change = abs(area_after - area_before) / area_before

        if relative_change > area_change_warning_ratio:
            if stats is not None:
                stats.repair_area_change_warnings += 1

            LOGGER.warning(
                "Geometry repair changed area for %s by %.2f%% "
                "(%.3f m² -> %.3f m²)",
                building_id or "unknown",
                relative_change * 100,
                area_before,
                area_after,
            )

    return repaired, True


def create_polygon(
    shell: Iterable[tuple[float, float]],
    holes: Iterable[Iterable[tuple[float, float]]],
    minimum_area_m2: float,
    stats: ExtractionStats,
    building_id: str,
) -> PolygonalGeometry | None:
    """Create and validate one polygon from an exterior ring and its holes."""

    normalized_shell = normalize_ring(shell)

    if not normalized_shell:
        stats.polygons_rejected += 1
        return None

    normalized_holes = [
        normalized
        for hole in holes
        if (normalized := normalize_ring(hole))
    ]

    try:
        raw_polygon = Polygon(
            normalized_shell,
            normalized_holes or None,
        )
    except Exception as exc:
        stats.polygons_rejected += 1
        stats.add_error(
            f"{building_id}: polygon construction failed: {exc}"
        )
        return None

    polygonal, _ = repair_polygonal_geometry(
        raw_polygon,
        stats=stats,
        building_id=building_id,
    )

    if polygonal is None:
        stats.polygons_rejected += 1
        return None

    if polygonal.area < minimum_area_m2:
        stats.polygons_rejected += 1
        return None

    stats.polygons_created += 1
    return polygonal


def find_gml_files(gml_dir: Path) -> list[Path]:
    """Locate LoD2 CityGML files in or below the supplied directory."""

    patterns = (
        "LoD2_32_*/LoD2_32_*_BW.gml",
        "LoD2_32_*_BW.gml",
        "**/LoD2_32_*_BW.gml",
    )

    files: set[Path] = set()

    for pattern in patterns:
        files.update(gml_dir.glob(pattern))

    return sorted(path.resolve() for path in files if path.is_file())


def extract_tile(
    gml_path: Path,
    minimum_area_m2: float,
    default_dimension: int,
) -> TileExtraction:
    """
    Extract raw GroundSurface and RoofSurface polygons from one CityGML tile.

    BuildingPart geometry is assigned to its enclosing root Building when a
    parent Building exists. This produces one footprint for the whole building
    instead of unrelated part-level footprint resources.
    """

    result = TileExtraction()
    stats = result.stats

    building_stack: list[BuildingContext] = []

    surface_kind: SurfaceKind | None = None

    polygon_active = False
    polygon_dimension: int | None = None

    ring_role: TypingLiteral["exterior", "interior"] | None = None
    ring_dimension: int | None = None
    ring_points: list[tuple[float, float]] = []

    current_shell: list[tuple[float, float]] | None = None
    current_holes: list[list[tuple[float, float]]] = []

    context: Iterator[tuple[str, ET.Element]] = ET.iterparse(
        str(gml_path),
        events=("start", "end"),
    )

    for event, elem in context:
        tag = lname(elem.tag)

        if event == "start":
            if tag == "Building":
                building_id = attribute_by_local_name(elem, "id")

                building_stack.append(
                    BuildingContext(
                        tag=tag,
                        own_id=building_id,
                        root_building_id=building_id,
                    )
                )

                stats.buildings_seen += 1

            elif tag == "BuildingPart":
                part_id = attribute_by_local_name(elem, "id")

                root_id = (
                    building_stack[0].root_building_id
                    if building_stack
                    else part_id
                )

                building_stack.append(
                    BuildingContext(
                        tag=tag,
                        own_id=part_id,
                        root_building_id=root_id,
                    )
                )

                stats.building_parts_seen += 1

            elif building_stack and tag == "GroundSurface":
                surface_kind = "ground"

            elif building_stack and tag == "RoofSurface":
                surface_kind = "roof"

            elif building_stack and surface_kind and tag == "Polygon":
                polygon_active = True
                polygon_dimension = parse_dimension(
                    attribute_by_local_name(elem, "srsDimension")
                )

                current_shell = None
                current_holes = []
                stats.polygons_seen += 1

            elif polygon_active and tag == "exterior":
                ring_role = "exterior"

            elif polygon_active and tag == "interior":
                ring_role = "interior"

            elif polygon_active and tag == "LinearRing":
                ring_dimension = parse_dimension(
                    attribute_by_local_name(elem, "srsDimension")
                )
                ring_points = []

            for key in elem.attrib:
                if lname(key) == "href":
                    stats.xlink_references_seen += 1
                    break

        else:
            if (
                tag == "posList"
                and polygon_active
                and ring_role is not None
                and elem.text
            ):
                poslist_dimension = parse_dimension(
                    attribute_by_local_name(elem, "srsDimension")
                )

                points = parse_poslist(
                    elem.text,
                    srs_dimension=(
                        poslist_dimension
                        or ring_dimension
                        or polygon_dimension
                    ),
                    default_dimension=default_dimension,
                )

                if points:
                    ring_points.extend(points)

                stats.poslists_seen += 1

            elif (
                tag == "pos"
                and polygon_active
                and ring_role is not None
                and elem.text
            ):
                position_dimension = parse_dimension(
                    attribute_by_local_name(elem, "srsDimension")
                )

                point = parse_pos(
                    elem.text,
                    srs_dimension=(
                        position_dimension
                        or ring_dimension
                        or polygon_dimension
                    ),
                    default_dimension=default_dimension,
                )

                if point is not None:
                    ring_points.append(point)

                stats.individual_positions_seen += 1

            elif tag == "LinearRing" and polygon_active:
                normalized = normalize_ring(ring_points)

                if normalized:
                    if ring_role == "exterior":
                        current_shell = normalized
                    elif ring_role == "interior":
                        current_holes.append(normalized)

                ring_points = []
                ring_dimension = None

            elif tag in {"exterior", "interior"}:
                ring_role = None

            elif tag == "Polygon" and polygon_active:
                building_id = (
                    building_stack[-1].root_building_id
                    if building_stack
                    else None
                )

                if (
                    building_id
                    and surface_kind
                    and current_shell is not None
                ):
                    polygon = create_polygon(
                        shell=current_shell,
                        holes=current_holes,
                        minimum_area_m2=minimum_area_m2,
                        stats=stats,
                        building_id=building_id,
                    )

                    if polygon is not None:
                        target = (
                            result.ground
                            if surface_kind == "ground"
                            else result.roof
                        )

                        target[building_id].append(polygon)

                polygon_active = False
                polygon_dimension = None
                current_shell = None
                current_holes = []
                ring_points = []
                ring_role = None
                ring_dimension = None

            elif tag in {"GroundSurface", "RoofSurface"}:
                surface_kind = None

            elif tag in {"Building", "BuildingPart"}:
                if building_stack:
                    building_stack.pop()

                elem.clear()

            elif not building_stack:
                elem.clear()

    return result


def merge_building_parts(
    ground_parts: dict[str, list[PolygonalGeometry]],
    roof_parts: dict[str, list[PolygonalGeometry]],
    source_files: dict[str, set[str]],
    minimum_area_m2: float,
    stats: ExtractionStats,
) -> dict[str, FootprintRecord]:
    """
    Merge all extracted geometry parts per building.

    GroundSurface geometry is preferred. RoofSurface geometry is only used when
    no valid GroundSurface polygon exists for that building.
    """

    footprints: dict[str, FootprintRecord] = {}

    building_ids = set(ground_parts) | set(roof_parts)

    for building_id in building_ids:
        source_kind: SurfaceKind
        parts: list[PolygonalGeometry]

        if ground_parts.get(building_id):
            source_kind = "ground"
            parts = ground_parts[building_id]
        else:
            source_kind = "roof"
            parts = roof_parts[building_id]

        try:
            raw_merged = unary_union(parts)
        except Exception as exc:
            stats.add_error(
                f"{building_id}: unary_union failed: {exc}"
            )
            continue

        merged, repaired = repair_polygonal_geometry(
            raw_merged,
            stats=stats,
            building_id=building_id,
        )

        if merged is None or merged.is_empty:
            continue

        if merged.area < minimum_area_m2:
            continue

        footprints[building_id] = FootprintRecord(
            geometry=merged,
            source_kind=source_kind,
            source_files=set(source_files.get(building_id, set())),
            repaired=repaired,
        )

    return footprints


def extract_all_footprints(
    gml_files: Iterable[Path],
    minimum_area_m2: float,
    default_dimension: int,
) -> tuple[dict[str, FootprintRecord], ExtractionStats]:
    """Extract and merge footprints from all supplied CityGML files."""

    all_ground: dict[str, list[Polygon]] = defaultdict(list)
    all_roof: dict[str, list[Polygon]] = defaultdict(list)
    source_files: dict[str, set[str]] = defaultdict(set)

    total_stats = ExtractionStats()

    for gml_path in gml_files:
        LOGGER.info("Parsing %s", gml_path.name)

        tile = extract_tile(
            gml_path=gml_path,
            minimum_area_m2=minimum_area_m2,
            default_dimension=default_dimension,
        )

        for building_id, polygons in tile.ground.items():
            all_ground[building_id].extend(polygons)
            source_files[building_id].add(gml_path.name)

        for building_id, polygons in tile.roof.items():
            all_roof[building_id].extend(polygons)
            source_files[building_id].add(gml_path.name)

        merge_stats(total_stats, tile.stats)

        tile_buildings = set(tile.ground) | set(tile.roof)
        roof_only = len(set(tile.roof) - set(tile.ground))

        LOGGER.info(
            "  %s: %d building IDs with geometry "
            "(%d roof-only fallback candidates, %d polygons accepted, "
            "%d polygons rejected)",
            gml_path.name,
            len(tile_buildings),
            roof_only,
            tile.stats.polygons_created,
            tile.stats.polygons_rejected,
        )

        if tile.stats.xlink_references_seen:
            LOGGER.warning(
                "  %s contains %d xlink:href reference(s). "
                "Referenced geometry is not resolved by this parser.",
                gml_path.name,
                tile.stats.xlink_references_seen,
            )

    footprints = merge_building_parts(
        ground_parts=all_ground,
        roof_parts=all_roof,
        source_files=source_files,
        minimum_area_m2=minimum_area_m2,
        stats=total_stats,
    )

    return footprints, total_stats


def merge_stats(
    target: ExtractionStats,
    source: ExtractionStats,
) -> None:
    """Add one statistics object into another."""

    target.buildings_seen += source.buildings_seen
    target.building_parts_seen += source.building_parts_seen
    target.polygons_seen += source.polygons_seen
    target.polygons_created += source.polygons_created
    target.polygons_rejected += source.polygons_rejected
    target.geometry_errors += source.geometry_errors
    target.xlink_references_seen += source.xlink_references_seen
    target.poslists_seen += source.poslists_seen
    target.individual_positions_seen += source.individual_positions_seen
    target.repaired_geometries += source.repaired_geometries
    target.repair_area_change_warnings += (
        source.repair_area_change_warnings
    )
    target.unsupported_geometry_results += (
        source.unsupported_geometry_results
    )

    remaining_error_slots = max(0, 10 - len(target.error_samples))

    if remaining_error_slots:
        target.error_samples.extend(
            source.error_samples[:remaining_error_slots]
        )


def sanitize_identifier(value: str) -> str:
    """
    Produce the legacy sanitized identifier used for fallback matching.

    This should not be used as a unique resource identifier because multiple
    source IDs can sanitize to the same value.
    """

    return re.sub(r"[^0-9A-Za-z]", "_", value)


def stable_resource_identifier(value: str) -> str:
    """
    Produce a readable, collision-resistant identifier for an RDF resource.
    """

    readable = re.sub(
        r"[^0-9A-Za-z]+",
        "_",
        value,
    ).strip("_")

    digest = hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:12]

    if readable:
        return f"{readable}_{digest}"

    return digest


def uri_local_name(uri: URIRef) -> str:
    """Return the final slash or fragment segment of an RDF URI."""

    value = str(uri)
    return value.rsplit("/", 1)[-1].rsplit("#", 1)[-1]


def build_graph_indexes(
    graph: Graph,
) -> tuple[
    dict[str, list[URIRef]],
    dict[str, list[URIRef]],
    dict[str, list[URIRef]],
]:
    """
    Build graph indexes used for CityGML ID matching.

    Matching priority:

    1. Explicit uhi:sourceGmlId value.
    2. Exact RDF URI local name.
    3. Legacy sanitized RDF URI local name.
    """

    explicit_ids: dict[str, list[URIRef]] = defaultdict(list)
    local_names: dict[str, list[URIRef]] = defaultdict(list)
    sanitized_local_names: dict[str, list[URIRef]] = defaultdict(list)

    graph_buildings = {
        subject
        for subject in graph.subjects(RDF.type, BOT.Building)
        if isinstance(subject, URIRef)
    }

    for building_uri in graph_buildings:
        local_name = uri_local_name(building_uri)

        local_names[local_name].append(building_uri)
        sanitized_local_names[
            sanitize_identifier(local_name)
        ].append(building_uri)

        for source_id in graph.objects(
            building_uri,
            UHI.sourceGmlId,
        ):
            explicit_ids[str(source_id)].append(building_uri)

    return explicit_ids, local_names, sanitized_local_names


def unique_candidate(
    candidates: Iterable[URIRef],
) -> tuple[URIRef | None, tuple[URIRef, ...]]:
    """Return one unambiguous candidate or an ambiguity list."""

    unique = tuple(dict.fromkeys(candidates))

    if len(unique) == 1:
        return unique[0], ()

    if len(unique) > 1:
        return None, unique

    return None, ()


def resolve_building_uri(
    gml_id: str,
    explicit_ids: dict[str, list[URIRef]],
    local_names: dict[str, list[URIRef]],
    sanitized_local_names: dict[str, list[URIRef]],
) -> MatchResult:
    """Resolve one CityGML building ID to an RDF building URI."""

    explicit_uri, explicit_ambiguous = unique_candidate(
        explicit_ids.get(gml_id, [])
    )

    if explicit_uri is not None:
        return MatchResult(
            uri=explicit_uri,
            method="explicit-source-id",
        )

    if explicit_ambiguous:
        return MatchResult(
            uri=None,
            method="ambiguous-explicit-source-id",
            ambiguous_candidates=explicit_ambiguous,
        )

    local_uri, local_ambiguous = unique_candidate(
        local_names.get(gml_id, [])
    )

    if local_uri is not None:
        return MatchResult(
            uri=local_uri,
            method="exact-local-name",
        )

    if local_ambiguous:
        return MatchResult(
            uri=None,
            method="ambiguous-exact-local-name",
            ambiguous_candidates=local_ambiguous,
        )

    sanitized_id = sanitize_identifier(gml_id)

    sanitized_uri, sanitized_ambiguous = unique_candidate(
        sanitized_local_names.get(sanitized_id, [])
    )

    if sanitized_uri is not None:
        return MatchResult(
            uri=sanitized_uri,
            method="sanitized-local-name",
        )

    if sanitized_ambiguous:
        return MatchResult(
            uri=None,
            method="ambiguous-sanitized-local-name",
            ambiguous_candidates=sanitized_ambiguous,
        )

    return MatchResult(
        uri=None,
        method=None,
    )


def ring_coordinates(
    ring,
    precision: int,
) -> str:
    """Serialize one Shapely linear ring as WKT coordinate text."""

    return ", ".join(
        f"{x:.{precision}f} {y:.{precision}f}"
        for x, y in ring.coords
    )


def polygon_wkt_body(
    polygon: Polygon,
    precision: int,
) -> str:
    """Serialize the coordinate body of one WKT Polygon."""

    rings = [
        f"({ring_coordinates(polygon.exterior, precision)})"
    ]

    rings.extend(
        f"({ring_coordinates(interior, precision)})"
        for interior in polygon.interiors
    )

    return "(" + ", ".join(rings) + ")"


def geometry_wkt(
    geometry: PolygonalGeometry,
    precision: int,
) -> str:
    """Serialize Polygon or MultiPolygon geometry as CRS-prefixed WKT."""

    if isinstance(geometry, Polygon):
        return (
            f"{CRS_PREFIX}"
            f"POLYGON{polygon_wkt_body(geometry, precision)}"
        )

    if isinstance(geometry, MultiPolygon):
        polygon_bodies = ", ".join(
            polygon_wkt_body(polygon, precision)
            for polygon in geometry.geoms
        )

        return (
            f"{CRS_PREFIX}"
            f"MULTIPOLYGON({polygon_bodies})"
        )

    raise ValueError(
        f"Unsupported geometry type: {geometry.geom_type}"
    )


def count_holes(
    geometry: PolygonalGeometry,
) -> int:
    """Count interior rings in Polygon or MultiPolygon geometry."""

    if isinstance(geometry, Polygon):
        return len(geometry.interiors)

    return sum(
        len(polygon.interiors)
        for polygon in geometry.geoms
    )


def remove_existing_footprints(
    graph: Graph,
) -> tuple[int, int]:
    """
    Remove all existing footprint links and their geometry-node triples.

    Returns:
        (number of building links removed, number of geometry resources removed)
    """

    links = list(
        graph.triples(
            (
                None,
                UHI.hasFootprintGeometry,
                None,
            )
        )
    )

    geometry_nodes = {
        geometry_uri
        for _, _, geometry_uri in links
    }

    for building_uri, predicate, geometry_uri in links:
        graph.remove(
            (
                building_uri,
                predicate,
                geometry_uri,
            )
        )

    for geometry_uri in geometry_nodes:
        graph.remove(
            (
                geometry_uri,
                None,
                None,
            )
        )
        graph.remove((None, None, geometry_uri))

    return len(links), len(geometry_nodes)


def declare_ontology_terms(graph: Graph) -> None:
    """
    Add lightweight declarations for footprint terms.

    Use this only when the terms are not already managed in a separate ontology
    graph.
    """

    graph.add(
        (
            UHI.hasFootprintGeometry,
            RDF.type,
            OWL.ObjectProperty,
        )
    )
    graph.add(
        (
            UHI.FootprintGeometry,
            RDF.type,
            OWL.Class,
        )
    )
    graph.add(
        (
            UHI.footprintSource,
            RDF.type,
            OWL.DatatypeProperty,
        )
    )
    graph.add(
        (
            UHI.sourceGmlId,
            RDF.type,
            OWL.DatatypeProperty,
        )
    )


def add_footprint_to_graph(
    graph: Graph,
    building_uri: URIRef,
    gml_id: str,
    record: FootprintRecord,
    precision: int,
) -> URIRef:
    """Add one footprint geometry and its provenance to the graph."""

    resource_id = stable_resource_identifier(gml_id)
    geometry_uri = EX[f"{resource_id}_footprint"]

    graph.add(
        (
            building_uri,
            UHI.hasFootprintGeometry,
            geometry_uri,
        )
    )
    graph.add(
        (
            geometry_uri,
            RDF.type,
            GEO.Geometry,
        )
    )
    graph.add(
        (
            geometry_uri,
            RDF.type,
            UHI.FootprintGeometry,
        )
    )
    graph.add(
        (
            geometry_uri,
            GEO.asWKT,
            Literal(
                geometry_wkt(
                    record.geometry,
                    precision=precision,
                ),
                datatype=GEO.wktLiteral,
            ),
        )
    )
    graph.add(
        (
            geometry_uri,
            UHI.sourceGmlId,
            Literal(gml_id),
        )
    )
    graph.add(
        (
            geometry_uri,
            UHI.footprintSource,
            Literal(
                (
                    "GroundSurface"
                    if record.source_kind == "ground"
                    else "RoofSurfaceProjection"
                )
            ),
        )
    )
    graph.add(
        (
            geometry_uri,
            UHI.geometryRepaired,
            Literal(record.repaired),
        )
    )

    for source_file in sorted(record.source_files):
        graph.add(
            (
                geometry_uri,
                UHI.sourceFile,
                Literal(source_file),
            )
        )

    return geometry_uri


def serialize_atomically(
    graph: Graph,
    destination: Path,
    create_backup: bool,
) -> Path | None:
    """
    Serialize a graph to a temporary file and atomically replace destination.

    Returns:
        Backup path when a backup was created, otherwise None.
    """

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    backup_path: Path | None = None

    if create_backup and destination.exists():
        backup_path = destination.with_suffix(
            destination.suffix + ".bak"
        )
        shutil.copy2(destination, backup_path)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )

    os.close(file_descriptor)

    temporary_path = Path(temporary_name)

    try:
        graph.serialize(
            destination=str(temporary_path),
            format="turtle",
        )

        os.replace(
            temporary_path,
            destination,
        )

    finally:
        if temporary_path.exists():
            temporary_path.unlink()

    return backup_path


def print_extraction_summary(
    footprints: dict[str, FootprintRecord],
    stats: ExtractionStats,
) -> None:
    """Log extraction statistics."""

    roof_fallback_count = sum(
        record.source_kind == "roof"
        for record in footprints.values()
    )

    multipart_count = sum(
        isinstance(record.geometry, MultiPolygon)
        for record in footprints.values()
    )

    courtyard_holes = sum(
        count_holes(record.geometry)
        for record in footprints.values()
    )

    LOGGER.info("")
    LOGGER.info("Extraction summary")
    LOGGER.info("------------------")
    LOGGER.info("CityGML buildings seen      : %d", stats.buildings_seen)
    LOGGER.info(
        "CityGML building parts seen : %d",
        stats.building_parts_seen,
    )
    LOGGER.info("Polygon elements seen       : %d", stats.polygons_seen)
    LOGGER.info("Polygon parts accepted      : %d", stats.polygons_created)
    LOGGER.info("Polygon parts rejected      : %d", stats.polygons_rejected)
    LOGGER.info("Final building footprints   : %d", len(footprints))
    LOGGER.info("Roof-surface fallbacks      : %d", roof_fallback_count)
    LOGGER.info("Multi-part footprints       : %d", multipart_count)
    LOGGER.info("Courtyard/interior rings    : %d", courtyard_holes)
    LOGGER.info("Geometry repairs performed  : %d", stats.repaired_geometries)
    LOGGER.info(
        "Repair area warnings        : %d",
        stats.repair_area_change_warnings,
    )
    LOGGER.info("Geometry errors             : %d", stats.geometry_errors)
    LOGGER.info(
        "XLink references encountered: %d",
        stats.xlink_references_seen,
    )

    if stats.error_samples:
        LOGGER.warning("")
        LOGGER.warning("Sample geometry errors")

        for message in stats.error_samples:
            LOGGER.warning("  - %s", message)


def configure_logging(verbose: bool) -> None:
    """Configure command-line logging."""

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )


def parse_arguments() -> argparse.Namespace:
    """Parse command-line arguments."""

    parser = argparse.ArgumentParser(
        description=(
            "Extract LoD2 CityGML building footprints and write them "
            "to an RDF Turtle graph."
        )
    )

    parser.add_argument(
        "--ttl",
        type=Path,
        default=DEFAULT_TTL_FILE,
        help=(
            "Input/output Turtle graph. "
            f"Default: {DEFAULT_TTL_FILE}"
        ),
    )

    parser.add_argument(
        "--gml-dir",
        type=Path,
        default=DEFAULT_GML_DIR,
        help=(
            "Directory containing LoD2 CityGML tiles. "
            f"Default: {DEFAULT_GML_DIR}"
        ),
    )

    parser.add_argument(
        "--min-area",
        type=float,
        default=DEFAULT_MIN_AREA_M2,
        help=(
            "Minimum accepted footprint area in square metres. "
            f"Default: {DEFAULT_MIN_AREA_M2}"
        ),
    )

    parser.add_argument(
        "--precision",
        type=int,
        default=DEFAULT_WKT_PRECISION,
        help=(
            "Decimal places used in WKT coordinates. "
            f"Default: {DEFAULT_WKT_PRECISION}"
        ),
    )

    parser.add_argument(
        "--default-dimension",
        type=int,
        default=DEFAULT_SRS_DIMENSION,
        help=(
            "Coordinate dimension used when srsDimension is missing. "
            f"Default: {DEFAULT_SRS_DIMENSION}"
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Parse and match footprints without modifying the Turtle file."
        ),
    )

    parser.add_argument(
        "--backup",
        action="store_true",
        help=(
            "Create a .bak copy of the Turtle graph before replacing it."
        ),
    )

    parser.add_argument(
        "--declare-terms",
        action="store_true",
        help=(
            "Add lightweight OWL/RDFS declarations for the footprint terms."
        ),
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )

    arguments = parser.parse_args()

    if arguments.min_area <= 0:
        parser.error("--min-area must be greater than zero.")

    if arguments.precision < 0:
        parser.error("--precision cannot be negative.")

    if arguments.default_dimension < 2:
        parser.error("--default-dimension must be at least 2.")

    return arguments


def main() -> None:
    """Run the complete extraction and RDF update workflow."""

    args = parse_arguments()
    configure_logging(args.verbose)

    ttl_file = args.ttl.resolve()
    gml_dir = args.gml_dir.resolve()

    if not ttl_file.exists():
        raise FileNotFoundError(
            f"{ttl_file} was not found. "
            "Run citygml_to_rdf.py first or provide --ttl."
        )

    if not gml_dir.exists():
        raise FileNotFoundError(
            f"CityGML directory does not exist: {gml_dir}"
        )

    gml_files = find_gml_files(gml_dir)

    if not gml_files:
        raise FileNotFoundError(
            f"No LoD2_32_*_BW.gml files found under {gml_dir}"
        )

    LOGGER.info(
        "Found %d CityGML tile(s) under %s",
        len(gml_files),
        gml_dir,
    )

    footprints, extraction_stats = extract_all_footprints(
        gml_files=gml_files,
        minimum_area_m2=args.min_area,
        default_dimension=args.default_dimension,
    )

    print_extraction_summary(
        footprints=footprints,
        stats=extraction_stats,
    )

    LOGGER.info("")
    LOGGER.info("Loading RDF graph: %s", ttl_file)

    graph = Graph()
    bind_all(graph)
    graph.parse(
        str(ttl_file),
        format="turtle",
    )

    triples_before = len(graph)

    LOGGER.info(
        "Triples loaded: %d",
        triples_before,
    )

    (
        explicit_ids,
        local_names,
        sanitized_local_names,
    ) = build_graph_indexes(graph)

    graph_building_count = len(
        {
            subject
            for subject in graph.subjects(
                RDF.type,
                BOT.Building,
            )
        }
    )

    LOGGER.info(
        "BOT buildings in graph: %d",
        graph_building_count,
    )

    matched_records: list[
        tuple[str, FootprintRecord, URIRef, str]
    ] = []

    unmatched_ids: list[str] = []
    ambiguous_matches: list[
        tuple[str, str, tuple[URIRef, ...]]
    ] = []

    match_method_counts: dict[str, int] = defaultdict(int)

    for gml_id, record in footprints.items():
        match = resolve_building_uri(
            gml_id=gml_id,
            explicit_ids=explicit_ids,
            local_names=local_names,
            sanitized_local_names=sanitized_local_names,
        )

        if match.uri is not None and match.method is not None:
            matched_records.append(
                (
                    gml_id,
                    record,
                    match.uri,
                    match.method,
                )
            )
            match_method_counts[match.method] += 1

        elif match.ambiguous_candidates:
            ambiguous_matches.append(
                (
                    gml_id,
                    match.method or "ambiguous",
                    match.ambiguous_candidates,
                )
            )

        else:
            unmatched_ids.append(gml_id)

    LOGGER.info("")
    LOGGER.info("Graph matching summary")
    LOGGER.info("----------------------")
    LOGGER.info(
        "Footprints matched       : %d",
        len(matched_records),
    )
    LOGGER.info(
        "Footprints unmatched     : %d",
        len(unmatched_ids),
    )
    LOGGER.info(
        "Ambiguous matches        : %d",
        len(ambiguous_matches),
    )

    for method, count in sorted(match_method_counts.items()):
        LOGGER.info(
            "Matched by %-20s: %d",
            method,
            count,
        )

    if unmatched_ids:
        LOGGER.warning("")
        LOGGER.warning(
            "Sample unmatched CityGML IDs:"
        )

        for gml_id in unmatched_ids[:10]:
            LOGGER.warning("  - %s", gml_id)

        LOGGER.warning(
            "For reliable matching, store the original CityGML ID on each "
            "building using uhi:sourceGmlId."
        )

    if ambiguous_matches:
        LOGGER.warning("")
        LOGGER.warning("Sample ambiguous matches:")

        for gml_id, method, candidates in ambiguous_matches[:10]:
            LOGGER.warning(
                "  - %s via %s -> %s",
                gml_id,
                method,
                ", ".join(map(str, candidates)),
            )

    if args.dry_run:
        LOGGER.info("")
        LOGGER.info(
            "Dry run complete. The Turtle graph was not modified."
        )
        return

    if args.declare_terms:
        declare_ontology_terms(graph)

    removed_links, removed_geometry_nodes = remove_existing_footprints(
        graph
    )

    LOGGER.info("")
    LOGGER.info(
        "Removed %d previous footprint link(s) and %d geometry node(s)",
        removed_links,
        removed_geometry_nodes,
    )

    written_count = 0

    for gml_id, record, building_uri, _ in matched_records:
        add_footprint_to_graph(
            graph=graph,
            building_uri=building_uri,
            gml_id=gml_id,
            record=record,
            precision=args.precision,
        )

        written_count += 1

    triples_after = len(graph)

    backup_path = serialize_atomically(
        graph=graph,
        destination=ttl_file,
        create_backup=args.backup,
    )

    LOGGER.info("")
    LOGGER.info("RDF update complete")
    LOGGER.info("-------------------")
    LOGGER.info("Footprints written : %d", written_count)
    LOGGER.info(
        "Triples before     : %d",
        triples_before,
    )
    LOGGER.info(
        "Triples after      : %d",
        triples_after,
    )
    LOGGER.info(
        "Net triple change  : %+d",
        triples_after - triples_before,
    )
    LOGGER.info("Output graph       : %s", ttl_file)

    if backup_path is not None:
        LOGGER.info("Backup graph       : %s", backup_path)


if __name__ == "__main__":
    main()
