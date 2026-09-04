import os
import time
import requests

# --- Replace this with your email address ---
CONTACT_EMAIL = "23r25a6702@mlrit.ac.in"

# Polite headers for API calls (Crossref Polite Pool)
API_HEADERS = {
    "User-Agent": f"AcademicEmailExtractor/1.0 (Research Outreach Tool; mailto:{CONTACT_EMAIL})"
}

# Stealth headers for PDF downloads (bypass anti-bot blocks)
STEALTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/pdf,application/xhtml+xml,text/html,application/xml;q=0.9,*/*;q=0.8"
}

# Publishers that ALWAYS block automated PDF downloads.
# Papers whose only available URLs are from these hosts are skipped
# immediately — no wasted timeout waiting for a connection that will fail.
PUBLISHER_BLOCKLIST = [
    "link.springer.com",
    "www.nature.com",
    "www.elsevier.com",
    "sciencedirect.com",
    "onlinelibrary.wiley.com",
    "academic.oup.com",
    "journals.sagepub.com",
    "www.tandfonline.com",
    "www.cell.com",
    "jamanetwork.com",
    "nejm.org",
    "bmj.com",
    "thelancet.com",
]


def _all_urls_blocked(urls):
    """
    Return True when EVERY URL in the list is from a known-blocked publisher.
    In that case we skip the paper entirely — no download will ever succeed.
    If even ONE URL is from a trusted/open host it's worth trying.
    """
    return all(
        any(blocked in url.lower() for blocked in PUBLISHER_BLOCKLIST)
        for url in urls
    )


def _download_pdf(url, file_path, timeout=12):
    """Download a PDF. Returns True on success, False on any failure."""
    try:
        resp = requests.get(url, headers=STEALTH_HEADERS, timeout=timeout, allow_redirects=True)
        if resp.status_code == 200 and (
            resp.content.startswith(b"%PDF") or b"%PDF-" in resp.content[:1024]
        ):
            with open(file_path, "wb") as f:
                f.write(resp.content)
            return True
    except Exception as e:
        print(f"    [PDF] Download failed: {e}")
    return False


def _query_openalex_chunk(batch):
    """Send exactly one chunk (≤50 DOIs) to OpenAlex. Returns {doi: [ordered_urls]}."""
    TRUSTED_HOSTS = [
        "arxiv.org", "europepmc.org", "biorxiv.org", "medrxiv.org",
        "ncbi.nlm.nih.gov", "plos.org", "mdpi.com", "frontiersin.org",
    ]
    result = {}
    if not batch:
        return result

    filter_str = "doi:" + "|".join(batch)
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={
                "filter": filter_str,
                "select": "doi,best_oa_location,locations",
                "per-page": 50,
                "mailto": CONTACT_EMAIL
            },
            headers=API_HEADERS,
            timeout=20
        )
        if resp.status_code != 200:
            print(f"  [OpenAlex batch] HTTP {resp.status_code} — {resp.text[:150]}")
            return result

        for work in resp.json().get("results", []):
            raw_doi = (work.get("doi") or "").replace("https://doi.org/", "").lower()
            if not raw_doi:
                continue

            # Collect ALL available PDF URLs
            all_pdf_urls = []
            best_oa = work.get("best_oa_location") or {}
            if best_oa.get("pdf_url"):
                all_pdf_urls.append(best_oa["pdf_url"])
            for loc in work.get("locations", []):
                url = loc.get("pdf_url")
                if url and url not in all_pdf_urls:
                    all_pdf_urls.append(url)

            if not all_pdf_urls:
                continue

            # Sort: trusted hosts first, publisher URLs last
            ordered_urls = []
            for trusted_host in TRUSTED_HOSTS:
                for url in all_pdf_urls:
                    if trusted_host in url.lower() and url not in ordered_urls:
                        ordered_urls.append(url)
            for url in all_pdf_urls:
                if url not in ordered_urls:
                    ordered_urls.append(url)

            result[raw_doi] = ordered_urls

    except Exception as e:
        print(f"  [OpenAlex batch] Error: {e}")

    return result


def _batch_resolve_pdf_urls(dois):
    """
    Resolve open-access PDF URLs for any number of DOIs via OpenAlex.
    Automatically splits into chunks of 50 (API limit) with a polite
    1-second delay between chunks.

    Returns {doi_lowercase: [ordered_url_list]} for every DOI that has
    at least one open-access PDF location.
    """
    doi_to_pdf = {}
    if not dois:
        return doi_to_pdf

    chunks = [dois[i:i + 50] for i in range(0, len(dois), 50)]
    for idx, chunk in enumerate(chunks):
        if idx > 0:
            time.sleep(1)   # Polite delay between OpenAlex API calls
        chunk_result = _query_openalex_chunk(chunk)
        doi_to_pdf.update(chunk_result)
        print(f"  [OpenAlex] Chunk {idx+1}/{len(chunks)}: resolved {len(chunk_result)} PDFs.")

    return doi_to_pdf


def fetch_crossref_papers(topic, limit, target_dir):
    """
    Two-phase approach for maximum PDF coverage:

    PHASE 1 — Crossref API calls (paginated):
        Get DOIs, titles, authors, journal names for candidates in chunks of 100.
        We keep fetching new pages (up to 10 max) until we successfully download `limit` PDFs.

    PHASE 2 — One OpenAlex batch call per chunk:
        Resolve open-access PDF URLs for all DOIs in the chunk.
    """
    base_url = "https://api.crossref.org/works"
    records = []
    saved_count = 0
    offset = 0
    max_offset = 1000  # Prevent infinite loops (max 10 pages of 100)

    print(f"[Crossref] Searching for: '{topic}' (targeting {limit} papers)...")

    while saved_count < limit and offset < max_offset:
        params = {
            "query": topic,
            "filter": "type:journal-article",
            "select": "DOI,title,author,container-title,publisher",
            "rows": 100,
            "offset": offset,
            "mailto": CONTACT_EMAIL
        }

        try:
            time.sleep(1)
            resp = requests.get(base_url, params=params, headers=API_HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"[Crossref] Search failed at offset {offset}. HTTP {resp.status_code}")
                break
            items = resp.json().get("message", {}).get("items", [])
        except Exception as e:
            print(f"[Crossref] API error: {e}")
            break

        if not items:
            print(f"[Crossref] No more candidates found at offset {offset}.")
            break

        print(f"\n[Crossref] --- Page {offset//100 + 1}: Found {len(items)} candidates ---")

        # --- PHASE 2: Batch-resolve PDF URLs via ONE OpenAlex call ---
        all_dois = [item.get("DOI", "") for item in items if item.get("DOI")]
        print(f"[Crossref] Batch-resolving PDF URLs for {len(all_dois)} DOIs via OpenAlex...")
        doi_to_pdf = _batch_resolve_pdf_urls(all_dois)
        print(f"[Crossref] Found open-access PDFs for {len(doi_to_pdf)} / {len(all_dois)} papers in this chunk.")

        for item in items:
            if saved_count >= limit:
                break

            doi = item.get("DOI", "N/A")
            if not doi or doi == "N/A":
                continue

            # Get the priority-ordered URL list for this DOI
            pdf_urls = doi_to_pdf.get(doi.lower())
            if not pdf_urls:
                continue   # No open-access PDF known for this paper

            titles = item.get("title", ["Untitled"])
            title = titles[0] if titles else "Untitled"

            # Author names
            authors_meta = []
            for auth in item.get("author", []):
                given = auth.get("given", "")
                family = auth.get("family", "")
                full_name = f"{given} {family}".strip()
                if full_name:
                    authors_meta.append(full_name)

            # Real journal name
            container_titles = item.get("container-title", [])
            publisher = item.get("publisher", "")
            if container_titles and container_titles[0].strip():
                source_journal = container_titles[0].strip()
            elif publisher.strip():
                source_journal = publisher.strip()
            else:
                source_journal = "Crossref"

            pdf_name = f"crossref_paper_{saved_count + 1}.pdf"
            file_path = os.path.join(target_dir, pdf_name)

            print(f"  [Crossref] ({saved_count + 1}/{limit}) [{source_journal[:25]}] {title[:45]}...")

            # Fast-skip: if every URL is from a known-blocked publisher,
            # don't waste 12+ seconds on connection timeouts — move to next candidate.
            if _all_urls_blocked(pdf_urls):
                print(f"    Skipped (all URLs from blocked publishers).")
                continue

            # Try each URL in priority order (trusted hosts first, publisher as fallback)
            success = False
            for attempt_url in pdf_urls:
                host_label = "trusted" if any(h in attempt_url for h in ["arxiv", "europepmc", "biorxiv", "medrxiv", "ncbi", "plos", "mdpi", "frontiersin"]) else "publisher"
                print(f"    Trying [{host_label}]: {attempt_url[:70]}")
                success = _download_pdf(attempt_url, file_path)
                if success:
                    print(f"    Downloaded OK.")
                    break
                else:
                    print(f"    Failed, trying next URL...")

            if success:
                saved_count += 1
                records.append({
                    "file_path": file_path,
                    "pdf_name": pdf_name,
                    "title": title,
                    "authors": authors_meta,
                    "doi": doi,
                    "source_journal": source_journal
                })
            else:
                print(f"    All URLs failed for this paper — skipping.")

            # Polite delay between download attempts
            time.sleep(0.5)

        # Move to next page of candidates
        offset += 100

    print(f"\n[Crossref] Done. Successfully downloaded {saved_count} PDFs total.")
    return records

