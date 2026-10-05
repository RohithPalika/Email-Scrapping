import os
import re
import logging
import requests

log = logging.getLogger("extraction.europepmc")

# Country display name → EuropePMC COUNTRY field values
# EuropePMC uses full English country names in its query syntax
_COUNTRY_NAMES = {
    # North America
    "USA":                  "United States",
    "United States":        "United States",
    "Canada":               "Canada",
    "Mexico":               "Mexico",
    # Europe
    "UK":                   "United Kingdom",
    "United Kingdom":       "United Kingdom",
    "Germany":              "Germany",
    "France":               "France",
    "Netherlands":          "Netherlands",
    "Belgium":              "Belgium",
    "Switzerland":          "Switzerland",
    "Sweden":               "Sweden",
    "Norway":               "Norway",
    "Denmark":              "Denmark",
    "Finland":              "Finland",
    "Ireland":              "Ireland",
    "Austria":              "Austria",
    "Luxembourg":           "Luxembourg",
    "Iceland":              "Iceland",
    "Italy":                "Italy",
    "Spain":                "Spain",
    "Portugal":             "Portugal",
    "Romania":              "Romania",
    # Asia
    "Japan":                "Japan",
    "South Korea":          "South Korea",
    "Singapore":            "Singapore",
    "Israel":               "Israel",
    "United Arab Emirates": "United Arab Emirates",
    "UAE":                  "United Arab Emirates",
    "Qatar":                "Qatar",
    "Saudi Arabia":         "Saudi Arabia",
    "Kuwait":               "Kuwait",
    "Bahrain":              "Bahrain",
    "Oman":                 "Oman",
    # Oceania
    "Australia":            "Australia",
    "New Zealand":          "New Zealand",
    # Other Regions
    "Brazil":               "Brazil",
    "Egypt":                "Egypt",
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

    # Article types — EuropePMC supports PUB_TYPE:"..."
    article_types = filters.get("article_types", [])
    if article_types:
        epmc_types = []
        for t in article_types:
            if t in ("Case Reports", "Case Report"):
                epmc_types.append('PUB_TYPE:"case reports"')
            elif t == "Review":
                epmc_types.append('PUB_TYPE:"review"')
            elif t == "Systematic Review":
                epmc_types.append('PUB_TYPE:"systematic review"')
        if epmc_types:
            query_parts.append(f"({' OR '.join(epmc_types)})")

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
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    })

    try:
        resp = session.get(base_url, params=params, headers={"Accept": "application/json"}, timeout=30)
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

        # Strategy 1: XML-First via Europe PMC official REST API
        # Avoids Cloudflare 403 blocks on Europe PMC PDF endpoints and is 10x faster.
        if pmcid:
            xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
            try:
                xml_resp = session.get(xml_url, headers={"Accept": "application/xml, text/xml, */*"}, timeout=15)
                if xml_resp.status_code == 200:
                    raw_emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', xml_resp.text)
                    clean_emails = [e.strip().rstrip('.') for e in raw_emails if '@' in e]
                    # Filter out publisher/editorial generic support emails
                    clean_emails = [
                        e for e in clean_emails
                        if not any(e.lower().startswith(p) for p in ("plosone@", "onepress@", "customercare@", "support@", "help@", "info@", "editorial@"))
                    ]
                    if clean_emails:
                        saved += 1
                        records.append({
                            "file_path": None,
                            "pdf_name": f"{pmcid}.xml",
                            "title": title,
                            "authors": authors_meta,
                            "doi": doi,
                            "source_journal": source_journal,
                            "_html_emails": list(dict.fromkeys(clean_emails)),
                        })
                        log.info(f"[EuropePMC] ({saved}/{limit}) Extracted {len(clean_emails)} emails via fullTextXML for {pmcid}")
                        continue
            except Exception as xml_err:
                log.warning(f"[EuropePMC] XML extraction failed for {pmcid}: {xml_err}")

        # Strategy 2: Fallback to direct PDF download attempts
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