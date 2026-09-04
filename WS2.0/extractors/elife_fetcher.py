import os
import requests

def fetch_elife_papers(topic, limit, target_dir):
    """Searches and downloads PDFs exclusively from eLife."""
    base_url = "https://api.elifesciences.org/search"
    params = {
        "for": topic,
        "page": 1,
        "per-page": limit,
        "sort": "relevance",
        "type[]": "research-article"
    }
    headers = {"Accept": "application/vnd.elife.search+json;version=2"}
    
    try:
        resp = requests.get(base_url, params=params, headers=headers, timeout=25)
        if resp.status_code != 200:
            return []
        items = resp.json().get("items", [])
    except Exception as e:
        print(f"eLife API error: {e}")
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
                with open(file_path, "wb") as f:
                    f.write(pdf_resp.content)
                records.append({
                    "file_path": file_path,
                    "pdf_name": pdf_name,
                    "title": title,
                    "authors": authors_meta,
                    "doi": doi
                })
        except Exception:
            continue
            
    return records