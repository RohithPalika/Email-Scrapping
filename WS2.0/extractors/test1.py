import os
import re
import requests

ELSEVIER_LINEAGE_ID = "p4310320990"

def get_pdf_candidates(work):
    candidates = []
    for loc in work.get("locations", []):
        pdf_url = loc.get("pdf_url")
        landing_url = loc.get("landing_page_url") or ""
        
        if "ncbi.nlm.nih.gov/pmc/articles/" in landing_url:
            pmc_match = re.search(r'(PMC\d+)', landing_url)
            if pmc_match:
                candidates.insert(0, f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmc_match.group(1)}/pdf/")
        
        if pdf_url and pdf_url not in candidates:
            if any(repo in pdf_url.lower() for repo in ["sciencedirect", "elsevier", "nih.gov", "europepmc", "zenodo", "biorxiv", "arxiv"]):
                candidates.insert(0, pdf_url)
            else:
                candidates.append(pdf_url)

    best_oa = work.get("best_oa_location") or {}
    best_pdf = best_oa.get("pdf_url")
    if best_pdf and best_pdf not in candidates:
        candidates.append(best_pdf)

    return candidates

def download_binary_pdf(url_list, file_path):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9"
    }

    for url in url_list:
        try:
            resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
            if resp.status_code == 200 and b"%PDF" in resp.content[:20]:
                with open(file_path, "wb") as f:
                    f.write(resp.content)
                return url
        except Exception:
            continue

    return None

def fetch_sciencedirect_papers(query, max_papers, output_dir, start_year=2020, end_year=2026):
    limit = max(1, min(int(max_papers), 500))
    base_url = "https://api.openalex.org/works"
    cursor = "*"
    downloaded_records = []

    filter_params = (
        f"default.search:{query},"
        f"from_publication_date:{start_year}-01-01,"
        f"to_publication_date:{end_year}-12-31,"
        f"primary_location.source.publisher_lineage:{ELSEVIER_LINEAGE_ID},"
        f"is_oa:true"
    )

    while len(downloaded_records) < limit and cursor:
        params = {
            "filter": filter_params,
            "sort": "relevance_score:desc",
            "per-page": 50,
            "cursor": cursor,
            "mailto": "research_pipeline@academic-parser.org"
        }

        try:
            response = requests.get(base_url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
            data = response.json()
            results = data.get("results", [])
            cursor = data.get("meta", {}).get("next_cursor")
        except Exception:
            break

        if not results:
            break

        for work in results:
            if len(downloaded_records) >= limit:
                break

            title = work.get("title", "Untitled")
            doi = work.get("doi", "")

            authors_meta = [
                a.get("author", {}).get("display_name")
                for a in work.get("authorships", [])
                if a.get("author", {}).get("display_name")
            ]

            pdf_candidates = get_pdf_candidates(work)
            if not pdf_candidates:
                continue

            count = len(downloaded_records) + 1
            temp_pdf_name = f"sciencedirect_oa_{count}.pdf"
            file_path = os.path.join(output_dir, temp_pdf_name)

            successful_url = download_binary_pdf(pdf_candidates, file_path)
            if successful_url:
                downloaded_records.append({
                    "pdf_name": temp_pdf_name,
                    "file_path": file_path,
                    "title": title,
                    "authors": authors_meta,
                    "doi": doi,
                    "pdf_url": successful_url
                })
                print(f"[{count}/{limit}] Downloaded ScienceDirect: {temp_pdf_name} - {title[:50]}...")

    return downloaded_records