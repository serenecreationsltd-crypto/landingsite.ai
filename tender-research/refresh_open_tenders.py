#!/usr/bin/env python3
"""Refresh the Serene Tender Hub's East Africa open-notice snapshot."""

import argparse
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup


EAT = ZoneInfo("Africa/Kampala")
NEW_YORK = ZoneInfo("America/New_York")
CENTRAL_EUROPE = {"CET": timezone(timedelta(hours=1)), "CEST": timezone(timedelta(hours=2))}
EGP = "https://egpuganda.go.ug/bid-notices"
GOAL = "https://www.goalglobal.org/tenders/"
KENYA_TENDERS = "https://tenders.go.ke/tenders/"
KENYA_ACTIVE = "https://tenders.go.ke/api/active-tenders"
TANZANIA_TENDERS = "https://nest.go.tz/tenders/published-tenders"
TANZANIA_API = "https://nest.go.tz/gateway/nest-app/graphql"
UNDP = "https://procurement-notices.undp.org/"
UNABSEC = "https://www.unabsec.co.ug/opportunities/tenders"
UNIDO = "https://www.unido.org/get-involved/procurement/procurement-opportunities"
WB = "https://search.worldbank.org/api/v2/procnotices"
WHH = "https://www.welthungerhilfe.org/tenders/"
WB_TYPES = (
    "Invitation for Bids", "Invitation for Prequalification", "Request for Expression of Interest",
    "Request for Bids", "Request for Proposals", "Request for Quotations",
)
MAX_STALE = timedelta(hours=36)
OPEN_NOTICE_TARGET = 200
REGIONAL_CATEGORIES = {
    "goods": "Supplies", "works": "Works", "consultancy": "Consultancy",
    "consultancy services": "Consultancy", "non consultancy": "Non-consultancy services",
    "non consultancy services": "Non-consultancy services",
}
CATEGORIES = {
    "works": "Works", "supplies": "Supplies", "consultancy services": "Consultancy",
    "non consultancy services": "Non-consultancy services", "CW": "Works", "GO": "Supplies",
    "CS": "Consultancy", "NC": "Non-consultancy services",
}


def compact(text):
    return " ".join((text or "").split())


def reference_key(value):
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def fetch(url, data=None):
    headers = {"User-Agent": "SereneCreationsTenderHub/1.0 (public tender index)", "Accept": "*/*"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers)
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


def parse_undp(html):
    page = BeautifulSoup(html, "html.parser")
    entries = page.select("a.vacanciesTable__row[href]")
    if not entries:
        raise ValueError("No UNDP procurement notices found")
    rows = []
    for entry in entries:
        fields = {}
        for cell in entry.select(".vacanciesTable__cell"):
            label = cell.select_one(".vacanciesTable__cell__label")
            value = cell.select_one("span")
            if label and value:
                fields[compact(label.get_text())] = compact(value.get_text(" "))
        office, _, country = fields.get("UNDP Office/Country", "").partition("/")
        if country.strip().upper() != "UGANDA":
            continue
        title = fields.get("Title", "")
        process = fields.get("Process", "").partition(" - ")[0]
        ref = re.fullmatch(r"([A-Z0-9]+(?:-[A-Z0-9]+)*-\d+)(?:,\d+)?", fields.get("Ref No", ""))
        link = re.fullmatch(r"/?view_negotiation\.cfm\?nego_id=\d+", entry["href"])
        if not ref or not link or process not in {"ITB", "RFQ", "RFP", "IC"} or re.search(r"\b(cancelled|withdrawn)\b", title, re.I):
            continue
        try:
            close = datetime.strptime(fields.get("Deadline", ""), "%d-%b-%y %I:%M %p (New York time)")
            close = close.replace(tzinfo=NEW_YORK).astimezone(EAT).isoformat()
            published = datetime.strptime(fields.get("Posted", ""), "%d-%b-%y").date().isoformat()
        except ValueError:
            continue
        category = {"ITB": "Procurement", "RFQ": "Procurement", "RFP": "Consultancy", "IC": "Consultancy"}[process]
        if re.search(r"\b(construction|civil works|rehabilitation of (a |the )?(road|building|bridge))\b", title, re.I):
            category = "Works"
        elif re.search(r"\bsuppl(?:y|ies|ier)\b", title, re.I):
            category = "Supplies"
        buyer = {"UNDP-UGA": "UNDP Uganda", "UNDP-HQ": "UNDP", "UNCDF": "UNCDF Uganda"}.get(office.strip(), office.strip())
        rows.append(notice("UNDP Procurement Notices", title, ref[1], buyer, category, close, published, urljoin(UNDP, link[0])))
    return rows


def central_europe_deadline(value, zone):
    local = datetime.strptime(value, "%d.%m.%Y %H:%M:%S")
    return local.replace(tzinfo=CENTRAL_EUROPE[zone]).astimezone(EAT).isoformat()


def parse_unido(html):
    page = BeautifulSoup(html, "html.parser")
    columns = ["Title", "Beneficiary Country", "Deadline", "Type", "Event Number", "Registration"]
    table = next((table for table in page.select("table")
                  if [compact(th.get_text()) for th in table.select("thead th")] == columns), None)
    if table is None:
        raise ValueError("No UNIDO current opportunities table found")
    rows = []
    for row in table.select("tbody tr"):
        cells = [compact(cell.get_text(" ")) for cell in row.find_all("td", recursive=False)]
        if len(cells) != len(columns):
            continue
        title, country, deadline, method, event, _ = cells
        if country.casefold() != "uganda" or not re.fullmatch(r"\d{10}", event):
            continue
        if re.search(r"\b(cancelled|withdrawn)\b", title + " " + method, re.I):
            continue
        clock, _, zone = deadline.rpartition(" ")
        try:
            close = central_europe_deadline(clock, zone)
        except (KeyError, ValueError):
            continue
        category = "Procurement"
        if re.search(r"\b(construction|civil works|rehabilitation)\b", title, re.I):
            category = "Works"
        elif re.search(r"\bconsultancy\b", title, re.I):
            category = "Consultancy"
        published = ""
        if event == "7000008958":
            # The published event title and release date are on the UNGM notice.
            # UNIDO's own current tender table provides the updated deadline.
            title = "Construction of the Italy–Uganda Coffee Training Centre in Kampala"
            published = "2026-11-06"
        rows.append(notice("UNIDO", title, event, "UNIDO", category, close, published, UNIDO))
    return rows


def whh_rows(read, now):
    page = BeautifulSoup(read(WHH), "html.parser")
    entries = page.select(".tender__list__item")
    if not entries:
        raise ValueError("No Welthungerhilfe tender listings found")
    rows = []
    unavailable = 0
    for entry in entries:
        buyer = entry.select_one(".tender__list__item__client")
        if not buyer or compact(buyer.get_text()).casefold() != "contracting authority: welthungerhilfe uganda":
            continue
        link = entry.select_one(".tender__list__item__title a[href]")
        listed_close = entry.select_one(".tender__list__item__deadline")
        posted = entry.select_one(".tender__list__item__date")
        if not link or not listed_close or not posted:
            continue
        portal = urlsplit(link["href"])
        identifier = parse_qs(portal.query).get("PID", [""])[0]
        if portal.scheme != "https" or portal.netloc != "eu.eu-supply.com" or not re.fullmatch(r"\d+", identifier):
            continue
        listed_day = re.search(r"\d{2}\.\d{2}\.\d{4}", listed_close.get_text())
        posted_day = re.search(r"\d{2}\.\d{2}\.\d{4}", posted.get_text())
        if not listed_day or not posted_day:
            continue
        try:
            # The index sometimes displays a malformed time. Read the portal
            # notice itself for the actual reference and deadline time.
            index_day = datetime.strptime(listed_day[0], "%d.%m.%Y").date()
            if index_day < now.astimezone(EAT).date() - timedelta(days=1):
                continue
            published = datetime.strptime(posted_day[0], "%d.%m.%Y").date().isoformat()
            url = f"https://eu.eu-supply.com/ctm/Supplier/PublicPurchase/{identifier}/0/0?b=WELTHUNGERHILFE"
            detail = BeautifulSoup(read(url), "html.parser")
            heading = detail.select_one(".ctm-header-pane h1")
            ref, delimiter, title = compact(heading.get_text(" ") if heading else "").partition(" - ")
            if not delimiter or not title or not re.search(r"\d", ref):
                raise ValueError("Missing Welthungerhilfe tender reference")
            if re.search(r"\b(cancelled|withdrawn)\b", title, re.I):
                continue
            fields = {}
            for label in detail.select("#tenderInfoSection .ctm-content-label"):
                value = label.find_next_sibling("p")
                if value:
                    fields[compact(label.get_text())] = compact(value.get_text(" "))
            deadline = next((central_europe_deadline(fields[f"Response deadline ({zone}):"], zone)
                             for zone in CENTRAL_EUROPE if f"Response deadline ({zone}):" in fields), "")
            if not deadline:
                raise ValueError("Missing Welthungerhilfe deadline")
        except (OSError, ValueError):
            unavailable += 1
            continue
        group = fields.get("Type of contract:", "").casefold()
        category = CATEGORIES.get(group, "Consultancy" if "consultancy" in title.casefold() else "Procurement")
        rows.append(notice("Welthungerhilfe Uganda", title, ref, "Welthungerhilfe Uganda", category, deadline, published, url))
    if unavailable:
        raise ValueError(f"Could not verify {unavailable} Welthungerhilfe detail notices")
    return rows


def pdf_digest(url):
    request = Request(url, headers={"User-Agent": "SereneCreationsTenderHub/1.0 (public tender index)"})
    try:
        with urlopen(request, timeout=20) as response:
            first = response.read(1024)
            if b"%PDF-" not in first:
                raise ValueError("Buyer notice is unavailable as a PDF")
            checksum = hashlib.sha256(first)
            for chunk in iter(lambda: response.read(65536), b""):
                checksum.update(chunk)
            return checksum.hexdigest()
    except HTTPError as error:
        if error.code in (404, 410):
            return ""
        raise


def unabsec_rows(html, now, digest=pdf_digest):
    """Read reviewed lots only while the buyer's PDF and bulletin deadline agree."""
    page = BeautifulSoup(html, "html.parser")
    links = page.select('a[href*="/storage/tenders/documents/"]')
    if not links:
        raise ValueError("No UNABSEC tender documents found")
    listings = {urljoin(UNABSEC, link["href"]): link.parent for link in links}
    catalog = Path(__file__).with_name("unabsec-notices.json")
    rows = []
    for doc in json.loads(catalog.read_text(encoding="utf-8")):
        url = doc["url"]
        card = listings.get(url)
        if card is None or deadline_instant(doc["deadline"]) <= now:
            continue
        dates = {}
        for field in card.select("p"):
            label, _, value = compact(field.get_text(" ")).partition(":")
            if label in {"Date of Posting", "Closing Date"}:
                dates[label] = goal_date(value)
        buyer = card.select_one("span")
        if not buyer or compact(buyer.get_text(" ")) != doc["buyer"]:
            continue
        if not dates.get("Date of Posting") or dates.get("Closing Date") != doc["deadline"][:10]:
            continue
        if digest(url) != doc["sha256"]:
            continue
        for item in doc["items"]:
            row = notice("UNABSEC tender bulletin", item["title"], item["reference"], doc["buyer"],
                         item["category"], doc["deadline"], dates["Date of Posting"], url)
            row["location"] = item.get("location", doc["location"])
            row["region"] = item.get("region", doc["region"])
            rows.append(row)
    return rows


def regional_notice(source, country, title, reference, buyer, category, closes, posted, url, now):
    if not all(isinstance(value, str) and value.strip()
               for value in (title, reference, buyer, category, closes, posted)):
        return None
    try:
        published = date.fromisoformat(posted[:10])
        if published > now.astimezone(EAT).date():
            return None
        if len(closes) == 10:
            deadline = date.fromisoformat(closes).isoformat()
        else:
            deadline = datetime.fromisoformat(closes)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=EAT)
            deadline = deadline.astimezone(EAT).isoformat()
    except ValueError:
        return None
    row = notice(source, title, reference, buyer, category, deadline, published.isoformat(), url)
    row.update(country=country, location=country, region=country)
    return row


def kenya_rows(read, now):
    methods = {"Open Tender", "Open International Tender", "Expression of Interest",
               "Request for Proposals", "Prequalification", "Framework Agreements"}
    for page in range(1, 21):
        query = urlencode({"perpage": 100, "page": page, "sortby": "published_at", "order": "desc"})
        result = json.loads(read(KENYA_ACTIVE + "?" + query))
        if not isinstance(result.get("data"), list) or result["current_page"] != page:
            raise ValueError("Unexpected Kenya active-tenders response")
        for item in result["data"]:
            identifier = item.get("id")
            method = (item.get("procurement_method") or {}).get("title")
            if (not isinstance(identifier, int) or method not in methods
                    or item.get("status_id") != 1 or item.get("published") != 1
                    or item.get("terminated") or item.get("is_agpo") or item.get("is_reservation")
                    or item.get("agpo_group_id")):
                continue
            category = (item.get("procurement_category") or {}).get("title") or ""
            row = regional_notice(
                "Kenya PPIP", "Kenya", item.get("title"), item.get("tender_ref"),
                (item.get("pe") or {}).get("name"),
                REGIONAL_CATEGORIES.get(category.lower().replace("-", " "), "Procurement"),
                item.get("close_at"), item.get("published_at"), KENYA_TENDERS + str(identifier), now,
            )
            if row:
                yield row
        if page >= result["last_page"]:
            return
    raise ValueError("Kenya active-tenders page limit reached")


TANZANIA_QUERY = """
query getPublishedEntityViewData($input: DataRequestInputInput, $withMetaData: Boolean) {
  items: getPublishedEntityViewData(input: $input, withMetaData: $withMetaData) {
    currentPage hasNext
    rows: data {
      descriptionOfTheProcurement entityNumber entityStatus entitySubCategoryAcronym
      entityType invitationDate procurementCategoryName procuringEntityName
      submissionOrOpeningDate
    }
  }
}
"""


def tanzania_rows(read, now):
    for page in range(1, 21):
        variables = {
            "input": {
                "fields": [{"fieldName": "invitationDate", "orderDirection": "DESC"}],
                "page": page, "pageSize": 100,
                "mustHaveFilters": [{"fieldName": "entityStatus", "operation": "IN", "inValues": ["PUBLISHED"]}],
            },
            "withMetaData": False,
        }
        request = json.dumps({"operationName": "getPublishedEntityViewData",
                              "query": TANZANIA_QUERY, "variables": variables}).encode("utf-8")
        response = json.loads(read(TANZANIA_API, request))
        if response.get("errors"):
            raise ValueError("Tanzania NeST returned a query error")
        result = response["data"]["items"]
        if result["currentPage"] != page or not isinstance(result.get("rows"), list):
            raise ValueError("Unexpected Tanzania published-tenders response")
        for item in result["rows"]:
            kind = item.get("entityType")
            subcategory = item.get("entitySubCategoryAcronym") or ""
            if item.get("entityStatus") != "PUBLISHED" or (
                kind != "TENDER" and not (kind == "FRAMEWORK" and subcategory.endswith("_OPEN"))
            ):
                continue
            reference = item.get("entityNumber") or ""
            category = item.get("procurementCategoryName") or ""
            row = regional_notice(
                "Tanzania NeST", "Tanzania", item.get("descriptionOfTheProcurement"), reference,
                item.get("procuringEntityName"), REGIONAL_CATEGORIES.get(category.lower(), "Procurement"),
                item.get("submissionOrOpeningDate"), item.get("invitationDate"),
                TANZANIA_TENDERS + "?" + urlencode({"reference": reference}), now,
            )
            if row:
                yield row
        if not result["hasNext"]:
            return
    raise ValueError("Tanzania published-tenders page limit reached")


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
        "UNDP Procurement Notices": lambda: parse_undp(read(UNDP)),
        "UNIDO": lambda: parse_unido(read(UNIDO)),
        "Welthungerhilfe Uganda": lambda: whh_rows(read, now),
        "UNABSEC tender bulletin": lambda: unabsec_rows(read(UNABSEC), now),
        "Kenya PPIP": lambda: kenya_rows(read, now),
        "Tanzania NeST": lambda: tanzania_rows(read, now),
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
                reason = f"HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
                print(f"{name}: source check failed ({reason})")
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
    unique_open = len({reference_key(row["reference"]) for row in records})
    return {"schema": 1, "generatedAt": stamp, "sources": metadata,
            "coverage": {"target": OPEN_NOTICE_TARGET, "uniqueOpen": unique_open}, "records": records}


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
    coverage = snapshot["coverage"]
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(summary).open("a", encoding="utf-8") as output:
            print(f"Open tender references: **{coverage['uniqueOpen']}** / target {coverage['target']}", file=output)
    if coverage["uniqueOpen"] < coverage["target"]:
        print(f"::warning::Only {coverage['uniqueOpen']} open tender references were available from the current sources")


if __name__ == "__main__":
    main()
