import os
import requests

def fetch_plos_papers(topic, limit, target_dir):
    """Searches and downloads PDFs exclusively from PLOS."""
    base_url = "https://api.plos.org/search"
    params = {
        "q": f"title:{topic} OR abstract:{topic}",
        "fl": "id,title,author_display",
        "wt": "json",
        "rows": limit
    }
    
    try:
        resp = requests.get(base_url, params=params, timeout=25)
        if resp.status_code != 200:
            return []
        docs = resp.json().get("response", {}).get("docs", [])
    except Exception as e:
        print(f"PLOS API error: {e}")
        return []

    records = []
    for i, doc in enumerate(docs, start=1):
        doi = doc.get("id")
        title = doc.get("title", "Untitled")
        authors_meta = doc.get("author_display", [])
        pdf_name = f"plos_paper_{i}.pdf"
        file_path = os.path.join(target_dir, pdf_name)
        pdf_url = f"https://journals.plos.org/plosone/article/file?id={doi}&type=printable"

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