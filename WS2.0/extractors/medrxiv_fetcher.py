import os
import requests

def download_binary_pdf(url, file_path):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
        if resp.status_code == 200 and b"%PDF" in resp.content[:20]:
            with open(file_path, "wb") as f:
                f.write(resp.content)
            return True
    except Exception:
        pass
    return False

def fetch_medrxiv_papers(query, max_papers, output_dir, start_year=2020, end_year=2026):
    """Fetches up to max_papers (max 500) medRxiv preprints with full text PDFs."""
    limit = max(1, min(int(max_papers), 500))
    base_url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
    downloaded_records = []
    cursor = "*"

    search_query = f'({query}) AND (SRC:PPR OR PUB_TYPE:"Preprint") AND (BOOK:"medRxiv" OR JOURNAL:"medRxiv") AND PUB_YEAR:[{start_year} TO {end_year}]'

    while len(downloaded_records) < limit and cursor:
        params = {
            "query": search_query,
            "format": "json",
            "pageSize": 50,
            "cursorMark": cursor,
            "resultType": "core"
        }

        try:
            res = requests.get(base_url, params=params, timeout=25).json()
            results = res.get("resultList", {}).get("result", [])
            cursor = res.get("nextCursorMark")
        except Exception as e:
            print(f"medRxiv API Pagination notice: {e}")
            break

        if not results:
            break

        for item in results:
            if len(downloaded_records) >= limit:
                break

            title = item.get("title", "Untitled")
            doi = item.get("doi", "")
            author_str = item.get("authorString", "")
            authors = [a.strip() for a in author_str.split(",") if a.strip()]

            # Resolve PDF download URL
            pdf_url = None
            for fulltext in item.get("fullTextUrlList", {}).get("fullTextUrl", []):
                if fulltext.get("documentStyle") == "pdf":
                    pdf_url = fulltext.get("url")
                    break

            # Fallback to direct DOI resolver
            if not pdf_url and doi:
                pdf_url = f"https://www.medrxiv.org/content/{doi}.full.pdf"

            if not pdf_url:
                continue

            count = len(downloaded_records) + 1
            temp_pdf_name = f"medrxiv_oa_{count}.pdf"
            file_path = os.path.join(output_dir, temp_pdf_name)

            if download_binary_pdf(pdf_url, file_path):
                downloaded_records.append({
                    "pdf_name": temp_pdf_name,
                    "file_path": file_path,
                    "title": title,
                    "authors": authors,
                    "doi": f"https://doi.org/{doi}" if doi else "",
                    "pdf_url": pdf_url
                })
                print(f"[{count}/{limit}] Downloaded medRxiv: {temp_pdf_name} - {title[:50]}...")

    return downloaded_records