"""
iMedPub fetcher — ISSUE-05 fix.

Uses BeautifulSoup for robust HTML parsing (replacing brittle regex anchor-tag
scraping) and supports pagination to retrieve more than one search-results page.

If beautifulsoup4 is not installed, the module falls back to the original
regex-based implementation so the rest of the application is never broken.

Install: pip install beautifulsoup4
"""
import os
import re
import time
import logging
import requests
from urllib.parse import urljoin, urlencode

log = logging.getLogger("extraction.imedpub")

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False
    log.warning(
        "[iMedPub] beautifulsoup4 not installed. "
        "Falling back to legacy regex parser. "
        "Run: pip install beautifulsoup4"
    )

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

_BASE_SEARCH_URL = "https://www.imedpub.com/search.php"

# URL fragments that identify navigation / non-article links
_SKIP_PATTERNS = frozenset([
    "submit-manuscript", "contact", "about-us", "about_us", "about",
    "home", "search", "archive", "login", "register", "privacy",
    "terms", "advertise", "sitemap", "editorial-board",
])


def _download_pdf_with_retry(url, file_path, retries=1, timeout=10, delay=1):
    """Fail-fast download: try at most twice, quickly timeout on stuck connections."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(
                url, headers=_HEADERS, timeout=timeout, allow_redirects=True
            )
            if resp.status_code == 200 and (
                resp.content.startswith(b"%PDF") or b"%PDF-" in resp.content[:1024]
            ):
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception as e:
            log.warning(f"  [iMedPub] Download attempt {attempt + 1} failed: {e}")
        time.sleep(delay)
    return False


# ---------------------------------------------------------------------------
# Primary implementation — BeautifulSoup-based (ISSUE-05)
# ---------------------------------------------------------------------------

def _is_article_link(href):
    """Return True if the URL looks like an article page, not navigation."""
    if not href or href.startswith("#") or href.startswith("javascript"):
        return False
    href_lower = href.lower()
    if any(skip in href_lower for skip in _SKIP_PATTERNS):
        return False
    # Accept URLs that contain /articles/, end with .php/.html, or have an
    # article-like path segment (iMedPub uses numeric IDs in paths)
    if "/articles/" in href_lower:
        return True
    if href_lower.endswith((".php", ".html")):
        return True
    return False


def _extract_article_links_from_soup(soup, base_url):
    """
    Extract candidate article URLs from a BeautifulSoup-parsed search page.
    Tries multiple CSS selectors in priority order; falls back to any <a>
    whose href matches article-link heuristics.
    """
    article_urls = []
    seen = set()

    # Priority selectors — most specific first
    selectors = [
        "h2 > a[href]", "h3 > a[href]", "h4 > a[href]",
        ".article-title a[href]", "a.article-link[href]",
        "div.result a[href]", ".search-result a[href]",
        "li.article a[href]",
    ]

    for sel in selectors:
        for tag in soup.select(sel):
            href = tag.get("href", "")
            if _is_article_link(href):
                full = urljoin(base_url, href)
                if full not in seen:
                    seen.add(full)
                    article_urls.append(full)

    # Generic fallback: any <a> that passes the heuristic
    if not article_urls:
        for tag in soup.find_all("a", href=True):
            href = tag["href"]
            if _is_article_link(href):
                full = urljoin(base_url, href)
                if full not in seen:
                    seen.add(full)
                    article_urls.append(full)

    return article_urls


def _parse_article_page(url, saved_count, target_dir):
    """
    Fetch an individual article page and extract title, authors, DOI, and PDF.
    Returns a record dict on success, or None on failure.
    """
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=25)
        if resp.status_code != 200:
            log.warning(f"  [iMedPub] Article page returned HTTP {resp.status_code}: {url}")
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        log.warning(f"  [iMedPub] Failed to fetch article page {url}: {e}")
        return None

    # --- PDF URL ---
    pdf_url = None
    # 1. citation_pdf_url meta tag (most reliable across academic sites)
    pdf_meta = soup.find("meta", {"name": "citation_pdf_url"})
    if pdf_meta and pdf_meta.get("content"):
        pdf_url = pdf_meta["content"]
    else:
        # 2. Any <a> link ending in .pdf
        for a in soup.find_all("a", href=True):
            if a["href"].lower().endswith(".pdf"):
                pdf_url = urljoin(url, a["href"])
                break

    if not pdf_url:
        return None

    # --- Title ---
    title = ""
    title_meta = soup.find("meta", {"name": "citation_title"})
    if title_meta and title_meta.get("content"):
        title = title_meta["content"].strip()
    else:
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

    # --- DOI ---
    doi = "N/A"
    doi_meta = soup.find("meta", {"name": "citation_doi"})
    if doi_meta and doi_meta.get("content"):
        doi = doi_meta["content"].strip()
    else:
        doi_match = re.search(r'(10\.\d{4,9}/[-._;()/:A-Z0-9]+)', resp.text, re.I)
        if doi_match:
            doi = doi_match.group(1)

    # --- Authors ---
    authors = [
        m["content"].strip()
        for m in soup.find_all("meta", {"name": "citation_author"})
        if m.get("content")
    ]
    if not authors:
        # Fallback: look for an authors div
        auth_div = soup.find(class_=re.compile(r'\bauthors?\b', re.I))
        if auth_div:
            authors = [
                s.strip()
                for s in auth_div.get_text().split(",")
                if s.strip() and len(s.strip()) > 2
            ]

    # --- Download PDF ---
    pdf_name = f"imedpub_paper_{saved_count + 1}.pdf"
    file_path = os.path.join(target_dir, pdf_name)
    if not _download_pdf_with_retry(pdf_url, file_path):
        return None

    log.info(f"  [iMedPub] ({saved_count + 1}) Downloaded: {title[:55]}")
    return {
        "file_path": file_path,
        "pdf_name": pdf_name,
        "title": title,
        "authors": authors,
        "doi": doi,
        "source_journal": "iMedPub",
    }


def _fetch_imedpub_bs4(topic, limit, target_dir):
    """
    BeautifulSoup-based iMedPub fetcher with pagination support.
    """
    records = []
    saved_count = 0
    page = 1
    global_seen_urls = set()

    while saved_count < limit:
        params = {"keyword": topic, "page": page}
        search_url = _BASE_SEARCH_URL + "?" + urlencode(params)
        log.info(f"[iMedPub] Fetching search page {page}: {search_url}")

        try:
            resp = requests.get(search_url, headers=_HEADERS, timeout=25)
            if resp.status_code != 200:
                log.warning(
                    f"[iMedPub] Search page {page} returned HTTP {resp.status_code}. Stopping."
                )
                break
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            log.error(f"[iMedPub] Search request failed on page {page}: {e}")
            break

        article_urls = _extract_article_links_from_soup(soup, search_url)
        new_urls = [u for u in article_urls if u not in global_seen_urls]
        global_seen_urls.update(new_urls)

        if not new_urls:
            log.info(f"[iMedPub] No new article links on page {page}. Stopping.")
            break

        log.info(
            f"[iMedPub] Page {page}: found {len(new_urls)} candidate article(s)."
        )

        found_on_page = 0
        for url in new_urls:
            if saved_count >= limit:
                break
            record = _parse_article_page(url, saved_count, target_dir)
            if record:
                records.append(record)
                saved_count += 1
                found_on_page += 1
            time.sleep(1)  # Polite delay between article requests

        # Check for a "Next" page link
        next_link = (
            soup.find("a", string=re.compile(r'\bnext\b|›|»', re.I))
            or soup.find("a", title=re.compile(r'\bnext\b', re.I))
            or soup.select_one("a.next, li.next > a, .pagination .next")
        )
        if not next_link or found_on_page == 0:
            log.info(
                f"[iMedPub] No next page or no articles found on page {page}. Done."
            )
            break

        page += 1

    log.info(f"[iMedPub] Total downloaded: {saved_count} papers.")
    return records


# ---------------------------------------------------------------------------
# Legacy fallback — original regex-based implementation
# Used only when beautifulsoup4 is not installed.
# ---------------------------------------------------------------------------

def _fetch_imedpub_legacy(topic, limit, target_dir):
    """Original regex-based implementation (single page, no pagination)."""
    search_query = topic.replace(" ", "+")
    search_url = f"https://www.imedpub.com/search.php?keyword={search_query}"

    try:
        resp = requests.get(search_url, headers=_HEADERS, timeout=25)
        if resp.status_code != 200:
            return []
        html = resp.text
    except Exception as e:
        log.error(f"[iMedPub legacy] Search error: {e}")
        return []

    article_links = re.findall(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', html, re.I)
    records = []
    saved_count = 0
    seen_urls = set()

    for href, title in article_links:
        if saved_count >= limit:
            break
        if not (
            href.endswith(".php") or href.endswith(".html") or "/articles/" in href
        ):
            continue
        if any(skip in href.lower() for skip in _SKIP_PATTERNS):
            continue

        full_url = urljoin("https://www.imedpub.com/", href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        try:
            art_resp = requests.get(full_url, headers=_HEADERS, timeout=25)
            if art_resp.status_code != 200:
                continue
            art_html = art_resp.text

            pdf_match = re.search(r'href="([^"]+\.pdf)"', art_html, re.I)
            if not pdf_match:
                continue
            pdf_url = urljoin(full_url, pdf_match.group(1))
            doi_match = re.search(r'(10\.\d{4,9}/[-._;()/:A-Z0-9]+)', art_html, re.I)
            doi = doi_match.group(1) if doi_match else "N/A"
            authors_meta = re.findall(
                r'<meta\s+name="citation_author"\s+content="([^"]+)"', art_html, re.I
            )

            pdf_name = f"imedpub_paper_{saved_count + 1}.pdf"
            file_path = os.path.join(target_dir, pdf_name)
            if _download_pdf_with_retry(pdf_url, file_path):
                saved_count += 1
                records.append({
                    "file_path": file_path,
                    "pdf_name": pdf_name,
                    "title": title.strip(),
                    "authors": authors_meta,
                    "doi": doi,
                    "source_journal": "iMedPub",
                })
            time.sleep(1)
        except Exception as e:
            log.warning(f"[iMedPub legacy] Error on {full_url}: {e}")
            continue

    return records


# ---------------------------------------------------------------------------
# Public API — called by app.py
# ---------------------------------------------------------------------------

def fetch_imedpub_papers(topic, limit, target_dir, filters=None):
    """
    Public entry point that handles the fallback automatically.
    If beautifulsoup4 is not available, uses the robust regex implementation.

    filters (dict, optional): not supported by iMedPub site — silently ignored.
    """
    if _BS4_AVAILABLE:
        log.info("[iMedPub] Using BeautifulSoup extraction method.")
        return _fetch_imedpub_bs4(topic, limit, target_dir)
    else:
        log.info("[iMedPub] Using regex extraction method.")
        return _fetch_imedpub_legacy(topic, limit, target_dir)