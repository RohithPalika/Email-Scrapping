import os
import sys
import re
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
from extractors.imedpub_fetcher import fetch_imedpub_papers
from extractors.openalex_fetcher import fetch_openalex_papers

# API-based fetchers
from extractors.arxiv_fetcher import fetch_arxiv_papers
from extractors.biorxiv_fetcher import fetch_biorxiv_papers
from extractors.crossref_fetcher import fetch_crossref_papers

app = Flask(__name__)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

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
import sys

_log_file = os.path.join(BASE_DIR, "extraction.log")
try:
    _file_handler = RotatingFileHandler(
        _log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
except Exception:
    _file_handler = logging.NullHandler()

# Wrap stdout in a UTF-8 stream with 'replace' for unencodable chars
_utf8_stdout = open(sys.stdout.fileno(), mode="w", encoding="utf-8",
                    errors="replace", closefd=False, buffering=1)
_console_handler = logging.StreamHandler(stream=_utf8_stdout)

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
    """Append newly found email rows to the master CSV file."""
    if not new_rows:
        return
    new_df = pd.DataFrame(new_rows)
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
    "wiley.com", "springer.com", "elsevier.com", "tandfonline.com",
    "nature.com", "oup.com", "cambridge.org",
})


def clean_and_validate_email(raw_email):
    if not raw_email:
        return None
    cleaned = raw_email.strip(" \t\n\r*\u2020\u2021\u00a7#()[]{}<>,;:'\"")
    cleaned = re.split(r'[\s\(\[\{]', cleaned)[0].rstrip(".,;:")

    # Must match a standard email pattern
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', cleaned):
        return None

    local_part = cleaned.split("@")[0].lower()
    domain = cleaned.split("@")[-1].lower()
    tld = domain.split(".")[-1]

    # ISSUE-06: Reject generic / non-personal local parts
    if local_part in _GENERIC_LOCAL_PARTS:
        return None

    # Reject fused/malformed emails.
    # Original: TLD > 7 chars.
    # ISSUE-06 improvement: also detect 'monash.eduqi.fang' pattern where a
    # TLD-like segment (2-6 chars) is immediately followed by more alphabetic
    # chars without a dot separator (e.g., '.eduqi' contains 'edu' + 'qi').
    if len(tld) > 7:
        return None
    if re.search(r'\.[a-zA-Z]{2,6}[a-zA-Z]{2,}', domain):
        return None

    # Reject if domain has consecutive dots or is too long
    if ".." in domain or len(domain) > 50 or domain.count(".") > 4:
        return None
    if len(cleaned) > 80:
        return None

    # ISSUE-06: Reject platform/internal domains (expanded list)
    if any(d in domain for d in _IGNORED_DOMAINS):
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
    candidate_names = []
    potential_blocks = re.findall(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', text)
    blacklist = {
        "Indian Journal", "Medical Research", "Medical College", "Research Centre",
        "Artificial Intelligence", "All Rights", "Creative Commons", "Open Access",
        "Author Response", "Authors Response", "Received January", "Accepted February",
        "Published June", "Conflicts Of", "Financial Support", "Public Health", "BMJ Open",
    }
    for name in potential_blocks:
        if name not in blacklist and len(name.split()) in [2, 3, 4]:
            if not any(
                b.lower() in name.lower()
                for b in ["department", "university", "hospital", "institute"]
            ):
                candidate_names.append(name.strip())
    return list(dict.fromkeys(candidate_names))


def extract_author_email_pairs(file_path, metadata_authors):
    mapped_pairs = []
    try:
        with pdfplumber.open(file_path) as pdf:
            if len(pdf.pages) == 0:
                return mapped_pairs

            # ISSUE-01: Scan first 3 pages + last 2 pages.
            # Rationale: author emails appear at the front (pages 1-3) AND at the
            # back (Acknowledgements / Author Contributions / last page).
            # Previously only the first 2 pages were scanned.
            n = len(pdf.pages)
            front = list(pdf.pages[:min(3, n)])
            if n > 3:
                back_start = max(3, n - 2)
                back = list(pdf.pages[back_start:])
            else:
                back = []
            pages_to_scan = front + back

            raw_text = ""
            for page in pages_to_scan:
                # ISSUE-02: layout=True preserves column structure better than
                # layout=False, reducing column-interleave fragmentation.
                raw_text += (page.extract_text(layout=True) or "") + "\n"

            text = unwrap_broken_emails(raw_text)
            pdf_names = extract_full_names_from_pdf(text)
            all_candidate_authors = list(dict.fromkeys(metadata_authors + pdf_names))

            raw_matches = re.findall(
                r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)'
                r'(?:\s*\(([A-Z]+)\))?',
                text
            )

            for raw_email, initials in raw_matches:
                clean_email = clean_and_validate_email(raw_email)
                if not clean_email:
                    continue

                username = clean_email.split('@')[0].lower()
                clean_username = re.sub(r'[^a-z]', '', username)
                matched_author = None

                for author in all_candidate_authors:
                    # Strip non-alphabet characters from each part
                    raw_parts = [
                        re.sub(r'[^a-z]', '', p.lower())
                        for p in author.split()
                    ]
                    # Only keep parts that actually contain letters
                    name_parts = [p for p in raw_parts if len(p) > 0]
                    if any(len(part) >= 3 and part in clean_username for part in name_parts):
                        matched_author = author
                        break
                    if len(name_parts) >= 2:
                        first_init_last = name_parts[0][0] + name_parts[-1]
                        if first_init_last in clean_username:
                            matched_author = author
                            break

                if not matched_author and initials:
                    for author in all_candidate_authors:
                        author_initials = "".join(
                            [p[0].upper() for p in author.split() if p]
                        )
                        if initials.upper() == author_initials:
                            matched_author = author
                            break

                # ISSUE-03: Do NOT silently assign to first author.
                # metadata_authors[0] is the FIRST author (by listing order),
                # which is almost never the corresponding author. Using it produced
                # systematically incorrect Author↔Email pairings in the master CSV.
                # "Unknown" is honest and filterable downstream.
                if not matched_author:
                    matched_author = "Unknown"
                    log.debug(
                        f"    [Match fallback] {clean_email} → Unknown (no username/initials match)"
                    )

                if clean_email not in [p[1] for p in mapped_pairs]:
                    mapped_pairs.append((matched_author, clean_email))

    except Exception as e:
        log.error(f"Error parsing PDF ({file_path}): {e}", exc_info=True)
    return mapped_pairs


# -----------------------------------------------------------
# Dispatch Mapping
# -----------------------------------------------------------
SOURCE_FETCHERS = {
    "plos":      ("PLOS",                    fetch_plos_papers),
    "elife":     ("eLife",                   fetch_elife_papers),
    "europepmc": ("Europe PMC",              fetch_europepmc_papers),
    "imedpub":   ("iMedPub",                fetch_imedpub_papers),
    "openalex":  ("OpenAlex (All Servers)", fetch_openalex_papers),
    "arxiv":     ("arXiv",                  fetch_arxiv_papers),
    "biorxiv":   ("bioRxiv / medRxiv",     fetch_biorxiv_papers),
    "crossref":  ("Crossref (Multi-Server)", fetch_crossref_papers),
}


# -----------------------------------------------------------
# Background Extraction Worker
# -----------------------------------------------------------
def _run_extraction_task(task_id, source_site, topic, max_papers, filters=None):
    """
    Runs the full extraction in a background thread.
    Holds the extraction lock for its entire duration.

    filters (dict, optional): Extraction criteria sent from the UI:
        - countries     (list[str]): e.g. ["USA", "UK"]
        - year_from     (int|None):  e.g. 2023
        - year_to       (int|None):  e.g. 2026
        - article_types (list[str]): e.g. ["Article", "Review"]

    ISSUE-20 (Partial Save):
    Data is flushed to master CSV in batches every 5 papers AND again in the
    finally block. A crash never loses more than ~5 papers of extracted data.
    The finally block always saves whatever was pending before releasing the lock.
    """
    filters = filters or {}
    short_id = task_id[:8]
    rows = []           # All rows successfully written this run (for Excel output)
    pending_rows = []   # Batch buffer — flushed every 5 papers

    def _flush_pending():
        """Append pending batch to master CSV and move rows into the master list."""
        nonlocal pending_rows
        if pending_rows:
            save_new_emails_to_master(pending_rows)
            rows.extend(pending_rows)
            log.info(
                f"[Task {short_id}] Batch flushed {len(pending_rows)} rows to master CSV."
            )
            pending_rows = []

    try:
        source_label, fetcher_func = SOURCE_FETCHERS[source_site]

        # ISSUE-12 note: topic_clean sanitised to avoid OS path-length / invalid-char errors
        topic_clean = re.sub(r'[^\w\-]', '_', topic).lower()[:80]

        topic_pdf_dir = os.path.join(PDFS_BASE_DIR, f"{source_site}_{topic_clean}_pdfs")
        os.makedirs(topic_pdf_dir, exist_ok=True)

        task_update(task_id, "running", "Loading previously seen emails...", percentage=5, contacts_found=0)
        seen_emails, seen_dois = load_seen_data()
        log.info(
            f"[Dedup] Loaded {len(seen_emails)} seen emails, {len(seen_dois)} seen DOIs."
        )

        task_update(task_id, "running", f"Fetching papers from {source_label}...", percentage=10, contacts_found=0)
        log.info(
            f"[Task {short_id}] Starting: source={source_site}, "
            f"topic='{topic}', max={max_papers}"
        )
        # Pass filters as a keyword argument — all fetchers accept filters=None by default
        downloaded = fetcher_func(topic, max_papers, topic_pdf_dir, filters=filters)

        num_papers = len(downloaded)
        task_update(
            task_id, "running",
            f"Extracting emails from {num_papers} papers...",
            percentage=15,
            contacts_found=0
        )
        doi_skipped_count = 0

        for paper_index, item in enumerate(downloaded):
            # Calculate dynamic progress: 15% to 95%
            current_pct = 15 + int(80 * (paper_index + 1) / max(1, num_papers))
            current_found = len(rows) + len(pending_rows)
            task_update(
                task_id, "running",
                f"Processing paper {paper_index + 1} of {num_papers}...",
                percentage=current_pct,
                contacts_found=current_found
            )
            item_doi_raw = item.get("doi", "")
            # ISSUE-10: use normalised key for all dedup comparisons
            item_doi = normalise_doi(item_doi_raw)

            # --- DOI-LEVEL DUPLICATE CHECK (before any download/parse) ---
            if item_doi and item_doi in seen_dois:
                log.info(
                    f"[Dedup] Already in collection (DOI match), skipping: "
                    f"{item.get('title', '')[:55]}"
                )
                doi_skipped_count += 1
                continue

            # ISSUE-09: Record DOI as attempted regardless of email outcome.
            # Prevents re-downloading PDFs that will never yield emails.
            _record_attempted_doi(item_doi)
            if item_doi:
                seen_dois.add(item_doi)

            # HTML-First shortcut: fetcher pre-extracted emails — skip PDF parse
            if "_html_emails" in item:
                for email in item["_html_emails"]:
                    clean_email = clean_and_validate_email(email)
                    if not clean_email or clean_email in seen_emails:
                        continue

                    # ISSUE-03: Author matching — "Unknown" if no match (not first author)
                    matched_author = "Unknown"
                    clean_uname = re.sub(r'[^a-z]', '', email.split("@")[0].lower())
                    for author in item.get("authors", []):
                        name_parts = [
                            re.sub(r'[^a-z]', '', p.lower())
                            for p in author.split() if len(p) >= 2
                        ]
                        if any(len(p) >= 3 and p in clean_uname for p in name_parts):
                            matched_author = author
                            break

                    pending_rows.append({
                        "PDF File Name": item["pdf_name"],
                        "Source Journal": item.get("source_journal", source_label),
                        "Paper Title": item["title"],
                        "Author Name": matched_author,
                        "Email ID": clean_email,
                        "DOI": item_doi_raw,
                    })
                    seen_emails.add(clean_email)

                # Batch flush every 5 papers (ISSUE-20)
                if (paper_index + 1) % 5 == 0:
                    _flush_pending()
                continue  # Skip PDF parse for HTML-extracted items

            # Standard path: extract from PDF
            pairs = extract_author_email_pairs(item["file_path"], item.get("authors", []))
            for author, email in pairs:
                if email in seen_emails:
                    log.debug(f"[Dedup] Skipped duplicate email: {email}")
                    continue
                pending_rows.append({
                    "PDF File Name": item["pdf_name"],
                    "Source Journal": item.get("source_journal", source_label),
                    "Paper Title": item["title"],
                    "Author Name": author,
                    "Email ID": email,
                    "DOI": item_doi_raw,
                })
                seen_emails.add(email)

            # Batch flush every 5 papers (ISSUE-20)
            if (paper_index + 1) % 5 == 0:
                _flush_pending()

        # Flush any remaining rows not yet saved
        _flush_pending()
        log.info(f"[Task {short_id}] Loop complete. New rows this run: {len(rows)}")

        if not rows:
            if doi_skipped_count > 0 and doi_skipped_count == len(downloaded):
                message = (
                    f"All {doi_skipped_count} papers for '{topic}' on {source_label} "
                    f"are already in your collection. ✅ "
                    f"Try a different keyword, or increase the count to get fresh papers."
                )
            elif doi_skipped_count > 0:
                message = (
                    f"{doi_skipped_count} papers already collected (skipped). "
                    f"Remaining papers had no extractable emails. Try a broader keyword."
                )
            else:
                message = (
                    f"No author emails could be extracted from {source_label} for '{topic}'. "
                    f"The PDFs may be image-only scans or the emails are not in the first pages."
                )
            task_update(task_id, "done", "", result={
                "success": False,
                "message": message,
                "data": [],
                "file": None,
            }, percentage=100, contacts_found=0)
            return

        # Write Excel output
        total_found = len(rows)
        task_update(task_id, "running", f"Writing {total_found} results to Excel...", percentage=95, contacts_found=total_found)
        excel_filename = f"{source_site}_{topic_clean}_authors.xlsx"
        excel_path = os.path.join(EXCEL_BASE_DIR, excel_filename)
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(excel_path, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name="Authors")
            ws = writer.sheets["Authors"]
            for col in ws.columns:
                max_len = max(len(str(cell.value or "")) for cell in col)
                ws.column_dimensions[col[0].column_letter].width = max(max_len + 3, 14)

        log.info(
            f"[Task {short_id}] Done. {len(rows)} rows written to {excel_filename}"
        )
        task_update(task_id, "done", "", result={
            "success": True,
            "total_records": len(rows),
            "download_file": excel_filename,
            "data": rows,
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
    source_site = request.form.get("source_site", "plos").lower()
    topic = request.form.get("topic", "").strip()
    max_papers = int(request.form.get("max_papers", 10))

    if not topic or source_site not in SOURCE_FETCHERS:
        return jsonify({"error": "Invalid request."}), 400

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
    with TASKS_LOCK:
        TASKS[task_id] = {
            "status": "starting",
            "progress": "Initialising extraction...",
            "percentage": 0,
            "contacts_found": 0,
            "result": None,
            "ts": time.time(),
        }

    thread = threading.Thread(
        target=_run_extraction_task,
        args=(task_id, source_site, topic, max_papers, filters),
        daemon=True,
    )
    thread.start()
    log.info(
        f"[Task {task_id[:8]}] Queued: source={source_site}, "
        f"topic='{topic}', max={max_papers}, filters={filters}"
    )

    return jsonify({"task_id": task_id})


@app.route("/status/<task_id>")
def task_status(task_id):
    """Polling endpoint — browser calls this every 3 seconds."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found."}), 404
    return jsonify({
        "status": task["status"],
        "progress": task["progress"],
        "percentage": task.get("percentage", 0),
        "contacts_found": task.get("contacts_found", 0),
    })


@app.route("/result/<task_id>")
def task_result(task_id):
    """Called once when status is 'done'. Returns the final data."""
    with TASKS_LOCK:
        task = TASKS.get(task_id)
    if not task or task["status"] not in ("done", "error"):
        return jsonify({"error": "Result not ready."}), 404
    # Clean up task from memory after delivering result
    with TASKS_LOCK:
        TASKS.pop(task_id, None)
    return jsonify(task["result"])


@app.route("/download/<filename>")
def download_excel(filename):
    file_path = os.path.join(EXCEL_BASE_DIR, filename)
    if os.path.exists(file_path):
        return send_file(file_path, as_attachment=True)
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

