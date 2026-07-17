#!/usr/bin/env python3
"""
intervention_example.py — "What could be changed?" Counterfactual greening
scenarios computed on the 100 m grid cells.

The knowledge graph stores every indicator explicitly per cell, so intervention
scenarios are simple counterfactuals: modify an indicator, recompute the same
composite score, and compare categories. This answers the planner question the
satellite cannot: "if we green this area, which cells drop out of ExtremeRisk?"

Scenarios (applied to every cell):
  A  Street-tree programme  : tree canopy +20 percentage points (capped at 1.0)
  B  De-sealing programme   : imperviousness -15 percentage points (floor 0.0)
  C  Combined               : A + B together

Outputs a before/after category table, the number of cells changing category,
and the top-10 highest-impact intervention candidates (ExtremeRisk cells with
the most improvable canopy/imperviousness). With --plot, renders a before/after
cell map (intervention_map.png).

Usage:  python intervention_example.py [--plot]
Reads:  stuttgart_buildings.ttl (with uhi:GridCell instances from subzone_grid.py)
"""

import argparse
import os
import re
import sys

import numpy as np

try:
    from namespaces import UHI, GEO
except ImportError:
    from rdflib import Namespace
    UHI = Namespace("https://w3id.org/stuttgart-uhi#")
    GEO = Namespace("http://www.opengis.net/ont/geosparql#")

TTL_FILE = "stuttgart_buildings.ttl"

# Weights + thresholds — identical to risk_assessment.py / subzone_grid.py
W = dict(svf=0.35, dens=0.20, topo=0.15, canopy=0.10, imperv=0.10, heat=0.10)
MAX_HEAT_DAYS = 30.0
MEDIUM, HIGH, EXTREME = 0.25, 0.35, 0.50

CAT_NAMES = ["LowRisk", "MediumRisk", "HighRisk", "ExtremeRisk"]


def clamp(v, lo=0.0, hi=1.0):
    return np.minimum(hi, np.maximum(lo, v))


def score(ind):
    return clamp(
        W["svf"] * (1 - ind["svf"]) + W["dens"] * ind["dens"] +
        W["topo"] * ind["topo"] + W["canopy"] * (1 - ind["canopy"]) +
        W["imperv"] * ind["imperv"] + W["heat"] * ind["heat"]
    )


def cat(s):
    c = np.zeros(s.shape, dtype=int)
    c[s >= MEDIUM] = 1
    c[s >= HIGH] = 2
    c[s >= EXTREME] = 3
    return c


def read_cells(ttl):
    from rdflib import Graph
    g = Graph()
    g.parse(ttl, format="turtle")
    pfx = f"PREFIX uhi: <{UHI}>\nPREFIX geo: <{GEO}>\n"

    heat_by_zone = {}
    for r in g.query(pfx + """
        SELECT ?z ?h WHERE { ?z a uhi:AnalysisZone . OPTIONAL { ?z uhi:hasHeatDayCount ?h } }"""):
        zid = str(r.z).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        heat_by_zone[zid] = float(r.h) if r.h is not None else 0.0

    parent = {}
    for r in g.query(pfx + "SELECT ?c ?z WHERE { ?c a uhi:GridCell ; uhi:hasParentZone ?z }"):
        cid = str(r.c).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        parent[cid] = str(r.z).rsplit("/", 1)[-1].rsplit("#", 1)[-1]

    poly = re.compile(r"POLYGON\(\(([^)]+)\)\)")
    ids, svf, dens, topo, canopy, imperv, heat, cx, cy = [], [], [], [], [], [], [], [], []
    for r in g.query(pfx + """
        SELECT ?c ?svf ?dens ?topo ?can ?imp ?wkt WHERE {
            ?c a uhi:GridCell ;
               uhi:hasSkyViewFactor ?svf ; uhi:hasUrbanDensity ?dens ;
               uhi:hasTopographicExposure ?topo ; uhi:hasTreeCanopyCoverage ?can ;
               uhi:hasImperviousSurfaceFraction ?imp ; geo:hasGeometry ?gm .
            ?gm geo:asWKT ?wkt . }"""):
        cid = str(r.c).rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        m = poly.search(str(r.wkt))
        if not m:
            continue
        xs, ys = [], []
        for pair in m.group(1).split(","):
            a, b = pair.strip().split()
            xs.append(float(a)); ys.append(float(b))
        ids.append(cid)
        svf.append(float(r.svf)); dens.append(float(r.dens)); topo.append(float(r.topo))
        canopy.append(float(r.can)); imperv.append(float(r.imp))
        heat.append(min(1.0, heat_by_zone.get(parent.get(cid, ""), 0.0) / MAX_HEAT_DAYS))
        cx.append(sum(xs) / len(xs)); cy.append(sum(ys) / len(ys))

    return ids, dict(
        svf=np.array(svf), dens=np.array(dens), topo=np.array(topo),
        canopy=np.array(canopy), imperv=np.array(imperv), heat=np.array(heat),
    ), np.array(cx), np.array(cy)


def dist(c):
    return [int(np.sum(c == i)) for i in range(4)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--canopy", type=float, default=0.20,
                    help="canopy increase in fraction points (default 0.20)")
    ap.add_argument("--imperv", type=float, default=0.15,
                    help="imperviousness decrease in fraction points (default 0.15)")
    args = ap.parse_args()

    if not os.path.exists(TTL_FILE):
        sys.exit(f"{TTL_FILE} not found — run the pipeline (incl. subzone_grid.py) first.")

    ids, ind, cx, cy = read_cells(TTL_FILE)
    n = len(ids)
    if n == 0:
        sys.exit("No uhi:GridCell instances found — run subzone_grid.py first.")
    print(f"Loaded {n} grid cells.\n")

    base_s = score(ind)
    base_c = cat(base_s)

    scenarios = {
        f"A  Canopy +{int(args.canopy*100)}pp": {**ind, "canopy": clamp(ind["canopy"] + args.canopy)},
        f"B  Imperviousness −{int(args.imperv*100)}pp": {**ind, "imperv": clamp(ind["imperv"] - args.imperv)},
        f"C  Combined (A + B)": {**ind,
                                 "canopy": clamp(ind["canopy"] + args.canopy),
                                 "imperv": clamp(ind["imperv"] - args.imperv)},
    }

    print(f"{'Scenario':<28}{'Low':>6}{'Med':>6}{'High':>6}{'Extr':>6}"
          f"{'  improved':>11}{'  left Extreme':>15}")
    b = dist(base_c)
    print(f"{'Baseline':<28}{b[0]:>6}{b[1]:>6}{b[2]:>6}{b[3]:>6}{'—':>11}{'—':>15}")

    last_c = None
    for name, s_ind in scenarios.items():
        s = score(s_ind)
        c = cat(s)
        d = dist(c)
        improved = int(np.sum(c < base_c))
        left_ext = int(np.sum((base_c == 3) & (c < 3)))
        print(f"{name:<28}{d[0]:>6}{d[1]:>6}{d[2]:>6}{d[3]:>6}{improved:>11}{left_ext:>15}")
        last_c = c  # scenario C (last) kept for the plot

    # Top-10 intervention candidates: ExtremeRisk cells with most improvable levers
    print("\nTop intervention candidates (ExtremeRisk, most improvable):")
    improvable = (1 - ind["canopy"]) * W["canopy"] + ind["imperv"] * W["imperv"]
    idx = [i for i in np.argsort(-improvable) if base_c[i] == 3][:10]
    print(f"{'Cell':<26}{'score':>7}{'canopy':>8}{'imperv':>8}{'max Δscore':>12}")
    for i in idx:
        print(f"{ids[i]:<26}{base_s[i]:>7.3f}{ind['canopy'][i]:>8.2f}"
              f"{ind['imperv'][i]:>8.2f}{-improvable[i]:>12.3f}")

    print("\nNote: SVF and density are structural (built form) and not modifiable by")
    print("greening; the modifiable levers are canopy and imperviousness (0.10 weight")
    print("each). The model therefore quantifies both what greening can achieve and")
    print("where structural factors dominate.")

    if args.plot:
        make_plot(cx, cy, base_s, score(scenarios["C  Combined (A + B)"]), base_c, last_c)


def make_plot(cx, cy, s0, s1, c0, c1):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import ListedColormap, BoundaryNorm
        from matplotlib.patches import Patch, Rectangle
        from matplotlib.collections import PatchCollection
    except ImportError:
        print("[i] matplotlib not installed; skipping plot.")
        return
 
    # ---------------------------------------------------------
    # General styling (unchanged)
    # ---------------------------------------------------------
    INK = "#1E2A33"
    ACCENT = "#B05A3C"
 
    CAT_COLORS = [
        "#FFF7BC",
        "#FEC44F",
        "#F03B20",
        "#7F0000",
    ]
 
    CAT_LABELS = [
        "Low",
        "Medium",
        "High",
        "Extreme",
    ]
 
    LEFT_EXTREME_C = "#08519C"      # dark blue
    OTHER_IMPROVED_C = "#6BAED6"    # light blue
    UNCHANGED_C = "#E3E7EB"         # light grey
 
    CELL = 100.0                    # metres — matches subzone_grid.py
    FILL_ALPHA = 0.6               # let the basemap show through
 
    # ---------------------------------------------------------
    # Improvement groups (unchanged)
    # ---------------------------------------------------------
    improved = c1 < c0
    left_extreme = (c0 == 3) & (c1 < 3)
    other_improved = improved & ~left_extreme
    unchanged = ~improved
 
    n_improved = int(improved.sum())
    n_left_extreme = int(left_extreme.sum())
    n_other_improved = int(other_improved.sum())
    n_unchanged = int(unchanged.sum())
 
    # ---------------------------------------------------------
    # True-scale cell rectangles at the real UTM32 coordinates
    # ---------------------------------------------------------
    half = CELL / 2.0
 
    def cell_rects(mask=None):
        idx = range(len(cx)) if mask is None else np.flatnonzero(mask)
        return [
            Rectangle((cx[i] - half, cy[i] - half), CELL, CELL)
            for i in idx
        ]
 
    def fill_cells(ax, facecolors):
        """Draw all cells as filled 100 m squares."""
        pc = PatchCollection(
            cell_rects(),
            facecolor=facecolors,
            edgecolor="white",
            linewidth=0.3,
            alpha=FILL_ALPHA,
            zorder=3,
        )
        ax.add_collection(pc)
 
    def outline_cells(ax, mask, color, linewidth):
        """Outline a subset of cells (improvement markers)."""
        pc = PatchCollection(
            cell_rects(mask),
            facecolor="none",
            edgecolor=color,
            linewidth=linewidth,
            zorder=4,
        )
        ax.add_collection(pc)
 
    def cat_facecolors(categories):
        return [CAT_COLORS[int(k)] for k in categories]
 
    # Shared extent with a margin so basemap context (Schlossgarten,
    # Hauptbahnhof, the Neckar slope) frames the grid.
    PAD = 220.0
    xlim = (cx.min() - half - PAD, cx.max() + half + PAD)
    ylim = (cy.min() - half - PAD, cy.max() + half + PAD)
 
    def add_basemap(ax):
        """CartoDB Positron under the cells; silent no-op if unavailable."""
        try:
            import contextily as ctx
            ctx.add_basemap(
                ax,
                crs="EPSG:25832",
                source=ctx.providers.CartoDB.Positron,
                zorder=1,
                attribution_size=5,
            )
            return True
        except Exception as exc:
            print(f"[i] basemap unavailable ({exc}); plain background.")
            return False
 
    def finish_axis(ax, title):
        ax.set_xlim(*xlim)
        ax.set_ylim(*ylim)
        ax.set_aspect("equal")
        ax.set_xticks([])
        ax.set_yticks([])
        ax.set_title(title, fontsize=12, color=INK, weight="bold")
 
    # ---------------------------------------------------------
    # Create figure
    # ---------------------------------------------------------
    fig, axes = plt.subplots(
        1,
        3,
        figsize=(15, 5.8),
    )
 
    # ---------------------------------------------------------
    # Panel 1: Baseline risk map
    # ---------------------------------------------------------
    fill_cells(axes[0], cat_facecolors(c0))
    finish_axis(axes[0], "Baseline risk category")
    basemap_ok = add_basemap(axes[0])
 
    axes[0].legend(
        handles=[Patch(facecolor=c, edgecolor="none", label=l)
                 for c, l in zip(CAT_COLORS, CAT_LABELS)],
        loc="upper center", bbox_to_anchor=(0.5, -0.03),
        ncol=4, fontsize=8, frameon=False,
        columnspacing=0.9, handlelength=1.2, handletextpad=0.5,
    )
 
    # ---------------------------------------------------------
    # Panel 2: After greening (fills = new category, outlines = change)
    # ---------------------------------------------------------
    fill_cells(axes[1], cat_facecolors(c1))
    outline_cells(axes[1], other_improved, OTHER_IMPROVED_C, 1.2)
    outline_cells(axes[1], left_extreme, LEFT_EXTREME_C, 1.8)
    finish_axis(axes[1], "After greening")
    add_basemap(axes[1])
 
    axes[1].legend(
        handles=[Patch(facecolor=c, edgecolor="none", label=l)
                 for c, l in zip(CAT_COLORS, CAT_LABELS)] + [
            Patch(facecolor="none", edgecolor=LEFT_EXTREME_C,
                  linewidth=1.8, label="left Extreme"),
            Patch(facecolor="none", edgecolor=OTHER_IMPROVED_C,
                  linewidth=1.2, label="improved"),
        ],
        loc="upper center", bbox_to_anchor=(0.5, -0.03),
        ncol=3, fontsize=8, frameon=False,
        columnspacing=0.9, handlelength=1.2, handletextpad=0.5,
    )
 
    # ---------------------------------------------------------
    # Panel 3: Improvement breakdown
    # ---------------------------------------------------------
    panel3_colors = np.full(len(c0), UNCHANGED_C, dtype=object)
    panel3_colors[other_improved] = OTHER_IMPROVED_C
    panel3_colors[left_extreme] = LEFT_EXTREME_C
 
    fill_cells(axes[2], list(panel3_colors))
    finish_axis(
        axes[2],
        f"Category improvement breakdown ({n_improved})",
    )
    add_basemap(axes[2])
 
    axes[2].legend(
        handles=[
            Patch(facecolor=LEFT_EXTREME_C, edgecolor="none",
                  label=f"left Extreme ({n_left_extreme})"),
            Patch(facecolor=OTHER_IMPROVED_C, edgecolor="none",
                  label=f"improved ({n_other_improved})"),
            Patch(facecolor=UNCHANGED_C, edgecolor="none",
                  label=f"unchanged ({n_unchanged})"),
        ],
        loc="upper center", bbox_to_anchor=(0.5, -0.03),
        ncol=3, fontsize=8, frameon=False,
        columnspacing=0.9, handlelength=1.2, handletextpad=0.5,
    )
 
    # ---------------------------------------------------------
    # Overall figure title
    # ---------------------------------------------------------
    fig.suptitle(
        (
            f"Greening counterfactual: "
            f"{n_improved} of {len(c0)} cells improve, "
            f"{n_left_extreme} leave ExtremeRisk"
        ),
        fontsize=14,
        color=ACCENT,
        weight="bold",
        y=1.00,
    )
 
    fig.subplots_adjust(
        bottom=0.24,
        wspace=0.10,
    )
 
    plt.savefig(
        "intervention_map.png",
        dpi=200,
        bbox_inches="tight",
        facecolor="white",
    )
 
    plt.close(fig)
 
    suffix = "with basemap" if basemap_ok else "WITHOUT basemap (offline?)"
    print(f"Saved intervention_map.png ({suffix})")
    

if __name__ == "__main__":
    main()
    