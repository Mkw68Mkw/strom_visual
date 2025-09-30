from __future__ import annotations

import json
import os
import re
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Tuple

from flask import Flask, Response, jsonify, render_template
import xml.etree.ElementTree as ET


# -----------------------------
# Domain models and containers
# -----------------------------

@dataclass
class Messwert:
    timestamp: datetime
    value: float  # absolute meter reading
    relative: float  # relative consumption


MeterSeries = OrderedDict[datetime, Messwert]
AllMeters = Dict[str, MeterSeries]  # key like "ID735" / "ID742"


# -----------------------------
# Constants and helpers
# -----------------------------

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
EXTRA_SDAT_DIR = os.path.join(BASE_DIR, "SDAT-Files")

# Mapping: ID735 = Einspeisung (export), ID742 = Bezug (import)
METER_EXPORT_ID = "ID735"
METER_IMPORT_ID = "ID742"

# ESL OBIS suffixes we care about
OBIS_IMPORT = {":1.8.1", ":1.8.2"}
OBIS_EXPORT = {":2.8.1", ":2.8.2"}


def ensure_datetime_utc(dt_str: str) -> datetime:
    """Parse many ISO-ish formats to a timezone-aware UTC datetime.

    Handles values with trailing 'Z' or without timezone. Returns aware UTC.
    """
    s = dt_str.strip()
    # Normalize 'Z' to +00:00 for fromisoformat
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Fallback for common compact forms
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(dt_str, fmt)
                break
            except ValueError:
                continue
        else:
            raise
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def add_or_update_messwert(series: MeterSeries, ts: datetime, *, value: Optional[float] = None, relative: Optional[float] = None) -> None:
    existing = series.get(ts)
    if existing is None:
        series[ts] = Messwert(
            timestamp=ts,
            value=(value if value is not None else float("nan")),
            relative=(relative if relative is not None else float("nan")),
        )
        return
    # Update existing, preferring provided values
    if value is not None:
        existing.value = value
    if relative is not None:
        existing.relative = relative


# -----------------------------
# SDAT parsing (relative values)
# -----------------------------

def parse_sdat_file(path: str, meters: AllMeters) -> bool:
    """Parse an SDAT XML file and merge relative observations into meters.

    Returns True if file looked like SDAT and was parsed, else False.
    """
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError:
        return False

    # Heuristic: require rsm namespace and DocumentID containing "ID" number
    text_content = "".join(root.itertext())
    if "DocumentID" not in text_content and "Interval" not in text_content:
        return False

    # SDAT often uses rsm: prefix; search leniently
    ns_candidates = ["rsm", "h1", "ns0", "ns1", ""]

    def find_text(tag_suffixes: Iterable[str]) -> Optional[str]:
        for p in ns_candidates:
            for suffix in tag_suffixes:
                tag = f"{p}:{suffix}" if p else suffix
                el = root.find(f".//{tag}")
                if el is not None and el.text:
                    return el.text.strip()
        return None

    document_id = find_text(["DocumentID"])
    if not document_id:
        return False

    m = re.search(r"ID(\d+)", document_id)
    if not m:
        return False
    meter_id = f"ID{m.group(1)}"

    start_text = find_text(["StartDateTime"]) or find_text(["StartTime", "Start" ])
    end_text = find_text(["EndDateTime"]) or find_text(["EndTime", "End"])  # not used but sanity
    res_value = find_text(["Resolution"])  # numeric
    res_unit = find_text(["Unit"])  # e.g., MIN

    if not start_text or not res_value or not res_unit:
        # Not an SDAT we understand
        return False

    try:
        start_dt = ensure_datetime_utc(start_text)
        resolution = int(res_value)
    except Exception:
        return False

    # Determine step as timedelta
    unit = res_unit.upper()
    if unit.startswith("MIN"):
        step = timedelta(minutes=resolution)
    elif unit.startswith("H"):
        step = timedelta(hours=resolution)
    elif unit.startswith("S"):
        step = timedelta(seconds=resolution)
    else:
        # Fallback assume minutes
        step = timedelta(minutes=resolution)

    # Iterate observations; accommodate various nesting
    observations = []
    for p in ns_candidates:
        obs_tag = f"{p}:Observation" if p else "Observation"
        observations.extend(root.findall(f".//{obs_tag}"))
    if not observations:
        return False

    series = meters.setdefault(meter_id, OrderedDict())

    for obs in observations:
        # Sequence
        seq_text = None
        vol_text = None
        for p in ns_candidates:
            seq_el = obs.find(f".//{p}:Sequence") if p else obs.find(".//Sequence")
            if seq_el is not None and seq_el.text:
                seq_text = seq_el.text.strip()
                break
        for p in ns_candidates:
            vol_el = obs.find(f".//{p}:Volume") if p else obs.find(".//Volume")
            if vol_el is not None and vol_el.text:
                vol_text = vol_el.text.strip()
                break
        if not seq_text or not vol_text:
            continue
        try:
            seq = int(seq_text)
            vol = float(vol_text.replace(",", "."))
        except ValueError:
            continue
        ts = start_dt + (seq - 1) * step
        add_or_update_messwert(series, ts, relative=vol)

    return True


# -----------------------------
# ESL parsing (absolute values)
# -----------------------------

def parse_esl_file(path: str, meters: AllMeters) -> bool:
    """Parse an ESL XML and merge absolute meter readings (sum HT+NT) into meters.

    Returns True if file looked like ESL and was parsed, else False.
    """
    try:
        tree = ET.parse(path)
        root = tree.getroot()
    except ET.ParseError:
        return False

    # Heuristic: must have TimePeriod and ValueRow
    time_periods = root.findall(".//TimePeriod")
    if not time_periods:
        return False

    for tp in time_periods:
        end_attr = tp.attrib.get("end") or tp.attrib.get("End")
        if not end_attr:
            # Some exports use 'start' only; skip if no end
            continue
        ts = ensure_datetime_utc(end_attr)

        sum_import = 0.0
        sum_export = 0.0
        import_present = False
        export_present = False

        for vr in tp.findall(".//ValueRow"):
            obis = vr.attrib.get("obis", "")
            status = vr.attrib.get("status", "")
            val_text = vr.attrib.get("value") or vr.attrib.get("val") or ""
            try:
                val = float(val_text.replace(",", "."))
            except ValueError:
                continue

            if any(suffix in obis for suffix in OBIS_IMPORT):
                sum_import += val
                import_present = True
            elif any(suffix in obis for suffix in OBIS_EXPORT):
                sum_export += val
                export_present = True

        if import_present:
            series = meters.setdefault(METER_IMPORT_ID, OrderedDict())
            add_or_update_messwert(series, ts, value=sum_import)
        if export_present:
            series = meters.setdefault(METER_EXPORT_ID, OrderedDict())
            add_or_update_messwert(series, ts, value=sum_export)

    return True


# -----------------------------
# Loading all XML files
# -----------------------------

def load_all_data(data_dirs: Optional[Iterable[str]] = None) -> AllMeters:
    """Load ESL and SDAT XML files from one or more directories recursively.

    Accepts a single path (string) or an iterable of paths. If omitted, scans
    both the default `data/` and `SDAT-Files/` directories when present.
    """
    meters: AllMeters = {}

    # Normalize input directories
    if data_dirs is None:
        candidate_dirs = [DATA_DIR, EXTRA_SDAT_DIR]
    elif isinstance(data_dirs, (str, os.PathLike)):
        candidate_dirs = [str(data_dirs)]
    else:
        candidate_dirs = [str(p) for p in data_dirs]

    for dir_path in candidate_dirs:
        if not os.path.isdir(dir_path):
            continue
        # Walk recursively for .xml files
        for root_dir, _dirs, files in os.walk(dir_path):
            for fname in files:
                if not fname.lower().endswith(".xml"):
                    continue
                fpath = os.path.join(root_dir, fname)

                # Try ESL first, then SDAT; if neither, skip silently
                parsed = parse_esl_file(fpath, meters)
                if not parsed:
                    parse_sdat_file(fpath, meters)

    # De-duplicate by timestamp: using OrderedDict naturally keeps last write
    # Sort each meter's series by timestamp to ensure chronological order
    for meter_id, series in list(meters.items()):
        ordered = OrderedDict(sorted(series.items(), key=lambda kv: kv[0]))
        meters[meter_id] = ordered

    return meters


def build_chartjs_payload(meters: AllMeters) -> Dict[str, object]:
    # Collect unified label set (timestamps) for both meters
    label_set = set()
    for series in meters.values():
        label_set.update(series.keys())
    labels = sorted(label_set)

    def series_values(meter_id: str) -> List[Optional[float]]:
        series = meters.get(meter_id, {})
        return [ (series[ts].value if ts in series else None) for ts in labels ]

    payload = {
        "labels": [dt.astimezone(timezone.utc).isoformat() for dt in labels],
        "datasets": [
            {
                "label": "Einspeisung (ID735)",
                "data": series_values(METER_EXPORT_ID),
                "borderColor": "#2ca02c",
                "backgroundColor": "rgba(44,160,44,0.2)",
                "tension": 0.2,
                "spanGaps": True,
            },
            {
                "label": "Bezug (ID742)",
                "data": series_values(METER_IMPORT_ID),
                "borderColor": "#1f77b4",
                "backgroundColor": "rgba(31,119,180,0.2)",
                "tension": 0.2,
                "spanGaps": True,
            },
        ],
    }
    return payload


# -----------------------------
# Consumption (ESL diffs)
# -----------------------------

def build_consumption_payload(meters: AllMeters) -> Dict[str, object]:
    """Build a payload with per-period consumption from ESL diffs.

    For each meter series, compute successive differences of absolute values.
    Negative or non-finite diffs are treated as missing.
    """

    def series_diffs(series: MeterSeries) -> Dict[datetime, Optional[float]]:
        timestamps = sorted(series.keys())
        diffs: Dict[datetime, Optional[float]] = {}
        for i in range(1, len(timestamps)):
            t_prev = timestamps[i - 1]
            t_curr = timestamps[i]
            v_prev = series[t_prev].value
            v_curr = series[t_curr].value
            try:
                diff = float(v_curr) - float(v_prev)
            except Exception:
                diff = float("nan")
            if not (diff == diff) or diff < 0:  # NaN or negative
                diffs[t_curr] = None
            else:
                diffs[t_curr] = diff
        return diffs

    import_diffs = series_diffs(meters.get(METER_IMPORT_ID, OrderedDict()))
    export_diffs = series_diffs(meters.get(METER_EXPORT_ID, OrderedDict()))

    label_set = set(import_diffs.keys()) | set(export_diffs.keys())
    labels = sorted(label_set)

    def values_for(diffs_map: Dict[datetime, Optional[float]]) -> List[Optional[float]]:
        return [diffs_map.get(ts) for ts in labels]

    payload = {
        "labels": [dt.astimezone(timezone.utc).isoformat() for dt in labels],
        "datasets": [
            {
                "label": "Einspeisung (Verbrauch) ID735",
                "data": values_for(export_diffs),
                "borderColor": "#2ca02c",
                "backgroundColor": "rgba(44,160,44,0.2)",
                "tension": 0.2,
                "spanGaps": True,
            },
            {
                "label": "Bezug (Verbrauch) ID742",
                "data": values_for(import_diffs),
                "borderColor": "#1f77b4",
                "backgroundColor": "rgba(31,119,180,0.2)",
                "tension": 0.2,
                "spanGaps": True,
            },
        ],
    }
    return payload

# -----------------------------
# Flask app
# -----------------------------

def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        meters = load_all_data(DATA_DIR)
        data_payload = build_chartjs_payload(meters)
        data_json = json.dumps(data_payload)
        return render_template("index.html", data_json=data_json)

    # Optional JSON endpoint for debugging
    @app.route("/api/data")
    def api_data():
        meters = load_all_data(DATA_DIR)
        return jsonify(build_chartjs_payload(meters))

    @app.route("/consumption")
    def consumption_page():
        meters = load_all_data(DATA_DIR)
        data_payload = build_consumption_payload(meters)
        data_json = json.dumps(data_payload)
        return render_template(
            "index.html",
            data_json=data_json,
            page_title="Verbrauchsdiagramm",
            y_label="Verbrauch (kWh)",
        )

    @app.route("/api/consumption")
    def api_consumption():
        meters = load_all_data(DATA_DIR)
        return jsonify(build_consumption_payload(meters))

    @app.route("/export.csv")
    def export_csv():
        meters = load_all_data(DATA_DIR)
        # Build rows: timestamp, import_value, export_value
        label_set = set()
        for series in meters.values():
            label_set.update(series.keys())
        labels = sorted(label_set)
        imp = meters.get(METER_IMPORT_ID, {})
        exp = meters.get(METER_EXPORT_ID, {})
        lines = ["timestamp,import_kwh,export_kwh"]
        for ts in labels:
            iv = imp[ts].value if ts in imp else ""
            ev = exp[ts].value if ts in exp else ""
            lines.append(f"{ts.astimezone(timezone.utc).isoformat()},{iv},{ev}")
        csv_data = "\n".join(lines) + "\n"
        return Response(csv_data, mimetype="text/csv")

    @app.route("/export_consumption.csv")
    def export_consumption_csv():
        meters = load_all_data(DATA_DIR)
        payload = build_consumption_payload(meters)
        labels = payload.get("labels", [])
        ds = payload.get("datasets", [])
        # Expect datasets[0] = export, datasets[1] = import as built above
        exp_vals = (ds[0].get("data") if len(ds) > 0 else []) or []
        imp_vals = (ds[1].get("data") if len(ds) > 1 else []) or []
        lines = ["timestamp,import_kwh,export_kwh"]
        for i, ts in enumerate(labels):
            iv = imp_vals[i] if i < len(imp_vals) and imp_vals[i] is not None else ""
            ev = exp_vals[i] if i < len(exp_vals) and exp_vals[i] is not None else ""
            lines.append(f"{ts},{iv},{ev}")
        csv_data = "\n".join(lines) + "\n"
        return Response(csv_data, mimetype="text/csv")

    return app


app = create_app()


if __name__ == "__main__":
    # Run in debug mode for development
    app.run(host="127.0.0.1", port=5000, debug=True)


