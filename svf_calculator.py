"""
True geometric Sky View Factor (SVF) from LoD2 CityGML 3D surfaces.

Algorithm
---------
1. Parse WallSurface and RoofSurface polygons (X Y Z triples) from every GML tile.
2. Build a grid-based spatial index for fast neighbour lookup.
3. For each building centroid (1.5 m above ground), gather all surface polygons
   within a 60 m search radius from other buildings.
4. Cast N = 256 rays uniformly distributed over the upper hemisphere using a
   Fibonacci / golden-angle spiral with equal-solid-angle spacing.
5. Each ray is tested against nearby triangles with a vectorised Möller–Trumbore
   intersection algorithm. SVF = (2/N) * Σ sin(elevation_i) * unblocked_i.
6. Write uhi:hasSkyViewFactor per building and zone-average SVF per zone into
   the existing TTL graph; risk_assessment.py reads these values automatically.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from statistics import mean

import numpy as np
from rdflib import Graph, Literal
from rdflib.namespace import RDF, XSD

from namespaces import UHI, BOT, EX, bind_all

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "LoD2_32_513_5402_2_bw"
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"

NS = {
    "bldg": "http://www.opengis.net/citygml/building/1.0",
    "gml":  "http://www.opengis.net/gml",
}

N_RAYS          = 256    # hemisphere rays per building
SEARCH_RADIUS_M = 60.0   # only test polygons from buildings within this distance
SENSOR_HEIGHT_M = 1.5    # ray origin above building ground elevation
GRID_SIZE_M     = 60.0   # spatial index cell size (matches search radius)

ZONE_MAP = {
    "LoD2_32_513_5402_1_BW": EX.Zone_513_5402,
    "LoD2_32_513_5403_1_BW": EX.Zone_513_5403,
    "LoD2_32_514_5402_1_BW": EX.Zone_514_5402,
    "LoD2_32_514_5403_1_BW": EX.Zone_514_5403,
}


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _parse_pos_list_3d(text: str) -> list[tuple[float, float, float]]:
    vals = list(map(float, text.split()))
    return [(vals[i], vals[i + 1], vals[i + 2]) for i in range(0, len(vals) - 2, 3)]


def _fan_triangulate(pts: list[tuple[float, float, float]]) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Fan triangulation from vertex 0. Skips degenerate triangles."""
    if len(pts) < 3:
        return []
    a = np.array(pts[0], dtype=np.float64)
    tris = []
    for i in range(1, len(pts) - 1):
        b = np.array(pts[i],     dtype=np.float64)
        c = np.array(pts[i + 1], dtype=np.float64)
        if np.linalg.norm(np.cross(b - a, c - a)) > 1e-6:
            tris.append((a, b, c))
    return tris


# ---------------------------------------------------------------------------
# GML tile parser
# ---------------------------------------------------------------------------

def parse_tile(path: Path) -> dict[str, dict]:
    """
    Returns {safe_building_id: {'cx': float, 'cy': float, 'cz': float,
                                 'tris': list[(v0,v1,v2)], 'zone': URIRef}}
    Only buildings with at least one wall/roof triangle and a ground surface are included.
    """
    root    = ET.parse(path).getroot()
    zone_uri = ZONE_MAP.get(path.stem)
    result  = {}

    for bldg_el in root.findall(".//bldg:Building", NS):
        gml_id = bldg_el.get("{http://www.opengis.net/gml}id", "")
        if not gml_id:
            continue

        tris:        list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        ground_pts:  list[tuple[float, float, float]] = []

        for surf_tag, collect_tris in [
            ("bldg:GroundSurface", False),
            ("bldg:WallSurface",   True),
            ("bldg:RoofSurface",   True),
        ]:
            for surf_el in bldg_el.findall(f".//{surf_tag}", NS):
                for pos_el in surf_el.findall(".//gml:posList", NS):
                    if not pos_el.text:
                        continue
                    pts = _parse_pos_list_3d(pos_el.text)
                    if surf_tag == "bldg:GroundSurface":
                        ground_pts.extend(pts)
                    if collect_tris:
                        tris.extend(_fan_triangulate(pts))

        if not tris or not ground_pts:
            continue

        cx = sum(p[0] for p in ground_pts) / len(ground_pts)
        cy = sum(p[1] for p in ground_pts) / len(ground_pts)
        cz = min(p[2] for p in ground_pts)   # lowest Z = ground elevation

        safe_id = gml_id.replace(":", "_").replace("/", "_")
        result[safe_id] = {
            "cx": cx, "cy": cy, "cz": cz,
            "tris": tris,
            "zone": zone_uri,
        }

    return result


# ---------------------------------------------------------------------------
# Spatial grid index
# ---------------------------------------------------------------------------

class _SpatialGrid:
    def __init__(self, cell_size: float) -> None:
        self._cs    = cell_size
        self._cells: dict[tuple[int, int], list[str]] = defaultdict(list)

    def _key(self, x: float, y: float) -> tuple[int, int]:
        return (int(x / self._cs), int(y / self._cs))

    def insert(self, x: float, y: float, bid: str) -> None:
        self._cells[self._key(x, y)].append(bid)

    def candidates(self, x: float, y: float) -> list[str]:
        cx, cy = self._key(x, y)
        result: list[str] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                result.extend(self._cells.get((cx + dx, cy + dy), []))
        return result


# ---------------------------------------------------------------------------
# Hemisphere ray generator
# ---------------------------------------------------------------------------

def _make_rays(n: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Return (directions, sin_elevations) for n rays uniformly distributed over
    the upper hemisphere using the golden-angle Fibonacci spiral.

    Spacing: sin(elevation) is uniformly spaced → equal solid angle per ray.
    SVF formula: SVF = (2/N) * Σ sin(el_i) * V_i  (V_i = 1 if sky visible).
    """
    i       = np.arange(n, dtype=np.float64)
    sin_el  = (i + 0.5) / n                       # uniform in [0, 1)
    el      = np.arcsin(sin_el)
    az      = 2.0 * np.pi * i * (1.0 + np.sqrt(5.0)) / 2.0   # golden angle

    cos_el = np.cos(el)
    dirs = np.column_stack([
        cos_el * np.cos(az),
        cos_el * np.sin(az),
        sin_el,
    ])
    return dirs, sin_el


# ---------------------------------------------------------------------------
# Vectorised Möller–Trumbore intersection (one ray vs N triangles)
# ---------------------------------------------------------------------------

def _ray_hits_any(origin: np.ndarray, direction: np.ndarray,
                  v0: np.ndarray, v1: np.ndarray, v2: np.ndarray) -> bool:
    """
    Returns True if 'direction' ray from 'origin' hits any of the N triangles.
    v0, v1, v2: (N, 3) float64 arrays.
    """
    edge1 = v1 - v0                                         # (N, 3)
    edge2 = v2 - v0                                         # (N, 3)
    pvec  = np.cross(direction, edge2)                      # (N, 3)
    det   = np.einsum("ij,ij->i", edge1, pvec)              # (N,)

    valid = np.abs(det) > 1e-10
    if not valid.any():
        return False

    inv_det = np.where(valid, 1.0 / np.where(valid, det, 1.0), 0.0)

    tvec  = origin - v0                                     # (N, 3)
    bary_u = inv_det * np.einsum("ij,ij->i", tvec, pvec)   # (N,)
    valid &= (bary_u >= 0.0) & (bary_u <= 1.0)
    if not valid.any():
        return False

    qvec  = np.cross(tvec, edge1)                           # (N, 3)
    dir_b = np.broadcast_to(direction, v0.shape).copy()
    bary_v = inv_det * np.einsum("ij,ij->i", dir_b, qvec)  # (N,)
    valid &= (bary_v >= 0.0) & (bary_u + bary_v <= 1.0)
    if not valid.any():
        return False

    t = inv_det * np.einsum("ij,ij->i", edge2, qvec)       # (N,)
    valid &= (t > 0.5)   # 0.5 m minimum to avoid self-hits on adjacent surfaces

    return bool(valid.any())


# ---------------------------------------------------------------------------
# Per-building SVF computation
# ---------------------------------------------------------------------------

def _compute_svf(bid: str, buildings: dict, grid: _SpatialGrid,
                 rays: np.ndarray, sin_el: np.ndarray) -> float:
    cx, cy, cz = buildings[bid]["cx"], buildings[bid]["cy"], buildings[bid]["cz"]
    origin = np.array([cx, cy, cz + SENSOR_HEIGHT_M], dtype=np.float64)

    # Gather triangles from nearby buildings, excluding self
    v0_list, v1_list, v2_list = [], [], []
    for nbr_id in grid.candidates(cx, cy):
        if nbr_id == bid:
            continue
        nbr = buildings[nbr_id]
        if ((cx - nbr["cx"]) ** 2 + (cy - nbr["cy"]) ** 2) > SEARCH_RADIUS_M ** 2:
            continue
        for a, b, c in nbr["tris"]:
            v0_list.append(a)
            v1_list.append(b)
            v2_list.append(c)

    if not v0_list:
        return 1.0   # no obstacles → open sky

    v0 = np.array(v0_list, dtype=np.float64)
    v1 = np.array(v1_list, dtype=np.float64)
    v2 = np.array(v2_list, dtype=np.float64)

    # SVF = (2/N) * Σ sin(el_i) * V_i
    svf_sum = 0.0
    for ray, w in zip(rays, sin_el):
        if not _ray_hits_any(origin, ray, v0, v1, v2):
            svf_sum += w

    return float(svf_sum * 2.0 / len(rays))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Parsing 3D surface geometry from CityGML tiles ...")
    all_buildings: dict[str, dict] = {}
    for gml_file in sorted(DATA_DIR.glob("*.gml")):
        tile = parse_tile(gml_file)
        all_buildings.update(tile)
        wall_tri_count = sum(len(d["tris"]) for d in tile.values())
        print(f"  {gml_file.name}: {len(tile)} buildings, {wall_tri_count} triangles")

    total_tris = sum(len(d["tris"]) for d in all_buildings.values())
    print(f"\nTotal buildings with 3D geometry : {len(all_buildings)}")
    print(f"Total wall/roof triangles         : {total_tris}")

    # Spatial index
    grid = _SpatialGrid(GRID_SIZE_M)
    for bid, d in all_buildings.items():
        grid.insert(d["cx"], d["cy"], bid)

    # Hemisphere rays
    rays, sin_el = _make_rays(N_RAYS)
    print(f"\nRay config: {N_RAYS} rays, search radius {SEARCH_RADIUS_M} m, "
          f"sensor height {SENSOR_HEIGHT_M} m")

    # Load existing RDF graph
    print("\nLoading RDF graph ...")
    g = Graph()
    bind_all(g)
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    # Only process buildings present in the graph
    known_uris = set(g.subjects(RDF.type, BOT.Building))

    print("\nComputing geometric SVF per building ...")
    zone_svf: dict = defaultdict(list)
    computed = skipped = 0

    for bid, data in all_buildings.items():
        bldg_uri = EX[bid]
        if bldg_uri not in known_uris:
            skipped += 1
            continue

        svf = _compute_svf(bid, all_buildings, grid, rays, sin_el)
        g.set((bldg_uri, UHI.hasSkyViewFactor, Literal(round(svf, 4), datatype=XSD.decimal)))

        if data["zone"]:
            zone_svf[data["zone"]].append(svf)

        computed += 1
        if computed % 500 == 0:
            print(f"  {computed}/{len(all_buildings)} ...")

    # Zone-average SVF
    for zone_uri, svf_list in zone_svf.items():
        avg = round(mean(svf_list), 4)
        g.set((zone_uri, UHI.hasSkyViewFactor, Literal(avg, datatype=XSD.decimal)))

    g.serialize(destination=str(TTL_FILE), format="turtle")

    print(f"\nBuildings processed  : {computed}")
    print(f"Buildings skipped    : {skipped}  (not in RDF graph)")
    print(f"Triples added        : {len(g) - triples_before}")
    print(f"Triples total        : {len(g)}")
    print("\nZone SVF summary (geometric):")
    for zone_uri, svf_list in sorted(zone_svf.items(), key=lambda x: str(x[0])):
        zone_name = str(zone_uri).rsplit("/", 1)[-1]
        avg  = mean(svf_list)
        lo   = min(svf_list)
        hi   = max(svf_list)
        print(f"  {zone_name:<20}  avg={avg:.3f}  min={lo:.3f}  max={hi:.3f}  n={len(svf_list)}")


if __name__ == "__main__":
    main()
