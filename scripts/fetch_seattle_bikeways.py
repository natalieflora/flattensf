#!/usr/bin/env python3
"""Fetch SDOT's Existing Bike Facilities layer into a small, stable GeoJSON.

The route finder's "prefer calm streets" weighting reads a city bikeway
layer (``sf_flat_routes/bikeways.py``).  For Seattle that is SDOT's Existing
Bike Facilities, published by the City of Seattle as an ArcGIS feature
service and refreshed daily.  The service is not reachable from every build
environment, so the result is committed to the repository and refreshed by a
scheduled GitHub Actions workflow (``.github/workflows/refresh-bikeways.yml``).

Only the standard library is used, so the workflow needs no install step.

The output is deterministic (features sorted by SDOT unit id, coordinates
rounded to 1e-6 degrees, no fetch timestamp) so an unchanged network gives
an unchanged file and the workflow only opens a pull request when SDOT has
actually changed something.

Usage::

    python scripts/fetch_seattle_bikeways.py [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

SERVICE = ("https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services/"
           "SDOT_Bike_Facilities/FeatureServer/2")
PAGE = 1000
FIELDS = ("UNITID", "UNITDESC", "CATEGORY", "MODEL_TYPE", "CURRENT_STATUS")

#: SDOT facility category -> the route finder's facility code
#: (see ``sf_flat_routes.bikeways.FACILITIES``)
CATEGORY_FACILITY = {
    "BKF-OFFST": "path",            # off-street trail
    "BKF-PBL": "separated",         # protected bike lane
    "BKF-BBL": "buffered_lane",     # buffered bike lane
    "BKF-BL": "lane",               # painted bike lane
    "BKF-CLMB": "lane",             # climbing lane: uphill lane, downhill sharrow
    "BKF-NGW": "neighborway",       # neighborhood greenway
    "BKF-SHW": "route",             # sharrows
}

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "data" / "sources" / "seattle_bike_facilities.geojson"


def _get(params: dict, tries: int = 4) -> dict:
    url = SERVICE + "/query?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "flatten-seattle-bikeways/1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.load(resp)
            if "error" in data:
                raise RuntimeError(f"service error: {data['error']}")
            return data
        except Exception as exc:  # network blips: back off and retry
            if attempt == tries - 1:
                raise
            wait = 2 ** attempt * 5
            print(f"  retry in {wait}s after: {exc}", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def street_name(desc: str | None) -> str:
    """'FREMONT AVE N BETWEEN N 43RD ST AND ...' -> 'FREMONT AVE N'."""
    if not desc:
        return ""
    return desc.split(" BETWEEN ")[0].split(" (BKF-")[0].strip()


def _round(coords: list) -> list:
    return [[round(x, 6), round(y, 6)] for x, y, *_ in coords]


def normalise(features: list[dict]) -> list[dict]:
    """SDOT GeoJSON features -> one LineString feature per part, with the
    facility code and a few readable fields, sorted for a stable diff."""
    out = []
    for f in features:
        p = f.get("properties") or {}
        g = f.get("geometry") or {}
        fac = CATEGORY_FACILITY.get((p.get("CATEGORY") or "").upper())
        if not fac:
            continue
        if g.get("type") == "LineString":
            parts = [g["coordinates"]]
        elif g.get("type") == "MultiLineString":
            parts = g["coordinates"]
        else:
            continue
        for k, part in enumerate(parts):
            if len(part) < 2:
                continue
            out.append({
                "type": "Feature",
                "properties": {
                    "unitid": p.get("UNITID") or "",
                    "part": k,
                    "facility": fac,
                    "category": p.get("CATEGORY"),
                    "direction": (p.get("MODEL_TYPE") or "").replace("BKF-", "").lower() or None,
                    "status": p.get("CURRENT_STATUS"),
                    "street": street_name(p.get("UNITDESC")),
                },
                "geometry": {"type": "LineString", "coordinates": _round(part)},
            })
    out.sort(key=lambda f: (f["properties"]["unitid"], f["properties"]["part"]))
    return out


def fetch() -> list[dict]:
    total = _get({"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"]
    print(f"SDOT Existing Bike Facilities: {total} features")
    features: list[dict] = []
    offset = 0
    while offset < total:
        page = _get({
            "where": "1=1", "outFields": ",".join(FIELDS), "outSR": 4326,
            "orderByFields": "OBJECTID", "resultOffset": offset,
            "resultRecordCount": PAGE, "f": "geojson",
        })
        got = page.get("features", [])
        if not got:
            break
        features.extend(got)
        offset += len(got)
    if len(features) < total:
        raise RuntimeError(f"fetched {len(features)} of {total} features")
    return features


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    feats = normalise(fetch())
    if len(feats) < 1000:  # a broken or emptied layer should fail loudly
        raise SystemExit(f"only {len(feats)} usable features; refusing to overwrite")
    doc = {
        "type": "FeatureCollection",
        "name": "seattle_bike_facilities",
        "source": SERVICE,
        "publisher": "City of Seattle, Seattle Department of Transportation",
        "licence": "City of Seattle Open Data (no restrictions on use)",
        "features": feats,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, separators=(",", ":"), ensure_ascii=False)
    # one feature per line keeps git diffs readable
    text = text.replace(',{"type":"Feature"', ',\n{"type":"Feature"')
    args.out.write_text(text + "\n")
    counts: dict[str, int] = {}
    for f in feats:
        counts[f["properties"]["facility"]] = counts.get(f["properties"]["facility"], 0) + 1
    print(f"wrote {args.out} ({len(feats)} segments): {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
