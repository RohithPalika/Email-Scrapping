import os
import time
import logging
import requests

log = logging.getLogger("extraction.openalex")

# Country name → ISO alpha-2 code (OpenAlex uses these codes)
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

# Article type name → OpenAlex type string
_TYPE_MAP = {
    "Article":          "article",
    "Review":           "review",
    "Systematic Review":"review",
    "Case Reports":     "article",
    "Case Report":      "article",
    "Brief Report":     "article",
}


def fetch_openalex_papers(topic, limit, target_dir, filters=None):
    """
    Searches and downloads Open Access PDFs from OpenAlex REST API.

    filters (dict, optional):
        countries     (list[str]): UI country names, mapped to ISO codes
        year_from     (int|None):  publication year ≥ this
        year_to       (int|None):  publication year ≤ this
        article_types (list[str]): article type labels mapped to OpenAlex types
    """
    filters = filters or {}
    base_url = "https://api.openalex.org/works"

    # --- Build filter string ---
    api_filters = ["has_oa_accepted_or_published_version:true", "is_oa:true"]

    # Year range
    year_from = filters.get("year_from")
    year_to   = filters.get("year_to")
    if year_from and year_to:
        api_filters.append(f"publication_year:{year_from}-{year_to}")
    elif year_from:
        api_filters.append(f"publication_year:>{year_from - 1}")
    elif year_to:
        api_filters.append(f"publication_year:<{year_to + 1}")

    # Countries — map UI names to ISO codes, join with |  (OR logic)
    countries = filters.get("countries", [])
    iso_codes = [_COUNTRY_CODES[c] for c in countries if c in _COUNTRY_CODES]
    if iso_codes:
        api_filters.append("institutions.country_code:" + "|".join(iso_codes))

    # Article types — map UI names to OpenAlex types, join with |  (OR logic)
    article_types = filters.get("article_types", [])
    oa_types = list({_TYPE_MAP[t] for t in article_types if t in _TYPE_MAP})
    if oa_types:
        api_filters.append("type:" + "|".join(oa_types))

    filter_str = ",".join(api_filters)
    log.info(f"[OpenAlex] filter={filter_str!r}")

    params = {
        "search":    topic,
        "filter":    filter_str,
        "per-page":  min(max(limit * 4, 25), 200),   # Fetch extra candidates up to OpenAlex max per-page (200)
        "sort":      "relevance_score:desc",
        "mailto":    "23r25a6702@mlrit.ac.in",
    }

    headers = {
        "User-Agent": "AcademicEmailExtractor/1.0 (Research Outreach Tool; mailto:23r25a6702@mlrit.ac.in)"
    }

    try:
        resp = requests.get(base_url, params=params, headers=headers, timeout=25)
        if resp.status_code != 200:
            log.error(f"[OpenAlex] API error: HTTP {resp.status_code} — {resp.text[:120]}")
            return []
        items = resp.json().get("results", [])
        log.info(f"[OpenAlex] Got {len(items)} candidates.")
        time.sleep(1)  # Polite delay after API call
    except Exception as e:
        log.error(f"[OpenAlex] API error: {e}")
        return []

    records = []
    saved_count = 0

    PDF_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "application/pdf,application/xhtml+xml,text/html,*/*;q=0.9",
    }

    for item in items:
        if saved_count >= limit:
            break

        title = item.get("display_name") or item.get("title") or "Untitled"
        doi   = item.get("doi") or "N/A"

        # Extract authors list
        authors_meta = []
        for a in item.get("authorships", []):
            name = (a.get("author") or {}).get("display_name")
            if name:
                authors_meta.append(name.strip())

        # Collect all PDF URLs (best_oa_location first, then all locations)
        pdf_urls = []
        best_oa = item.get("best_oa_location") or {}
        if best_oa.get("pdf_url"):
            pdf_urls.append(best_oa["pdf_url"])
        primary_loc = item.get("primary_location") or {}
        if primary_loc.get("pdf_url") and primary_loc["pdf_url"] not in pdf_urls:
            pdf_urls.append(primary_loc["pdf_url"])
        for loc in item.get("locations", []):
            u = loc.get("pdf_url")
            if u and u not in pdf_urls:
                pdf_urls.append(u)

        if not pdf_urls:
            continue

        pdf_name = f"openalex_paper_{saved_count + 1}.pdf"
        file_path = os.path.join(target_dir, pdf_name)
        download_success = False

        for url in pdf_urls:
            try:
                pdf_resp = requests.get(url, headers=PDF_HEADERS, timeout=20, allow_redirects=True)
                if pdf_resp.status_code == 200 and (
                    pdf_resp.content.startswith(b"%PDF") or b"%PDF-" in pdf_resp.content[:1024]
                ):
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, "wb") as f:
                        f.write(pdf_resp.content)
                    download_success = True
                    break
            except Exception as e:
                log.warning(f"[OpenAlex] PDF download failed ({url}): {e}")

        if download_success:
            saved_count += 1
            source_journal = "OpenAlex"
            src_obj = (item.get("primary_location") or {}).get("source") or {}
            if src_obj.get("display_name"):
                source_journal = src_obj["display_name"]
            records.append({
                "file_path":     file_path,
                "pdf_name":      pdf_name,
                "title":         title,
                "authors":       authors_meta,
                "doi":           doi,
                "source_journal": source_journal,
            })
            time.sleep(1)  # Polite delay between downloads

    return records