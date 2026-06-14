import json
import math
import re
from collections import defaultdict
from pathlib import Path
import html

import folium
from pyproj import Transformer
from rdflib import Graph
from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

from namespaces import bind_all

BASE_DIR = Path(__file__).resolve().parent
TTL_FILE = BASE_DIR / "stuttgart_buildings.ttl"
MAP_FILE = BASE_DIR / "stuttgart_heat_risk_map.html"

TO_WGS84 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)

SPARQL_PREFIXES = """
PREFIX uhi:  <https://w3id.org/stuttgart-uhi#>
PREFIX bot:  <https://w3id.org/bot#>
PREFIX sosa: <http://www.w3.org/ns/sosa/>
PREFIX geo:  <http://www.opengis.net/ont/geosparql#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""

CATEGORY_STYLE = {
    "LowRisk": {"color": "#2ecc71", "radius": 2},
    "MediumRisk": {"color": "#f1c40f", "radius": 3},
    "HighRisk": {"color": "#e67e22", "radius": 5},
    "ExtremeRisk": {"color": "#c0392b", "radius": 6},
}

# Zone bboxes in UTM32, matching citygml_to_rdf.py / terrain_dgm.py / clms_landcover.py.
ZONE_BBOXES_UTM = {
    "Zone_513_5402": (513000, 5402000, 514000, 5403000),
    "Zone_513_5403": (513000, 5403000, 514000, 5404000),
    "Zone_514_5402": (514000, 5402000, 515000, 5403000),
    "Zone_514_5403": (514000, 5403000, 515000, 5404000),
}

# Zoom level at which the visualisation switches from zone-heatmap (zoomed out)
# to per-building markers (zoomed in). Stuttgart-Mitte 2x2 km fits comfortably
# at zoom 14; individual buildings become legible from 15 upward.
ZOOM_THRESHOLD = 15


def local_name(uri) -> str:
    text = str(uri)
    return text.rsplit("#", 1)[-1].rstrip("/").rsplit("/", 1)[-1]


def wkt_to_latlon(wkt: str) -> tuple[float, float] | None:
    m = re.search(r"POINT\(\s*([-0-9.]+)\s+([-0-9.]+)\s*\)", wkt)
    if not m:
        return None
    easting, northing = float(m.group(1)), float(m.group(2))
    lon, lat = TO_WGS84.transform(easting, northing)
    return lat, lon


def wkt_to_utm_point(wkt: str) -> tuple[float, float] | None:
    """Parse the UTM32 (E, N) coordinates from a building POINT WKT, without reprojecting."""
    m = re.search(r"POINT\(\s*([-0-9.]+)\s+([-0-9.]+)\s*\)", wkt)
    if not m:
        return None
    return float(m.group(1)), float(m.group(2))


def building_octagon_utm(cx: float, cy: float, area_m2: float) -> list[tuple[float, float]]:
    """Return an 8-vertex regular polygon around (cx, cy) approximating a building of given footprint area.

    The graph stores building geometry as centroid POINTs plus uhi:hasFootprintArea.
    For the zone-heatmap cutout we only need the *aggregate* visual pattern, so an
    octagonal approximation r = sqrt(area / pi) is adequate at zoom-out scales
    (each building is at most a few pixels across when 4 km^2 of city fits on screen).
    """
    r = math.sqrt(max(area_m2, 1.0) / math.pi)
    return [
        (cx + r * math.cos(theta), cy + r * math.sin(theta))
        for theta in (math.tau * i / 8 for i in range(8))
    ]


def utm_ring_to_lonlat(coords_utm: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Reproject a UTM32 ring of (E, N) to a WGS84 ring of (lon, lat) — Shapely's coordinate order."""
    return [TO_WGS84.transform(x, y) for x, y in coords_utm]


def build_zone_heatmap_layer(g: Graph) -> tuple[folium.FeatureGroup, dict]:
    """Build a FeatureGroup with one polygon per zone, colored by risk category,
    with building footprints punched out as holes.

    Returns (layer, zone_info) where zone_info maps zone_id -> {"score", "category"}.
    """
    zone_query = SPARQL_PREFIXES + """
    SELECT ?zone ?score ?category WHERE {
        ?zone uhi:hasHeatRiskAssessment ?a .
        ?a uhi:hasHeatRiskScore ?score ;
           uhi:hasRiskCategory ?category .
    }
    """
    zone_info: dict[str, dict] = {}
    for row in g.query(zone_query):
        zid = local_name(row.zone)
        zone_info[zid] = {"score": float(row.score), "category": local_name(row.category)}

    building_query = SPARQL_PREFIXES + """
    SELECT ?zone ?wkt ?footprint WHERE {
        ?b a bot:Building ;
           uhi:inAnalysisZone ?zone ;
           uhi:hasFootprintArea ?footprint ;
           geo:hasGeometry ?geom .
        ?geom geo:asWKT ?wkt .
    }
    """
    buildings_by_zone: dict[str, list[Polygon]] = defaultdict(list)
    for row in g.query(building_query):
        zid = local_name(row.zone)
        pt = wkt_to_utm_point(str(row.wkt))
        if pt is None:
            continue
        cx, cy = pt
        coords_utm = building_octagon_utm(cx, cy, float(row.footprint))
        coords_lonlat = utm_ring_to_lonlat(coords_utm)
        try:
            poly = Polygon(coords_lonlat)
            if poly.is_valid and not poly.is_empty:
                buildings_by_zone[zid].append(poly)
        except Exception:
            continue

    layer = folium.FeatureGroup(name="Zone vulnerability heatmap", show=True)

    for zid, bbox_utm in ZONE_BBOXES_UTM.items():
        if zid not in zone_info:
            continue
        xmin, ymin, xmax, ymax = bbox_utm
        zone_ring_utm = [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]
        zone_ring_ll = utm_ring_to_lonlat(zone_ring_utm)
        zone_polygon = Polygon(zone_ring_ll)

        building_polys = buildings_by_zone.get(zid, [])
        if building_polys:
            buildings_union = unary_union(building_polys)
            holed = zone_polygon.difference(buildings_union)
        else:
            holed = zone_polygon

        if holed.is_empty:
            continue

        category = zone_info[zid]["category"]
        score = zone_info[zid]["score"]
        fill_color = CATEGORY_STYLE.get(category, {"color": "#7f8c8d"})["color"]
        n_buildings = len(building_polys)

        feature = {
            "type": "Feature",
            "geometry": mapping(holed),
            "properties": {"zone": zid, "score": score, "category": category},
        }
        folium.GeoJson(
            data=feature,
            style_function=lambda _f, c=fill_color: {
                "fillColor": c,
                "color": "#333333",
                "weight": 1.5,
                "fillOpacity": 0.55,
            },
            highlight_function=lambda _f: {"weight": 3, "fillOpacity": 0.7},
            tooltip=folium.Tooltip(
                f"<b>{html.escape(zid)}</b><br>"
                f"Risk: <b>{html.escape(category)}</b><br>"
                f"Score: {score:.3f}<br>"
                f"Buildings shown as cut-outs: {n_buildings}"
            ),
        ).add_to(layer)

    return layer, zone_info


def inject_zoom_toggle_js(m: folium.Map, zone_layer: folium.FeatureGroup,
                          building_layers: list[folium.FeatureGroup],
                          default_visible_categories: set[str]) -> None:
    """Inject Leaflet JS that switches between zone heatmap and per-building markers based on zoom.

    Behaviour
    ---------
    - Below ZOOM_THRESHOLD: zone heatmap visible, all building layers hidden.
    - At or above ZOOM_THRESHOLD: zone heatmap hidden, default-visible building
      layers shown (LayerControl can still override manually between threshold crossings).
    - Threshold crossing logic: only toggles when zoom crosses the boundary, so
      manual LayerControl choices made while zoomed in are preserved until the
      user zooms out and back in.
    """
    map_name = m.get_name()
    zone_name = zone_layer.get_name()
    # Pair each building layer JS name with whether it should be shown by default at zoom >= threshold.
    building_specs = [
        (bl.get_name(), local_name_from_layer(bl) in default_visible_categories)
        for bl in building_layers
    ]
    layer_list_js = "[" + ", ".join(
        f"{{layer: {name}, defaultShow: {str(show).lower()}}}"
        for name, show in building_specs
    ) + "]"

    js = f"""
    <script>
    (function() {{
        function setup() {{
            if (typeof {map_name} === 'undefined' || typeof {zone_name} === 'undefined') {{
                setTimeout(setup, 50);
                return;
            }}
            var map = {map_name};
            var zoneLayer = {zone_name};
            var buildingSpecs = {layer_list_js};
            var threshold = {ZOOM_THRESHOLD};
            var lastBucket = null;

            function update() {{
                var bucket = (map.getZoom() < threshold) ? 'zones' : 'buildings';
                if (bucket === lastBucket) return;
                if (bucket === 'zones') {{
                    if (!map.hasLayer(zoneLayer)) zoneLayer.addTo(map);
                    buildingSpecs.forEach(function(s) {{
                        if (map.hasLayer(s.layer)) map.removeLayer(s.layer);
                    }});
                }} else {{
                    if (map.hasLayer(zoneLayer)) map.removeLayer(zoneLayer);
                    buildingSpecs.forEach(function(s) {{
                        if (s.defaultShow && !map.hasLayer(s.layer)) s.layer.addTo(map);
                    }});
                }}
                lastBucket = bucket;
            }}
            map.on('zoomend', update);
            update();
        }}
        setup();
    }})();
    </script>
    """
    m.get_root().html.add_child(folium.Element(js))


def local_name_from_layer(layer: folium.FeatureGroup) -> str:
    """Best-effort recovery of a building category key from a FeatureGroup's display name."""
    name = (layer.layer_name or "").lower()
    if "extreme" in name:
        return "ExtremeRisk"
    if "high" in name:
        return "HighRisk"
    if "medium" in name:
        return "MediumRisk"
    if "low" in name:
        return "LowRisk"
    return ""


def run_queries(g: Graph) -> None:
    print("=" * 70)
    print("SPARQL query results — score-based heat-risk ontology")
    print("=" * 70)

    q1 = SPARQL_PREFIXES + """
    SELECT ?zone ?category (COUNT(DISTINCT ?b) AS ?n) (AVG(?score) AS ?avgScore)
    WHERE {
        ?b a bot:Building ;
           uhi:inAnalysisZone ?zone ;
           uhi:hasHeatRiskAssessment ?assessment .
        ?assessment uhi:hasRiskCategory ?category ;
                    uhi:hasHeatRiskScore ?score .
    }
    GROUP BY ?zone ?category
    ORDER BY ?zone DESC(?avgScore)
    """

    print("\nQ1 — Building risk categories per zone")
    print(f"  {'Zone':<18} {'Category':<14} {'Buildings':>9} {'AvgScore':>9}")
    for row in g.query(q1):
        print(
            f"  {local_name(row.zone):<18} {local_name(row.category):<14} "
            f"{int(row.n):>9} {float(row.avgScore):>9.3f}"
        )

    q2 = SPARQL_PREFIXES + """
    SELECT ?building ?category ?score ?deltaT ?zone
    WHERE {
        ?building a bot:Building ;
                  uhi:inAnalysisZone ?zone ;
                  uhi:hasHeatRiskAssessment ?assessment .
        ?assessment uhi:hasRiskCategory ?category ;
                    uhi:hasHeatRiskScore ?score ;
                    uhi:hasIndicativeDeltaT ?deltaT .
        FILTER(?category IN (uhi:HighRisk, uhi:ExtremeRisk))
    }
    ORDER BY DESC(?score)
    LIMIT 15
    """

    print("\nQ2 — Most vulnerable buildings by assessment score")
    print(f"  {'Building':<24} {'Zone':<18} {'Category':<14} {'Score':>7} {'ΔT':>7}")
    for row in g.query(q2):
        print(
            f"  {local_name(row.building):<24} {local_name(row.zone):<18} "
            f"{local_name(row.category):<14} {float(row.score):>7.3f} {float(row.deltaT):>6.2f}°C"
        )

    q3 = SPARQL_PREFIXES + """
    SELECT ?zone ?score ?category ?svf ?density ?topo ?canopy ?veg ?trees ?imperv ?heatDays
    WHERE {
        ?zone uhi:hasHeatRiskAssessment ?assessment ;
              uhi:hasSkyViewFactor ?svf ;
              uhi:hasUrbanDensity ?density ;
              uhi:hasTopographicExposure ?topo ;
              uhi:hasTreeCanopyCoverage ?canopy ;
              uhi:hasVegetationFraction ?veg ;
              uhi:hasTreeCount ?trees ;
              uhi:hasImperviousSurfaceFraction ?imperv ;
              uhi:hasHeatDayCount ?heatDays .
        ?assessment a uhi:ZoneHeatRiskAssessment ;
                    uhi:hasHeatRiskScore ?score ;
                    uhi:hasRiskCategory ?category .
    }
    ORDER BY DESC(?score)
    """

    print("\nQ3 — Why zones are risky: indicator explanation")
    print(f"  {'Zone':<18} {'Cat':<12} {'Score':>6} {'SVF':>6} {'Dens':>6} {'Topo':>6} {'TCD':>6} {'OSMVeg':>7} {'Trees':>7} {'Imperv':>7} {'Heat':>5}")
    for row in g.query(q3):
        print(
            f"  {local_name(row.zone):<18} {local_name(row.category):<12} "
            f"{float(row.score):>6.3f} {float(row.svf):>6.3f} {float(row.density):>6.3f} "
            f"{float(row.topo):>6.3f} {float(row.canopy):>6.3f} {float(row.veg):>7.3f} {int(row.trees):>7} "
            f"{float(row.imperv):>7.3f} {int(row.heatDays):>5}"
        )

    q4 = SPARQL_PREFIXES + """
    SELECT DISTINCT ?zone ?vegType ?dominantType ?trees ?veg
    WHERE {
        ?zone uhi:hasVegetationFraction ?veg ;
              uhi:hasTreeCount ?trees .
        OPTIONAL { ?zone uhi:hasVegetationType ?vegType . }
        OPTIONAL { ?zone uhi:hasDominantVegetationType ?dominantType . }
    }
    ORDER BY ?zone ?vegType
    """

    print("\nQ4 — OSM vegetation enrichment by zone")
    seen = set()
    for row in g.query(q4):
        key = (row.zone, row.vegType, row.dominantType)
        if key in seen:
            continue
        seen.add(key)
        veg_type = local_name(row.vegType) if row.vegType else "-"
        dominant = local_name(row.dominantType) if row.dominantType else "-"
        print(
            f"  {local_name(row.zone):<18} veg={float(row.veg):.3f} "
            f"trees={int(row.trees):<5} type={veg_type:<20} dominant={dominant}"
        )


def build_map(g: Graph) -> None:
    print("\nBuilding interactive map …")

    q_map = SPARQL_PREFIXES + """
    SELECT ?building ?wkt ?height ?footprint ?zone ?assessment ?score ?deltaT ?category
    WHERE {
        ?building a bot:Building ;
                  uhi:hasMeasuredHeight ?height ;
                  uhi:hasFootprintArea ?footprint ;
                  uhi:inAnalysisZone ?zone ;
                  geo:hasGeometry ?geom ;
                  uhi:hasHeatRiskAssessment ?assessment .
        ?geom geo:asWKT ?wkt .
        ?assessment uhi:hasHeatRiskScore ?score ;
                    uhi:hasIndicativeDeltaT ?deltaT ;
                    uhi:hasRiskCategory ?category .
    }
    """

    m = folium.Map(location=[48.762, 9.179], zoom_start=14, tiles="CartoDB positron")
    layers = {
        "LowRisk": folium.FeatureGroup(name="Low risk", show=False),
        "MediumRisk": folium.FeatureGroup(name="Medium risk", show=False),
        "HighRisk": folium.FeatureGroup(name="High risk", show=True),
        "ExtremeRisk": folium.FeatureGroup(name="Extreme risk", show=True),
    }

    plotted = 0
    skipped = 0
    category_counts = {key: 0 for key in layers}

    rows = list(g.query(q_map))
    if not rows:
        raise RuntimeError(
            "No HeatRiskAssessment found. Run risk_assessment.py first."
        )
    print(f"  Plotting {len(rows)} assessed buildings …")

    for row in rows:
        ll = wkt_to_latlon(str(row.wkt))
        if ll is None:
            skipped += 1
            continue

        category = local_name(row.category)
        style = CATEGORY_STYLE.get(category, {"color": "#7f8c8d", "radius": 3})
        layer = layers.get(category, layers["MediumRisk"])
        lat, lon = ll
        score = float(row.score)
        delta_t = float(row.deltaT)
        height = float(row.height)
        footprint = float(row.footprint)
        building_id = local_name(row.building)
        zone_id = local_name(row.zone)

        building_id_safe = html.escape(building_id)
        zone_id_safe = html.escape(zone_id)
        category_safe = html.escape(category)

        popup_html = (
            f"<b>{building_id_safe}</b><br>"
            f"Zone: {zone_id_safe}<br>"
            f"Category: <b>{category_safe}</b><br>"
            f"Heat risk score: {score:.3f}<br>"
            f"Indicative ΔT: {delta_t:.2f} °C<br>"
            f"Height: {height:.1f} m<br>"
            f"Footprint: {footprint:.0f} m²"
        )

        folium.CircleMarker(
            location=[lat, lon],
            radius=style["radius"],
            color=style["color"],
            fill=True,
            fill_color=style["color"],
            fill_opacity=0.75,
            weight=1,
            popup=folium.Popup(popup_html, max_width=260),
            tooltip=f"{category_safe}: {building_id_safe} ({score:.2f})",
        ).add_to(layer)

        plotted += 1
        category_counts[category] = category_counts.get(category, 0) + 1

    for layer in layers.values():
        layer.add_to(m)

    # Zone vulnerability heatmap (zoomed-out view) with building footprints cut out.
    zone_layer, zone_info = build_zone_heatmap_layer(g)
    zone_layer.add_to(m)

    # JS toggle: zone heatmap below ZOOM_THRESHOLD, per-building markers at or above.
    default_visible = {"HighRisk", "ExtremeRisk"}
    inject_zoom_toggle_js(m, zone_layer, list(layers.values()), default_visible)

    legend_html = f"""
    <div style="position:fixed;bottom:30px;left:30px;z-index:9999;
                background:white;padding:12px 16px;border-radius:6px;
                border:1px solid #ccc;font-family:sans-serif;font-size:13px;
                max-width:260px;">
      <b>Stuttgart UHI Heat Risk</b><br>
      <span style="color:#2ecc71">●</span> LowRisk<br>
      <span style="color:#f1c40f">●</span> MediumRisk<br>
      <span style="color:#e67e22">●</span> HighRisk<br>
      <span style="color:#c0392b">●</span> ExtremeRisk<br>
      <br>
      <small><b>Zoom &lt; {ZOOM_THRESHOLD}:</b> zone heatmap with building cut-outs.<br>
      <b>Zoom ≥ {ZOOM_THRESHOLD}:</b> individual building markers.<br>
      Layer Control overrides apply between threshold crossings.</small><br>
      <br>
      <small>LoD2 + Open-Meteo + OSM + CLMS HRL + DGM1</small>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))
    folium.LayerControl(collapsed=False).add_to(m)
    m.save(str(MAP_FILE))

    print(f"  Plotted : {plotted} buildings ({skipped} skipped)")
    print(f"  Counts  : {category_counts}")
    print(f"  Map saved: {MAP_FILE.name}")


def main() -> None:
    print("Loading graph …")
    g = Graph()
    bind_all(g)

    if not TTL_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {TTL_FILE}. Run the pipeline first."
        )
    g.parse(str(TTL_FILE), format="turtle")
    print(f"  {len(g)} triples")

    run_queries(g)
    build_map(g)


if __name__ == "__main__":
    main()
    