import os
import logging
import requests

log = logging.getLogger("extraction.europepmc")

# Country display name → EuropePMC COUNTRY field values
# EuropePMC uses full English country names in its query syntax
_COUNTRY_NAMES = {
    "USA":          "United States",
    "UK":           "United Kingdom",
    "Italy":        "Italy",
    "Spain":        "Spain",
    "Romania":      "Romania",
    "France":       "France",
    "Brazil":       "Brazil",
    "Germany":      "Germany",
    "Australia":    "Australia",
    "Canada":       "Canada",
    "Mexico":       "Mexico",
    "Saudi Arabia": "Saudi Arabia",
    "Egypt":        "Egypt",
}


def fetch_europepmc_papers(topic, limit, target_dir, filters=None):
    """
    Searches and downloads Open Access PDFs exclusively from Europe PMC.

    filters (dict, optional):
        countries     (list[str]): UI country names → added as COUNTRY: clauses
        year_from     (int|None):  earliest publication year
        year_to       (int|None):  latest publication year
        article_types (list[str]): not supported by EuropePMC query — ignored
    """
    filters = filters or {}

    # --- Build EuropePMC query ---
    query_parts = [f'"{topic}"', "HAS_PDF:y", "OPEN_ACCESS:y"]

    # Year range — EuropePMC supports PUB_YEAR:[from TO to]
    year_from = filters.get("year_from")
    year_to   = filters.get("year_to")
    if year_from and year_to:
        query_parts.append(f"PUB_YEAR:[{year_from} TO {year_to}]")
    elif year_from:
        query_parts.append(f"PUB_YEAR:[{year_from} TO 2099]")
    elif year_to:
        query_parts.append(f"PUB_YEAR:[1900 TO {year_to}]")

    # Countries — build (COUNTRY:"X" OR COUNTRY:"Y") clause
    countries = filters.get("countries", [])
    mapped    = [_COUNTRY_NAMES[c] for c in countries if c in _COUNTRY_NAMES]
    if mapped:
        country_clause = " OR ".join(f'COUNTRY:"{c}"' for c in mapped)
        query_parts.append(f"({country_clause})")

    query = " AND ".join(query_parts)
    log.info(f"[EuropePMC] Query: {query!r}")

    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {
        "query":      query,
        "format":     "json",
        "pageSize":   limit * 2,
        "resultType": "core",
    }

    session = requests.Session()
    session.headers.update({
        "User-Agent": "AcademicEmailExtractor/1.0 (Research Outreach Tool)",
        "Accept": "application/json",
    })

    try:
        resp = session.get(base_url, params=params, timeout=30)
        if resp.status_code != 200:
            log.error(f"[EuropePMC] HTTP {resp.status_code}")
            return []
        results = resp.json().get("resultList", {}).get("result", [])
        log.info(f"[EuropePMC] Got {len(results)} candidates.")
    except Exception as e:
        log.error(f"[EuropePMC] API error: {e}")
        return []

    records = []
    saved = 0

    for item in results:
        if saved >= limit:
            break

        pmcid        = item.get("pmcid")
        doi          = item.get("doi", "N/A")
        title        = item.get("title", "Untitled").rstrip(".")
        author_string= item.get("authorString", "")
        authors_meta = [a.strip() for a in author_string.split(",") if a.strip()]
        source_journal = item.get("journalTitle", "Europe PMC") or "Europe PMC"

        pdf_urls = []
        if pmcid:
            pdf_urls.append(f"https://europepmc.org/articles/{pmcid}?pdf=render")
            pdf_urls.append(f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf")
        if doi and doi != "N/A":
            pdf_urls.append(f"https://doi.org/{doi}")

        if not pdf_urls:
            continue

        pdf_name  = f"europepmc_paper_{saved + 1}.pdf"
        file_path = os.path.join(target_dir, pdf_name)

        download_success = False
        for url in pdf_urls:
            try:
                pdf_resp = session.get(url, timeout=30, allow_redirects=True)
                if pdf_resp.status_code == 200 and (
                    pdf_resp.content.startswith(b"%PDF") or b"%PDF-" in pdf_resp.content[:1024]
                ):
                    with open(file_path, "wb") as f:
                        f.write(pdf_resp.content)
                    download_success = True
                    break
            except Exception as e:
                log.warning(f"[EuropePMC] Download failed ({url}): {e}")

        if download_success:
            saved += 1
            records.append({
                "file_path":      file_path,
                "pdf_name":       pdf_name,
                "title":          title,
                "authors":        authors_meta,
                "doi":            doi,
                "source_journal": source_journal,
            })

    return records