#!/usr/bin/env python3
"""Build the Tender Hub's annual research indexes from PPDA's OCDS export."""

import argparse
import gzip
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


REGISTRY = "https://data.open-contracting.org/en/publication/130"
PORTAL = "https://gpp.ppda.go.ug/public/bid-invitations"
LICENSE = "https://opendatacommons.org/licenses/by/1-0/"
FIELDS = [
    "ocid", "title", "buyer", "method", "status", "deadline", "updated", "awards", "portalId",
]


def clean(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def date_part(value):
    try:
        return date.fromisoformat(clean(value)[:10]).isoformat()
    except ValueError:
        return ""


def records(path):
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for line in source:
            release = json.loads(line)
            tender = release.get("tender") or {}
            ocid = clean(release.get("ocid"))
            title = clean(tender.get("title"))
            if not ocid or not title:
                continue
            period = tender.get("tenderPeriod") or {}
            yield [
                ocid, title, clean((release.get("buyer") or {}).get("name")),
                clean(tender.get("procurementMethod")), clean(tender.get("status")),
                date_part(period.get("endDate")), date_part(release.get("date")),
                len(release.get("awards") or []), clean(str(tender.get("id") or "")),
            ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("years", type=int, nargs="+")
    parser.add_argument("--snapshot", required=True, type=date.fromisoformat)
    parser.add_argument("--input-dir", type=Path, help="Use local YEAR.jsonl.gz exports")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "archive")
    args = parser.parse_args()
    years = sorted(set(args.years), reverse=True)
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    seen = set()
    files = []

    with tempfile.TemporaryDirectory() as temp:
        for year in years:
            filename = f"{year}.jsonl.gz"
            url = f"{REGISTRY}/download?{urlencode({'name': filename})}"
            if args.input_dir:
                path = args.input_dir / filename
            else:
                path = Path(temp) / filename
                request = Request(url, headers={"User-Agent": "SereneCreationsTenderResearch/1.0"})
                with urlopen(request, timeout=60) as response, path.open("wb") as target:
                    shutil.copyfileobj(response, target)
            rows = []
            for row in records(path):
                if row[0] not in seen:
                    rows.append(row)
                    seen.add(row[0])
            rows.sort(key=lambda row: (row[6], row[5], row[0]), reverse=True)
            data = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
            compressed = gzip.compress(data, compresslevel=9, mtime=0)
            fingerprint = hashlib.sha256(compressed).hexdigest()[:12]
            name = f"ppda-{year}-{fingerprint}.json.gz"
            (output / name).write_bytes(compressed)
            files.append({"year": year, "file": name, "records": len(rows), "source": url})
            print(year, len(rows), len(compressed), dict(Counter(row[4] for row in rows)))

    manifest = {
        "schema": 1, "publisher": "Public Procurement and Disposal of Public Assets Authority (Uganda)",
        "source": REGISTRY, "officialPortal": PORTAL, "license": LICENSE,
        "snapshotDate": args.snapshot.isoformat(), "totalRecords": len(seen),
        "columns": FIELDS, "years": files,
    }
    staging = output / ".manifest.tmp"
    staging.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    staging.replace(output / "manifest.json")


if __name__ == "__main__":
    main()
