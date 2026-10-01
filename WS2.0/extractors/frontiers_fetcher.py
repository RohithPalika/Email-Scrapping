import os
import re
import time
import logging
import requests

log = logging.getLogger("extraction.frontiers")

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


def fetch_frontiers_papers(topic, limit, target_dir, filters=None):
    """
    Searches and extracts author emails from Frontiers journals (https://www.frontiersin.org).
    Frontiers articles (prefix 10.3389) are Open Access. We query OpenAlex & Crossref for Frontiers
    works, then extract author emails directly from the Frontiers article HTML metadata or download
    the open-access Frontiers PDF.

    filters (dict, optional):
        year_from     (int|None)
        year_to       (int|None)
        countries     (list[str])
    """
    filters = filters or {}
    base_url = "https://api.openalex.org/works"

    # Frontiers prefix: 10.3389
    api_filters = [
        "doi:10.3389*",
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
    log.info(f"[Frontiers] filter={filter_str!r}, search={topic!r}")

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
            log.error(f"[Frontiers] OpenAlex API HTTP {resp.status_code}")
            return []
        items = resp.json().get("results", [])
        log.info(f"[Frontiers] Got {len(items)} works matching Frontiers.")
    except Exception as e:
        log.error(f"[Frontiers] API error: {e}")
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
        journal_name = journal_info.get("display_name", "Frontiers") or "Frontiers"

        authors_meta = []
        for authorship in item.get("authorships", []):
            name = authorship.get("author", {}).get("display_name")
            if name and name not in authors_meta:
                authors_meta.append(name)

        # Strategy 1: HTML-First author email extraction from Frontiers article page
        html_emails = []
        if doi:
            article_urls = [
                f"https://www.frontiersin.org/articles/{doi}/full",
                f"https://doi.org/{doi}",
            ]
            for a_url in article_urls:
                try:
                    h_resp = requests.get(a_url, headers=STEALTH_HEADERS, timeout=12, allow_redirects=True)
                    if h_resp.status_code == 200 and "<html" in h_resp.text.lower():
                        # Look for meta tags: <meta name="citation_author_email" content="...">
                        meta_emails = re.findall(r'<meta[^>]+citation_author_email[^>]+content=["\']([^"\']+)["\']', h_resp.text, re.I)
                        # Look for mailto links:
                        mailto_emails = re.findall(r'href=["\']mailto:([^"?\'\s>]+)', h_resp.text, re.I)
                        # Look for general email patterns inside corresponding author sections:
                        all_found = meta_emails + mailto_emails
                        for fe in all_found:
                            ce = _clean_email(fe)
                            if ce and not any(ce.lower().startswith(p) for p in ("support@", "help@", "info@", "editorial@", "production@")):
                                if ce not in html_emails:
                                    html_emails.append(ce)
                        if html_emails:
                            break
                except Exception as h_err:
                    log.debug(f"[Frontiers] HTML check error: {h_err}")

        if html_emails:
            saved += 1
            records.append({
                "file_path": None,
                "pdf_name": f"frontiers_{saved}.html",
                "title": title,
                "authors": authors_meta,
                "doi": doi,
                "source_journal": journal_name,
                "_html_emails": html_emails,
            })
            log.info(f"[Frontiers] ({saved}/{limit}) Extracted {len(html_emails)} email(s) directly from Frontiers HTML ({doi})")
            continue

        # Strategy 2: Direct PDF download
        pdf_urls = []
        best_oa = item.get("best_oa_location", {}) or {}
        if best_oa.get("pdf_url"):
            pdf_urls.append(best_oa["pdf_url"])
        if doi:
            pdf_urls.append(f"https://www.frontiersin.org/articles/{doi}/pdf")

        pdf_name = f"frontiers_paper_{saved + 1}.pdf"
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
                log.warning(f"[Frontiers] PDF download error ({p_url}): {p_err}")

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
            log.info(f"[Frontiers] ({saved}/{limit}) Downloaded Frontiers PDF for {doi}")

    return records
