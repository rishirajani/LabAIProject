# Stuttgart Heat Risk Knowledge Graph

Knowledge graph for score-based urban heat island (UHI) risk assessment in Stuttgart-Mitte. The project integrates LoD2 CityGML building geometry, Open-Meteo climate observations, OpenStreetMap vegetation indicators, and computed heat-risk assessments under a shared RDF/OWL model.

The current model no longer classifies vulnerable buildings with a simple “at least two risk factors” rule. Instead, it represents explicit `uhi:HeatRiskAssessment` instances, assigns `uhi:HeatRiskCategory` values, and classifies buildings as `uhi:VulnerableBuilding` when their building-level heat-risk assessment exceeds the project-defined threshold.

## Overview

The pipeline creates a unified knowledge graph from five data sources:

- **CityGML LoD2**: building geometry, height, footprint, roof type, function, analysis zone
- **Open-Meteo**: 2024 daily maximum temperature observations and heat-day observations using SOSA
- **OpenStreetMap**: vegetation fraction, tree count, and vegetation types per analysis zone
- **LGL Baden-Württemberg DGM1**: 1 m digital terrain model, used to derive per-zone topographic exposure
- **Copernicus Land Monitoring Service HRL**: tree cover density 2023 and imperviousness density 2024, 10 m raster

Derived indicators such as Sky View Factor, urban density, topographic exposure, impervious surface fraction, tree canopy coverage, and heat-day count are combined into zone-level and building-level heat-risk assessments.

## Data

**Building geometry source:** LGL Baden-Württemberg — <https://opengeodata.lgl-bw.de>  
**Tiles:** `LoD2_32_513_5402_1_BW`, `LoD2_32_513_5403_1_BW`, `LoD2_32_514_5402_1_BW`, `LoD2_32_514_5403_1_BW`  
**License:** Datenlizenz Deutschland – Namensnennung – Version 2.0 (dl-de/by-2-0)

Place the extracted GML files in:

```text
LoD2_32_513_5402_2_bw/
```

**Terrain source:** LGL Baden-Württemberg DGM1 (ATKIS-DGM1, ALS-derived, 2023 update) — <https://opengeodata.lgl-bw.de>
**Tiles:** `dgm1_32_513_5402_1_bw_2023.xyz`, `dgm1_32_513_5403_1_bw_2023.xyz`, `dgm1_32_514_5402_1_bw_2023.xyz`, `dgm1_32_514_5403_1_bw_2023.xyz`
**Resolution:** 1 m horizontal, ±0.15 m vertical accuracy
**Reference systems:** ETRS89 / UTM Zone 32N (horizontal), DHHN2016 (vertical)
**License:** Datenlizenz Deutschland – Zero – Version 2.0 (dl-de/zero-2-0)

Place the extracted XYZ files in:

```text
dgm1_32_513_5402_2_bw/
```

**CLMS Tree Cover & Imperviousness sources:**

- Tree Cover Density 2023, 10 m raster, clipped to NUTS-3 DE111 — <https://land.copernicus.eu/en/products/high-resolution-layer-forests-and-tree-cover>
- Imperviousness Density 2024, 10 m raster, EEA tile E42N28 (NVLCC product family) — <https://land.copernicus.eu/en/products/high-resolution-layer-imperviousness>

**Reference system:** ETRS89 / LAEA Europe (EPSG:3035)
**License:** Free, full, open access under the Copernicus license (no fees, no use restrictions; attribution required as quoted in the Attribution section).

Place the GeoTIFFs in:

```text
clms_landcover/
```

The script `clms_landcover.py` locates them automatically by filename pattern (`*TCD*.tif`, `*IMD*.tif`).

## Installation

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate      # macOS/Linux
# .venv\Scripts\activate       # Windows
pip install -r requirements.txt
```

## Pipeline

Run the scripts in order. Each enrichment step reads and writes `stuttgart_buildings.ttl`.

| Step | Script | Purpose |
|---|---|---|
| 1 | `audit.py` | Inspect raw CityGML tiles and report geometry/statistics |
| 2 | `citygml_to_rdf.py` | Convert LoD2 buildings to RDF and create building/zone triples |
| 3 | `climate_data.py` | Add Open-Meteo SOSA observations and heat-day observations |
| 4 | `osm_enrichment.py` | Add OSM vegetation fraction, tree count, and vegetation types |
| 5 | `clms_landcover.py` | Per-zone tree canopy coverage and impervious fraction from CLMS HRL |
| 6 | `terrain_dgm.py` | Compute per-zone topographic exposure from the LGL DGM1 (TPI) |
| 7 | `risk_assessment.py` | Compute zone/building heat-risk assessments and categories |
| 8 | `queries_and_viz.py` | Run SPARQL queries and generate an interactive map |

```bash
python citygml_to_rdf.py
python climate_data.py
python osm_enrichment.py
python clms_landcover.py
python terrain_dgm.py
python risk_assessment.py
python queries_and_viz.py
```

`reasoning.py` is kept as a compatibility wrapper and now delegates to `risk_assessment.py`.

## Ontology

The ontology is stored in `uhi_ontology.ttl` under the namespace:

```text
https://w3id.org/stuttgart-uhi#
```

Core classes:

- `uhi:AnalysisZone`
- `uhi:HeatRiskAssessment`
- `uhi:ZoneHeatRiskAssessment`
- `uhi:BuildingHeatRiskAssessment`
- `uhi:HeatRiskCategory`
- `uhi:VulnerableBuilding`
- `uhi:VegetationType`

Core properties:

- `uhi:hasHeatRiskAssessment`
- `uhi:hasHeatRiskScore`
- `uhi:hasIndicativeDeltaT`
- `uhi:hasRiskCategory`
- `uhi:hasSkyViewFactor`
- `uhi:hasUrbanDensity`
- `uhi:hasTopographicExposure` (replaces deprecated `uhi:hasBasinDepth`)
- `uhi:hasMeanElevation`, `uhi:hasMinElevation`, `uhi:hasMaxElevation`, `uhi:hasMeanDepressionDepth` (DGM1-derived, stored for traceability)
- `uhi:hasTreeCanopyCoverage` (CLMS HRL TCD-derived, used in the score)
- `uhi:hasVegetationFraction` (OSM-derived, broader land-use; kept for separate queries)
- `uhi:hasTreeCount`
- `uhi:hasImperviousSurfaceFraction` (CLMS HRL IMD-derived)
- `uhi:hasHeatDayCount`

Reused vocabularies:

- BOT for buildings and zones
- GeoSPARQL for geometry
- SOSA for climate observations

## Topographic exposure indicator

`terrain_dgm.py` derives `uhi:hasTopographicExposure` per zone from the LGL Baden-Württemberg DGM1 using the Topographic Position Index (TPI). The four 1 km × 1 km XYZ tiles are merged into a single 2 km × 2 km grid at 1 m resolution. For each cell, TPI is the difference between the cell's elevation and the mean elevation of its surrounding circular neighbourhood (300 m radius, square kernel approximation). Cells with negative TPI sit below their surroundings — i.e. in local depressions. Per zone, the indicator is computed as:

```text
mean_depression_depth = mean(max(-TPI, 0))   over the 1 km × 1 km tile  [metres]
topographic_exposure  = clamp(mean_depression_depth / 10, 0, 1)         [0..1]
```

The reference relief of 10 m corresponds to one standard deviation of TPI over the Stuttgart-Mitte grid at the 300 m scale and matches the canonical valley-cell threshold in De Reu et al. (2013). The constant is fixed rather than min-max normalised so that adding tiles to the study area does not change existing zone scores. Higher values indicate stronger basin-like depression and therefore higher heat-retention potential due to cold-air pooling and reduced ventilation, which is the dominant non-anthropogenic UHI driver in Stuttgart (Baumüller et al., 1996; Emeis et al., 2022).

### References

- Weiss, A. D. (2001). *Topographic Position and Landforms Analysis*. Poster Presentation, ESRI International User Conference, San Diego, CA, 9–13 July 2001.
- De Reu, J., Bourgeois, J., Bats, M., Zwertvaegher, A., Gelorini, V., De Smedt, P., Chu, W., Antrop, M., De Maeyer, P., Finke, P., Van Meirvenne, M., Verniers, J., & Crombé, P. (2013). Application of the topographic position index to heterogeneous landscapes. *Geomorphology*, 186, 39–49. <https://doi.org/10.1016/j.geomorph.2012.12.015>
- Wilson, J. P., & Gallant, J. C. (2000). *Terrain Analysis: Principles and Applications*. Wiley.
- Jenness, J. (2006). *Topographic Position Index (tpi_jen.avx) extension for ArcView 3.x*. Jenness Enterprises.
- Oke, T. R. (1987). *Boundary Layer Climates* (2nd ed.). Routledge.
- Baumüller, J., Hoffmann, U., & Reuter, U. (1996). *Climate Booklet for Urban Development*. Ministry of Economy Baden-Württemberg.
- Ketterer, C., & Matzarakis, A. (2014). Human-biometeorological assessment of the urban heat island in a city with complex topography – The case of Stuttgart, Germany. *Urban Climate*, 10, 573–584. <https://doi.org/10.1016/j.uclim.2014.01.003>
- Emeis, S., et al. (2022). Urban Atmospheric Boundary-Layer Structure in Complex Topography: An Empirical 3D Case Study for Stuttgart, Germany. *Frontiers in Earth Science*, 10, 840112. <https://doi.org/10.3389/feart.2022.840112>

## Tree canopy coverage and impervious surface fraction (CLMS)

`clms_landcover.py` populates per-zone tree canopy coverage (`uhi:hasTreeCanopyCoverage`) and impervious surface fraction (`uhi:hasImperviousSurfaceFraction`) from Copernicus Land Monitoring Service High Resolution Layers:

- **Tree Cover Density 2023** (10 m raster, clipped to NUTS-3 DE111 Stuttgart Stadtkreis) — values 0..100 % canopy cover per 10 m pixel.
- **Imperviousness Density 2024** (10 m raster, NVLCC product family, EEA tile E42N28) — values 0..100 % artificially sealed surface per 10 m pixel.

For each 1 km × 1 km zone, the UTM32 zone polygon is reprojected to EPSG:3035 (LAEA Europe, native CRS of both rasters), the rasters are masked to the polygon, and the mean of valid pixels is written as a fraction in [0, 1]. The new tree canopy property feeds the `(1 − treeCanopyCoverage)` term of the composite score in place of the OSM-derived vegetation fraction. The OSM-derived `uhi:hasVegetationFraction` is retained because it answers a different question (broad land-use polygons rather than satellite tree canopy).

The 0.80 hardcoded impervious-fraction fallback previously used in `risk_assessment.py` is now superseded by per-zone CLMS values. Stuttgart-Mitte zones range from 46 % (Zone_514_5402, climbing toward Bopser) to 76 % (Zone_513_5402), substantially differentiated rather than uniformly assumed.

### References

- European Union, Copernicus Land Monitoring Service (2023). *High Resolution Layer Tree Cover Density 2023, raster 10 m, Europe, yearly.* European Environment Agency. <https://land.copernicus.eu/en/products/high-resolution-layer-forests-and-tree-cover>
- European Union, Copernicus Land Monitoring Service (2024). *Non-Vegetated Land Cover Characteristics – Imperviousness Density 2024, raster 10 m, Europe.* European Environment Agency. <https://land.copernicus.eu/en/products/high-resolution-layer-imperviousness>
- Cecilia, A., Casasanta, G., Petenko, I., Conidi, A., & Argentini, S. (2022). Measuring the urban heat island of Rome through a dense weather station network and imperviousness Copernicus Land Monitoring Service data. *17th Plinius Conference on Mediterranean Risks*, Plinius17-52. <https://doi.org/10.5194/egusphere-plinius17-52>
- Półrolniczak, M., Kolendowicz, L., & Tomczyk, A. M. (2024). Urban growth's implications on land surface temperature in a medium-sized European city based on LCZ classification. *Scientific Reports*. (Quantifies LST/IMD relationship at ~0.14 K per 10 % IMD increase.)
- Copernicus Land Monitoring Service (2024). *Urban heat islands: measured, mapped and managed.* CLMS Feature Article. <https://land.copernicus.eu/en/feature-articles/urban-heat-islands-measured-mapped-and-managed>

## Validation against Theeuwes (2017)

`theeuwes_validation.py` provides an external sanity check by computing per-zone maximum urban heat island intensity using the diagnostic equation of Theeuwes et al. (2017), the closest published semi-empirical "industry-standard" UHI equation calibrated for north-western European cities (14-city observational validation):

```text
UHImax = (2 - SVF - F_veg) * (S * DTR^3 / U)^(1/4)
```

The equation uses no terrain term; it depends only on morphology (SVF, vegetation fraction) and meteorology (specific global radiation, diurnal temperature range, 10 m wind speed). For the four zones in Stuttgart-Mitte the meteorology is shared, so the ranking by Theeuwes UHImax is determined by the morphology factor `2 - SVF - F_veg`. Comparing this ranking against the composite-score ranking from `risk_assessment.py` provides an independent check that the additional terms (topographic exposure, density, imperviousness, heat days) preserve rather than distort the ordering established by validated literature. The Spearman rank correlation and the specific disagreements are printed by the script.

Reference: Theeuwes, N. E., Steeneveld, G. J., Ronda, R. J., & Holtslag, A. A. M. (2017). A diagnostic equation for the daily maximum urban heat island effect for cities in northwestern Europe. *International Journal of Climatology*, 37(1), 443–454. <https://doi.org/10.1002/joc.4717>

## Example SPARQL query

```sparql
PREFIX uhi: <https://w3id.org/stuttgart-uhi#>

SELECT ?building ?category ?score
WHERE {
  ?building uhi:hasHeatRiskAssessment ?assessment .
  ?assessment
      uhi:hasRiskCategory ?category ;
      uhi:hasHeatRiskScore ?score .
  FILTER(?category IN (uhi:HighRisk, uhi:ExtremeRisk))
}
ORDER BY DESC(?score)
```

## Outputs

- `stuttgart_buildings.ttl` — enriched RDF knowledge graph
- `stuttgart_heat_risk_map.html` — interactive Folium map of buildings and risk categories

## Attribution

- Building geometry: © LGL Baden-Württemberg, dl-de/by-2-0
- Terrain (DGM1): © LGL Baden-Württemberg, dl-de/zero-2-0
- Climate data: Open-Meteo Historical Weather API, CC BY 4.0
- OSM data: © OpenStreetMap contributors, ODbL
- Tree cover and imperviousness: This publication has been prepared using European Union's Copernicus Land Monitoring Service information.
