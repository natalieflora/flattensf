"""City bikeway conflation and the bike comfort multiplier."""
from __future__ import annotations

import geopandas as gpd
import numpy as np
import pandas as pd
import pytest
from shapely.geometry import LineString

from sf_flat_routes import bikeways


def test_facility_codes_follow_sfmta_class_and_symbology():
    f = bikeways._facility
    assert f({"facility_t": "CLASS I", "symbology": "BIKE PATH"}) == "path"
    assert f({"facility_t": "CLASS IV", "symbology": "SEPARATED BIKEWAY"}) == "separated"
    assert f({"facility_t": "CLASS II", "symbology": "BIKE LANE", "buffered": "NO"}) == "lane"
    assert f({"facility_t": "CLASS II", "symbology": "BIKE LANE", "buffered": "YES"}) == "buffered_lane"
    assert f({"facility_t": "CLASS III", "symbology": "BIKE ROUTE"}) == "route"
    assert f({"facility_t": "CLASS III", "symbology": "NEIGHBORWAY"}) == "neighborway"
    assert f({"facility_t": None, "symbology": None}) == ""


def test_facility_codes_from_the_seattle_extract():
    f = bikeways._facility
    assert f({"facility": "separated", "category": "BKF-PBL", "status": "INSVC"}) == "separated"
    assert f({"facility": "neighborway", "status": "PLNRECON"}) == "neighborway"
    # not rideable yet, or an unknown code: dropped
    assert f({"facility": "lane", "status": "UNDERCONS"}) == ""
    assert f({"facility": "velodrome", "status": "INSVC"}) == ""


def test_fetch_script_maps_every_sdot_category(tmp_path):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "fetch_seattle_bikeways",
        Path(__file__).resolve().parent.parent / "scripts" / "fetch_seattle_bikeways.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert set(mod.CATEGORY_FACILITY.values()) <= set(bikeways.FACILITIES) - {""}
    feats = mod.normalise([
        {"properties": {"UNITID": "BKF-2", "CATEGORY": "BKF-PBL", "MODEL_TYPE": "BKF-TWOWAY",
                        "CURRENT_STATUS": "INSVC",
                        "UNITDESC": "DEXTER AVE N BETWEEN A ST AND B ST (BKF-PBL)"},
         "geometry": {"type": "MultiLineString",
                      "coordinates": [[[-122.1234567, 47.1], [-122.2, 47.2]], [[0, 0]]]}},
        {"properties": {"UNITID": "BKF-1", "CATEGORY": "BKF-CLMB"},
         "geometry": {"type": "LineString", "coordinates": [[-122.3, 47.6], [-122.3, 47.7]]}},
        {"properties": {"UNITID": "BKF-3", "CATEGORY": None},
         "geometry": {"type": "LineString", "coordinates": [[-122.3, 47.6], [-122.3, 47.7]]}},
    ])
    assert [x["properties"]["unitid"] for x in feats] == ["BKF-1", "BKF-2"]
    climb, pbl = (x["properties"] for x in feats)
    assert climb["facility"] == "lane" and pbl["facility"] == "separated"
    assert pbl["street"] == "DEXTER AVE N" and pbl["direction"] == "twoway"
    assert feats[1]["geometry"]["coordinates"][0] == [-122.123457, 47.1]
    # and the loader reads what the script writes
    path = tmp_path / "bw.geojson"
    path.write_text('{"type":"FeatureCollection","features":' + __import__("json").dumps(feats) + "}")
    bw = bikeways.load_bikeways(path)
    assert list(bw["facility"]) == ["lane", "separated"]


def test_stress_prefers_protected_lanes_and_penalises_bare_arterials():
    edges = pd.DataFrame({
        "cls": ["primary", "primary", "secondary", "residential", "residential",
                "trunk", "cycleway", "tertiary"],
    })
    fac = pd.Series(["", "separated", "lane", "", "route", "", "", "route"])
    m = bikeways.stress(edges, fac)
    assert m[0] == 1.6                       # Geary with nothing
    assert m[1] == 0.8                       # Geary with a protected lane
    assert m[2] == 1.0                       # a painted lane feels like a quiet street
    assert m[3] == 1.0                       # the baseline
    assert m[4] == pytest.approx(0.95)       # sharrows on a quiet street
    assert m[5] == 2.0                       # 19th Ave
    assert m[6] == 0.8
    # a class III route on a bigger street never counts worse than a tertiary
    assert m[7] == pytest.approx(1.2 * bikeways.ROUTE_SOFTEN)
    assert m.min() >= 0.8 and m.max() <= 2.0


def test_conflation_matches_parallel_nearby_segments_only():
    # three east-west blocks in UTM metres; a bikeway runs along the first
    # two, offset by 4 m (a centreline vs. a lane), and a cross street
    # passes through the third at right angles
    edges = gpd.GeoDataFrame({
        "cls": ["residential"] * 3, "bike_ok": [True] * 3,
        "geometry": [LineString([(0, 0), (100, 0)]), LineString([(100, 0), (200, 0)]),
                     LineString([(200, 0), (300, 0)])],
    }, crs="EPSG:26910")
    bw = gpd.GeoDataFrame({
        "facility": ["lane", "separated"], "street": ["A", "B"],
        "geometry": [LineString([(-10, 4), (205, 4)]), LineString([(250, -50), (250, 50)])],
    }, crs="EPSG:26910")
    fac = bikeways.conflate(edges, bw)
    assert list(fac) == ["lane", "lane", ""]


def test_conflation_takes_the_most_protected_overlapping_facility():
    edges = gpd.GeoDataFrame({
        "cls": ["primary"], "bike_ok": [True],
        "geometry": [LineString([(0, 0), (100, 0)])],
    }, crs="EPSG:26910")
    bw = gpd.GeoDataFrame({
        "facility": ["route", "separated"], "street": ["A", "A"],
        "geometry": [LineString([(0, 3), (100, 3)]), LineString([(0, -3), (100, -3)])],
    }, crs="EPSG:26910")
    assert list(bikeways.conflate(edges, bw)) == ["separated"]


def test_conflation_keeps_lanes_off_sidewalks_and_trails_off_roads():
    # a street with a painted lane, a sidewalk 8 m to its side, and a trail
    # running alongside 9 m the other way
    edges = gpd.GeoDataFrame({
        "cls": ["secondary", "footway", "cycleway", "secondary"],
        "bike_ok": [True] * 4,
        "geometry": [LineString([(0, 0), (100, 0)]), LineString([(0, 8), (100, 8)]),
                     LineString([(0, -9), (100, -9)]), LineString([(200, 0), (300, 0)])],
    }, crs="EPSG:26910")
    bw = gpd.GeoDataFrame({
        "facility": ["lane", "path", "separated"], "street": ["A", "trail", "B"],
        "geometry": [LineString([(0, 0), (100, 0)]), LineString([(0, -9), (100, -9)]),
                     LineString([(200, 4), (300, 4)])],
    }, crs="EPSG:26910")
    # the street keeps its lane (not the trail), the sidewalk gets nothing,
    # the trail edge gets the trail, and a protected lane lands on a street
    assert list(bikeways.conflate(edges, bw)) == ["lane", "", "path", "separated"]


needs_data = pytest.mark.skipif(not bikeways.BIKEWAYS_GEOJSON.exists(),
                                reason="Seattle bikeway GeoJSON not fetched")


@needs_data
def test_the_real_network_has_every_facility_class():
    bw = bikeways.load_bikeways()
    counts = bw["facility"].value_counts()
    for fac in ("path", "separated", "buffered_lane", "lane", "neighborway", "route"):
        assert counts.get(fac, 0) > 20, fac
    assert len(bw) > 2500
    assert bw.crs.to_epsg() == 4326
    assert np.all(np.isfinite(bw.geometry.length))
    # everything lies in and around Seattle
    minx, miny, maxx, maxy = bw.total_bounds
    assert -122.5 < minx and maxx < -122.2 and 47.45 < miny and maxy < 47.8
