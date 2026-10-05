"""
Repair & Realign Utility for Email-Scraping / WS2.0
===================================================
1. Auto-realigns shifted columns across all generated Excel and CSV files.
2. Restores missing DOIs by cross-referencing against master lists and source workbooks.
3. Cleans titles (unescapes HTML entities, removes linebreaks, trims spaces).
4. Sanitizes author names (removes section headers, multi-line artifacts, extracts true person names).
5. Enforces strict sequential order: [Paper Title, Author Name, Email ID, DOI] with executive Navy formatting.
"""

import os
import sys
import glob
import re
import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
WS2_DIR = os.path.join(PROJECT_ROOT, "WS2.0")
if WS2_DIR not in sys.path:
    sys.path.insert(0, WS2_DIR)

from app import format_and_save_excel, EXCEL_BASE_DIR, MASTER_EMAIL_FILE  # type: ignore # noqa: E402
from extractors.email_verifier import (  # type: ignore # noqa: E402
    clean_paper_title,
    clean_author_name,
    normalize_and_realign_records,
)


def build_doi_lookup_cache():
    """Builds a comprehensive lookup cache of (Email ID, Paper Title) -> DOI."""
    lookup = {}

    # 1. From master_email_list.csv
    if os.path.exists(MASTER_EMAIL_FILE):
        try:
            m_df = pd.read_csv(MASTER_EMAIL_FILE, encoding="utf-8-sig")
            if "DOI" in m_df.columns:
                for _, r in m_df.iterrows():
                    doi_val = str(r.get("DOI", "")).strip()
                    if doi_val and doi_val.lower() not in ("nan", "none", "n/a", ""):
                        em = str(r.get("Email ID", "")).strip().lower()
                        ti = clean_paper_title(str(r.get("Paper Title", ""))).lower()
                        if em and "@" in em:
                            lookup[em] = doi_val
                        if ti:
                            lookup[ti] = doi_val
        except Exception as e:
            print(f"[Warning] Failed loading master CSV for lookup: {e}")

    # 2. From all multisource / publisher Excel files
    for p in glob.glob(os.path.join(EXCEL_BASE_DIR, "*.xlsx")):
        base = os.path.basename(p)
        if base.startswith("verified_"):
            continue
        try:
            x_df = pd.read_excel(p)
            if "DOI" in x_df.columns:
                for _, r in x_df.iterrows():
                    doi_val = str(r.get("DOI", "")).strip()
                    if doi_val and doi_val.lower() not in ("nan", "none", "n/a", ""):
                        em = str(r.get("Email ID", "")).strip().lower()
                        ti = clean_paper_title(str(r.get("Paper Title", ""))).lower()
                        if em and "@" in em:
                            lookup[em] = doi_val
                        if ti:
                            lookup[ti] = doi_val
        except Exception:
            pass

    return lookup


def repair_excel_file(file_path, doi_cache):
    """Repairs and formats an Excel workbook into pristine sequential order."""
    base = os.path.basename(file_path)
    print(f"Repairing Excel: {base}...")
    try:
        df = pd.read_excel(file_path)
        records = df.to_dict(orient="records")
        realigned = normalize_and_realign_records(records)

        # Restore missing DOIs from cache
        for rec in realigned:
            cur_doi = rec.get("DOI", "").strip()
            if not cur_doi or cur_doi.lower() in ("nan", "none", "n/a", ""):
                em = rec.get("Email ID", "").lower()
                ti = rec.get("Paper Title", "").lower()
                cached_doi = doi_cache.get(em) or doi_cache.get(ti)
                if cached_doi:
                    rec["DOI"] = cached_doi

        sheet = "Verified Authors" if (base.startswith("verified_") or "_updated" in base.lower()) else "Authors"
        format_and_save_excel(realigned, file_path, sheet_name=sheet)
        print(f" -> Successfully repaired and formatted {base} ({len(realigned)} rows)")
    except Exception as e:
        print(f" -> Error repairing {base}: {e}")


def repair_master_csv(doi_cache):
    """Repairs and realigns the master_email_list.csv file."""
    if not os.path.exists(MASTER_EMAIL_FILE):
        return
    print(f"Repairing Master CSV: {MASTER_EMAIL_FILE}...")
    try:
        df = pd.read_csv(MASTER_EMAIL_FILE, encoding="utf-8-sig")
        records = df.to_dict(orient="records")
        realigned = normalize_and_realign_records(records)

        # Restore missing DOIs
        for rec in realigned:
            cur_doi = rec.get("DOI", "").strip()
            if not cur_doi or cur_doi.lower() in ("nan", "none", "n/a", ""):
                em = rec.get("Email ID", "").lower()
                ti = rec.get("Paper Title", "").lower()
                cached_doi = doi_cache.get(em) or doi_cache.get(ti)
                if cached_doi:
                    rec["DOI"] = cached_doi

        out_df = pd.DataFrame(realigned)
        target_cols = ["Paper Title", "Author Name", "Email ID", "DOI"]
        for col in target_cols:
            if col not in out_df.columns:
                out_df[col] = ""
        out_df = out_df[target_cols]
        out_df.to_csv(MASTER_EMAIL_FILE, index=False, encoding="utf-8-sig")
        print(f" -> Successfully repaired master_email_list.csv ({len(out_df)} rows)")
    except Exception as e:
        print(f" -> Error repairing master_email_list.csv: {e}")


def main():
    print("=" * 60)
    print(" Email Scrapper WS2.0: Deep Alignment & Data Repair Engine")
    print("=" * 60)

    doi_cache = build_doi_lookup_cache()
    print(f"Built DOI lookup cache with {len(doi_cache)} indexed entries.")

    # 1. Repair all Excel files in author details
    xlsx_files = glob.glob(os.path.join(EXCEL_BASE_DIR, "*.xlsx"))
    for file_path in xlsx_files:
        repair_excel_file(file_path, doi_cache)

    # 2. Repair master email list
    repair_master_csv(doi_cache)

    print("=" * 60)
    print(" All files have been repaired, realigned, and formatted!")
    print("=" * 60)


if __name__ == "__main__":
    main()
