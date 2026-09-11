"""
ScienceDirect Fetcher — Definitive Implementation
==================================================

Confirmed working endpoints (tested against free dev key):
  - Scopus Search API  -> HTTP 200  (USE THIS for search)
  - SciDir Search API  -> HTTP 410  (DEAD - permanently deprecated)
  - Elsevier PDF API   -> HTTP 403  (requires institutional licence)

Strategy:
  Step 1: Scopus Search API  -> get DOIs + metadata  (free key works)
  Step 2: OpenAlex           -> resolve DOI to OA PDF URL (~40% hit rate)
  Step 3: Unpaywall          -> fallback OA resolver (50M+ papers)
  Step 4: PubMed Central     -> NIH-funded Elsevier papers
"""
import os
import re
import time
import logging
import requests

log = logging.getLogger("extraction.sciencedirect")

# =====================================================================
# PASTE YOUR FULL ELSEVIER API KEY BELOW
# =====================================================================
ELSEVIER_API_KEY = "a1552f50b092a9f6bf3a1628bd3f7201"

_CONTACT_EMAIL = "23r25a6702@mlrit.ac.in"

_HEADERS = {
    "User-Agent": f"EmailExtractionResearchTool/1.0 (mailto:{_CONTACT_EMAIL})"
}

# -------------------------------------------------------------------
# Step 1: Scopus Search — the ONLY working search endpoint for free keys
# -------------------------------------------------------------------
def _scopus_search(topic, count, filters=None):
    """
    Uses the Scopus Search API to fetch paper metadata (DOIs + authors).

    filters (dict, optional):
        year_from     (int|None):  added as DATE() constraint to query
        year_to       (int|None):  added as DATE() constraint to query
        article_types (list[str]): mapped to DOCTYPE() constraint
        countries: not available in free Scopus key — silently ignored
    """
    filters      = filters or {}
    year_from    = filters.get("year_from")
    year_to      = filters.get("year_to")
    article_types= filters.get("article_types", [])

    # Build Scopus CQL query
    query = f"TITLE-ABS-KEY({topic})"

    # Year range in Scopus: PUBYEAR > 2022 AND PUBYEAR < 2027
    if year_from:
        query += f" AND PUBYEAR > {year_from - 1}"
    if year_to:
        query += f" AND PUBYEAR < {year_to + 1}"

    # Article type in Scopus: DOCTYPE(ar) = Article, DOCTYPE(re) = Review
    _SCOPUS_TYPES = {
        "Article":          "ar",
        "Review":           "re",
        "Systematic Review":"re",
        "Brief Report":     "ar",
    }
    scopus_types = list({_SCOPUS_TYPES[t] for t in article_types if t in _SCOPUS_TYPES})
    if scopus_types:
        dtype_clause = " OR ".join(f"DOCTYPE({t})" for t in scopus_types)
        query += f" AND ({dtype_clause})"

    log.info(f"[ScienceDirect] Scopus query: {query!r}")
    url = "https://api.elsevier.com/content/search/scopus"
    params = {
        "query": query,   # Built above with PUBYEAR / DOCTYPE filters applied
        "count": count,
        # Only fetch the fields we actually need — keeps response small & fast
        "field": "prism:doi,dc:title,dc:creator,prism:publicationName,author",
        "sort":  "relevancy",
    }
    headers = {**_HEADERS, "X-ELS-APIKey": ELSEVIER_API_KEY, "Accept": "application/json"}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
        if resp.status_code != 200:
            log.error(
                f"[ScienceDirect] Scopus search failed: HTTP {resp.status_code}. "
                f"Response: {resp.text[:200]}"
            )
            return []

        entries = resp.json().get("search-results", {}).get("entry", [])
        log.info(f"[ScienceDirect] Scopus returned {len(entries)} candidates for '{topic}'.")

        results = []
        for e in entries:
            doi = (e.get("prism:doi") or "").strip()
            if not doi:
                continue

            title = (e.get("dc:title") or "Untitled").strip()

            # Authors: the 'author' field is a list of dicts in Scopus responses
            authors = []
            author_list = e.get("author", [])
            if isinstance(author_list, dict):
                author_list = [author_list]
            for a in author_list:
                name = (a.get("authname") or a.get("ce:indexed-name") or "").strip()
                if name:
                    authors.append(name)
            # Fallback: dc:creator (first author string)
            if not authors and e.get("dc:creator"):
                authors = [e.get("dc:creator").strip()]

            journal = (e.get("prism:publicationName") or "ScienceDirect").strip()

            results.append({
                "doi": doi,
                "title": title,
                "authors": authors,
                "journal": journal,
            })

        return results

    except Exception as exc:
        log.error(f"[ScienceDirect] Scopus exception: {exc}")
        return []


# Publishers that actively block script-based PDF downloads (403 / bot detection)
# We never attempt these — they waste time and always fail.
_BLOCKED_PUBLISHER_HOSTS = {
    "onlinelibrary.wiley.com",
    "linkinghub.elsevier.com",
    "www.sciencedirect.com",
    "link.springer.com",
    "www.tandfonline.com",
    "www.nature.com",
    "academic.oup.com",
    "journals.sagepub.com",
    "journals.lww.com",
}

def _is_blocked_url(url):
    """Return True if the URL is from a known bot-blocking publisher."""
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower()
        return any(blocked in host for blocked in _BLOCKED_PUBLISHER_HOSTS)
    except Exception:
        return False


# -------------------------------------------------------------------
# Step 2: OpenAlex — DOI → OA PDF URL (fastest, ~40% hit rate)
# -------------------------------------------------------------------
def _openalex_pdf_url(doi):
    """Query OpenAlex for the best open-access PDF URL for a given DOI."""
    url = f"https://api.openalex.org/works/doi:{doi}"
    params = {"select": "doi,best_oa_location,locations", "mailto": _CONTACT_EMAIL}
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        best = data.get("best_oa_location") or {}
        if best.get("pdf_url") and not _is_blocked_url(best["pdf_url"]):
            return best["pdf_url"]
        for loc in data.get("locations", []):
            if loc.get("pdf_url") and not _is_blocked_url(loc["pdf_url"]):
                return loc["pdf_url"]
    except Exception:
        pass
    return None


# -------------------------------------------------------------------
# Step 3: Unpaywall — DOI → OA PDF URL (broadest coverage, 50M+ papers)
# -------------------------------------------------------------------
def _unpaywall_pdf_url(doi):
    """
    Query Unpaywall for an open-access PDF URL for a given DOI.
    Unpaywall sometimes returns PMC article page URLs instead of direct PDFs.
    We detect this and convert them to the correct /pdf/ URL.
    """
    url = f"https://api.unpaywall.org/v2/{doi}"
    params = {"email": _CONTACT_EMAIL}
    try:
        resp = requests.get(url, params=params, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("is_oa"):
            return None
        best_loc = data.get("best_oa_location") or {}
        pdf_url = best_loc.get("url_for_pdf") or best_loc.get("url")
        if not pdf_url:
            return None
        # Block publisher domains that reject scripts
        if _is_blocked_url(pdf_url):
            return None
        # Unpaywall sometimes returns PMC HTML article pages.
        # Convert: https://www.ncbi.nlm.nih.gov/pmc/articles/4210374
        # To:      https://www.ncbi.nlm.nih.gov/pmc/articles/PMC4210374/pdf/
        import re as _re
        pmc_match = _re.search(
            r'ncbi\.nlm\.nih\.gov/pmc/articles/(?:PMC)?(\d+)', pdf_url
        )
        if pmc_match:
            pmc_id = pmc_match.group(1)
            return f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmc_id}/pdf/"
        return pdf_url
    except Exception:
        pass
    return None


# -------------------------------------------------------------------
# Step 4: PubMed Central — catches NIH-funded Elsevier papers
# -------------------------------------------------------------------
def _pmc_pdf_url(doi):
    """Look up a DOI in PubMed Central and return the correct PDF URL."""
    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    params = {
        "db": "pmc",
        "term": f"{doi}[doi]",
        "retmode": "json",
        "tool": "EmailExtractionTool",
        "email": _CONTACT_EMAIL,
    }
    try:
        resp = requests.get(search_url, params=params, headers=_HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        ids = resp.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return None
        pmcid = ids[0]
        # Construct the direct PDF URL — PMC serves PDFs at this path
        return f"https://www.ncbi.nlm.nih.gov/pmc/articles/PMC{pmcid}/pdf/"
    except Exception:
        pass
    return None


# -------------------------------------------------------------------
# PDF Downloader — validates the response is a real PDF file
# -------------------------------------------------------------------
def _download_pdf_from_url(pdf_url, file_path):
    """Download a PDF from any URL and validate it is a real PDF."""
    try:
        resp = requests.get(pdf_url, headers=_HEADERS, timeout=25, allow_redirects=True)
        if resp.status_code == 200 and (
            resp.content.startswith(b"%PDF") or b"%PDF-" in resp.content[:1024]
        ):
            with open(file_path, "wb") as f:
                f.write(resp.content)
            return True
    except Exception as e:
        log.debug(f"  [ScienceDirect] Download failed ({pdf_url[:60]}): {e}")
    return False


# -------------------------------------------------------------------
# Main Public Entry Point
# -------------------------------------------------------------------
def fetch_sciencedirect_papers(topic, limit, target_dir, filters=None):
    """
    Fetches papers from ScienceDirect via a 3-step strategy:
      1. Scopus Search API (free key) -> get DOIs + authors  (filters applied here)
      2. OpenAlex              -> resolve DOI to free OA PDF
      3. Unpaywall             -> if OpenAlex has no PDF
      4. PubMed Central        -> if Unpaywall has no PDF

    filters (dict, optional):
        year_from / year_to    -> applied as PUBYEAR constraints in Scopus CQL
        article_types          -> applied as DOCTYPE() constraints in Scopus CQL
        countries              -> not supported by free Scopus key
    """
    filters = filters or {}
    if not ELSEVIER_API_KEY.strip() or ELSEVIER_API_KEY == "YOUR_API_KEY_HERE":
        log.error("[ScienceDirect] API Key is missing!")
        return []

    # Step 1: Search via Scopus — fetch limit*3 candidates since many won't have OA PDFs
    papers = _scopus_search(topic, count=min(limit * 3, 50), filters=filters)
    if not papers:
        log.info("[ScienceDirect] No papers returned from Scopus. Try a different topic.")
        return []

    records = []
    saved_count = 0

    for paper in papers:
        if saved_count >= limit:
            break

        doi = paper["doi"]
        title = paper["title"]
        authors = paper["authors"]
        journal = paper["journal"]
        # Collapse Unicode whitespace for safe console logging
        safe_title = re.sub(r'[\u2000-\u200f\u2028\u2029\u00a0]', ' ', title)

        log.info(
            f"  [ScienceDirect] ({saved_count + 1}/{limit}) "
            f"Resolving: {safe_title[:50]}..."
        )

        pdf_name = f"sciencedirect_paper_{saved_count + 1}.pdf"
        file_path = os.path.join(target_dir, pdf_name)
        downloaded = False
        source_used = ""

        # --- Layer 2: OpenAlex ---
        oa_url = _openalex_pdf_url(doi)
        if oa_url:
            downloaded = _download_pdf_from_url(oa_url, file_path)
            if downloaded:
                source_used = "OpenAlex"

        # --- Layer 3: Unpaywall ---
        if not downloaded:
            uw_url = _unpaywall_pdf_url(doi)
            if uw_url:
                downloaded = _download_pdf_from_url(uw_url, file_path)
                if downloaded:
                    source_used = "Unpaywall"

        # --- Layer 4: PubMed Central ---
        if not downloaded:
            pmc_url = _pmc_pdf_url(doi)
            if pmc_url:
                downloaded = _download_pdf_from_url(pmc_url, file_path)
                if downloaded:
                    source_used = "PMC"

        if downloaded:
            log.info(f"    [OK] PDF downloaded via {source_used}.")
            saved_count += 1
            records.append({
                "file_path": file_path,
                "pdf_name": pdf_name,
                "title": title,
                "authors": authors,
                "doi": doi,
                "source_journal": journal,
            })
        else:
            log.info(f"    [SKIP] No open-access PDF found (DOI: {doi}).")

        time.sleep(0.5)  # Polite delay

    log.info(
        f"[ScienceDirect] Done. "
        f"Downloaded {saved_count} OA PDFs from {len(papers)} Scopus candidates."
    )
    return records
