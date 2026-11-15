"""City bikeway network, conflated onto the street graph, and the bike
"stress" multiplier the route page uses to prefer calm streets.

For Seattle the network is SDOT's Existing Bike Facilities, fetched by
``scripts/fetch_seattle_bikeways.py`` into ``data/sources/`` (committed,
because the City's service is not reachable from every build environment)
with each segment already carrying a ``facility`` code.  San Francisco's
SFMTA Bike Network properties are still understood, so upstream's file
works unchanged.

City layers are keyed by their own street-segment ids, which Overture does
not carry, so the match is geometric: an edge takes a facility when at least
half of the points sampled along it lie within ``MATCH_M`` of a bikeway
segment that runs roughly parallel to it.  Where several facilities match,
the most protected one wins.

Facility codes: ``path`` (off-street trail), ``separated`` (protected lane),
``buffered_lane``, ``lane`` (painted; SDOT climbing lanes too),
``neighborway`` (traffic-calmed residential street: SDOT neighborhood
greenways, SFMTA neighborways), ``route`` (signed route / sharrows).

The stress multiplier is in equivalent metres per metre: a block that feels
like 1.4 blocks.  It is a comfort scale, not a speed model, and it only ever
applies to the distance objective in bike mode when the rider asks for calm
streets, so the climbing axis of the frontier is untouched.
"""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString
from shapely.strtree import STRtree

from .config import DATA_DIR
from .utils import get_logger, step

log = get_logger("sf_flat_routes.bikeways")

BIKEWAYS_GEOJSON = DATA_DIR / "sources" / "seattle_bike_facilities.geojson"

#: SDOT facility statuses left out: not yet rideable
SKIP_STATUS = frozenset({"UNDERCONS"})

#: distance within which an edge sample point counts as "on" a bikeway segment
MATCH_M = 12.0
#: and the largest angle between the two for them to count as the same street
MATCH_DEG = 25.0
#: spacing of sample points along an edge
SAMPLE_M = 10.0

#: facility code per edge, most protected first (higher rank wins a tie)
FACILITIES = ("", "route", "neighborway", "lane", "buffered_lane", "separated", "path")
_RANK = {f: i for i, f in enumerate(FACILITIES)}

#: Which facilities an edge may take, by its road class.  Without this a
#: lane on a street also lands on the sidewalks drawn a few metres either
#: side of it (Seattle's OSM sidewalks are mapped as separate footways),
#: making them look like protected lanes; and a trail beside a road lends
#: the road its rating.  Off-street trails go only on off-street edges,
#: painted lanes, greenways and sharrows only on streets, and a protected
#: lane on either (OSM maps many as their own cycleway).  Classes not
#: listed take any facility.
_OFF_STREET = frozenset({"path", "separated"})
_ON_STREET = frozenset({"separated", "buffered_lane", "lane", "neighborway", "route"})
CLASS_FACILITIES = {
    "cycleway": _OFF_STREET, "path": _OFF_STREET, "track": _OFF_STREET,
    "footway": frozenset({"path"}), "pedestrian": frozenset({"path"}),
    "steps": frozenset(), "bridleway": frozenset(),
    **{c: _ON_STREET for c in ("motorway", "trunk", "primary", "secondary",
                               "tertiary", "residential", "unclassified",
                               "living_street", "service")},
}

#: comfort multiplier by road class when there is no bikeway on the street
CLASS_STRESS = {
    "cycleway": 0.8, "living_street": 0.9, "path": 1.0, "residential": 1.0,
    "unclassified": 1.0, "track": 1.1, "service": 1.1, "unknown": 1.1,
    "pedestrian": 1.2, "tertiary": 1.2, "footway": 1.3, "secondary": 1.4,
    "primary": 1.6, "trunk": 2.0,
}
#: and by facility where there is one; a class III route keeps the street's
#: own rating, slightly softened and never worse than a tertiary street,
#: because sharrows do not change the traffic but the city did pick the street
FACILITY_STRESS = {"path": 0.8, "separated": 0.8, "buffered_lane": 0.9,
                   "lane": 1.0, "neighborway": 0.9}
ROUTE_SOFTEN = 0.95
ROUTE_CAP = 1.2


def _facility(props: dict) -> str:
    """Facility code for one bikeway feature ('' to drop it)."""
    if "facility" in props:                       # pre-mapped (Seattle / SDOT)
        if (props.get("status") or "").upper() in SKIP_STATUS:
            return ""
        fac = props.get("facility") or ""
        return fac if fac in _RANK else ""
    sym = (props.get("symbology") or "").upper()
    cls = (props.get("facility_t") or "").upper()
    if sym == "BIKE PATH" or cls == "CLASS I":
        return "path"
    if sym == "SEPARATED BIKEWAY" or cls == "CLASS IV":
        return "separated"
    if sym == "BIKE LANE" or cls == "CLASS II":
        return "buffered_lane" if (props.get("buffered") or "").upper() == "YES" else "lane"
    if sym == "NEIGHBORWAY":
        return "neighborway"
    if sym == "BIKE ROUTE" or cls == "CLASS III":
        return "route"
    return ""


def load_bikeways(path: Path = BIKEWAYS_GEOJSON) -> gpd.GeoDataFrame:
    """City bikeway segments with a ``facility`` column, in WGS84."""
    with open(path) as fh:
        data = json.load(fh)
    rows = []
    for f in data["features"]:
        g = f.get("geometry")
        if not g:
            continue
        if g["type"] == "LineString":
            parts = [g["coordinates"]]
        elif g["type"] == "MultiLineString":
            parts = g["coordinates"]
        else:
            continue
        props = f.get("properties") or {}
        fac = _facility(props)
        if not fac:
            continue
        street = props.get("street") or props.get("streetname") or ""
        for part in parts:
            if len(part) >= 2:
                rows.append({"facility": fac, "street": street,
                             "geometry": LineString([c[:2] for c in part])})
    gdf = gpd.GeoDataFrame(rows, crs="EPSG:4326")
    log.info("city bikeways (%s): %d segments, %s", path.name, len(gdf),
             dict(gdf["facility"].value_counts()))
    return gdf


def _segments(line: LineString) -> np.ndarray:
    c = np.asarray(line.coords)[:, :2]
    return np.c_[c[:-1], c[1:]]            # x0 y0 x1 y1 per segment


def _sample(line: LineString, spacing: float) -> tuple[np.ndarray, np.ndarray]:
    """Points along the line and the local bearing (radians) at each."""
    n = max(2, int(line.length // spacing) + 1)
    d = np.linspace(0.0, line.length, n)
    # bearing from a short step along the line, so curves get local headings
    eps = min(1.0, line.length / 2)
    pts = [line.interpolate(x) for x in d]
    ahead = [line.interpolate(min(x + eps, line.length)) for x in d]
    behind = [line.interpolate(max(x - eps, 0.0)) for x in d]
    xy = np.array([(p.x, p.y) for p in pts])
    br = np.arctan2([a.y - b.y for a, b in zip(ahead, behind)],
                    [a.x - b.x for a, b in zip(ahead, behind)])
    return xy, br


def conflate(edges: gpd.GeoDataFrame, bikeways: gpd.GeoDataFrame | None = None) -> pd.Series:
    """Facility per edge ('' where the street has none), aligned to ``edges``."""
    if bikeways is None:
        bikeways = load_bikeways()
    bw = bikeways.to_crs(edges.crs)
    # one straight piece per bikeway vertex pair, so the heading test is local
    pieces, piece_fac = [], []
    for fac, geom in zip(bw["facility"], bw.geometry):
        for x0, y0, x1, y1 in _segments(geom):
            pieces.append(LineString([(x0, y0), (x1, y1)]))
            piece_fac.append(fac)
    piece_fac = np.array(piece_fac)
    piece_br = np.array([np.arctan2(p.coords[1][1] - p.coords[0][1],
                                    p.coords[1][0] - p.coords[0][0]) for p in pieces])
    tree = STRtree(pieces)
    out = np.full(len(edges), "", dtype=object)
    cand = edges.index[edges["bike_ok"].fillna(False).astype(bool)] \
        if "bike_ok" in edges else edges.index
    edge_cls = edges["cls"] if "cls" in edges else pd.Series(None, index=edges.index)
    with step(f"conflating {len(pieces)} bikeway pieces onto {len(cand)} edges", log):
        for i in cand:
            geom = edges.geometry.loc[i]
            if geom is None or geom.is_empty or geom.length < 1.0:
                continue
            allowed = CLASS_FACILITIES.get(edge_cls.loc[i])
            xy, br = _sample(geom, SAMPLE_M)
            from shapely.geometry import Point
            pts = [Point(x, y) for x, y in xy]
            hit = np.zeros(len(pts), dtype=int) - 1
            for k, p in enumerate(pts):
                best = -1
                for j in tree.query(p.buffer(MATCH_M)):
                    if allowed is not None and piece_fac[j] not in allowed:
                        continue
                    if pieces[j].distance(p) > MATCH_M:
                        continue
                    da = abs((piece_br[j] - br[k] + np.pi / 2) % np.pi - np.pi / 2)
                    if np.degrees(da) > MATCH_DEG:
                        continue
                    if best < 0 or _RANK[piece_fac[j]] > _RANK[piece_fac[best]]:
                        best = j
                hit[k] = best
            matched = hit >= 0
            if matched.sum() * 2 >= len(pts):
                facs = piece_fac[hit[matched]]
                out[edges.index.get_loc(i)] = max(set(facs), key=lambda f: _RANK[f])
    s = pd.Series(out, index=edges.index, name="city_facility")
    log.info("edges with a city bikeway facility: %s", dict(s[s != ""].value_counts()))
    return s


def stress(edges: gpd.GeoDataFrame, facility: pd.Series | None = None) -> np.ndarray:
    """Bike comfort multiplier per edge (1.0 = an ordinary residential block)."""
    cls = edges["cls"].fillna("unknown").to_numpy()
    m = np.array([CLASS_STRESS.get(c, 1.1) for c in cls])
    if facility is None:
        facility = edges["city_facility"] if "city_facility" in edges else pd.Series("", index=edges.index)
    fac = facility.fillna("").to_numpy()
    for f, v in FACILITY_STRESS.items():
        m[fac == f] = np.minimum(m[fac == f], v)
    r = fac == "route"
    m[r] = np.maximum(0.9, np.minimum(m[r], ROUTE_CAP) * ROUTE_SOFTEN)
    return m
