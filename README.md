# Stuttgart Heat Risk Knowledge Graph

Semantic knowledge graph for urban heat island risk analysis in Stuttgart, using CityGML building geometry, OWL DL reasoning, and real climate data. Built for the AI Lab course at the University of Stuttgart.

## Overview

The pipeline converts 3D building data (LGL BW LoD2 CityGML) into an RDF knowledge graph, enriches it with 2024 temperature observations from the Open-Meteo archive API, and uses the HermiT OWL DL reasoner to infer heat-vulnerable buildings. All three data layers are unified under a shared zone URI, enabling cross-source SPARQL queries without manual data merging.

**Results (Stuttgart-Mitte, 4 × 1 km² tiles, 2024):**
- 5,801 buildings converted · 76,399 RDF triples
- 145 `uhi:VulnerableBuilding` instances inferred by HermiT (≥ 2 distinct heat risk factors)
- 12 heat days (> 30 °C) confirmed in the hottest zone (Zone 513/5403, NW)

## Prerequisites

- Python 3.11+
- Java 11+ (required by the HermiT reasoner via owlready2)
- CityGML LoD2 tiles — see **Data** below

```bash
pip install rdflib owlready2 folium pyproj
```

## Data

**Source:** LGL Baden-Württemberg — [opengeodata.lgl-bw.de](https://opengeodata.lgl-bw.de)  
**Tiles:** `LoD2_32_513_5402_1_BW`, `LoD2_32_513_5403_1_BW`, `LoD2_32_514_5402_1_BW`, `LoD2_32_514_5403_1_BW`  
**License:** [Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)](https://www.govdata.de/dl-de/by-2-0)

Download the four GML tiles and place them in `LoD2_32_513_5402_2_bw/` (the folder name the scripts expect).

The derived knowledge graph (`stuttgart_buildings.ttl`) is included in this repository. It is redistributed under the same dl-de/by-2-0 terms with attribution to LGL BW and [Open-Meteo](https://open-meteo.com) (CC BY 4.0).

## Pipeline

Run scripts in order. Each script reads and/or writes `stuttgart_buildings.ttl`.

| Script | Purpose | Output |
|---|---|---|
| `audit.py` | Inspect raw GML tiles; report building counts, height/footprint stats, missing values | Console |
| `citygml_to_rdf.py` | Parse GML → RDF; compute footprint area, map ALKIS codes, classify risk factors | `stuttgart_buildings.ttl` (created) |
| `climate_data.py` | Fetch 2024 daily max temperatures from Open-Meteo; add SOSA observations | `stuttgart_buildings.ttl` (enriched) |
| `reasoning.py` | Run HermiT OWL DL reasoner; materialise `uhi:VulnerableBuilding` instances | `stuttgart_buildings.ttl` (enriched) |
| `queries_and_viz.py` | Run cross-source SPARQL queries; generate interactive folium map | `stuttgart_heat_risk_map.html` |

## Ontology

`uhi_ontology.ttl` defines the domain vocabulary under `http://example.org/uhi#`. Key axiom:

```turtle
uhi:VulnerableBuilding owl:equivalentClass [
    owl:intersectionOf (
        bot:Building
        [ owl:onProperty uhi:hasRiskFactor ;
          owl:minQualifiedCardinality 2 ;
          owl:onClass uhi:HeatRiskFactor ]
    )
] .
```

Reused standards: [BOT](https://w3id.org/bot) · [SOSA](https://www.w3.org/TR/vocab-ssn/) · [GeoSPARQL](https://www.ogc.org/standards/geosparql)

## Attribution

- Building geometry: © LGL BW, [dl-de/by-2-0](https://www.govdata.de/dl-de/by-2-0)
- Climate data: [Open-Meteo Historical Weather API](https://open-meteo.com/en/docs/historical-weather-api), CC BY 4.0
