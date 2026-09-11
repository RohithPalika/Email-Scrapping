import os
import re
import time
import logging
import requests
import xml.etree.ElementTree as ET

log = logging.getLogger("extraction.arxiv")

# arXiv Atom feed namespace
ATOM_NS = "http://www.w3.org/2005/Atom"

# Polite headers for API search
API_HEADERS = {
    "User-Agent": "AcademicEmailExtractor/1.0 (Research Outreach Tool)"
}

# Stealth headers for HTML page / PDF downloads
STEALTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

# Email regex
EMAIL_RE = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')


# Proper email validation: TLD must be 2+ alphabetic characters only.
# This rejects version strings like dompurify@2.3.5 or similar false positives.
VALID_EMAIL_RE = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}$')


def _extract_emails_from_html(abstract_url):
    """
    Fetch the lightweight arXiv abstract HTML page (~50KB) and extract any
    REAL email addresses found in the page — no PDF download needed.

    Key fix: uses VALID_EMAIL_RE (requires alphabetic TLD) to reject
    JavaScript library version strings like 'dompurify@2.3.5'.
    """
    try:
        resp = requests.get(abstract_url, headers=STEALTH_HEADERS, timeout=8)
        if resp.status_code != 200:
            return []
        html = resp.text
        raw_emails = EMAIL_RE.findall(html)

        # Apply strict validation: TLD must be alphabetic, not a version number
        clean = []
        for e in raw_emails:
            if not VALID_EMAIL_RE.match(e):
                continue               # Rejects: dompurify@2.3.5, icon@1.0, etc.
            if len(e) > 80:
                continue
            if any(x in e.lower() for x in ["arxiv", "latex", ".png", ".jpg", ".css", "example"]):
                continue
            clean.append(e.lower())

        return list(set(clean))
    except Exception as e:
        log.warning(f"[arXiv] HTML email extraction failed for {abstract_url}: {e}")
        return []


def _download_pdf(url, file_path, timeout=12):
    """Download PDF — used only as fallback when HTML has no emails."""
    try:
        resp = requests.get(url, headers=STEALTH_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and (
            resp.content.startswith(b"%PDF") or b"%PDF-" in resp.content[:1024]
        ):
            with open(file_path, "wb") as f:
                f.write(resp.content)
            return True
    except Exception as e:
        log.warning(f"  [arXiv] PDF fallback failed: {e}")
    return False


def fetch_arxiv_papers(topic, limit, target_dir, filters=None):
    """
    HTML-First strategy:
      1. Query arXiv API to get paper list (1 request total).
      2. For each paper, fetch the abstract HTML page (~50KB) to extract emails.
      3. Only download the full PDF as a last resort if HTML has no emails.

    filters (dict, optional):
        year_from (int|None): adds submittedDate filter to the arXiv query
        year_to   (int|None): adds submittedDate filter to the arXiv query
        countries / article_types: not supported by arXiv — silently ignored
    """
    filters   = filters or {}
    year_from = filters.get("year_from")
    year_to   = filters.get("year_to")

    # Build search query — add submittedDate constraint if year filters present
    search_query = f"all:{topic}"
    if year_from or year_to:
        from_str = f"{year_from}0101" if year_from else "19900101"
        to_str   = f"{year_to}1231"   if year_to   else "20991231"
        search_query += f" AND submittedDate:[{from_str}000000 TO {to_str}235959]"

    base_url = "http://export.arxiv.org/api/query"
    params = {
        "search_query": search_query,
        "start":        0,
        "max_results":  limit,
        "sortBy":       "relevance",
        "sortOrder":    "descending",
    }

    log.info(f"[arXiv] Searching: {search_query!r} (up to {limit} papers)...")

    try:
        time.sleep(1)  # Reduced from 3s — only 1 API call needed
        resp = requests.get(base_url, params=params, headers=API_HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning(f"[arXiv] Search failed. HTTP {resp.status_code}")
            return []
        root = ET.fromstring(resp.text)
    except Exception as e:
        log.error(f"[arXiv] API error: {e}")
        return []

    entries = root.findall(f"{{{ATOM_NS}}}entry")
    log.info(f"[arXiv] Found {len(entries)} results. Extracting via HTML-first approach...")

    records = []

    for i, entry in enumerate(entries[:limit]):
        # --- Extract metadata ---
        title_el = entry.find(f"{{{ATOM_NS}}}title")
        title = title_el.text.strip().replace("\n", " ") if title_el is not None else "Untitled"

        id_el = entry.find(f"{{{ATOM_NS}}}id")
        arxiv_id_url = id_el.text.strip() if id_el is not None else ""

        # DOI — use arXiv ID URL as fallback
        doi = arxiv_id_url
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            if link.get("rel") == "related" and "doi" in link.get("href", ""):
                doi = link.get("href")
                break

        # Authors from API metadata
        authors_meta = []
        for author_el in entry.findall(f"{{{ATOM_NS}}}author"):
            name_el = author_el.find(f"{{{ATOM_NS}}}name")
            if name_el is not None and name_el.text:
                authors_meta.append(name_el.text.strip())

        # PDF link
        pdf_url = None
        for link in entry.findall(f"{{{ATOM_NS}}}link"):
            if link.get("title") == "pdf":
                pdf_url = link.get("href")
                break

        log.info(f"  [arXiv] ({i + 1}/{min(limit, len(entries))}) HTML scan: {title[:55]}...")

        # ---- STEP 1: Try HTML abstract page first (fast, ~50KB) ----
        abstract_url = arxiv_id_url  # e.g. http://arxiv.org/abs/1234.5678
        html_emails = _extract_emails_from_html(abstract_url)

        pdf_name = f"arxiv_paper_{i + 1}.pdf"
        file_path = os.path.join(target_dir, pdf_name)

        if html_emails:
            # Create a tiny placeholder file so app.py's PDF parser still works
            # but pre-inject the emails so no PDF parsing is needed
            with open(file_path, "wb") as f:
                f.write(b"HTML_EMAIL_EXTRACTED")
            record = {
                "file_path": file_path,
                "pdf_name": pdf_name,
                "title": title,
                "authors": authors_meta,
                "doi": doi,
                "_html_emails": html_emails  # Pre-extracted emails — skip PDF parse
            }
        elif pdf_url:
            # ---- STEP 2: Fall back to PDF only if HTML had nothing ----
            log.info(f"  [arXiv]   No email in HTML — trying PDF fallback...")
            success = _download_pdf(pdf_url, file_path)
            if not success:
                log.warning(f"  [arXiv]   PDF also failed, skipping.")
                time.sleep(0.5)
                continue
            record = {
                "file_path": file_path,
                "pdf_name": pdf_name,
                "title": title,
                "authors": authors_meta,
                "doi": doi
            }
        else:
            continue

        records.append(record)

        # Minimal polite delay — just 1 second between HTML fetches
        time.sleep(1)

    log.info(f"[arXiv] Done. Collected metadata for {len(records)} papers.")
    return records

