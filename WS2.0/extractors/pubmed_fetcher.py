import os
import re
import time
import logging
import xml.etree.ElementTree as ET
import requests

log = logging.getLogger("extraction.pubmed")

CONTACT_EMAIL = "23r25a6702@mlrit.ac.in"
API_HEADERS = {
    "User-Agent": f"AcademicEmailExtractor/1.0 (Research Outreach Tool; mailto:{CONTACT_EMAIL})"
}
STEALTH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8"
}

_PUBMED_TYPE_MAP = {
    "Article": "Journal Article",
    "Review": "Review",
    "Systematic Review": "Systematic Review",
    "Case Reports": "Case Reports",
    "Case Report": "Case Reports",
    "Brief Report": "Case Reports",
}


def _clean_email(raw):
    """Basic extraction and validation of emails from text."""
    if not raw:
        return None
    cleaned = raw.strip(" \t\n\r*\u2020\u2021\u00a7#()[]{}<>,;:'\"")
    cleaned = re.split(r'[\s\(\[\{]', cleaned)[0].rstrip(".,;:")
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', cleaned):
        return cleaned
    return None


def fetch_pubmed_papers(topic, limit, target_dir, filters=None):
    """
    Searches and extracts author emails from PubMed / NCBI (https://pubmed.ncbi.nlm.nih.gov).
    Utilizes NCBI E-utilities (esearch + efetch) with high-accuracy author-affiliation
    parsing and fallback to PubMed Central (PMC) full-text XML / PDF.

    filters (dict, optional):
        year_from     (int|None): e.g. 2023
        year_to       (int|None): e.g. 2026
        article_types (list[str]): mapped to PubMed [Publication Type]
    """
    filters = filters or {}
    year_from = filters.get("year_from")
    year_to = filters.get("year_to")
    article_types = filters.get("article_types", [])

    # 1. Build NCBI E-search query
    query_terms = [f'("{topic}"[Title/Abstract])']

    if year_from and year_to:
        query_terms.append(f'("{year_from}"[Date - Publication] : "{year_to}"[Date - Publication])')
    elif year_from:
        query_terms.append(f'("{year_from}"[Date - Publication] : "3000"[Date - Publication])')
    elif year_to:
        query_terms.append(f'("1800"[Date - Publication] : "{year_to}"[Date - Publication])')

    pm_types = [_PUBMED_TYPE_MAP[t] for t in article_types if t in _PUBMED_TYPE_MAP]
    if pm_types:
        type_clause = " OR ".join(f'"{t}"[Publication Type]' for t in pm_types)
        query_terms.append(f"({type_clause})")

    full_term = " AND ".join(query_terms)
    log.info(f"[PubMed] Term: {full_term!r}")

    # 2. Search PubMed IDs
    esearch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": full_term,
        "retmode": "json",
        "retmax": min(limit * 3, 500),
        "sort": "pub_date",
        "email": CONTACT_EMAIL,
        "tool": "AcademicEmailExtractor",
    }

    try:
        resp = requests.get(esearch_url, params=search_params, headers=API_HEADERS, timeout=25)
        if resp.status_code != 200:
            log.error(f"[PubMed] Esearch HTTP {resp.status_code}")
            return []
        data = resp.json()
        id_list = data.get("esearchresult", {}).get("idlist", [])
        log.info(f"[PubMed] Found {len(id_list)} PMIDs.")
    except Exception as e:
        log.error(f"[PubMed] Esearch error: {e}")
        return []

    if not id_list:
        return []

    # 3. E-fetch PubMed Article XML in batches
    records = []
    saved = 0
    batch_size = 20

    for b_idx in range(0, len(id_list), batch_size):
        if saved >= limit:
            break

        chunk_ids = id_list[b_idx: b_idx + batch_size]
        efetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
        fetch_params = {
            "db": "pubmed",
            "id": ",".join(chunk_ids),
            "retmode": "xml",
            "email": CONTACT_EMAIL,
            "tool": "AcademicEmailExtractor",
        }

        try:
            fetch_resp = requests.get(efetch_url, params=fetch_params, headers=API_HEADERS, timeout=30)
            if fetch_resp.status_code != 200:
                log.error(f"[PubMed] Efetch HTTP {fetch_resp.status_code}")
                continue
            root = ET.fromstring(fetch_resp.content)
        except Exception as e:
            log.warning(f"[PubMed] Efetch XML error: {e}")
            continue

        for article_tag in root.findall(".//PubmedArticle"):
            if saved >= limit:
                break

            pmid_el = article_tag.find(".//PMID")
            pmid = pmid_el.text if pmid_el is not None else ""

            # Extract Title
            title_el = article_tag.find(".//ArticleTitle")
            title = "".join(title_el.itertext()).strip().rstrip(".") if title_el is not None else "Untitled"

            # Extract Journal Name
            journal_el = article_tag.find(".//Journal/Title")
            journal = journal_el.text.strip() if (journal_el is not None and journal_el.text) else "PubMed"

            # Extract DOI & PMCID
            doi = ""
            pmcid = ""
            for aid in article_tag.findall(".//ArticleId"):
                id_type = aid.get("IdType")
                if id_type == "doi":
                    doi = aid.text.strip() if aid.text else ""
                elif id_type == "pmc":
                    pmcid = aid.text.strip() if aid.text else ""

            # Extract Authors & Affiliations
            authors_meta = []
            extracted_emails = []

            for author_el in article_tag.findall(".//AuthorList/Author"):
                last_name = author_el.find("LastName")
                fore_name = author_el.find("ForeName")
                if last_name is not None and last_name.text:
                    fn = fore_name.text if fore_name is not None and fore_name.text else ""
                    full_name = f"{fn} {last_name.text}".strip()
                    if full_name:
                        authors_meta.append(full_name)

                # Author affiliations (often contain email addresses in PubMed!)
                for aff_el in author_el.findall(".//AffiliationInfo/Affiliation"):
                    aff_text = aff_el.text or ""
                    found_emails = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', aff_text)
                    for fe in found_emails:
                        ce = _clean_email(fe)
                        if ce and ce not in extracted_emails:
                            extracted_emails.append(ce)

            # Strategy 1: Affiliation Email direct match (XML-First)
            if extracted_emails:
                saved += 1
                records.append({
                    "file_path": None,
                    "pdf_name": f"pubmed_{pmid}.xml" if pmid else f"pubmed_{saved}.xml",
                    "title": title,
                    "authors": authors_meta,
                    "doi": doi or (f"pubmed:{pmid}" if pmid else "N/A"),
                    "source_journal": journal,
                    "_html_emails": extracted_emails,
                })
                log.info(f"[PubMed] ({saved}/{limit}) Extracted {len(extracted_emails)} email(s) directly from PubMed affiliation XML (PMID: {pmid})")
                continue

            # Strategy 2: If PMC ID is available, try EuropePMC fullTextXML
            if pmcid:
                try:
                    xml_url = f"https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
                    xml_resp = requests.get(xml_url, headers={"Accept": "application/xml"}, timeout=12)
                    if xml_resp.status_code == 200:
                        raw_matches = re.findall(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', xml_resp.text)
                        clean_matches = [
                            _clean_email(e) for e in raw_matches
                            if _clean_email(e) and not any(e.lower().startswith(p) for p in ("support@", "help@", "info@", "editorial@"))
                        ]
                        clean_matches = list(dict.fromkeys([e for e in clean_matches if e]))
                        if clean_matches:
                            saved += 1
                            records.append({
                                "file_path": None,
                                "pdf_name": f"{pmcid}.xml",
                                "title": title,
                                "authors": authors_meta,
                                "doi": doi or (f"pmc:{pmcid}"),
                                "source_journal": journal,
                                "_html_emails": clean_matches,
                            })
                            log.info(f"[PubMed] ({saved}/{limit}) Extracted {len(clean_matches)} email(s) via PMC fullTextXML for {pmcid}")
                            continue
                except Exception as pmc_err:
                    log.debug(f"[PubMed] PMC fullTextXML error: {pmc_err}")

            # Strategy 3: PMC Open-Access PDF download
            if pmcid:
                pdf_urls = [
                    f"https://europepmc.org/articles/{pmcid}?pdf=render",
                    f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/pdf/",
                ]
                pdf_name = f"pubmed_{pmid or pmcid}.pdf"
                file_path = os.path.join(target_dir, pdf_name)
                download_success = False

                for p_url in pdf_urls:
                    try:
                        p_resp = requests.get(p_url, headers=STEALTH_HEADERS, timeout=20, allow_redirects=True)
                        if p_resp.status_code == 200 and (p_resp.content.startswith(b"%PDF") or b"%PDF-" in p_resp.content[:1024]):
                            with open(file_path, "wb") as f:
                                f.write(p_resp.content)
                            download_success = True
                            break
                    except Exception:
                        pass

                if download_success:
                    saved += 1
                    records.append({
                        "file_path": file_path,
                        "pdf_name": pdf_name,
                        "title": title,
                        "authors": authors_meta,
                        "doi": doi or (f"pmc:{pmcid}"),
                        "source_journal": journal,
                    })
                    log.info(f"[PubMed] ({saved}/{limit}) Downloaded PMC PDF for {pmcid}")
                    continue

        time.sleep(0.35)  # Respect NCBI E-utilities rate limit (max 3 req/sec without API key)

    return records
