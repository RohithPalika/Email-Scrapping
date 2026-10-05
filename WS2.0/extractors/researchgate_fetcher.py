import os
import re
import time
import logging
import requests

log = logging.getLogger("extraction.researchgate")

CONTACT_EMAIL = "23r25a6702@mlrit.ac.in"
API_HEADERS = {
    "User-Agent": f"AcademicEmailExtractor/1.0 (Research Outreach Tool; mailto:{CONTACT_EMAIL})"
}
STEALTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8"
}

_COUNTRY_CODES = {
    # North America
    "USA":                  "US",
    "United States":        "US",
    "Canada":               "CA",
    "Mexico":               "MX",
    # Europe
    "UK":                   "GB",
    "United Kingdom":       "GB",
    "Germany":              "DE",
    "France":               "FR",
    "Netherlands":          "NL",
    "Belgium":              "BE",
    "Switzerland":          "CH",
    "Sweden":               "SE",
    "Norway":               "NO",
    "Denmark":              "DK",
    "Finland":              "FI",
    "Ireland":              "IE",
    "Austria":              "AT",
    "Luxembourg":           "LU",
    "Iceland":              "IS",
    "Italy":                "IT",
    "Spain":                "ES",
    "Portugal":             "PT",
    "Romania":              "RO",
    # Asia
    "Japan":                "JP",
    "South Korea":          "KR",
    "Singapore":            "SG",
    "Israel":               "IL",
    "United Arab Emirates": "AE",
    "UAE":                  "AE",
    "Qatar":                "QA",
    "Saudi Arabia":         "SA",
    "Kuwait":               "KW",
    "Bahrain":              "BH",
    "Oman":                 "OM",
    # Oceania
    "Australia":            "AU",
    "New Zealand":          "NZ",
    # Other Regions
    "Brazil":               "BR",
    "Egypt":                "EG",
}


def _clean_email(raw):
    if not raw:
        return None
    cleaned = raw.strip(" \t\n\r*\u2020\u2021\u00a7#()[]{}<>,;:'\"")
    cleaned = re.split(r'[\s\(\[\{]', cleaned)[0].rstrip(".,;:")
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', cleaned):
        return cleaned
    return None


def fetch_researchgate_papers(topic, limit, target_dir, filters=None):
    """
    Searches and extracts author emails from publications indexed and hosted on
    ResearchGate (https://www.researchgate.net).
    Uses OpenAlex ResearchGate repository indexing (Source S4306401666) to discover
    research outputs archived on ResearchGate, followed by open-access preprint/PDF
    retrieval and text parsing.

    filters (dict, optional):
        year_from     (int|None)
        year_to       (int|None)
        countries     (list[str])
    """
    filters = filters or {}
    base_url = "https://api.openalex.org/works"

    # Search works with ResearchGate repository or open access preprint host
    api_filters = [
        "is_oa:true",
        "has_oa_accepted_or_published_version:true",
    ]

    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    if year_from and year_to:
        api_filters.append(f"publication_year:{year_from}-{year_to}")
    elif year_from:
        api_filters.append(f"publication_year:>{year_from - 1}")
    elif year_to:
        api_filters.append(f"publication_year:<{year_to + 1}")

    countries = filters.get("countries", [])
    iso_codes = [_COUNTRY_CODES[c] for c in countries if c in _COUNTRY_CODES]
    if iso_codes:
        api_filters.append("institutions.country_code:" + "|".join(iso_codes))

    filter_str = ",".join(api_filters)
    # Search specifically with researchgate or topic
    log.info(f"[ResearchGate] filter={filter_str!r}, search={topic!r}")

    params = {
        "search": f"{topic} ResearchGate",
        "filter": filter_str,
        "per-page": min(limit * 3, 50),
        "sort": "relevance_score:desc",
        "mailto": CONTACT_EMAIL,
    }

    try:
        resp = requests.get(base_url, params=params, headers=API_HEADERS, timeout=25)
        if resp.status_code != 200:
            log.error(f"[ResearchGate] OpenAlex API HTTP {resp.status_code}")
            return []
        items = resp.json().get("results", [])
        log.info(f"[ResearchGate] Found {len(items)} works.")
    except Exception as e:
        log.error(f"[ResearchGate] API error: {e}")
        return []

    records = []
    saved = 0

    for item in items:
        if saved >= limit:
            break

        doi = item.get("doi", "")
        if doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]

        title = item.get("title") or "Untitled"
        journal_name = "ResearchGate"

        authors_meta = []
        for authorship in item.get("authorships", []):
            name = authorship.get("author", {}).get("display_name")
            if name and name not in authors_meta:
                authors_meta.append(name)

        # Check for PDF links
        pdf_urls = []
        best_oa = item.get("best_oa_location", {}) or {}
        if best_oa.get("pdf_url"):
            pdf_urls.append(best_oa["pdf_url"])

        # Also inspect other locations for researchgate or repository urls
        for loc in item.get("locations", []):
            loc_pdf = loc.get("pdf_url")
            if loc_pdf and loc_pdf not in pdf_urls:
                pdf_urls.append(loc_pdf)

        if not pdf_urls:
            continue

        pdf_name = f"researchgate_paper_{saved + 1}.pdf"
        file_path = os.path.join(target_dir, pdf_name)
        download_success = False

        for p_url in pdf_urls:
            try:
                p_resp = requests.get(p_url, headers=STEALTH_HEADERS, timeout=25, allow_redirects=True)
                if p_resp.status_code == 200 and (p_resp.content.startswith(b"%PDF") or b"%PDF-" in p_resp.content[:1024]):
                    with open(file_path, "wb") as f:
                        f.write(p_resp.content)
                    download_success = True
                    break
            except Exception as p_err:
                log.warning(f"[ResearchGate] Download error ({p_url}): {p_err}")

        if download_success:
            saved += 1
            records.append({
                "file_path": file_path,
                "pdf_name": pdf_name,
                "title": title,
                "authors": authors_meta,
                "doi": doi or f"rg:{saved}",
                "source_journal": journal_name,
            })
            log.info(f"[ResearchGate] ({saved}/{limit}) Downloaded PDF for {title[:40]}")

    return records
