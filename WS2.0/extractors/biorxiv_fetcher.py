import os
import re
import time
import logging
import requests

log = logging.getLogger("extraction.biorxiv")

# Polite headers for APIs
API_HEADERS = {
    "User-Agent": "AcademicEmailExtractor/1.0 (Research Outreach Tool)"
}

# Stealth headers for PDF downloads
STEALTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/pdf,application/xhtml+xml,text/html,application/xml;q=0.9,*/*;q=0.8"
}

# Both bioRxiv and medRxiv share the same API structure
SERVERS = ["biorxiv", "medrxiv"]


def _download_pdf_with_retry(url, file_path, retries=1, timeout=10, delay=1):
    """Fail-fast download: Try once (or twice max), quickly timeout if stuck."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=STEALTH_HEADERS, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200 and (
                resp.content.startswith(b"%PDF") or b"%PDF-" in resp.content[:1024]
            ):
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception as e:
            log.warning(f"  [bioRxiv] Download failed (timeout/block): {e}")

        time.sleep(delay)

    return False


def fetch_biorxiv_papers(topic, limit, target_dir, filters=None):
    """
    Searches and downloads PDFs from bioRxiv AND medRxiv.
    Uses Crossref (with prefix:10.1101) to find relevant preprints by topic,
    then constructs direct PDF links and tries both servers.

    filters (dict, optional):
        year_from (int|None): applied as from-pub-date in Crossref filter
        year_to   (int|None): applied as until-pub-date in Crossref filter
        countries / article_types: not supported by bioRxiv — silently ignored
    """
    filters     = filters or {}
    year_from   = filters.get("year_from")
    year_to     = filters.get("year_to")

    # Build Crossref filter for bioRxiv/medRxiv preprints
    filter_parts = ["prefix:10.1101", "type:posted-content"]
    if year_from:
        filter_parts.append(f"from-pub-date:{year_from}-01-01")
    if year_to:
        filter_parts.append(f"until-pub-date:{year_to}-12-31")
    crossref_filter = ",".join(filter_parts)

    records = []
    saved_count = 0
    offset = 0
    max_offset = 1000

    log.info(f"[bioRxiv/medRxiv] Searching for: '{topic}' (targeting {limit} papers)...")
    log.info(f"[bioRxiv/medRxiv] Crossref filter: {crossref_filter!r}")

    while saved_count < limit and offset < max_offset:
        crossref_url = "https://api.crossref.org/works"
        params = {
            "query":   topic,
            "filter":  crossref_filter,
            "select":  "DOI,title,author,published",
            "rows":    min(100, (limit - saved_count) * 2),
            "offset":  offset,
            "mailto":  "23r25a6702@mlrit.ac.in"
        }

        try:
            time.sleep(1)
            resp = requests.get(crossref_url, params=params, headers=API_HEADERS, timeout=15)
            if resp.status_code != 200:
                log.warning(f"[bioRxiv/medRxiv] Search failed. HTTP {resp.status_code}")
                break
            items = resp.json().get("message", {}).get("items", [])
        except Exception as e:
            log.error(f"[bioRxiv/medRxiv] API error: {e}")
            break

        if not items:
            log.info(f"[bioRxiv/medRxiv] No more candidates found at offset {offset}.")
            break

        log.info(f"  [bioRxiv/medRxiv] Found {len(items)} candidates at offset {offset}. Downloading PDFs...")

        for item in items:
            if saved_count >= limit:
                break

            doi = item.get("DOI", "N/A")
            titles = item.get("title", ["Untitled"])
            title = titles[0] if titles else "Untitled"

            # Extract author names
            authors_meta = []
            for auth in item.get("author", []):
                given = auth.get("given", "")
                family = auth.get("family", "")
                full_name = f"{given} {family}".strip()
                if full_name:
                    authors_meta.append(full_name)

            if not doi or doi == "N/A" or not re.match(r"^10\.1101/\d{4}\.", doi):
                continue

            pdf_name = f"biorxiv_paper_{saved_count + 1}.pdf"
            file_path = os.path.join(target_dir, pdf_name)
            
            log.info(f"    ({saved_count + 1}/{limit}) {title[:50]}...")
            
            # Since both use the 10.1101 prefix, a paper could be on either server.
            # We try biorxiv first, then medrxiv if it fails (404).
            success = False
            found_server = "biorxiv"
            for server in SERVERS:
                pdf_url = f"https://www.{server}.org/content/{doi}.full.pdf"
                if _download_pdf_with_retry(pdf_url, file_path, retries=0):
                    found_server = server
                    success = True
                    break
                # Polite delay between fallback attempts
                time.sleep(0.5)

            if success:
                saved_count += 1
                records.append({
                    "file_path": file_path,
                    "pdf_name": pdf_name,
                    "title": title,
                    "authors": authors_meta,
                    "doi": doi,
                    "source_journal": found_server.capitalize()
                })
            else:
                log.warning(f"      -> Skipped (not found on either server or download failed).")

            # Polite 1-second delay between each paper
            time.sleep(1)
            
        offset += params["rows"]

    log.info(f"[bioRxiv/medRxiv] Done. Successfully downloaded {saved_count} PDFs.")
    return records
