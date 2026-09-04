import os
import time
import requests

def fetch_openalex_papers(topic, limit, target_dir):
    """
    Searches and downloads Open Access PDFs from OpenAlex REST API.
    """
    base_url = "https://api.openalex.org/works"
    params = {
        "search": topic,
        "filter": "has_oa_accepted_or_published_version:true,is_oa:true",
        "per-page": limit * 2,  # Fetch extra to handle direct PDF access fallbacks
        "sort": "relevance_score:desc"
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 (mailto:researcher@example.com)"
    }
    
    try:
        resp = requests.get(base_url, params=params, headers=headers, timeout=25)
        if resp.status_code != 200:
            return []
        items = resp.json().get("results", [])
        time.sleep(1)  # Polite delay after API call
    except Exception as e:
        print(f"OpenAlex API error: {e}")
        return []

    records = []
    saved_count = 0

    for item in items:
        if saved_count >= limit:
            break

        title = item.get("display_name") or item.get("title") or "Untitled"
        doi = item.get("doi") or "N/A"
        
        # Extract authors list
        authors_meta = []
        authorships = item.get("authorships", [])
        for a in authorships:
            author_obj = a.get("author", {})
            name = author_obj.get("display_name")
            if name:
                authors_meta.append(name.strip())

        # Extract direct PDF URLs
        pdf_urls = []
        best_oa = item.get("best_oa_location") or {}
        primary_loc = item.get("primary_location") or {}

        if best_oa.get("pdf_url"):
            pdf_urls.append(best_oa["pdf_url"])
        if primary_loc.get("pdf_url") and primary_loc["pdf_url"] not in pdf_urls:
            pdf_urls.append(primary_loc["pdf_url"])
            
        # Additional locations
        locations = item.get("locations", [])
        for loc in locations:
            pdf_u = loc.get("pdf_url")
            if pdf_u and pdf_u not in pdf_urls:
                pdf_urls.append(pdf_u)

        if not pdf_urls:
            continue

        pdf_name = f"openalex_paper_{saved_count + 1}.pdf"
        file_path = os.path.join(target_dir, pdf_name)
        download_success = False

        for url in pdf_urls:
            try:
                pdf_resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
                if pdf_resp.status_code == 200 and (pdf_resp.content.startswith(b"%PDF") or b"%PDF-" in pdf_resp.content[:1024]):
                    with open(file_path, "wb") as f:
                        f.write(pdf_resp.content)
                    download_success = True
                    break
            except Exception:
                continue

        if download_success:
            saved_count += 1
            # Extract journal/source name for the Source Journal column
            source_journal = "OpenAlex"
            primary_loc = item.get("primary_location") or {}
            source_obj = primary_loc.get("source") or {}
            if source_obj.get("display_name"):
                source_journal = source_obj["display_name"]
            records.append({
                "file_path": file_path,
                "pdf_name": pdf_name,
                "title": title,
                "authors": authors_meta,
                "doi": doi,
                "source_journal": source_journal
            })
            time.sleep(1)  # Polite delay between downloads

    return records