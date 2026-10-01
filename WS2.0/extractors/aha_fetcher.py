import os
import re
import time
import logging
import requests

log = logging.getLogger("extraction.aha")

CONTACT_EMAIL = "23r25a6702@mlrit.ac.in"
API_HEADERS = {
    "User-Agent": f"AcademicEmailExtractor/1.0 (Research Outreach Tool; mailto:{CONTACT_EMAIL})"
}
STEALTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8"
}

_COUNTRY_CODES = {
    "USA":          "US",
    "UK":           "GB",
    "Italy":        "IT",
    "Spain":        "ES",
    "Romania":      "RO",
    "France":       "FR",
    "Brazil":       "BR",
    "Germany":      "DE",
    "Australia":    "AU",
    "Canada":       "CA",
    "Mexico":       "MX",
    "Saudi Arabia": "SA",
    "Egypt":        "EG",
}


def _clean_email(raw):
    if not raw:
        return None
    cleaned = raw.strip(" \t\n\r*\u2020\u2021\u00a7#()[]{}<>,;:'\"")
    cleaned = re.split(r'[\s\(\[\{]', cleaned)[0].rstrip(".,;:")
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', cleaned):
        return cleaned
    return None


def fetch_aha_papers(topic, limit, target_dir, filters=None):
    """
    Searches and extracts author emails from American Heart Association Journals (https://www.ahajournals.org).
    Finds AHA works (Circulation, Stroke, JAHA, Hypertension - prefix 10.1161), resolves emails
    via PMC/EuropePMC fullTextXML or downloads open-access PDFs for parsing.

    filters (dict, optional):
        year_from     (int|None)
        year_to       (int|None)
        countries     (list[str])
    """
    filters = filters or {}
    base_url = "https://api.openalex.org/works"

    # Filter for AHA publisher lineage or DOI prefix 10.1161 + Open Access
    api_filters = [
        "primary_location.source.host_organization_lineage:https://openalex.org/P4310319760",
        "is_oa:true",
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
    log.info(f"[AHA] filter={filter_str!r}, search={topic!r}")

    params = {
        "search": topic,
        "filter": filter_str,
        "per-page": min(limit * 3, 200),
        "sort": "relevance_score:desc",
        "mailto": CONTACT_EMAIL,
    }

    try:
        resp = requests.get(base_url, params=params, headers=API_HEADERS, timeout=25)
        if resp.status_code != 200:
            log.error(f"[AHA] OpenAlex API HTTP {resp.status_code}")
            return []
        items = resp.json().get("results", [])
        log.info(f"[AHA] Found {len(items)} Open Access AHA articles.")
    except Exception as e:
        log.error(f"[AHA] API error: {e}")
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
        journal_info = item.get("primary_location", {}).get("source", {}) or {}
        journal_name = journal_info.get("display_name", "AHA Journals") or "AHA Journals"

        authors_meta = []
        for authorship in item.get("authorships", []):
            name = authorship.get("author", {}).get("display_name")
            if name and name not in authors_meta:
                authors_meta.append(name)

        # Strategy 1: Check EuropePMC for XML full text (JAHA & open AHA papers)
        if doi:
            try:
                epmc_search = f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:%22{doi}%22&format=json&resultType=core"
                epmc_resp = requests.get(epmc_search, headers={"Accept": "application/json"}, timeout=10)
                if epmc_resp.status_code == 200:
                    rlist = epmc_resp.json().get("resultList", {}).get("result", [])
                    if rlist:
                        pmcid = rlist[0].get("pmcid")
                        if pmcid:
                            xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                            xml_resp = requests.get(xml_url, headers={"Accept": "application/xml"}, timeout=12)
                            if xml_resp.status_code == 200:
                                raw_emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', xml_resp.text)
                                clean_emails = [
                                    _clean_email(e) for e in raw_emails
                                    if _clean_email(e) and not any(e.lower().startswith(p) for p in ("support@", "help@", "info@", "editorial@"))
                                ]
                                clean_emails = list(dict.fromkeys([e for e in clean_emails if e]))
                                if clean_emails:
                                    saved += 1
                                    records.append({
                                        "file_path": None,
                                        "pdf_name": f"{pmcid}.xml",
                                        "title": title,
                                        "authors": authors_meta,
                                        "doi": doi,
                                        "source_journal": journal_name,
                                        "_html_emails": clean_emails,
                                    })
                                    log.info(f"[AHA] ({saved}/{limit}) Extracted {len(clean_emails)} email(s) via PMC XML for {doi}")
                                    continue
            except Exception as epmc_err:
                log.debug(f"[AHA] EuropePMC fallback check: {epmc_err}")

        # Strategy 2: Download Open Access PDF
        pdf_urls = []
        best_oa = item.get("best_oa_location", {}) or {}
        if best_oa.get("pdf_url"):
            pdf_urls.append(best_oa["pdf_url"])
        if doi:
            pdf_urls.append(f"https://www.ahajournals.org/doi/pdf/{doi}")

        pdf_name = f"aha_paper_{saved + 1}.pdf"
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
                log.warning(f"[AHA] PDF download error ({p_url}): {p_err}")

        if download_success:
            saved += 1
            records.append({
                "file_path": file_path,
                "pdf_name": pdf_name,
                "title": title,
                "authors": authors_meta,
                "doi": doi,
                "source_journal": journal_name,
            })
            log.info(f"[AHA] ({saved}/{limit}) Downloaded AHA PDF for {doi}")

    return records
