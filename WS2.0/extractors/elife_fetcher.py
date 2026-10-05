import os
import logging
import requests

log = logging.getLogger("extraction.elife")

# eLife article type mapping
_ELIFE_TYPE_MAP = {
    "Article":          "research-article",
    "Review":           "review-article",
    "Systematic Review":"review-article",
    "Case Reports":     "research-article",
    "Case Report":      "research-article",
    "Brief Report":     "short-report",
}


def fetch_elife_papers(topic, limit, target_dir, filters=None):
    """Searches and downloads PDFs exclusively from eLife.

    filters (dict, optional):
        year_from     (int|None):  mapped to start-date in eLife API
        year_to       (int|None):  mapped to end-date in eLife API
        article_types (list[str]): mapped to type[] eLife API params
        countries: not supported by eLife API — silently ignored
    """
    filters       = filters or {}
    year_from     = filters.get("year_from")
    year_to       = filters.get("year_to")
    article_types = filters.get("article_types", [])

    base_url = "https://api.elifesciences.org/search"
    params = {
        "for":      topic,
        "page":     1,
        "per-page": limit,
        "sort":     "relevance",
    }

    # Map UI types to eLife type strings (deduplicate)
    elife_types = list({_ELIFE_TYPE_MAP[t] for t in article_types if t in _ELIFE_TYPE_MAP})
    if elife_types:
        params["type[]"] = elife_types
    else:
        params["type[]"] = "research-article"  # Default: research articles only

    if year_from:
        params["start-date"] = f"{year_from}-01-01"
    if year_to:
        params["end-date"]   = f"{year_to}-12-31"

    headers = {"Accept": "application/vnd.elife.search+json;version=2"}
    log.info(f"[eLife] Params: {params}")
    
    try:
        resp = requests.get(base_url, params=params, headers=headers, timeout=25)
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
    except Exception as e:
        log.error(f"[eLife] API error: {e}")
        return []

    records = []
    for i, item in enumerate(items, start=1):
        article_id = item.get("id")
        doi = item.get("doi", f"10.7554/eLife.{article_id}")
        title = item.get("title", "Untitled")
        
        authors_meta = []
        raw_authors = item.get("authorLine", "")
        if isinstance(raw_authors, str):
            authors_meta = [name.strip() for name in raw_authors.split(",") if name.strip()]
        elif isinstance(raw_authors, list):
            for a in raw_authors:
                if isinstance(a, dict):
                    name = a.get("name", {}).get("preferred") or a.get("name", {}).get("index", "")
                    if name:
                        authors_meta.append(name.strip())
                elif isinstance(a, str):
                    authors_meta.append(a.strip())

        pdf_name = f"elife_paper_{i}.pdf"
        file_path = os.path.join(target_dir, pdf_name)
        pdf_url = item.get("pdf", f"https://elifesciences.org/articles/{article_id}.pdf")
        
        try:
            pdf_resp = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if pdf_resp.status_code == 200 and b"%PDF" in pdf_resp.content[:10]:
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "wb") as f:
                    f.write(pdf_resp.content)
                records.append({
                    "file_path": file_path,
                    "pdf_name": pdf_name,
                    "title": title,
                    "authors": authors_meta,
                    "doi": doi
                })
        except Exception as e:
            log.warning(f"[eLife] PDF download failed for article {article_id}: {e}")
            continue
            
    return records