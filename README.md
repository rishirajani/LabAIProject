# Stuttgart Urban Heat Island Knowledge Graph

An RDF/OWL knowledge graph for cross-source urban heat-risk assessment in Stuttgart-Mitte. Five heterogeneous urban data sources are integrated through shared zone and building URIs into a single semantic model, enabling SPARQL-based querying, explanation, and decision support for heat-vulnerable buildings.

The graph currently covers four 1 km² analysis zones in Stuttgart-Mitte (4 km² total), containing 5,801 buildings and approximately 159,000 triples.

## Goal and contribution

**Goal:** Rank and identify heat-vulnerable buildings in dense urban areas.

**Contribution:** A knowledge-representation framework that links building geometry, climate observations, terrain, satellite land cover, and street-level vegetation into one queryable graph. The score is one application of the framework; the explicit, extensible, multi-source integration is the contribution.

## Data sources

| Source | Resolution | Provider | What it contributes |
|---|---|---|---|
| CityGML LoD2 | per building | LGL Baden-Württemberg | Geometry, height, footprint, roof type, zone membership |
| Open-Meteo | daily per zone | Open-Meteo API | 2024 maximum temperatures, heat-day observations (SOSA) |
| OpenStreetMap | polygon-level | Overpass API | Vegetation fraction, tree count, vegetation types |
| DGM1 terrain | 1 m | LGL Baden-Württemberg | Topographic Position Index → topographic exposure |
| CLMS HRL | 10 m | Copernicus Land Monitoring | Tree Cover Density 2023, Imperviousness Density 2024 |
| DWD CDC | station-level | Deutscher Wetterdienst | Hourly temperatures for ΔT calibration |

## Ontology

Namespace: `https://w3id.org/stuttgart-uhi#` (structured for w3id.org persistent-identifier registration).

**Reused vocabularies:** BOT (buildings/zones), GeoSPARQL (geometry), SOSA/SSN (observations), OWL/RDFS.

**Custom classes:**

| Class | Role |
|---|---|
| `uhi:AnalysisZone` | Spatial unit for indicator aggregation (subclasses: `GridCell`, `LoD2Tile`, `HighRiskZone`) |
| `uhi:HeatRiskAssessment` | Assessment node with score + category (subclasses: Zone, Building, GridCell variants) |
| `uhi:HeatRiskCategory` | LowRisk, MediumRisk, HighRisk, ExtremeRisk |
| `uhi:VulnerableBuilding` | Building whose assessment exceeds the risk threshold |
| `uhi:CalibrationResult` | DWD-calibrated ΔT coefficients (α, β) |
| `uhi:GridCell` | 100 m sub-zone analysis cell for fine-grained scoring |

**Key properties:** `hasSkyViewFactor`, `hasUrbanDensity`, `hasTopographicExposure`, `hasTreeCanopyCoverage`, `hasImperviousSurfaceFraction`, `hasHeatDayCount`, `hasHeatRiskScore`, `hasRiskCategory`, `hasIndicativeDeltaT`, `hasParentZone`, `hasGridResolution`, `hasCalibrationAlpha`, `hasCalibrationBeta`.

## Composite risk score

```
score = 0.35·(1−SVF) + 0.20·density + 0.15·topoExposure
      + 0.10·(1−canopy) + 0.10·imperviousness + 0.10·heatDayNorm

ΔT = α + β · score    (α, β calibrated from DWD station data)
```

Each indicator is grounded in published UHI literature. The indicator ordering (SVF dominant) reflects consensus from Oke (1981, 1987), Stewart & Oke (2012), and others. The exact weight values are expert-defined heuristics; sensitivity analysis confirms the risk rankings and satellite agreement are robust under ±25% perturbation.

## Pipeline

| Step | Script | Purpose |
|---|---|---|
| 1 | `citygml_to_rdf.py` | Convert LoD2 buildings to RDF (building/zone triples) |
| 2 | `climate_data.py` | Fetch Open-Meteo temperatures as SOSA observations |
| 3 | `osm_enrichment.py` | Add OSM vegetation fraction, tree count, types |
| 4 | `svf_calculator.py` | Geometric SVF per building (256-ray cast vs LoD2 surfaces) |
| 5 | `clms_landcover.py` | Per-zone tree canopy + imperviousness from CLMS HRL 10 m |
| 6 | `terrain_dgm.py` | Per-zone topographic exposure from DGM1 via `terrain_tpi.py` |
| 7 | `risk_assessment.py` | Zone + building assessments (1st pass, default ΔT) |
| 8 | `uhi_calibration.py` | Calibrate ΔT from DWD stations; write α/β to graph |
| 9 | `risk_assessment.py` | Re-apply assessments with calibrated ΔT (2nd pass) |
| 10 | `subzone_grid.py` | 100 m grid-cell scoring (400 `uhi:GridCell` instances) |
| 11 | `queries_and_viz.py` | SPARQL queries + interactive Folium map |

```bash
# Full pipeline (automatic venv management):
python run.py

# Or manually:
source .venv/bin/activate
python citygml_to_rdf.py
python climate_data.py
python osm_enrichment.py
python svf_calculator.py
python clms_landcover.py
python terrain_dgm.py
python risk_assessment.py
python uhi_calibration.py
python risk_assessment.py
python subzone_grid.py
python queries_and_viz.py
```

**Shared modules:**

| Module | Used by |
|---|---|
| `terrain_tpi.py` | `terrain_dgm.py`, `subzone_grid.py` — DGM1 loading + TPI computation |
| `namespaces.py` | All scripts — canonical namespace definitions |

## Sub-zone grid (100 m)

`subzone_grid.py` subdivides each 1 km² zone into a 10 × 10 grid of 100 m cells. Four of the six indicators are resolved per cell at their native resolution: tree canopy (10 m CLMS), imperviousness (10 m CLMS), sky view factor (per-building, averaged per cell), and topographic exposure (1 m DGM1 via `terrain_tpi.py`). Heat-day count remains at zone level. Each cell is written to the graph as a `uhi:GridCell` with its own `uhi:GridCellHeatRiskAssessment`, producing a continuous risk gradient instead of four flat zone blocks. The 100 m resolution was chosen to match the effective thermal resolution of Landsat for the satellite validation.

## Validation

**Theoretical — Theeuwes (2017):** `theeuwes_validation.py` compares the composite zone ranking against the diagnostic UHI_max equation of Theeuwes et al. (2017, Int. J. Climatology 37:443–454). Spearman ρ = 1.000 (shared TCD input), ρ = 0.800 (independent OSM vegetation input).

**Empirical — Landsat 8/9 surface temperature:** `lst_validation.py` correlates the model's risk scores (which use no thermal input) against observed Landsat Level-2 surface temperature from four cloud-free summer 2024 scenes (Jul 29, Jul 30, Aug 23, Aug 31).

| Scale | Spearman ρ | n |
|---|---|---|
| Zone | 1.000 | 4 |
| Grid cell (100 m) | 0.599 | 400 |
| Building | 0.507 | 5,801 |

The grid-cell ρ = 0.60 (n = 400, p < 0.001) is the statistically robust result. LST at ~10:00 UTC measures surface temperature, not air temperature, so moderate-to-strong positive ρ is the expected honest result.

**Sensitivity:** `sensitivity_analysis.py` perturbs the composite weights ±25% across 1,000 random draws. Risk rankings are preserved (Spearman ρ = 0.996) and agreement with satellite LST holds (ρ = 0.56–0.64). The model's conclusions do not depend on the exact heuristic weight values.

## Intervention scenarios

`intervention_example.py` demonstrates the decision-support capability: because every indicator is an explicit node in the graph, "what could be changed?" is a counterfactual query rather than a new study. Three greening scenarios (street trees: canopy +20 pp; de-sealing: imperviousness −15 pp; combined) are applied to all 400 grid cells and scores are recomputed with the identical model. The combined programme moves 34 of 75 ExtremeRisk cells out of the top category and identifies the highest-leverage intervention candidates (fully sealed, zero-canopy cells). SVF and density are structural (built form) and not modifiable by greening, so the model also shows where greening alone cannot help — cells where urban form dominates the risk. Scenario magnitudes are parameterised (`--canopy`, `--imperv`); `--plot` renders a before/after category map.

## Visualisation

`queries_and_viz.py` generates an interactive Folium map with zoom-dependent layers:

- **Zoom < 15:** 100 m sub-zone risk gradient (continuous YlOrRd color scale)
- **Zoom ≥ 15:** individual building markers by risk category
- **Toggleable:** flat zone heatmap with building cutouts (alternative low-zoom view)

## Installation

Python 3.11+ required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Data setup

Place the following in the project root (all gitignored):

```
LoD2_32_513_5402_2_bw/          ← CityGML GML files
dgm1_32_513_5402_2_bw/          ← DGM1 XYZ tiles
clms_landcover/                 ← CLMS HRL TCD + IMD GeoTIFFs
landsat_lst/                    ← Landsat ST_B10 + QA_PIXEL per date subfolder
    20240729/
    20240730/
    20240823/
    20240831/
```

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

| File | Generated by |
|---|---|
| `stuttgart_buildings.ttl` | Pipeline (~159k triples) |
| `stuttgart_heat_risk_map.html` | `queries_and_viz.py` |
| `sensitivity_tornado.png` | `sensitivity_analysis.py --plot` |
| `sensitivity_lst_hist.png` | `sensitivity_analysis.py --plot` |
| `lst_cell_validation.png` | `lst_validation.py --plot` |
| `validation_figure.png` | `make_validation_figure.py` |
| `intervention_map.png` | `intervention_example.py --plot` |

All outputs are gitignored and regenerated by their respective scripts.

## Attribution

- Building geometry: © LGL Baden-Württemberg, dl-de/by-2-0
- Terrain (DGM1): © LGL Baden-Württemberg, dl-de/zero-2-0
- Climate data: Open-Meteo Historical Weather API, CC BY 4.0
- OSM data: © OpenStreetMap contributors, ODbL
- Tree cover and imperviousness: This publication has been prepared using European Union's Copernicus Land Monitoring Service information.
- Landsat surface temperature: USGS/NASA Landsat 8/9 Collection 2 Level-2, courtesy of the U.S. Geological Survey.

## References

- Oke, T. R. (1981). Canyon geometry and the nocturnal urban heat island. *J. Climatol.*, 1(3), 237–254.
- Oke, T. R. (1987). *Boundary Layer Climates* (2nd ed.). Routledge.
- Stewart, I. D., & Oke, T. R. (2012). Local Climate Zones for urban temperature studies. *BAMS*, 93(12), 1879–1900.
- Oke, T. R., Mills, G., Christen, A., & Voogt, J. A. (2017). *Urban Climates*. Cambridge University Press.
- Theeuwes, N. E., et al. (2017). A diagnostic equation for the daily maximum UHI effect. *Int. J. Climatol.*, 37(1), 443–454.
- Erell, E., Pearlmutter, D., & Williamson, T. (2011). *Urban Microclimate*. Earthscan.
- Weiss, A. D. (2001). Topographic Position and Landforms Analysis. ESRI UC poster.
- De Reu, J., et al. (2013). Application of TPI to heterogeneous landscapes. *Geomorphology*, 186, 39–49.
- Emeis, S., et al. (2022). Urban ABL structure in complex topography: Stuttgart. *Front. Earth Sci.*, 10, 840112.
- Baumüller, J., Hoffmann, U., & Reuter, U. (1996). *Climate Booklet for Urban Development*. Stuttgart.
- Cecilia, A., et al. (2022). Measuring the UHI of Rome with Copernicus IMD. *Plinius17-52*.
- Półrolniczak, M., et al. (2024). Urban growth and LST based on LCZ classification. *Sci. Reports*.
- Sangiorgio, V., et al. (2020). AHP-calibrated UHI index. *Sci. Reports*, 10, 17913.
- Grüninger, M., & Fox, M. S. (1995). Methodology for ontology design. IJCAI Workshop.
- Saaty, T. L. (1980). *The Analytic Hierarchy Process*. McGraw-Hill.
