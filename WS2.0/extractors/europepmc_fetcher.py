import os
import requests

def fetch_europepmc_papers(topic, limit, target_dir):
    """Searches and downloads Open Access PDFs exclusively from Europe PMC."""
    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    params = {
        "query": f'"{topic}" AND HAS_PDF:y AND OPEN_ACCESS:y',
        "format": "json",
        "pageSize": limit * 2,
        "resultType": "core"
    }
    
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    })
    
    try:
        resp = session.get(base_url, params=params, timeout=30)
        if resp.status_code != 200:
            return []
        results = resp.json().get("resultList", {}).get("result", [])
    except Exception as e:
        print(f"Europe PMC API error: {e}")
        return []

    records = []
    saved = 0

    for item in results:
        if saved >= limit:
            break

        pmcid = item.get("pmcid")
        doi = item.get("doi", "N/A")
        title = item.get("title", "Untitled").rstrip(".")
        author_string = item.get("authorString", "")
        authors_meta = [a.strip() for a in author_string.split(",") if a.strip()]

        pdf_urls = []
        if pmcid:
            pdf_urls.append(f"https://europepmc.org/articles/{pmcid}?pdf=render")
            pdf_urls.append(f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf")
        if doi and doi != "N/A":
            pdf_urls.append(f"https://doi.org/{doi}")

        if not pdf_urls:
            continue

        pdf_name = f"europepmc_paper_{saved + 1}.pdf"
        file_path = os.path.join(target_dir, pdf_name)
        
        download_success = False
        for url in pdf_urls:
            try:
                pdf_resp = session.get(url, timeout=30, allow_redirects=True)
                if pdf_resp.status_code == 200 and (pdf_resp.content.startswith(b"%PDF") or b"%PDF-" in pdf_resp.content[:1024]):
                    with open(file_path, "wb") as f:
                        f.write(pdf_resp.content)
                    download_success = True
                    break
            except Exception:
                continue

        if download_success:
            saved += 1
            records.append({
                "file_path": file_path,
                "pdf_name": pdf_name,
                "title": title,
                "authors": authors_meta,
                "doi": doi
            })

    return records