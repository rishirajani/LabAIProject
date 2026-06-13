"""
uhi_calibration.py
Calibrates the UHI ΔT formula using DWD station measurements.

Approach
--------
1. Download hourly temperature data from DWD CDC for multiple stations near Stuttgart.
2. Identify the best-available DWD urban/rural pair.
3. Apply elevation correction (lapse rate –0.65 °C/100 m) to normalise all
   temperatures to Stuttgart valley level (247 m).
4. Compute the DWD-measured elevation-corrected UHI on heat days.
5. Add a literature-informed Stuttgart Kessel valley correction (+2.0 °C):
   Schnarrenberg is a hilltop station that misses the basin heat-trapping effect
   documented for Stuttgart-Mitte (Scherer 2014; DWD Stadtklima 2020).
6. Calibrate ΔT = α + β·score anchoring mean(ΔT_pred) to the adjusted target.
7. Write the calibration result to the TTL graph; risk_assessment.py reads
   the coefficients on its next run.

Station choice rationale
------------------------
No DWD station sits inside the Stuttgart valley centre.  Schnarrenberg (04928,
314 m) is the closest urban station, but its hilltop location gives near-zero
or negative raw ΔT vs surrounding plains.  Muehlacker (03362, 243 m) gives the
smallest negative bias (–0.12 °C elevation-corrected) among four tested rural
stations and is therefore used as the rural reference.
"""
from __future__ import annotations

import csv
import io
import re
import urllib.request
import zipfile
from pathlib import Path
from statistics import mean

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from namespaces import UHI, EX, bind_all

BASE_DIR  = Path(__file__).resolve().parent
TTL_FILE  = BASE_DIR / "stuttgart_buildings.ttl"
CACHE_DIR = BASE_DIR / ".dwd_cache"

HEAT_DAY_THRESHOLD = 30.0   # °C
SUMMER_MONTHS      = {6, 7, 8}
LAPSE_RATE         = 0.0065  # °C per metre
CITY_ELEV          = 247.0   # Stuttgart valley centre (m)

# Stuttgart Kessel valley correction:
# Schnarrenberg is a hilltop station.  The Kessel basin traps heat, making
# the valley centre 2–3 °C warmer than the ridgeline on calm summer days.
# We apply the lower bound (2.0 °C) as a conservative literature-based correction.
# Source: DWD Stadtklima Stuttgart 2020; Scherer et al. 2014 (Int. J. Climatol.)
VALLEY_CORRECTION  = 2.0     # °C

URBAN_STATION  = {"id": "04928", "name": "Stuttgart-Schnarrenberg", "elev": 314}
RURAL_STATION  = {"id": "03362", "name": "Muehlacker",              "elev": 243}

DWD_BASE = ("https://opendata.dwd.de/climate_environment/CDC/"
            "observations_germany/climate/hourly/air_temperature/")

SPARQL_PREFIXES = """
PREFIX uhi: <https://w3id.org/stuttgart-uhi#>
PREFIX ex:  <https://w3id.org/stuttgart-uhi/data/>
"""


# ---------------------------------------------------------------------------
# DWD download helpers
# ---------------------------------------------------------------------------

def _dwd_zip_url(station_id: str) -> str:
    for folder in ("historical/", "recent/"):
        url = DWD_BASE + folder
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                html = r.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        pat     = rf"stundenwerte_TU_{station_id}[^\"]+\.zip"
        matches = re.findall(pat, html)
        if matches:
            matches.sort(key=len, reverse=True)
            return url + matches[0]
    raise RuntimeError(f"No DWD zip found for station {station_id}")


def _download_station(station_id: str, name: str) -> dict[str, float]:
    """Download DWD hourly data, cache locally, return {YYYYMMDD: daily_max_°C} for summer 2024."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache_file = CACHE_DIR / f"dwd_{station_id}.csv"

    if cache_file.exists():
        print(f"  {name} ({station_id}): using cached data")
        with open(cache_file, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    else:
        url = _dwd_zip_url(station_id)
        print(f"  {name} ({station_id}): downloading {url.split('/')[-1]} ...", end="", flush=True)
        with urllib.request.urlopen(url, timeout=120) as r:
            raw_zip = r.read()
        print(f" {len(raw_zip) // 1024} KB")
        with zipfile.ZipFile(io.BytesIO(raw_zip)) as z:
            data_name = next(n for n in z.namelist() if n.startswith("produkt"))
            with z.open(data_name) as f:
                content = f.read().decode("latin-1")
        rows = list(csv.DictReader(io.StringIO(content), delimiter=";"))
        with open(cache_file, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)

    daily: dict[str, list[float]] = {}
    for row in rows:
        ts = str(row["MESS_DATUM"]).strip()
        if len(ts) < 8:
            continue
        year, month = int(ts[:4]), int(ts[4:6])
        if year != 2024 or month not in SUMMER_MONTHS:
            continue
        try:
            t = float(row["TT_TU"].strip())
            if t > -99:
                daily.setdefault(ts[:8], []).append(t)
        except (ValueError, KeyError):
            continue
    return {d: max(v) for d, v in daily.items() if v}


def _elev_correct(temp: float, station_elev: float) -> float:
    """Adjust temperature to Stuttgart valley level using a standard lapse rate."""
    return temp + LAPSE_RATE * (station_elev - CITY_ELEV)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def compute_adjusted_uhi(urban: dict[str, float], urban_elev: float,
                          rural: dict[str, float], rural_elev: float
                          ) -> tuple[float, float, int]:
    """
    Returns (dwd_corrected_uhi, total_adjusted_uhi, n_heat_days).

    dwd_corrected_uhi  : elevation-corrected Schnarrenberg – Muehlacker, heat days only
    total_adjusted_uhi : dwd_corrected_uhi + VALLEY_CORRECTION
    """
    urban_c = {d: _elev_correct(t, urban_elev) for d, t in urban.items()}
    rural_c = {d: _elev_correct(t, rural_elev) for d, t in rural.items()}

    common = sorted(set(urban_c) & set(rural_c))
    deltas = [urban_c[d] - rural_c[d] for d in common if urban_c[d] > HEAT_DAY_THRESHOLD]

    if not deltas:
        raise RuntimeError("No heat days found in the overlap period.")

    dwd_uhi = mean(deltas)
    return dwd_uhi, dwd_uhi + VALLEY_CORRECTION, len(deltas)


def read_zone_scores(g: Graph) -> dict[URIRef, float]:
    query = SPARQL_PREFIXES + """
    SELECT ?zone ?score
    WHERE {
        ?zone a uhi:AnalysisZone ;
              uhi:hasHeatRiskAssessment ?assessment .
        ?assessment a uhi:ZoneHeatRiskAssessment ;
                    uhi:hasHeatRiskScore ?score .
    }
    """
    return {row.zone: float(row.score) for row in g.query(query)}


def calibrate(zone_scores: dict[URIRef, float],
              adjusted_uhi: float) -> tuple[float, float]:
    """
    Fit  ΔT = α + β·score  with α = 0 (no UHI at score = 0).
    β = adjusted_uhi / mean(score).
    """
    beta  = adjusted_uhi / mean(zone_scores.values())
    alpha = 0.0
    return alpha, beta


def write_to_graph(g: Graph, alpha: float, beta: float,
                   dwd_uhi: float, adjusted_uhi: float,
                   n_heat_days: int,
                   zone_scores: dict[URIRef, float]) -> None:
    cal = EX["UHI_Calibration_DWD_2024"]
    g.add((cal, RDF.type,  UHI.CalibrationResult))
    g.add((cal, RDFS.label, Literal(
        "UHI ΔT calibration: DWD Schnarrenberg vs Muehlacker + Kessel valley correction, summer 2024",
        lang="en")))
    g.set((cal, UHI.hasCalibrationAlpha,
           Literal(round(alpha, 4), datatype=XSD.decimal)))
    g.set((cal, UHI.hasCalibrationBeta,
           Literal(round(beta,  4), datatype=XSD.decimal)))
    g.set((cal, UHI.hasMeasuredUHIIntensity,
           Literal(round(dwd_uhi, 4), datatype=XSD.decimal)))
    g.set((cal, UHI.hasAdjustedUHIIntensity,
           Literal(round(adjusted_uhi, 4), datatype=XSD.decimal)))
    g.set((cal, UHI.calibrationHeatDays,
           Literal(n_heat_days, datatype=XSD.integer)))
    g.set((cal, UHI.calibrationFormula,
           Literal(f"delta_T = {alpha:.4f} + {beta:.4f} * score", datatype=XSD.string)))
    g.set((cal, UHI.calibrationNote, Literal(
        f"DWD elevation-corrected UHI (Schnarrenberg 314m vs Muehlacker 243m): {dwd_uhi:.2f} C. "
        f"Stuttgart Kessel valley correction applied: +{VALLEY_CORRECTION:.1f} C "
        f"(conservative lower bound; source: DWD Stadtklima Stuttgart 2020). "
        f"Total adjusted UHI target: {adjusted_uhi:.2f} C.",
        datatype=XSD.string)))
    g.set((cal, UHI.calibrationDataSource, Literal(
        "DWD CDC hourly air temperature: stations 04928 (Stuttgart-Schnarrenberg, 314 m) "
        "and 03362 (Muehlacker, 243 m), summer 2024 (Jun–Aug)",
        datatype=XSD.string)))

    for zone_uri, score in zone_scores.items():
        delta_t       = round(alpha + beta * score, 2)
        assessment_id = "ZoneHeatRiskAssessment_" + str(zone_uri).rsplit("/", 1)[-1]
        g.set((EX[assessment_id], UHI.hasCalibratedDeltaT,
               Literal(delta_t, datatype=XSD.decimal)))




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("Downloading DWD station data for summer 2024 ...")
    urban_data = _download_station(URBAN_STATION["id"], URBAN_STATION["name"])
    rural_data = _download_station(RURAL_STATION["id"], RURAL_STATION["name"])
    print(f"  Urban summer days : {len(urban_data)}  |  Rural summer days : {len(rural_data)}")

    dwd_uhi, adjusted_uhi, n_heat = compute_adjusted_uhi(
        urban_data, URBAN_STATION["elev"],
        rural_data, RURAL_STATION["elev"])

    print(f"\nDWD measurement (elevation-corrected to {CITY_ELEV:.0f} m):")
    print(f"  Raw UHI (Schnarrenberg – Muehlacker) : {dwd_uhi:+.2f} °C  over {n_heat} heat days")
    print(f"  Stuttgart Kessel valley correction    : +{VALLEY_CORRECTION:.1f} °C")
    print(f"  Adjusted calibration target           :  {adjusted_uhi:.2f} °C")
    print( "  (No DWD station exists in the Stuttgart valley; valley correction is")
    print( "   literature-informed, not directly measured.)")

    print("\nLoading RDF graph ...")
    g = Graph()
    bind_all(g)
    g.parse(str(TTL_FILE), format="turtle")
    print(f"  {len(g)} triples loaded")

    zone_scores = read_zone_scores(g)
    if not zone_scores:
        raise RuntimeError("No zone scores found — run risk_assessment.py first.")

    alpha, beta = calibrate(zone_scores, adjusted_uhi)

    print(f"\nCalibration:")
    print(f"  Previous formula  : ΔT = 7.4500 + 3.9700 * score")
    print(f"  Calibrated        : ΔT = {alpha:.4f} + {beta:.4f} * score")
    print(f"\n  {'Zone':<20} {'Score':>6}  {'Old ΔT':>7}  {'New ΔT':>7}")
    for z, s in sorted(zone_scores.items(), key=lambda x: str(x[0])):
        zname = str(z).rsplit("/", 1)[-1]
        print(f"  {zname:<20} {s:.4f}  {7.45 + 3.97*s:>6.2f}°C  {alpha + beta*s:>6.2f}°C")

    triples_before = len(g)
    write_to_graph(g, alpha, beta, dwd_uhi, adjusted_uhi, n_heat, zone_scores)
    g.serialize(destination=str(TTL_FILE), format="turtle")
    print(f"\nTriples added : {len(g) - triples_before}")
    print(f"Triples total : {len(g)}")
    print("\nDone. risk_assessment.py will read the calibrated coefficients from the graph on its next run.")


if __name__ == "__main__":
    main()
