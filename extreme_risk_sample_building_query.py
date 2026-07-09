from pathlib import Path
from rdflib import Graph

TTL_FILE = Path("stuttgart_buildings.ttl")

SPARQL = """
PREFIX uhi: <https://w3id.org/stuttgart-uhi#>
PREFIX bot: <https://w3id.org/bot#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>

SELECT ?building ?zone ?score ?deltaT ?height ?footprint
       ?svf ?density ?topo ?canopy ?veg ?trees ?imperv ?heatDays ?wkt
WHERE {
  ?building a bot:Building ;
            uhi:inAnalysisZone ?zone ;
            uhi:hasMeasuredHeight ?height ;
            uhi:hasFootprintArea ?footprint ;
            geo:hasGeometry ?geom ;
            uhi:hasHeatRiskAssessment ?assessment .
            
  ?geom geo:asWKT ?wkt .

  ?assessment uhi:hasRiskCategory uhi:ExtremeRisk ;
              uhi:hasHeatRiskScore ?score ;
              uhi:hasIndicativeDeltaT ?deltaT .

  ?zone uhi:hasSkyViewFactor ?svf ;
        uhi:hasUrbanDensity ?density ;
        uhi:hasTopographicExposure ?topo ;
        uhi:hasTreeCanopyCoverage ?canopy ;
        uhi:hasVegetationFraction ?veg ;
        uhi:hasTreeCount ?trees ;
        uhi:hasImperviousSurfaceFraction ?imperv ;
        uhi:hasHeatDayCount ?heatDays .
}
ORDER BY RAND()
LIMIT 1
"""

g = Graph()
g.parse(TTL_FILE, format="turtle")

for row in g.query(SPARQL):
    print("Random ExtremeRisk building")
    print("---------------------------")
    print(f"Building: {row.building}")
    print(f"Zone: {row.zone}")
    print(f"Score: {float(row.score):.3f}")
    print(f"Indicative ΔT: {float(row.deltaT):.2f} °C")
    print(f"Height: {float(row.height):.1f} m")
    print(f"Footprint: {float(row.footprint):.0f} m²")
    print()
    print("Zone indicators:")
    print(f"SVF: {float(row.svf):.3f}")
    print(f"Density: {float(row.density):.3f}")
    print(f"Topographic exposure: {float(row.topo):.3f}")
    print(f"Tree canopy coverage (CLMS): {float(row.canopy):.3f}")
    print(f"OSM vegetation fraction: {float(row.veg):.3f}")
    print(f"Vegetation fraction: {float(row.veg):.3f}")
    print(f"Tree count: {int(row.trees)}")
    print(f"Impervious fraction: {float(row.imperv):.3f}")
    print(f"Heat days: {int(row.heatDays)}")
    print(f"WKT Point: {row.wkt}")
    