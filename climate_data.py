import json
import time
import urllib.request
import urllib.parse
from pathlib import Path

from rdflib import Graph, Namespace, URIRef, Literal
from rdflib.namespace import RDF, RDFS, XSD

UHI   = Namespace("http://example.org/uhi#")
SOSA  = Namespace("http://www.w3.org/ns/sosa/")
EX    = Namespace("http://example.org/data#")
BOT   = Namespace("https://w3id.org/bot#")
GEO   = Namespace("http://www.opengis.net/ont/geosparql#")
ALKIS = Namespace("http://example.org/alkis#")

# Zone centres in WGS84, converted from ETRS89 UTM32 tile centroids
ZONES = {
    EX.Zone_513_5402: {"lat": 48.7572, "lon": 9.1715, "label": "Stuttgart tile 513/5402 (SW)"},
    EX.Zone_513_5403: {"lat": 48.7662, "lon": 9.1715, "label": "Stuttgart tile 513/5403 (NW)"},
    EX.Zone_514_5402: {"lat": 48.7572, "lon": 9.1860, "label": "Stuttgart tile 514/5402 (SE)"},
    EX.Zone_514_5403: {"lat": 48.7662, "lon": 9.1860, "label": "Stuttgart tile 514/5403 (NE)"},
}

HEAT_DAY_THRESHOLD = 30.0   # DWD definition of a heat day (Hitzetag)
START_DATE = "2024-01-01"
END_DATE   = "2024-12-31"


def fetch_temperatures(lat: float, lon: float) -> dict[str, float | None]:
    """Fetch daily max temperature from Open-Meteo archive API. No API key required."""
    params = {
        "latitude":   lat,
        "longitude":  lon,
        "start_date": START_DATE,
        "end_date":   END_DATE,
        "daily":      "temperature_2m_max",
        "timezone":   "Europe/Berlin",
    }
    url = "https://archive-api.open-meteo.com/v1/archive?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return dict(zip(data["daily"]["time"], data["daily"]["temperature_2m_max"]))


def safe_zone_id(zone_uri: URIRef) -> str:
    return str(zone_uri).split("#")[-1]


def add_climate_triples(g: Graph, zone_uri: URIRef, temps: dict[str, float | None]) -> dict:
    zone_id   = safe_zone_id(zone_uri)
    heat_days = []

    sensor_uri = EX[f"Sensor_{zone_id}"]
    g.add((sensor_uri, RDF.type,    SOSA.Sensor))
    g.add((sensor_uri, RDFS.label,  Literal(f"Open-Meteo virtual sensor for {zone_id}")))
    g.add((sensor_uri, RDFS.comment, Literal(
        "Data source: Open-Meteo Historical Weather API "
        "(https://open-meteo.com/en/docs/historical-weather-api). "
        "Variable: temperature_2m_max. Timezone: Europe/Berlin."
    )))

    for date_str, temp in temps.items():
        if temp is None:
            continue

        obs_uri = EX[f"Obs_{zone_id}_{date_str}"]
        g.add((obs_uri, RDF.type,                  SOSA.Observation))
        g.add((obs_uri, RDF.type,                  UHI.TemperatureObservation))
        g.add((obs_uri, SOSA.hasFeatureOfInterest, zone_uri))
        g.add((obs_uri, SOSA.observedProperty,     UHI.DailyMaxTemperature))
        g.add((obs_uri, SOSA.hasSimpleResult,      Literal(round(temp, 1), datatype=XSD.decimal)))
        g.add((obs_uri, SOSA.resultTime,           Literal(date_str, datatype=XSD.date)))
        g.add((obs_uri, SOSA.madeBySensor,         sensor_uri))

        if temp > HEAT_DAY_THRESHOLD:
            g.add((obs_uri, RDF.type, UHI.HeatDayObservation))
            heat_days.append((date_str, temp))

    return {
        "obs_count": sum(1 for t in temps.values() if t is not None),
        "heat_days": len(heat_days),
        "max_temp":  max(t for t in temps.values() if t is not None),
        "hottest":   max(heat_days, key=lambda x: x[1]) if heat_days else None,
    }


def main():
    TTL_FILE = Path(r"D:\Downloads\AI Lab Project\stuttgart_buildings.ttl")

    print("Loading existing graph ...")
    g = Graph()
    for prefix, ns in [("uhi", UHI), ("sosa", SOSA), ("ex", EX),
                       ("bot", BOT), ("geo", GEO), ("alkis", ALKIS), ("xsd", XSD)]:
        g.bind(prefix, ns)
    g.parse(str(TTL_FILE), format="turtle")
    triples_before = len(g)
    print(f"  {triples_before} triples loaded")

    g.add((UHI.DailyMaxTemperature, RDF.type,    SOSA.ObservableProperty))
    g.add((UHI.DailyMaxTemperature, RDFS.label,  Literal("Daily maximum air temperature")))
    g.add((UHI.DailyMaxTemperature, RDFS.comment, Literal("Unit: degrees Celsius")))

    print(f"\nFetching 2024 climate data for {len(ZONES)} zones ...")
    all_stats = {}

    for zone_uri, meta in ZONES.items():
        zone_id = safe_zone_id(zone_uri)
        print(f"  {zone_id} ... ", end="", flush=True)
        temps = fetch_temperatures(meta["lat"], meta["lon"])
        stats = add_climate_triples(g, zone_uri, temps)
        all_stats[zone_id] = stats
        print(f"{stats['obs_count']} obs, {stats['heat_days']} heat days, max={stats['max_temp']}C")
        time.sleep(0.5)  # respect Open-Meteo rate limit

    g.serialize(destination=str(TTL_FILE), format="turtle")

    print(f"\nTriples added  : {len(g) - triples_before}")
    print(f"Triples total  : {len(g)}")
    print(f"\n{'Zone':<25} {'Obs':>6} {'HeatDays':>9} {'MaxTemp':>8} {'HottestDay':>12}")
    for zone_id, s in all_stats.items():
        hottest = s["hottest"][0] if s["hottest"] else "-"
        print(f"  {zone_id:<23} {s['obs_count']:>6} {s['heat_days']:>9} {s['max_temp']:>7.1f}C  {hottest:>12}")


if __name__ == "__main__":
    main()
