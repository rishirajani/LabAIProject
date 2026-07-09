import xml.etree.ElementTree as ET
from pathlib import Path
from collections import Counter

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "LoD2_32_513_5402_2_bw"
TILES = sorted(DATA_DIR.glob("*.gml"))

NS = {
    "core": "http://www.opengis.net/citygml/1.0",
    "bldg": "http://www.opengis.net/citygml/building/1.0",
    "gml":  "http://www.opengis.net/gml",
    "gen":  "http://www.opengis.net/citygml/generics/1.0",
    "app":  "http://www.opengis.net/citygml/appearance/1.0",
}

ROOF_TYPE_LABELS = {
    "1000": "Flat roof",
    "2100": "Monopitch roof",
    "2200": "Duopitch / gabled",
    "2300": "Hip roof",
    "2400": "Hipped gable",
    "3100": "Barrel vault",
    "3200": "Dome",
    "5000": "Mixed/complex form (Mischform)",
    "9999": "Unknown",
}

ALKIS_FUNCTION_PREFIX = {
    "31001": "Residential",
    "31002": "Residential (mixed)",
    "31003": "Residential (farm)",
    "32001": "Business / commercial",
    "32002": "Office",
    "33001": "Industrial",
    "34001": "Transport",
    "35001": "Religious",
    "36001": "Public / administrative",
    "37001": "Agricultural",
    "38001": "Recreation / sport",
    "39001": "Other",
}


def alkis_function_label(code: str) -> str:
    if not code:
        return "missing"
    prefix = code[:5] if "_" not in code else code.split("_")[0][:5]
    for k, v in ALKIS_FUNCTION_PREFIX.items():
        if prefix.startswith(k[:4]):
            return v
    return f"Unknown ({prefix})"


def polygon_area_2d(pos_list: list[float]) -> float:
    """Shoelace on XY coordinates; pos_list is a flat X Y Z X Y Z... sequence."""
    coords = [(pos_list[i], pos_list[i + 1]) for i in range(0, len(pos_list) - 3, 3)]
    n = len(coords)
    if n < 3:
        return 0.0
    area = sum(
        coords[i][0] * coords[(i + 1) % n][1] - coords[(i + 1) % n][0] * coords[i][1]
        for i in range(n)
    )
    return abs(area) / 2.0


def parse_tile(path: Path) -> list[dict]:
    tree = ET.parse(path)
    root = tree.getroot()
    records = []

    for bldg in root.findall(".//bldg:Building", NS):
        rec = {
            "gml_id":       bldg.get("{http://www.opengis.net/gml}id", ""),
            "tile":         path.stem,
            "height":       None,
            "roof_type":    None,
            "function":     None,
            "footprint_m2": None,
            "gemeinde":     None,
            "has_geometry": False,
        }

        h = bldg.find("bldg:measuredHeight", NS)
        if h is not None and h.text:
            try:
                rec["height"] = float(h.text)
            except ValueError:
                pass

        rt = bldg.find("bldg:roofType", NS)
        if rt is not None and rt.text:
            rec["roof_type"] = rt.text.strip()

        fn = bldg.find("bldg:function", NS)
        if fn is not None and fn.text:
            rec["function"] = fn.text.strip()

        for attr in bldg.findall("gen:stringAttribute", NS):
            if attr.get("name", "") == "Gemeindeschluessel":
                val_el = attr.find("gen:value", NS)
                if val_el is not None:
                    rec["gemeinde"] = val_el.text

        ground_area = 0.0
        for gs in bldg.findall(".//bldg:GroundSurface", NS):
            for pos_el in gs.findall(".//gml:posList", NS):
                if pos_el.text:
                    ground_area += polygon_area_2d(list(map(float, pos_el.text.split())))

        if ground_area > 0:
            rec["footprint_m2"] = ground_area
            rec["has_geometry"] = True
        else:
            rec["has_geometry"] = bldg.find(".//bldg:lod2Solid", NS) is not None

        records.append(rec)

    return records


all_records = []
print("=" * 60)
print("CityGML LoD2 - Tile-level summary")
print("=" * 60)

for tile in TILES:
    recs = parse_tile(tile)
    all_records.extend(recs)
    heights = [r["height"] for r in recs if r["height"] is not None]
    print(f"\n{tile.name}")
    print(f"  Buildings : {len(recs)}")
    print(f"  Has height: {len(heights)} ({100*len(heights)/len(recs):.1f}%)")
    print(f"  Has geom  : {sum(r['has_geometry'] for r in recs)}")

total = len(all_records)
print("\n" + "=" * 60)
print(f"TOTAL BUILDINGS (all 4 tiles): {total}")
print("=" * 60)

heights = sorted(r["height"] for r in all_records if r["height"] is not None)
print(f"\nHeight (m) - {len(heights)} buildings have a value")
print(f"  min   : {heights[0]:.2f}")
print(f"  median: {heights[len(heights)//2]:.2f}")
print(f"  mean  : {sum(heights)/len(heights):.2f}")
print(f"  max   : {heights[-1]:.2f}")
print(f"  missing: {total - len(heights)}")

print(f"\nRoof type distribution ({total} buildings)")
rt_counter = Counter(r["roof_type"] for r in all_records)
for code, count in rt_counter.most_common():
    code_str = str(code) if code is not None else "None"
    label = ROOF_TYPE_LABELS.get(code_str, f"code {code_str}")
    print(f"  {code_str:>6}  {label:<30} {count:>5}  ({100*count/total:.1f}%)")

print(f"\nBuilding function distribution (top 10)")
fn_counter = Counter(r["function"] for r in all_records)
for code, count in fn_counter.most_common(10):
    label = alkis_function_label(str(code) if code else "")
    print(f"  {str(code):<20} {label:<25} {count:>5}  ({100*count/total:.1f}%)")

fp = sorted(r["footprint_m2"] for r in all_records if r["footprint_m2"] is not None)
print(f"\nFootprint area (m2) - {len(fp)} buildings computed")
if fp:
    print(f"  min   : {fp[0]:.1f}")
    print(f"  median: {fp[len(fp)//2]:.1f}")
    print(f"  mean  : {sum(fp)/len(fp):.1f}")
    print(f"  max   : {fp[-1]:.1f}")
    print(f"  >1000m2: {sum(1 for x in fp if x > 1000)}")
    print(f"  >5000m2: {sum(1 for x in fp if x > 5000)}")

gemeinde = Counter(r["gemeinde"] for r in all_records)
print(f"\nGemeindeschluessel (municipality codes)")
for code, count in gemeinde.most_common():
    print(f"  {code}  -> {count} buildings")

print(f"\nMissing value summary")
print(f"  height missing      : {sum(1 for r in all_records if r['height'] is None)}")
print(f"  roof_type missing   : {sum(1 for r in all_records if r['roof_type'] is None)}")
print(f"  function missing    : {sum(1 for r in all_records if r['function'] is None)}")
print(f"  footprint missing   : {sum(1 for r in all_records if r['footprint_m2'] is None)}")
print(f"  geometry missing    : {sum(1 for r in all_records if not r['has_geometry'])}")

print("\nDone.")
