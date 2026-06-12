import re
from pathlib import Path
import html

import folium
from pyproj import Transformer
from rdflib import Graph

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
    SELECT ?zone ?score ?category ?svf ?density ?topo ?veg ?trees ?imperv ?heatDays
    WHERE {
        ?zone uhi:hasHeatRiskAssessment ?assessment ;
              uhi:hasSkyViewFactor ?svf ;
              uhi:hasUrbanDensity ?density ;
              uhi:hasTopographicExposure ?topo ;
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
    print(f"  {'Zone':<18} {'Cat':<12} {'Score':>6} {'SVF':>6} {'Dens':>6} {'Topo':>6} {'Veg':>6} {'Trees':>7} {'Imperv':>7} {'Heat':>5}")
    for row in g.query(q3):
        print(
            f"  {local_name(row.zone):<18} {local_name(row.category):<12} "
            f"{float(row.score):>6.3f} {float(row.svf):>6.3f} {float(row.density):>6.3f} "
            f"{float(row.topo):>6.3f} {float(row.veg):>6.3f} {int(row.trees):>7} "
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

    legend_html = """
    <div style="position:fixed;bottom:30px;left:30px;z-index:9999;
                background:white;padding:12px 16px;border-radius:6px;
                border:1px solid #ccc;font-family:sans-serif;font-size:13px;">
      <b>Stuttgart UHI Heat Risk</b><br>
      <span style="color:#2ecc71">●</span> LowRisk<br>
      <span style="color:#f1c40f">●</span> MediumRisk<br>
      <span style="color:#e67e22">●</span> HighRisk<br>
      <span style="color:#c0392b">●</span> ExtremeRisk<br>
      <br>
      <small>Score-based HeatRiskAssessment model<br>
      LoD2 + Open-Meteo + OSM vegetation</small>
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
    