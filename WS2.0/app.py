import os
import sys
import re
import sys
import time
import uuid
import threading
import logging
import webbrowser
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pdfplumber
import pandas as pd
from flask import Flask, render_template, request, send_file, jsonify

# Existing extractors
from extractors.plos_fetcher import fetch_plos_papers
from extractors.elife_fetcher import fetch_elife_papers
from extractors.europepmc_fetcher import fetch_europepmc_papers

# New extractors
from extractors.openalex_fetcher import fetch_openalex_papers

# API-based fetchers
from extractors.arxiv_fetcher import fetch_arxiv_papers
from extractors.biorxiv_fetcher import fetch_biorxiv_papers
from extractors.crossref_fetcher import fetch_crossref_papers
from extractors.email_verifier import (
    check_email_deliverability,
    clean_and_repair_email,
    clean_paper_title,
    clean_author_name,
    is_valid_author_name,
    is_country_allowed,
    detect_country,
    normalize_country_name,
    normalize_and_realign_records,
    batch_verify_records,
    verify_single_record,
)

from extractors.pubmed_fetcher import fetch_pubmed_papers
from extractors.frontiers_fetcher import fetch_frontiers_papers
from extractors.aha_fetcher import fetch_aha_papers

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "templates"),
    static_folder=os.path.join(BASE_DIR, "static"),
)

DOWNLOADS_DIR = os.path.join(BASE_DIR, "downloads")
PDFS_BASE_DIR = os.path.join(DOWNLOADS_DIR, "Pdf Files")
EXCEL_BASE_DIR = os.path.join(DOWNLOADS_DIR, "author details")

os.makedirs(PDFS_BASE_DIR, exist_ok=True)
os.makedirs(EXCEL_BASE_DIR, exist_ok=True)

# -----------------------------------------------------------
# Logging Setup — ISSUE-17
# Replaces all print() calls with a persistent, rotating log file.
# Console handler uses UTF-8 with 'replace' so Unicode paper titles
# never crash the Windows terminal (which defaults to cp1252).
# -----------------------------------------------------------
_log_file = os.path.join(BASE_DIR, "extraction.log")
try:
    _file_handler = RotatingFileHandler(
        _log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
except Exception:
    _file_handler = logging.NullHandler()

# Wrap stdout in a UTF-8 stream with 'replace' for unencodable chars
try:
    _utf8_stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8",
                        errors="replace", closefd=False, buffering=1)
    _console_handler = logging.StreamHandler(stream=_utf8_stdout)
except Exception:
    _console_handler = logging.StreamHandler(stream=sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[_file_handler, _console_handler],
)
log = logging.getLogger("extraction")

# -----------------------------------------------------------
# Background Task Store
# -----------------------------------------------------------
# Maps task_id -> {"status": ..., "progress": ..., "result": ...}
TASKS = {}
TASKS_LOCK = threading.Lock()

# Intra-process concurrency guard (one extraction per worker thread)
_INTRA_PROCESS_LOCK = threading.Lock()

# -----------------------------------------------------------
# Filesystem Lock — ISSUE-12
# Cross-process lock using a timestamp-stamped lock file.
# Works correctly under multi-worker WSGI (gunicorn --workers N).
# The threading lock handles same-process races; the file lock
# handles cross-process races on the master_email_list.csv.
# -----------------------------------------------------------
LOCK_FILE = os.path.join(DOWNLOADS_DIR, "extraction.lock")
_LOCK_TTL_SECONDS = 7200  # 2 hours — treat locks older than this as stale


def _acquire_extraction_lock():
    """
    Acquire the two-layer extraction lock:
      Layer 1: threading.Lock — prevents races within the same process.
      Layer 2: filesystem lock file — prevents races across processes.

    Returns True if the lock was acquired, False if it is already held.
    """
    # Layer 1: fast intra-process check
    if not _INTRA_PROCESS_LOCK.acquire(blocking=False):
        return False

    # Layer 2: cross-process file lock
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r") as f:
                timestamp = float(f.read().strip() or "0")
            age = time.time() - timestamp
            if age < _LOCK_TTL_SECONDS:
                # Live lock held by another process — reject
                _INTRA_PROCESS_LOCK.release()
                return False
            # Stale lock — clear it and proceed
            log.warning(
                f"[Lock] Stale extraction.lock found (age {age / 60:.0f} min). Clearing."
            )
            os.remove(LOCK_FILE)
        except Exception as e:
            log.warning(f"[Lock] Could not read lock file: {e}. Clearing and proceeding.")
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass

    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        log.error(f"[Lock] Failed to write lock file: {e}")
        _INTRA_PROCESS_LOCK.release()
        return False

    return True


def _release_extraction_lock():
    """Release the filesystem lock and the intra-process threading lock."""
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception as e:
        log.warning(f"[Lock] Could not remove lock file: {e}")
    try:
        _INTRA_PROCESS_LOCK.release()
    except RuntimeError:
        pass  # Already released — safe to ignore


def _cleanup_stale_tasks():
    """Remove tasks that finished or were abandoned more than 30 minutes ago."""
    cutoff = time.time() - 1800
    with TASKS_LOCK:
        stale = [
            tid for tid, t in TASKS.items()
            if t.get("status") in ("done", "error") and t.get("ts", 0) < cutoff
        ]
        for tid in stale:
            TASKS.pop(tid, None)
    if stale:
        log.info(f"[Tasks] Cleaned up {len(stale)} stale task(s).")


def task_update(task_id, status, progress="", result=None, percentage=0, contacts_found=0):
    """Thread-safe update of task status."""
    with TASKS_LOCK:
        if task_id in TASKS:
            TASKS[task_id]["status"] = status
            TASKS[task_id]["progress"] = progress
            TASKS[task_id]["percentage"] = percentage
            TASKS[task_id]["contacts_found"] = contacts_found
            if result is not None:
                TASKS[task_id]["result"] = result
            if status in ("done", "error"):
                TASKS[task_id]["ts"] = time.time()


# -----------------------------------------------------------
# Master Email Tracker (Deduplication)
# -----------------------------------------------------------
MASTER_EMAIL_FILE = os.path.join(DOWNLOADS_DIR, "master_email_list.csv")

# ISSUE-09: Separate file to track papers that were attempted (even if no emails found).
# Prevents re-downloading PDFs that will never yield emails.
ATTEMPTED_DOIS_FILE = os.path.join(DOWNLOADS_DIR, "attempted_dois.txt")


def normalise_doi(raw):
    """
    ISSUE-10: Convert any DOI representation to a stable, lowercase dedup key.

    Handles:
      - https://doi.org/10.xxxx/...  →  10.xxxx/...
      - http://arxiv.org/abs/1234v2  →  arxiv:1234  (version stripped)
      - 10.xxxx/...                  →  10.xxxx/...  (unchanged)
      - "N/A", "", None              →  ""  (excluded from seen_dois)

    Returns "" for missing/invalid values so they are NEVER added to seen_dois,
    preventing false-positive deduplication collisions (e.g. two "N/A" papers
    being treated as the same paper).
    """
    if not raw:
        return ""
    s = str(raw).strip().lower()
    if s in ("n/a", "na", "", "none", "null"):
        return ""

    # Strip https://doi.org/ and http://doi.org/ prefixes
    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/"):
        if s.startswith(prefix):
            return s[len(prefix):]

    # Normalise arXiv abstract URLs → stable arxiv:<id> key (version stripped)
    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/"):
        if s.startswith(prefix):
            arxiv_id = s[len(prefix):]
            arxiv_id = re.sub(r'v\d+$', '', arxiv_id)  # strip v1/v2/...
            return f"arxiv:{arxiv_id}"

    return s  # Raw DOI (e.g. 10.1038/...) — already a clean key


def load_seen_data():
    """
    Load the set of all previously scraped emails AND DOIs from:
      1. master_email_list.csv  (emails + DOIs of extracted papers)
      2. attempted_dois.txt     (DOIs of all attempted papers, incl. empty ones)

    Returns (seen_emails, seen_dois) — two separate sets.

    Why two sets?
    - seen_emails: skip individual duplicate emails
    - seen_dois:   skip the ENTIRE PAPER before any download/parse
    """
    seen_emails = set()
    seen_dois = set()

    # --- Load from master CSV ---
    if os.path.exists(MASTER_EMAIL_FILE):
        try:
            # utf-8-sig handles both plain UTF-8 and UTF-8-with-BOM files
            df = pd.read_csv(MASTER_EMAIL_FILE, encoding="utf-8-sig")
            if "Email ID" in df.columns:
                seen_emails = set(df["Email ID"].dropna().str.lower().tolist())
            if "DOI" in df.columns:
                for raw in df["DOI"].dropna().tolist():
                    key = normalise_doi(raw)
                    if key:
                        seen_dois.add(key)
        except Exception as e:
            log.warning(f"[Dedup] Could not read master CSV: {e}")

    # --- Load from attempted-dois file (ISSUE-09) ---
    if os.path.exists(ATTEMPTED_DOIS_FILE):
        try:
            with open(ATTEMPTED_DOIS_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    key = line.strip()
                    if key:
                        seen_dois.add(key)
        except Exception as e:
            log.warning(f"[Dedup] Could not read attempted_dois.txt: {e}")

    return seen_emails, seen_dois


def _record_attempted_doi(doi_key):
    """
    ISSUE-09: Append a normalised DOI key to the attempted-dois file.
    Called after every paper attempt, regardless of whether emails were found.
    This prevents re-downloading empty/image PDFs on future runs.
    """
    if not doi_key:
        return
    try:
        with open(ATTEMPTED_DOIS_FILE, "a", encoding="utf-8") as f:
            f.write(doi_key + "\n")
    except Exception as e:
        log.warning(f"[Dedup] Could not write attempted_dois.txt: {e}")


def save_new_emails_to_master(new_rows):
    """Append newly found email rows to the master CSV file after sanitizing them."""
    if not new_rows:
        return
    target_cols = ["Paper Title", "Author Name", "Email ID", "Country", "DOI"]
    sanitized_rows = []
    for r in new_rows:
        row_copy = dict(r)
        if "Paper Title" in row_copy:
            row_copy["Paper Title"] = clean_paper_title(row_copy["Paper Title"])
        if "Author Name" in row_copy:
            row_copy["Author Name"] = clean_author_name(row_copy["Author Name"], email=str(row_copy.get("Email ID", "")))
        if "Email ID" in row_copy:
            row_copy["Email ID"] = str(row_copy["Email ID"]).strip().lower()
        if "Country" in row_copy and row_copy["Country"] and str(row_copy["Country"]).strip():
            row_copy["Country"] = normalize_country_name(row_copy["Country"])
        else:
            row_copy["Country"] = detect_country(
                email=str(row_copy.get("Email ID", "")),
                doi=str(row_copy.get("DOI", "")),
                paper_title=str(row_copy.get("Paper Title", ""))
            )
        if "DOI" in row_copy:
            row_copy["DOI"] = re.sub(r'[\r\n\t]+', '', str(row_copy["DOI"])).strip()

        clean_dict = {col: row_copy.get(col, "") for col in target_cols}
        sanitized_rows.append(clean_dict)

    new_df = pd.DataFrame(sanitized_rows)[target_cols]
    # utf-8-sig = UTF-8 with BOM — Excel on Windows opens it without garbled characters.
    # mode='a' appends; header only when the file is new/empty.
    file_is_empty = (
        not os.path.exists(MASTER_EMAIL_FILE)
        or os.path.getsize(MASTER_EMAIL_FILE) == 0
    )
    new_df.to_csv(
        MASTER_EMAIL_FILE,
        mode="a",
        index=False,
        header=file_is_empty,
        encoding="utf-8-sig",
    )


# -----------------------------------------------------------
# Email Utilities
# -----------------------------------------------------------

# ISSUE-06: Generic / non-personal local-part names — these are platform
# or navigation addresses, not individual academic authors.
_GENERIC_LOCAL_PARTS = frozenset({
    "info", "admin", "support", "contact", "editor", "editorial", "webmaster",
    "noreply", "no-reply", "help", "line", "mail", "office", "submit",
    "submission", "postmaster", "reply", "donotreply", "do-not-reply",
    "mailer", "bounce", "newsletter", "list", "subscribe", "sales",
    "enquiries", "enquiry", "query", "feedback", "helpdesk",
})

# ISSUE-06: Expanded platform/publisher domain blocklist.
# These domains belong to publishers/platforms, not individual researchers.
_IGNORED_DOMAINS = frozenset({
    "plos.org", "elifesciences.org", "europepmc.org", "ebi.ac.uk",
    "ncbi.nlm.nih.gov", "crossref.org", "orcid.org", "adobe.com",
    "openalex.org", "imedpub.com",
    # New additions (ISSUE-06):
    "biorxiv.org", "medrxiv.org", "doi.org", "pubmed.ncbi.nlm.nih.gov",
    "frontiersin.org", "mdpi.com", "arxiv.org", "ssrn.com",
    "wiley.com", "onlinelibrary.wiley.com", "springer.com", "elsevier.com", "tandfonline.com",
    "nature.com", "oup.com", "cambridge.org", "ahajournals.org", "researchgate.net",
})


def clean_and_validate_email(raw_email):
    if not raw_email:
        return None
    cleaned = clean_and_repair_email(raw_email)
    if not cleaned:
        return None

    local_part, domain = cleaned.split("@", 1)

    # Reject generic / non-personal local parts
    if local_part in _GENERIC_LOCAL_PARTS:
        return None

    # Reject platform/internal domains (expanded list)
    if any(d in domain for d in _IGNORED_DOMAINS):
        return None

    # Fast Deliverability & Domain Verification (Cached DNS MX check)
    status, reason = check_email_deliverability(cleaned)
    if status == "Undeliverable":
        log.warning(f"[EmailVerifier] Dropped invalid/undeliverable email {cleaned}: {reason}")
        return None

    return cleaned.lower()



def unwrap_broken_emails(text):
    """
    Reassemble email addresses fragmented by PDF line-wrapping and
    multi-column layout interleaving. (ISSUE-02)
    """
    # --- Existing patterns ---
    # Stitch whitespace around @ and dots: 'john .doe @ uni . edu'
    text = re.sub(r'([\w.-]+)\s*@\s*([\w.-]+)\s*\.\s*([a-zA-Z]{2,})', r'\1@\2.\3', text)
    text = re.sub(r'(\b[\w]+)\s*\.\s*([\w]+)\s*\.\s*([\w]+@)', r'\1.\2.\3', text)
    text = re.sub(r'(\b[\w]+)\s*\.\s*([\w]+@)', r'\1.\2', text)
    text = re.sub(r'(\w+)-\s*\n\s*(\w+)', r'\1-\2', text)
    text = re.sub(r'([\w.+-]+)@\s*\n\s*([\w.-]+)', r'\1@\2', text)
    text = re.sub(r'([\w.-]+)\.\s*\n\s*([a-zA-Z]{2,})', r'\1.\2', text)
    text = re.sub(r'([\w.+-]+@[\w.-]+)\s*\n\s*([\w.-]+\.[a-zA-Z]{2,})', r'\1\2', text)
    # --- New patterns for multi-column PDF interleaving (ISSUE-02) ---
    # Domain split at TLD with whitespace: 'john@uni .edu' → 'john@uni.edu'
    text = re.sub(
        r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)\s+([a-zA-Z]{2,6})\b',
        r'\1.\2', text
    )
    # Full email with whitespace around each segment: 'j.doe @ uni . edu'
    text = re.sub(
        r'([a-zA-Z0-9._%+-]+)\s*\.\s*([a-zA-Z0-9._%-]+)\s*@\s*'
        r'([a-zA-Z0-9.-]+)\s*\.\s*([a-zA-Z]{2,})',
        r'\1.\2@\3.\4', text
    )
    # Line break inside domain: 'john@university\n.edu'
    text = re.sub(
        r'(@[a-zA-Z0-9.-]+)\n([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
        r'\1\2', text
    )
    return text


def extract_full_names_from_pdf(text):
    """
    Extracts high-confidence author candidates from text.
    Evaluates lines strictly independently to prevent multi-line / cross-header pollution.
    """
    if not text:
        return []
    candidate_names = []
    blacklist_words = {
        "indian journal", "medical research", "medical college", "research centre",
        "artificial intelligence", "all rights", "creative commons", "open access",
        "author response", "authors response", "received january", "accepted february",
        "published june", "conflicts of", "financial support", "public health", "bmj open",
        "case report", "case reports", "control study", "case study", "mini review",
        "comprehensive review", "systematic review", "review", "original research",
        "abstract", "correspondence", "corresponding author", "email", "e-mail",
        "department", "division", "faculty", "institute", "hospital", "university",
        "college", "school", "center", "centre", "journal", "students", "medical students",
        "synthetic data", "face image", "acne vulgaris", "in this work", "inthiswork"
    }

    # Evaluate line-by-line — NEVER combine text across newlines!
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or len(line) < 3 or len(line) > 100:
            continue
        line_lower = line.lower()
        if any(b in line_lower for b in blacklist_words):
            continue

        # Match 2 to 4 capitalized words separated strictly by horizontal spaces/tabs
        potential_blocks = re.findall(r'\b([A-Z][a-z]{1,20}(?:[ \t]+[A-Z][a-z]{1,20}){1,3})\b', line)
        for name in potential_blocks:
            clean_name = re.sub(r'[ \t]+', ' ', name).strip()
            name_lower = clean_name.lower()
            if not any(b in name_lower for b in blacklist_words) and len(clean_name.split()) in [2, 3, 4]:
                if not any(
                    b in name_lower
                    for b in ["department", "university", "hospital", "institute", "journal", "college", "school", "faculty"]
                ):
                    candidate_names.append(clean_name)
    return list(dict.fromkeys(candidate_names))


def match_author_for_email(email, candidate_authors, initials=None, metadata_authors=None):
    """
    Attempts to match an email address to a known author.
    Prioritizes official publisher metadata_authors first, then falls back to candidate_authors.
    Strictly sanitizes and validates author names.
    NEVER returns 'Unknown' or multi-line corrupted strings.
    """
    if not email:
        return None

    try:
        def _clean(name):
            return clean_author_name(name, email=email)

        # 1. Highest priority: official publisher metadata authors
        meta_authors_clean = [_clean(a) for a in (metadata_authors or []) if a]
        meta_authors_clean = [a for a in meta_authors_clean if is_valid_author_name(a)]

        clean_uname = re.sub(r'[^a-z]', '', email.split('@')[0].lower())
        if not clean_uname:
            clean_uname = email.split('@')[0].lower().strip()

        # Strategy 1A: Match surname or given name parts in official metadata
        for author in meta_authors_clean:
            raw_parts = [re.sub(r'[^a-z]', '', p.lower()) for p in author.split()]
            name_parts = [p for p in raw_parts if len(p) >= 3]
            if any(p in clean_uname for p in name_parts):
                return author

        # Strategy 2A: Match initials combinations in official metadata
        for author in meta_authors_clean:
            raw_parts = [re.sub(r'[^a-z]', '', p.lower()) for p in author.split()]
            name_parts = [p for p in raw_parts if len(p) >= 2]
            if len(name_parts) >= 2 and len(name_parts[0]) > 0 and len(name_parts[-1]) > 0:
                first_init_last = name_parts[0][0] + name_parts[-1]
                if first_init_last in clean_uname:
                    return author
                first_last_init = name_parts[0] + name_parts[-1][0]
                if first_last_init in clean_uname:
                    return author

        # Strategy 3A: Single-author paper confirmed in metadata
        if len(meta_authors_clean) == 1:
            return meta_authors_clean[0]

        # 2. Second priority: candidate authors extracted from PDF text
        candidates_clean = [_clean(a) for a in (candidate_authors or []) if a]
        candidates_clean = [a for a in candidates_clean if is_valid_author_name(a)]

        for author in candidates_clean:
            raw_parts = [re.sub(r'[^a-z]', '', p.lower()) for p in author.split()]
            name_parts = [p for p in raw_parts if len(p) >= 3]
            if any(p in clean_uname for p in name_parts):
                return author

        for author in candidates_clean:
            raw_parts = [re.sub(r'[^a-z]', '', p.lower()) for p in author.split()]
            name_parts = [p for p in raw_parts if len(p) >= 2]
            if len(name_parts) >= 2 and len(name_parts[0]) > 0 and len(name_parts[-1]) > 0:
                first_init_last = name_parts[0][0] + name_parts[-1]
                if first_init_last in clean_uname:
                    return author
                first_last_init = name_parts[0] + name_parts[-1][0]
                if first_last_init in clean_uname:
                    return author

        # Strategy 4: Initials match
        if initials:
            clean_inits = initials.strip().upper()
            for author in (meta_authors_clean + candidates_clean):
                author_initials = "".join([p.strip()[0].upper() for p in author.split() if p and p.strip() and p.strip()[0].isalpha()])
                if clean_inits and clean_inits == author_initials:
                    return author

        # 3. Third priority: Derive readable person name from structured academic email
        # e.g. firstname.lastname@institution.edu -> "Firstname Lastname"
        uname_raw = email.split('@')[0]
        parts = [p for p in re.split(r'[^a-zA-Z]+', uname_raw) if len(p) >= 2]
        parts = [p.capitalize() for p in parts if p.lower() not in ('dr', 'prof', 'mr', 'ms', 'md', 'phd')]
        if len(parts) >= 2:
            derived = " ".join(parts)
            if is_valid_author_name(derived):
                return derived

    except Exception as e:
        log.warning(f"[match_author_for_email] Guarded against matching exception for {email}: {e}")

    return None


def extract_author_email_pairs(file_path, metadata_authors):
    """
    Extracts verified author-email pairs from a PDF file.
    Fast two-tier scan:
      Tier 1: Fast scan on front 2 pages (where 98% of corresponding emails reside).
      Tier 2: Scan back pages only if front pages yield no matched emails.
    """
    mapped_pairs = []
    try:
        with pdfplumber.open(file_path) as pdf:
            n = len(pdf.pages)
            if n == 0:
                return mapped_pairs

            # Tier 1: Front pages (page 1 and page 2)
            front_pages = list(pdf.pages[:min(2, n)])
            raw_text = ""
            for page in front_pages:
                raw_text += (page.extract_text() or "") + "\n"

            def _search_text(text_block):
                pairs = []
                unwrapped = unwrap_broken_emails(text_block)
                pdf_names = extract_full_names_from_pdf(unwrapped)
                raw_matches = re.findall(
                    r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)(?:\s*\(([A-Z]+)\))?',
                    unwrapped
                )
                for raw_email, initials in raw_matches:
                    clean_email = clean_and_validate_email(raw_email)
                    if not clean_email:
                        continue
                    matched_author = match_author_for_email(
                        clean_email,
                        candidate_authors=pdf_names,
                        initials=initials,
                        metadata_authors=metadata_authors
                    )
                    if not matched_author:
                        continue
                    if clean_email not in [p[1] for p in pairs]:
                        pairs.append((matched_author, clean_email))
                return pairs

            mapped_pairs = _search_text(raw_text)

            # Tier 2: Check back page only if front pages had no matched pairs and PDF has > 2 pages
            if not mapped_pairs and n > 2:
                back_text = ""
                for page in pdf.pages[max(2, n - 2):]:
                    back_text += (page.extract_text() or "") + "\n"
                mapped_pairs = _search_text(back_text)

    except Exception as e:
        log.error(f"Error parsing PDF ({file_path}): {e}", exc_info=True)
    return mapped_pairs



# -----------------------------------------------------------
# Dispatch Mapping (Active 12 Sources)
# -----------------------------------------------------------
SOURCE_FETCHERS = {
    "plos":         ("PLOS",                          fetch_plos_papers),
    "europepmc":    ("Europe PMC",                    fetch_europepmc_papers),
    "elife":        ("eLife",                         fetch_elife_papers),
    "openalex":     ("OpenAlex (All Servers)",       fetch_openalex_papers),
    "arxiv":        ("arXiv",                        fetch_arxiv_papers),
    "biorxiv":      ("bioRxiv / medRxiv",           fetch_biorxiv_papers),
    "crossref":     ("Crossref (Multi-Server)",      fetch_crossref_papers),
    "pubmed":       ("PubMed / NCBI",                fetch_pubmed_papers),
    "frontiers":    ("Frontiers",                    fetch_frontiers_papers),
    "aha":          ("AHA Journals",                 fetch_aha_papers),
}


def format_and_save_excel(data, excel_path, sheet_name="Authors"):
    """
    Saves and formats records or DataFrame into a pristine, professionally formatted Excel workbook:
    - Enforces strict sequential 5-column order: Paper Title, Author Name, Email ID, Country, DOI
    - Thoroughly sanitizes all fields (eliminates newlines, HTML entities, and rogue spaces)
    - Professional executive styling:
      * Deep Navy header (#1F4E79) with bold white text
      * Alternating subtle zebra rows for effortless scanning
      * Vertically centered alignment (20pt row height)
      * Custom fitted column widths
      * Freeze panes enabled on row 2 so headers remain visible when scrolling
      * Excel auto-filters applied across all 5 columns
    """
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    if isinstance(data, pd.DataFrame):
        df = data.copy()
    else:
        df = pd.DataFrame(data)

    target_cols = ["Paper Title", "Author Name", "Email ID", "Country", "DOI"]
    for col in target_cols:
        if col not in df.columns:
            df[col] = ""
    df = df[target_cols]

    # Sanitize each column
    df["Paper Title"] = [clean_paper_title(t) for t in df["Paper Title"]]
    cleaned_authors = []
    for _, row in df.iterrows():
        cleaned_authors.append(clean_author_name(row["Author Name"], email=str(row["Email ID"])))
    df["Author Name"] = cleaned_authors
    df["Email ID"] = [str(e).strip().lower() for e in df["Email ID"]]

    # Sanitize / detect country
    cleaned_countries = []
    for _, row in df.iterrows():
        c_val = str(row["Country"]).strip() if pd.notna(row["Country"]) else ""
        if c_val and c_val.lower() not in ("nan", "none", "null", "n/a", "na", "unknown"):
            cleaned_countries.append(normalize_country_name(c_val))
        else:
            detected = detect_country(
                email=str(row["Email ID"]),
                doi=str(row["DOI"]),
                paper_title=str(row["Paper Title"])
            )
            cleaned_countries.append(detected or "Unknown")
    df["Country"] = cleaned_countries

    df["DOI"] = [
        re.sub(r'[\r\n\t]+', '', str(d)).strip() if pd.notna(d) and str(d) != "nan" else ""
        for d in df["DOI"]
    ]

    # Write using openpyxl
    with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=sheet_name)
        ws = writer.sheets[sheet_name]

        header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
        header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
        header_align = Alignment(horizontal="center", vertical="center", wrap_text=False)

        data_font = Font(name="Calibri", size=10, color="000000")
        zebra_fill = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")
        white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")
        border_side = Side(style="thin", color="E5E7EB")
        data_border = Border(left=border_side, right=border_side, top=border_side, bottom=border_side)

        num_cols = len(target_cols)
        # Style Header Row
        ws.row_dimensions[1].height = 26
        for col_idx in range(1, num_cols + 1):
            cell = ws.cell(row=1, column=col_idx)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = header_align
            cell.border = data_border

        # Style Data Rows
        num_rows = len(df)
        for r_idx in range(2, num_rows + 2):
            ws.row_dimensions[r_idx].height = 20
            row_fill = zebra_fill if (r_idx % 2 == 0) else white_fill
            for c_idx in range(1, num_cols + 1):
                cell = ws.cell(row=r_idx, column=c_idx)
                cell.font = data_font
                cell.fill = row_fill
                cell.border = data_border
                horiz = "center" if c_idx == 4 else "left"
                cell.alignment = Alignment(horizontal=horiz, vertical="center", wrap_text=False)

        # Set optimal column widths
        col_widths = {
            1: 65,  # Paper Title
            2: 28,  # Author Name
            3: 32,  # Email ID
            4: 24,  # Country
            5: 38,  # DOI
        }
        for c_idx, width in col_widths.items():
            ws.column_dimensions[get_column_letter(c_idx)].width = width

        # Freeze panes at A2 so header stays sticky
        ws.freeze_panes = "A2"
        # Enable Excel Auto-Filter across all 5 columns
        if num_rows > 0:
            ws.auto_filter.ref = f"A1:{get_column_letter(num_cols)}{num_rows + 1}"


# -----------------------------------------------------------
# Background Extraction Worker
# -----------------------------------------------------------
def _run_extraction_task(task_id, source_sites, topic, max_papers, filters=None, topics=None):
    """
    Runs the full extraction in a background thread across one or more selected sources.
    Supports single or multiple keywords simultaneously.
    Holds the extraction lock for its entire duration.

    filters (dict, optional): Extraction criteria sent from the UI:
        - countries     (list[str]): e.g. ["USA", "UK"]
        - year_from     (int|None):  e.g. 2023
        - year_to       (int|None):  e.g. 2026
        - article_types (list[str]): e.g. ["Article", "Review"]
    """
    if isinstance(source_sites, str):
        source_sites = [source_sites]
    source_sites = [s for s in source_sites if s in SOURCE_FETCHERS]
    if not source_sites:
        source_sites = ["plos"]

    if topics is None:
        if isinstance(topic, list):
            topics = [t.strip() for t in topic if t and t.strip()]
        elif isinstance(topic, str):
            topics = [t.strip() for t in topic.split(",") if t.strip()]
    if not topics:
        topics = [topic] if isinstance(topic, str) and topic else ["Research"]

    topic_display = ", ".join(topics)
    max_papers = max(1, min(int(max_papers), 500))

    filters = filters or {}
    selected_countries = filters.get("countries", [])
    short_id = task_id[:8]
    rows = []           # All rows successfully written this run (for Excel output)
    pending_rows = []   # Batch buffer — flushed every 5 papers
    created_pdf_dirs = []

    with TASKS_LOCK:
        task_info = TASKS.get(task_id, {})
        pause_event = task_info.get("pause_event")
        stop_event = task_info.get("stop_event")
        cancel_event = task_info.get("cancel_event")

    if pause_event is None:
        pause_event = threading.Event()
        pause_event.set()
    if stop_event is None:
        stop_event = threading.Event()
    if cancel_event is None:
        cancel_event = threading.Event()

    def _flush_pending():
        """Append pending batch to master CSV and move rows into the master list."""
        nonlocal pending_rows
        if cancel_event and cancel_event.is_set():
            return
        if pending_rows:
            save_new_emails_to_master(pending_rows)
            rows.extend(pending_rows)
            log.info(
                f"[Task {short_id}] Batch flushed {len(pending_rows)} rows to master CSV."
            )
            pending_rows = []

    try:
        topic_clean = re.sub(r'[^\w\-]', '_', "_".join(topics[:2])).lower()[:40]

        task_update(task_id, "running", "Loading previously seen emails...", percentage=5, contacts_found=0)
        seen_emails, seen_dois = load_seen_data()
        log.info(
            f"[Dedup] Loaded {len(seen_emails)} seen emails, {len(seen_dois)} seen DOIs."
        )

        total_sources = len(source_sites)
        total_papers_fetched = 0
        total_papers_processed = 0
        doi_skipped_total = 0

        for source_idx, source_site in enumerate(source_sites):
            if cancel_event.is_set() or stop_event.is_set():
                break

            source_label, fetcher_func = SOURCE_FETCHERS[source_site]
            topic_pdf_dir = os.path.join(PDFS_BASE_DIR, f"{source_site}_{topic_clean}_pdfs")
            os.makedirs(topic_pdf_dir, exist_ok=True)
            if topic_pdf_dir not in created_pdf_dirs:
                created_pdf_dirs.append(topic_pdf_dir)

            source_pct_base = 5 + int(85 * source_idx / total_sources)
            source_pct_span = max(1, int(85 / total_sources))

            downloaded = []
            seen_batch_dois = set()
            for kw_idx, kw in enumerate(topics):
                if cancel_event.is_set() or stop_event.is_set():
                    break
                kw_status = (
                    f"[{source_idx + 1}/{total_sources}] Fetching {source_label} for '{kw}'..."
                    if len(topics) > 1
                    else f"[{source_idx + 1}/{total_sources}] Fetching papers from {source_label}..."
                )
                task_update(
                    task_id, "running",
                    kw_status,
                    percentage=source_pct_base + int((source_pct_span * 0.3) * kw_idx / max(1, len(topics))),
                    contacts_found=len(rows) + len(pending_rows)
                )
                log.info(
                    f"[Task {short_id}] ({source_idx + 1}/{total_sources}) Starting: source={source_site}, "
                    f"keyword='{kw}', max={max_papers}"
                )

                try:
                    kw_downloaded = fetcher_func(kw, max_papers, topic_pdf_dir, filters=filters)
                except Exception as fetch_err:
                    log.error(f"[Task {short_id}] Fetcher error for {source_site} on '{kw}': {fetch_err}", exc_info=True)
                    kw_downloaded = []

                for item in kw_downloaded:
                    item_doi = (item.get("doi") or "").strip().lower()
                    if item_doi and item_doi in seen_batch_dois:
                        continue
                    if item_doi:
                        seen_batch_dois.add(item_doi)
                    downloaded.append(item)

            num_papers = len(downloaded)
            total_papers_fetched += num_papers
            log.info(f"[Task {short_id}] {source_label} returned {num_papers} papers across {len(topics)} keyword(s).")

            for paper_index, item in enumerate(downloaded):
                total_papers_processed += 1

                # 0. Cancel check
                if cancel_event.is_set():
                    log.info(f"[Task {short_id}] Cancel requested. Aborting loop immediately.")
                    break

                # 1. Stop check
                if stop_event.is_set():
                    log.info(f"[Task {short_id}] User requested stop. Exiting early.")
                    break

                # 2. Pause check
                if not pause_event.is_set():
                    current_found = len(rows) + len(pending_rows)
                    current_pct = source_pct_base + int(source_pct_span * paper_index / max(1, num_papers))
                    log.info(f"[Task {short_id}] Paused at {source_label} paper {paper_index + 1}/{num_papers}.")
                    task_update(
                        task_id, "paused",
                        f"Extraction stopped at {source_label} paper {paper_index + 1}/{num_papers} ({current_found} contacts found). Click Continue to resume.",
                        percentage=current_pct,
                        contacts_found=current_found
                    )
                    while not pause_event.is_set():
                        if stop_event.is_set() or cancel_event.is_set():
                            break
                        time.sleep(0.25)

                    if cancel_event.is_set() or stop_event.is_set():
                        break

                    log.info(f"[Task {short_id}] Extraction continued by user.")
                    task_update(
                        task_id, "running",
                        f"Continuing extraction at {source_label} paper {paper_index + 1}/{num_papers}...",
                        percentage=current_pct,
                        contacts_found=current_found
                    )

                # Progress percentage
                current_pct = source_pct_base + int(source_pct_span * (paper_index + 1) / max(1, num_papers))
                current_found = len(rows) + len(pending_rows)
                task_update(
                    task_id, "running",
                    f"[{source_idx + 1}/{total_sources}] {source_label}: paper {paper_index + 1}/{num_papers}...",
                    percentage=current_pct,
                    contacts_found=current_found
                )

                item_doi_raw = item.get("doi", "")
                item_doi = normalise_doi(item_doi_raw)

                # DOI-level dedup check
                if item_doi and item_doi in seen_dois:
                    log.info(f"[Dedup] Already in collection (DOI match): {item.get('title', '')[:55]}")
                    doi_skipped_total += 1
                    continue

                _record_attempted_doi(item_doi)
                if item_doi:
                    seen_dois.add(item_doi)

                # HTML-First shortcut
                if "_html_emails" in item:
                    for email in item["_html_emails"]:
                        clean_email = clean_and_validate_email(email)
                        if not clean_email or clean_email in seen_emails:
                            continue

                        matched_author = match_author_for_email(clean_email, item.get("authors", []), metadata_authors=item.get("authors", []))
                        if not matched_author:
                            continue

                        clean_auth = clean_author_name(matched_author, email=clean_email)
                        if not clean_auth or not is_valid_author_name(clean_auth):
                            continue

                        # Strict country filtering (blocks Chinese domains/authors and non-target countries)
                        is_ok, country_reason = is_country_allowed(
                            email=clean_email,
                            allowed_countries=selected_countries,
                            author_name=clean_auth,
                            paper_title=item.get("title", ""),
                            doi=item_doi_raw
                        )
                        if not is_ok:
                            log.info(f"[CountryFilter] Excluded email {clean_email}: {country_reason}")
                            continue

                        detected_country = detect_country(
                            email=clean_email,
                            doi=item_doi_raw,
                            affiliation=item.get("affiliation", ""),
                            paper_title=item.get("title", ""),
                            allowed_countries=selected_countries
                        )
                        pending_rows.append({
                            "PDF File Name": item["pdf_name"],
                            "Source Journal": item.get("source_journal", source_label),
                            "Paper Title": clean_paper_title(item["title"]),
                            "Author Name": clean_auth,
                            "Email ID": clean_email,
                            "Country": detected_country,
                            "DOI": re.sub(r'[\r\n\t]+', '', str(item_doi_raw)).strip(),
                        })
                        seen_emails.add(clean_email)
                        # Grace point: capture ALL valid authors per article (no break)

                    if (paper_index + 1) % 5 == 0:
                        _flush_pending()
                    continue

                if cancel_event.is_set() or stop_event.is_set():
                    break

                # PDF parsing
                pairs = extract_author_email_pairs(item["file_path"], item.get("authors", []))
                for author, email in pairs:
                    clean_auth = clean_author_name(author, email=email)
                    if not clean_auth or not is_valid_author_name(clean_auth):
                        continue
                    if email in seen_emails:
                        log.debug(f"[Dedup] Skipped duplicate email: {email}")
                        continue

                    # Strict country filtering (blocks Chinese domains/authors and non-target countries)
                    is_ok, country_reason = is_country_allowed(
                        email=email,
                        allowed_countries=selected_countries,
                        author_name=clean_auth,
                        paper_title=item.get("title", ""),
                        doi=item_doi_raw
                    )
                    if not is_ok:
                        log.info(f"[CountryFilter] Excluded email {email}: {country_reason}")
                        continue

                    detected_country = detect_country(
                        email=email,
                        doi=item_doi_raw,
                        affiliation=item.get("affiliation", ""),
                        paper_title=item.get("title", ""),
                        allowed_countries=selected_countries
                    )
                    pending_rows.append({
                        "PDF File Name": item["pdf_name"],
                        "Source Journal": item.get("source_journal", source_label),
                        "Paper Title": clean_paper_title(item["title"]),
                        "Author Name": clean_auth,
                        "Email ID": email,
                        "Country": detected_country,
                        "DOI": re.sub(r'[\r\n\t]+', '', str(item_doi_raw)).strip(),
                    })
                    seen_emails.add(email)
                    # Grace point: capture ALL valid authors per article (no break)

                if (paper_index + 1) % 5 == 0:
                    _flush_pending()

            # Flush remaining after each source completes
            _flush_pending()

        # Handle cancellation
        if cancel_event.is_set():
            log.info(f"[Task {short_id}] Cancelled and deleted by user. Discarding output and cleaning up.")
            import shutil
            for pdir in created_pdf_dirs:
                try:
                    if os.path.exists(pdir):
                        shutil.rmtree(pdir, ignore_errors=True)
                except Exception as cleanup_err:
                    log.warning(f"[Task {short_id}] Could not delete pdf dir on cancel: {cleanup_err}")

            task_update(task_id, "cancelled", "Extraction cancelled and deleted.", result={
                "success": False,
                "message": "Extraction was cancelled and deleted.",
                "cancelled": True,
                "data": [],
                "file": None,
            }, percentage=0, contacts_found=0)
            return

        _flush_pending()
        was_stopped = stop_event.is_set()
        log.info(f"[Task {short_id}] Run complete (stopped_early={was_stopped}). New rows this run: {len(rows)}")

        if not rows:
            if was_stopped:
                message = "Extraction was stopped by user before any contacts were found."
            elif doi_skipped_total > 0 and doi_skipped_total == total_papers_fetched:
                message = (
                    f"All {doi_skipped_total} papers for '{topic}' across selected sources "
                    f"are already in your collection. ✅ Try a different keyword or increase paper count."
                )
            elif doi_skipped_total > 0:
                message = (
                    f"{doi_skipped_total} papers were already in collection. "
                    f"Remaining papers had no extractable emails. Try a broader keyword."
                )
            else:
                sources_str = ", ".join([SOURCE_FETCHERS[s][0] for s in source_sites if s in SOURCE_FETCHERS])
                message = (
                    f"No author emails could be extracted from {sources_str} for '{topic}'. "
                    f"Try another keyword or select additional journal sources."
                )
            task_update(task_id, "done", "", result={
                "success": False,
                "message": message,
                "data": [],
                "file": None,
                "stopped_early": was_stopped,
            }, percentage=100, contacts_found=0)
            return

        # Write Excel output
        total_found = len(rows)
        task_update(task_id, "running", f"Writing {total_found} results to Excel...", percentage=95, contacts_found=total_found)
        if len(source_sites) == 1:
            excel_filename = f"{source_sites[0]}_{topic_clean}_authors.xlsx"
        else:
            excel_filename = f"multisource_{topic_clean}_authors.xlsx"
        excel_path = os.path.join(EXCEL_BASE_DIR, excel_filename)

        df = pd.DataFrame(rows)
        format_and_save_excel(df, excel_path, sheet_name="Authors")

        status_msg = f"Extraction stopped early. {len(rows)} contacts saved." if was_stopped else f"Extraction complete. {len(rows)} contacts found."
        log.info(f"[Task {short_id}] {status_msg} {len(rows)} rows written to {excel_filename}")

        task_update(task_id, "done", "", result={
            "success": True,
            "total_records": len(rows),
            "download_file": excel_filename,
            "data": rows,
            "stopped_early": was_stopped,
            "message": status_msg,
            "source_sites": source_sites,
            "source_site": source_sites[0],
            "topic": topic_display,
            "topics": topics,
            "max_papers": max_papers,
            "filters": filters,
            "processed_papers": total_papers_processed,
            "total_papers": total_papers_fetched,
        }, percentage=100, contacts_found=len(rows))

    except Exception as e:
        log.error(f"[Task {short_id}] CRASHED: {e}", exc_info=True)
        # ISSUE-20: Crash recovery — flush whatever is pending before giving up.
        # save_new_emails_to_master is called here so no accumulated data is lost.
        _flush_pending()
        if rows:
            log.info(
                f"[Task {short_id}] Crash recovery: {len(rows)} rows were already "
                f"saved to master CSV before the crash."
            )
        task_update(task_id, "error", str(e), result={
            "success": False,
            "message": f"Extraction failed: {str(e)}",
            "data": rows,   # Return partial data so the user sees what was collected
            "file": None,
        }, percentage=100, contacts_found=len(rows))
    finally:
        # Always release both locks, even if extraction crashed mid-way.
        _release_extraction_lock()
        _cleanup_stale_tasks()


# -----------------------------------------------------------
# Flask Routes
# -----------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start-extraction", methods=["POST"])
def start_extraction():
    """
    Starts extraction in a background thread immediately.
    Returns task_id to the browser — takes <1 second.
    Rejects the request if an extraction is already running.

    ISSUE-12: Uses the two-layer lock (_acquire_extraction_lock) instead of
    a threading-only lock, so this works correctly under multi-worker WSGI.
    """
    source_sites = request.form.getlist("source_sites[]")
    if not source_sites:
        single_source = request.form.get("source_site", "").strip().lower()
        if single_source:
            source_sites = [single_source]
        else:
            source_sites = list(SOURCE_FETCHERS.keys())

    source_sites = [s for s in source_sites if s in SOURCE_FETCHERS]

    raw_topics = request.form.getlist("topics[]")
    if not raw_topics:
        raw_topic = request.form.get("topic", "").strip()
        if raw_topic:
            raw_topics = [t.strip() for t in raw_topic.split(",") if t.strip()]
    topics = [t.strip() for t in raw_topics if t.strip()]
    if not topics:
        return jsonify({"error": "Please enter at least one research topic or keyword."}), 400

    topic = ", ".join(topics)

    try:
        max_papers = int(request.form.get("max_papers", 10))
    except (ValueError, TypeError):
        max_papers = 10
    max_papers = max(1, min(max_papers, 500))

    if not source_sites:
        return jsonify({"error": "Please select at least one valid journal source."}), 400

    # --- Read extraction filters from the form ---
    raw_year_from = request.form.get("year_from", "").strip()
    raw_year_to   = request.form.get("year_to",   "").strip()
    filters = {
        "countries":     request.form.getlist("countries[]"),     # e.g. ["USA", "UK"]
        "year_from":     int(raw_year_from) if raw_year_from.isdigit() else None,
        "year_to":       int(raw_year_to)   if raw_year_to.isdigit()   else None,
        "article_types": request.form.getlist("article_types[]"), # e.g. ["Article", "Review"]
    }

    # Cross-process safe lock acquisition (ISSUE-12)
    if not _acquire_extraction_lock():
        return jsonify({
            "error": "An extraction is already in progress. Please wait for it to finish."
        }), 429

    task_id = str(uuid.uuid4())
    pause_event = threading.Event()
    pause_event.set()
    stop_event = threading.Event()
    cancel_event = threading.Event()

    with TASKS_LOCK:
        TASKS[task_id] = {
            "status": "starting",
            "progress": "Initialising extraction...",
            "percentage": 0,
            "contacts_found": 0,
            "result": None,
            "ts": time.time(),
            "pause_event": pause_event,
            "stop_event": stop_event,
            "cancel_event": cancel_event,
            "source_sites": source_sites,
            "source_site": source_sites[0],
            "topic": topic,
            "topics": topics,
            "max_papers": max_papers,
            "filters": filters,
        }

    thread = threading.Thread(
        target=_run_extraction_task,
        args=(task_id, source_sites, topic, max_papers, filters, topics),
        daemon=True,
    )
    thread.start()
    log.info(
        f"[Task {task_id[:8]}] Queued: sources={source_sites}, "
        f"topics={topics}, max={max_papers}, filters={filters}"
    )

    return jsonify({"task_id": task_id})


@app.route("/stop-extraction/<task_id>", methods=["POST"])
@app.route("/pause-extraction/<task_id>", methods=["POST"])
def stop_extraction(task_id):
    """
    Stops (pauses) the running extraction task.
    The worker thread pauses processing remaining papers until Continued or Finished.
    """
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found."}), 404

    if task["status"] in ("done", "error"):
        return jsonify({"error": "Task has already finished.", "status": task["status"]}), 400

    pause_event = task.get("pause_event")
    if pause_event:
        pause_event.clear()

    contacts = task.get("contacts_found", 0)
    task_update(
        task_id,
        "paused",
        progress=f"Extraction stopped ({contacts} contacts found so far). Click Continue to resume or Finish to save.",
        percentage=task.get("percentage", 0),
        contacts_found=contacts,
    )
    log.info(f"[Task {task_id[:8]}] Extraction stopped/paused by user.")
    return jsonify({"success": True, "status": "paused", "message": "Extraction stopped."})


@app.route("/continue-extraction/<task_id>", methods=["POST"])
@app.route("/resume-extraction/<task_id>", methods=["POST"])
def continue_extraction(task_id):
    """
    Continues / resumes a stopped extraction task from where it was paused.
    """
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found."}), 404

    if task["status"] in ("done", "error"):
        return jsonify({"error": "Task has already finished.", "status": task["status"]}), 400

    pause_event = task.get("pause_event")
    if pause_event:
        pause_event.set()

    task_update(
        task_id,
        "running",
        progress="Continuing extraction...",
        percentage=task.get("percentage", 0),
        contacts_found=task.get("contacts_found", 0),
    )
    log.info(f"[Task {task_id[:8]}] Extraction continued by user.")
    return jsonify({"success": True, "status": "running", "message": "Extraction continued."})


@app.route("/finish-extraction/<task_id>", methods=["POST"])
def finish_extraction(task_id):
    """
    Finishes early and saves all contacts extracted so far into Excel and master CSV.
    """
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found."}), 404

    if task["status"] in ("done", "error"):
        return jsonify({"status": task["status"], "message": "Task already completed."})

    stop_event = task.get("stop_event")
    pause_event = task.get("pause_event")
    if stop_event:
        stop_event.set()
    if pause_event:
        pause_event.set()  # Unblock wait loop if it was paused

    task_update(
        task_id,
        "running",
        progress="Stopping extraction... Saving collected contacts to Excel...",
        percentage=90,
        contacts_found=task.get("contacts_found", 0),
    )
    log.info(f"[Task {task_id[:8]}] User requested early finish and save.")
    return jsonify({"success": True, "status": "finishing", "message": "Saving results..."})


@app.route("/cancel-extraction/<task_id>", methods=["POST"])
@app.route("/cancel-and-delete/<task_id>", methods=["POST"])
def cancel_extraction(task_id):
    """
    Cancels and deletes the current extraction task immediately.
    Stops worker thread, releases locks, deletes temporary PDF files,
    and frees the application so the user can immediately choose another topic.
    """
    with TASKS_LOCK:
        task = TASKS.get(task_id)

    if not task:
        _release_extraction_lock()
        return jsonify({"success": True, "status": "cancelled", "message": "Extraction stopped and lock cleared."})

    cancel_event = task.get("cancel_event")
    stop_event = task.get("stop_event")
    pause_event = task.get("pause_event")

    if cancel_event:
        cancel_event.set()
    if stop_event:
        stop_event.set()
    if pause_event:
        pause_event.set()

    task["status"] = "cancelled"
    task["progress"] = "Extraction cancelled and deleted."

    # Immediate release of lock so next extraction can begin immediately
    _release_extraction_lock()

    # Delete temporary PDFs directories for this run
    try:
        source_sites = task.get("source_sites") or ([task.get("source_site")] if task.get("source_site") else [])
        topic = task.get("topic", "")
        if topic and source_sites:
            topic_clean = re.sub(r'[^\w\-]', '_', topic).lower()[:80]
            import shutil
            for s in source_sites:
                topic_pdf_dir = os.path.join(PDFS_BASE_DIR, f"{s}_{topic_clean}_pdfs")
                if os.path.exists(topic_pdf_dir):
                    shutil.rmtree(topic_pdf_dir, ignore_errors=True)
                    log.info(f"[Task {task_id[:8]}] Cleaned up temporary directory: {topic_pdf_dir}")
    except Exception as e:
        log.warning(f"[Task {task_id[:8]}] Cleanup error during cancel: {e}")

    log.info(f"[Task {task_id[:8]}] Extraction cancelled and deleted by user request.")
    return jsonify({
        "success": True,
        "status": "cancelled",
        "message": "Extraction cancelled and deleted. You can choose another topic now."
    })


@app.route("/status/<task_id>")
def task_status(task_id):
    """Polling endpoint — browser calls this every 1.5 - 3 seconds."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found."}), 404
    return jsonify({
        "status": task["status"],
        "progress": task["progress"],
        "percentage": task.get("percentage", 0),
        "contacts_found": task.get("contacts_found", 0),
        "is_paused": task["status"] == "paused",
        "is_cancelled": task["status"] == "cancelled",
    })


@app.route("/result/<task_id>")
def task_result(task_id):
    """Called once when status is 'done'. Returns the final data."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task or task["status"] not in ("done", "error"):
        return jsonify({"error": "Result not ready."}), 404
    # Note: kept in TASKS so page refreshes or reconnects do not lose results;
    # cleaned up automatically by _cleanup_stale_tasks after 30 minutes.
    return jsonify(task["result"])


@app.route("/reset-lock", methods=["POST", "GET"])
@app.route("/stop-all", methods=["POST", "GET"])
def reset_lock():
    """
    Force clears any stuck extraction lock file and stops any running tasks.
    Can be called directly from browser or UI if a task gets stuck.
    """
    with TASKS_LOCK:
        for tid, task in list(TASKS.items()):
            if task.get("status") in ("running", "paused", "starting"):
                stop_event = task.get("stop_event")
                pause_event = task.get("pause_event")
                if stop_event:
                    stop_event.set()
                if pause_event:
                    pause_event.set()
                task["status"] = "done"
                task["progress"] = "Extraction stopped by reset request."

    _release_extraction_lock()
    log.info("[Lock] Extraction lock force-cleared and all active tasks stopped by user request.")
    return jsonify({
        "success": True,
        "message": "All extractions have been stopped and locks have been cleared."
    })


@app.route("/download/<filename>")
def download_excel(filename):
    file_path = os.path.join(EXCEL_BASE_DIR, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
    return "File not found", 404


# -----------------------------------------------------------
# Stage 2: Deep Filtration & Email Verifier Engine
# -----------------------------------------------------------
FILTER_TASKS = {}
FILTER_TASKS_LOCK = threading.Lock()


def _run_filter_task(task_id, file_path, original_filename, api_key=None, allowed_countries=None):
    try:
        # Read Excel or CSV file
        if original_filename.lower().endswith(".csv"):
            df = pd.read_csv(file_path, encoding="utf-8-sig")
        else:
            df = pd.read_excel(file_path)

        raw_records = df.to_dict(orient="records")
        # Pre-pass: Deep Realignment & Sanitization of shifted/unaligned fields
        records = normalize_and_realign_records(raw_records)
        total = len(records)

        with FILTER_TASKS_LOCK:
            FILTER_TASKS[task_id]["total"] = total
            FILTER_TASKS[task_id]["status"] = "running"
            FILTER_TASKS[task_id]["message"] = f"Validating {total} records concurrently with 20 threads..."

        def _progress(processed, total_cnt, valid_cnt, dropped_cnt):
            pct = int((processed / max(1, total_cnt)) * 95)
            with FILTER_TASKS_LOCK:
                if task_id in FILTER_TASKS:
                    FILTER_TASKS[task_id]["processed"] = processed
                    FILTER_TASKS[task_id]["percentage"] = pct
                    FILTER_TASKS[task_id]["valid_count"] = valid_cnt
                    FILTER_TASKS[task_id]["dropped_count"] = dropped_cnt
                    FILTER_TASKS[task_id]["message"] = (
                        f"Checked {processed}/{total_cnt} ({valid_cnt} live inboxes confirmed, {dropped_cnt} rejected)..."
                    )

        valid_records, dropped_records = batch_verify_records(
            records,
            api_key=api_key,
            allowed_countries=allowed_countries,
            max_workers=20,
            progress_callback=_progress
        )

        # Build clean Excel file with strictly the 5 requested columns
        clean_df = pd.DataFrame(valid_records)
        target_cols = ["Paper Title", "Author Name", "Email ID", "Country", "DOI"]
        for col in target_cols:
            if col not in clean_df.columns:
                clean_df[col] = ""
        clean_df = clean_df[target_cols]

        # Retain original uploaded filename and append '_updated.xlsx' (no random/UUID names)
        base_name = os.path.splitext(os.path.basename(original_filename))[0] if original_filename else "verified_authors"
        clean_filename = f"{base_name}_updated.xlsx"
        clean_path = os.path.join(EXCEL_BASE_DIR, clean_filename)
        format_and_save_excel(clean_df, clean_path, sheet_name="Verified Authors")

        dropped_filename = None
        if dropped_records:
            dropped_rows = []
            for item in dropped_records:
                r = dict(item.get("Record", {}))
                r["Rejection Reason"] = item.get("Reason", "Unknown")
                dropped_rows.append(r)
            dropped_filename = f"{base_name}_dropped_audit.csv"
            dropped_path = os.path.join(EXCEL_BASE_DIR, dropped_filename)
            pd.DataFrame(dropped_rows).to_csv(dropped_path, index=False, encoding="utf-8-sig")

        with FILTER_TASKS_LOCK:
            FILTER_TASKS[task_id].update({
                "status": "done",
                "percentage": 100,
                "message": f"Filtration complete: {len(valid_records)} verified live inboxes ready!",
                "valid_count": len(valid_records),
                "dropped_count": len(dropped_records),
                "excel_filename": clean_filename,
                "dropped_filename": dropped_filename,
                "preview": valid_records[:30],
            })

    except Exception as e:
        log.error(f"[FilterTask {task_id[:8]}] Error: {e}", exc_info=True)
        with FILTER_TASKS_LOCK:
            FILTER_TASKS[task_id].update({
                "status": "error",
                "message": f"Filtration error: {str(e)}",
                "error": str(e)
            })
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass


@app.route("/upload-and-filter", methods=["POST"])
def upload_and_filter():
    file = request.files.get("file")
    if not file or not file.filename:
        return jsonify({"error": "No file uploaded. Please upload an Excel (.xlsx) or CSV file."}), 400

    orig_filename = str(file.filename)
    filename = orig_filename.lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".xls") or filename.endswith(".csv")):
        return jsonify({"error": "Unsupported file format. Please upload .xlsx or .csv"}), 400

    api_key = request.form.get("api_key", "").strip() or None
    allowed_countries = request.form.getlist("countries[]")
    if not allowed_countries:
        raw_c = request.form.get("countries", "").strip()
        if raw_c:
            allowed_countries = [c.strip() for c in raw_c.split(",") if c.strip()]

    task_id = str(uuid.uuid4())
    temp_path = os.path.join(DOWNLOADS_DIR, f"temp_upload_{task_id}_{orig_filename}")
    file.save(temp_path)

    with FILTER_TASKS_LOCK:
        FILTER_TASKS[task_id] = {
            "status": "starting",
            "message": "Initializing multi-threaded verification engine...",
            "percentage": 5,
            "processed": 0,
            "total": 0,
            "valid_count": 0,
            "dropped_count": 0,
            "preview": [],
            "excel_filename": None,
            "dropped_filename": None,
            "ts": time.time()
        }

    t = threading.Thread(
        target=_run_filter_task,
        args=(task_id, temp_path, orig_filename, api_key, allowed_countries),
        daemon=True
    )
    t.start()

    return jsonify({"success": True, "task_id": task_id, "message": "Verification started."})


@app.route("/filter-status/<task_id>")
def filter_task_status(task_id):
    with FILTER_TASKS_LOCK:
        t = FILTER_TASKS.get(task_id)
    if not t:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(t)


@app.route("/download-filtered/<filename>")
def download_filtered_excel(filename):
    file_path = os.path.join(EXCEL_BASE_DIR, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True, download_name=filename)
    return "File not found", 404


@app.route("/download-dropped/<filename>")
def download_dropped_csv(filename):
    file_path = os.path.join(EXCEL_BASE_DIR, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True, download_name=filename)
    return "File not found", 404


def _open_browser():
    """Wait briefly for Flask to bind to port 5000, then launch default browser."""
    time.sleep(1.2)
    webbrowser.open("http://127.0.0.1:5000")


if __name__ == "__main__":
    # When Flask debug reloader is enabled, prevent opening two browser tabs
    if not os.environ.get("WERKZEUG_RUN_MAIN"):
        threading.Thread(target=_open_browser, daemon=True).start()

    app.run(debug=True, port=5000)

