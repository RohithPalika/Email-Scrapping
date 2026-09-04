import os
import re
import time
import requests
from urllib.parse import urljoin


def _download_pdf_with_retry(url, headers, file_path, retries=1, timeout=10, delay=1):
    """Fail-fast download: Try once (or twice max), quickly timeout if stuck."""
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            if resp.status_code == 200 and (
                resp.content.startswith(b"%PDF") or b"%PDF-" in resp.content[:1024]
            ):
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception as e:
            print(f"  [iMedPub] Download failed (timeout/block): {e}")
        time.sleep(delay)
    return False

def fetch_imedpub_papers(topic, limit, target_dir):
    """
    Searches and downloads open access articles/PDFs from iMedPub journals.
    """
    search_query = topic.replace(" ", "+")
    base_search_url = f"https://www.imedpub.com/search.php?keyword={search_query}"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    try:
        resp = requests.get(base_search_url, headers=headers, timeout=25)
        if resp.status_code != 200:
            return []
        html = resp.text
    except Exception as e:
        print(f"iMedPub search error: {e}")
        return []

    # Find article links and titles from iMedPub search results
    article_links = re.findall(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', html, re.I)
    
    records = []
    saved_count = 0
    seen_urls = set()

    for href, title in article_links:
        if saved_count >= limit:
            break

        # Filter out navigation links
        if not href.endswith(".php") and not href.endswith(".html") and "/articles/" not in href:
            continue
        if any(skip in href.lower() for skip in ["submit-manuscript", "contact", "about-us", "home", "search", "archive"]):
            continue

        full_url = urljoin("https://www.imedpub.com/", href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        try:
            art_resp = requests.get(full_url, headers=headers, timeout=25)
            if art_resp.status_code != 200:
                continue
            art_html = art_resp.text

            # Look for direct PDF download button/link
            pdf_match = re.search(r'href="([^"]+\.pdf)"', art_html, re.I)
            if not pdf_match:
                continue

            pdf_url = urljoin(full_url, pdf_match.group(1))

            # Extract DOI if present in article page
            doi_match = re.search(r'(10\.\d{4,9}/[-._;()/:A-Z0-9]+)', art_html, re.I)
            doi = doi_match.group(1) if doi_match else "N/A"

            # Extract Authors from HTML meta tags or header lines
            authors_meta = re.findall(r'<meta\s+name="citation_author"\s+content="([^"]+)"', art_html, re.I)
            if not authors_meta:
                # Fallback: scan author line text
                auth_block = re.search(r'<div[^>]*class="[^"]*authors?[^"]*"[^>]*>(.*?)</div>', art_html, re.I | re.S)
                if auth_block:
                    authors_meta = [re.sub(r'<[^>]+>', '', a).strip() for a in auth_block.group(1).split(",") if a.strip()]

            # Download PDF with retry logic
            pdf_name = f"imedpub_paper_{saved_count + 1}.pdf"
            file_path = os.path.join(target_dir, pdf_name)

            success = _download_pdf_with_retry(pdf_url, headers, file_path)
            if success:
                saved_count += 1
                records.append({
                    "file_path": file_path,
                    "pdf_name": pdf_name,
                    "title": title.strip(),
                    "authors": authors_meta,
                    "doi": doi
                })

            # Polite 1-second delay between each download
            time.sleep(1)

        except Exception:
            continue

    return records