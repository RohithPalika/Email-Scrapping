import os
import logging
import requests

log = logging.getLogger("extraction.plos")

# PLOS article type labels → PLOS article_type values
_PLOS_TYPE_MAP = {
    "Article":          "Research Article",
    "Review":           "Review",
    "Systematic Review":"Systematic Review",
    "Case Reports":     "Case Report",
    "Case Report":      "Case Report",
    "Brief Report":     "Short Report",
}


def fetch_plos_papers(topic, limit, target_dir, filters=None):
    """
    Searches and downloads PDFs exclusively from PLOS.

    filters (dict, optional):
        year_from     (int|None):  post-fetch year check on publication_date field
        year_to       (int|None):  post-fetch year check on publication_date field
        article_types (list[str]): mapped to PLOS article_type values in query
        countries: not supported by PLOS API — silently ignored
    """
    filters       = filters or {}
    year_from     = filters.get("year_from")
    year_to       = filters.get("year_to")
    article_types = filters.get("article_types", [])

    # Build PLOS query — topic + optional article type filter
    query = f"title:{topic} OR abstract:{topic}"
    plos_types = list({_PLOS_TYPE_MAP[t] for t in article_types if t in _PLOS_TYPE_MAP})
    if plos_types:
        type_clause = " OR ".join(f'article_type:"{t}"' for t in plos_types)
        query = f"({query}) AND ({type_clause})"

    # Add year filter into the Solr query if specified
    if year_from and year_to:
        query += f" AND publication_date:[{year_from}-01-01T00:00:00Z TO {year_to}-12-31T23:59:59Z]"
    elif year_from:
        query += f" AND publication_date:[{year_from}-01-01T00:00:00Z TO *]"
    elif year_to:
        query += f" AND publication_date:[* TO {year_to}-12-31T23:59:59Z]"

    base_url = "https://api.plos.org/search"
    params = {
        "q":    query,
        "fl":   "id,title,author_display,publication_date",
        "wt":   "json",
        "rows": limit,
    }
    log.info(f"[PLOS] Query: {query!r}")

    try:
        resp = requests.get(base_url, params=params, timeout=25)
        if resp.status_code != 200:
            log.error(f"[PLOS] API HTTP {resp.status_code}")
            return []
        docs = resp.json().get("response", {}).get("docs", [])
    except Exception as e:
        log.error(f"[PLOS] API error: {e}")
        return []

    records = []
    for i, doc in enumerate(docs, start=1):
        doi          = doc.get("id")
        title        = doc.get("title", "Untitled")
        authors_meta = doc.get("author_display", [])
        pdf_name     = f"plos_paper_{i}.pdf"
        file_path    = os.path.join(target_dir, pdf_name)
        pdf_url      = f"https://journals.plos.org/plosone/article/file?id={doi}&type=printable"

        try:
            pdf_resp = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            if pdf_resp.status_code == 200 and b"%PDF" in pdf_resp.content[:10]:
                with open(file_path, "wb") as f:
                    f.write(pdf_resp.content)
                records.append({
                    "file_path": file_path,
                    "pdf_name":  pdf_name,
                    "title":     title,
                    "authors":   authors_meta,
                    "doi":       doi,
                })
        except Exception as e:
            log.warning(f"[PLOS] PDF download failed for DOI {doi}: {e}")
            continue

    return records