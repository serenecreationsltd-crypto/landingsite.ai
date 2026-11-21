#!/usr/bin/env python3
"""Refresh the Serene Tender Hub's Uganda open-notice snapshot."""

import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


EAT = ZoneInfo("Africa/Kampala")
EGP = "https://egpuganda.go.ug/bid-notices"
GOAL = "https://www.goalglobal.org/tenders/"
WB = "https://search.worldbank.org/api/v2/procnotices"
WB_TYPES = (
    "Invitation for Bids", "Invitation for Prequalification", "Request for Expression of Interest",
    "Request for Bids", "Request for Proposals", "Request for Quotations",
)
MAX_STALE = timedelta(hours=36)
CATEGORIES = {
    "works": "Works", "supplies": "Supplies", "consultancy services": "Consultancy",
    "non consultancy services": "Non-consultancy services", "CW": "Works", "GO": "Supplies",
    "CS": "Consultancy", "NC": "Non-consultancy services",
}


def compact(text):
    return " ".join((text or "").split())


def reference_key(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def fetch(url):
    request = Request(url, headers={"User-Agent": "SereneCreationsTenderHub/1.0 (public tender index)"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=22) as response:
                return response.read().decode("utf-8", errors="replace")
        except OSError:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def deadline_instant(value):
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), datetime.min.time(), EAT)
    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not value.tzinfo:
        raise ValueError("Deadline datetime requires a timezone")
    return value


def closing_value(day, clock=""):
    if not day:
        return ""
    date_value = date.fromisoformat(day[:10])
    if not clock:
        return date_value.isoformat()
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})(?::\d{2})?\s*(am|pm)?", clock.strip().lower())
    if not match:
        return date_value.isoformat()
    hour, minute, period = match.groups()
    hour = int(hour)
    if period:
        hour = hour % 12 + (12 if period == "pm" else 0)
    return datetime(date_value.year, date_value.month, date_value.day, hour, int(minute), tzinfo=EAT).isoformat()


def eligible(row, now):
    try:
        if not all(row.get(key) for key in ("title", "reference", "closingDate", "bidDocumentsUrl")):
            return False
        if not reference_key(row["reference"]):
            return False
        url = urlsplit(row["bidDocumentsUrl"])
        return url.scheme == "https" and bool(url.hostname) and not url.username and deadline_instant(row["closingDate"]) > now
    except (TypeError, ValueError):
        return False


def notice(source, title, reference, buyer, category, deadline, published, url):
    return {
        "source": source, "title": compact(title), "reference": compact(reference),
        "procuringEntity": compact(buyer), "category": category, "location": "Uganda",
        "region": "Uganda", "closingDate": deadline, "publishedDate": published,
        "bidDocumentsUrl": url, "status": "active",
    }


def parse_egp(html):
    page = BeautifulSoup(html, "html.parser")
    columns = ["procuring entity", "type", "subject of procurement", "published", "deadline"]
    table = next((table for table in page.find_all("table")
                  if [compact(th.get_text()).lower() for th in table.select("thead th")][:5] == columns), None)
    if table is None:
        raise ValueError("No Uganda eGP public notice table found")
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td", recursive=False)
        if len(cells) != 6:
            continue
        link = cells[0].select_one("a.text-default[href]")
        buyer = link.select_one(".text-muted") if link else None
        if not link or buyer is None:
            continue
        url = urljoin(EGP, link["href"])
        if urlsplit(url).hostname not in {"egpuganda.go.ug", "www.egpuganda.go.ug"}:
            continue
        ref = compact(" ".join(link.find_all(string=True, recursive=False)))
        category = CATEGORIES.get(compact(cells[1].get_text()).lower(), "Procurement")
        try:
            close = closing_value(compact(cells[4].get_text()))
            published = date.fromisoformat(compact(cells[3].get_text())).isoformat()
        except ValueError:
            continue
        rows.append(notice("Uganda eGP", cells[2].get_text(" "), ref, buyer.get_text(" "), category, close, published, url))
    if not rows and table.select("tbody tr td"):
        raise ValueError("Unexpected Uganda eGP notice rows")
    return rows


def parse_wb(text):
    payload = json.loads(text)
    items = payload.get("procnotices", [])
    if not isinstance(items, list) or not ("total" in payload and (items or int(payload["total"]) == 0)):
        raise ValueError("Unexpected World Bank response")
    rows = []
    for item in items:
        if item.get("project_ctry_name") != "Uganda" or item.get("notice_status") != "Published" or item.get("notice_type") not in WB_TYPES:
            continue
        notice_id = item.get("id", "")
        if not re.fullmatch(r"OP\d+", notice_id):
            continue
        try:
            close = closing_value(item.get("submission_deadline_date") or "", item.get("submission_deadline_time") or "")
            published = date.fromisoformat((item.get("submission_date") or "")[:10]).isoformat()
        except ValueError:
            continue
        rows.append(notice(
            "World Bank", item.get("bid_description"), item.get("bid_reference_no") or notice_id,
            item.get("contact_organization") or item.get("project_name"),
            CATEGORIES.get(item.get("procurement_group"), "Procurement"), close, published,
            "https://projects.worldbank.org/en/projects-operations/procurement-detail/" + notice_id,
        ))
    return rows


def goal_date(text):
    match = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)?\s*([A-Za-z]+)\s*(20\d\s*\d)", text, re.I)
    if not match:
        return ""
    day, month, year = match.groups()
    try:
        return datetime.strptime(f"{day} {month[:3]} {year.replace(' ', '')}", "%d %b %Y").date().isoformat()
    except ValueError:
        return ""


def parse_goal(html):
    page = BeautifulSoup(html, "html.parser")
    entries = page.select(".fl-accordion-item")
    if not entries:
        raise ValueError("No GOAL tender accordion found")
    rows = []
    for entry in entries:
        label = entry.select_one(".fl-accordion-button-label")
        body = entry.select_one(".fl-accordion-content[id]")
        if not label or not body:
            continue
        title = compact(label.get_text())
        if not re.match(r"GOAL\s+Uganda\s*:", title, re.I):
            continue
        ref = re.search(r"\bRef\s*:\s*([A-Z0-9][A-Z0-9-]+)", title, re.I)
        if not ref or re.search(r"\b(cancelled|withdrawn)\b", title, re.I):
            continue
        deadlines = []
        published = ""
        for item in body.find_all("li"):
            value = compact(item.get_text())
            if re.match(r"(?:ITT|RFP|RFQ) Publication\s*:", value, re.I):
                published = goal_date(value)
            if re.match(r"Closing date(?: and time)? for (?:the )?receipt of tenders\s*:", value, re.I):
                match = re.search(r"\b(\d{1,2}[:.]\d{2}(?:\s*(?:am|pm))?)\s*EAT\b", value, re.I)
                result = closing_value(goal_date(value), match[1] if match else "")
                if result:
                    deadlines.append(result)
        if len(set(deadlines)) != 1:
            continue
        category = "Consultancy" if re.search(r"consultancy|assessment|review", title, re.I) else "Procurement"
        rows.append(notice("GOAL Uganda", title[:ref.start()].strip(), ref[1], "GOAL Uganda", category, deadlines[0], published, GOAL + "#" + body["id"]))
    return rows


def wb_rows(read, today):
    offset = 0
    while True:
        params = {"format": "json", "rows": 50, "os": offset, "project_ctry_code": "UG", "deadline_strdate": today, "notice_type_exact": "^".join(WB_TYPES)}
        payload = read(WB + "?" + urlencode(params))
        yield from parse_wb(payload)
        result = json.loads(payload)
        if not result.get("procnotices") or offset + 50 >= int(result["total"]):
            return
        offset += 50
        if offset >= 500:
            raise ValueError("Unexpectedly large World Bank result")


def fetch_source(name, source, now):
    rows = [row for row in source() if eligible(row, now)]
    by_reference = {}
    for row in sorted(rows, key=lambda item: (item["publishedDate"], item["closingDate"]), reverse=True):
        by_reference.setdefault(reference_key(row["reference"]), row)
    return name, list(by_reference.values())


def refresh(previous, now, read=fetch):
    today = now.astimezone(EAT).date().isoformat()
    adapters = {
        "Uganda eGP": lambda: parse_egp(read(EGP)),
        "World Bank": lambda: wb_rows(read, today),
        "GOAL Uganda": lambda: parse_goal(read(GOAL)),
    }
    metadata = {}
    records = []
    stamp = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    successful = 0
    with ThreadPoolExecutor(max_workers=len(adapters)) as executor:
        jobs = {name: executor.submit(fetch_source, name, adapter, now) for name, adapter in adapters.items()}
        for name, job in jobs.items():
            try:
                _, rows = job.result()
                last_checked = stamp
                state = "ok"
                successful += 1
            except (OSError, TypeError, ValueError, KeyError) as error:
                print(f"{name}: source check failed ({type(error).__name__})")
                last_checked = previous.get("sources", {}).get(name, {}).get("checkedAt")
                fresh = last_checked and now - datetime.fromisoformat(last_checked) <= MAX_STALE
                rows = [row for row in previous.get("records", []) if row.get("source") == name and eligible(row, now)] if fresh else []
                state = "cached" if fresh else "unavailable"
            metadata[name] = {"checkedAt": last_checked, "state": state, "openRecords": len(rows)}
            for row in rows:
                records.append({**row, "checkedAt": last_checked})
    if not successful:
        raise RuntimeError("All sources failed; keep the last published snapshot")
    records.sort(key=lambda row: (row["publishedDate"], row["closingDate"], row["reference"]), reverse=True)
    return {"schema": 1, "generatedAt": stamp, "sources": metadata, "records": records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "live" / "open-tenders.json")
    args = parser.parse_args()
    prior = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {}
    snapshot = refresh(prior, datetime.now(timezone.utc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    staging = args.output.with_suffix(".tmp")
    staging.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    staging.replace(args.output)
    print(json.dumps(snapshot["sources"], indent=2))


if __name__ == "__main__":
    main()
