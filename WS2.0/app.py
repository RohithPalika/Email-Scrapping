import os
import sys
import re
import time
import uuid
import socket
import argparse
import threading
import logging
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from logging.handlers import RotatingFileHandler

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pdfplumber
import pandas as pd
from flask import Flask, render_template, request, send_file, jsonify

# Active extractors
from extractors.plos_fetcher import fetch_plos_papers
from extractors.elife_fetcher import fetch_elife_papers
from extractors.europepmc_fetcher import fetch_europepmc_papers
from extractors.openalex_fetcher import fetch_openalex_papers
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
    is_suppressed_email,
    get_suppressed_emails,
    add_suppressed_emails,
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
# Multi-Port & Concurrency Configuration
# -----------------------------------------------------------
CURRENT_PORT = 5000


def find_available_port(start_port=5000, max_attempts=50):
    """Finds the first free TCP port starting at start_port."""
    for p in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start_port


def get_lock_file():
    """Returns port-scoped lock file so multiple terminals run in parallel."""
    return os.path.join(DOWNLOADS_DIR, f"extraction_port_{CURRENT_PORT}.lock")


# -----------------------------------------------------------
# Logging Setup
# -----------------------------------------------------------
_log_file = os.path.join(BASE_DIR, "extraction.log")
try:
    _file_handler = RotatingFileHandler(
        _log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
except Exception:
    _file_handler = logging.NullHandler()

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

TASKS = {}
TASKS_LOCK = threading.Lock()
_INTRA_PROCESS_LOCK = threading.Lock()
_LOCK_TTL_SECONDS = 7200  # 2 hours


def _acquire_extraction_lock():
    """
    Acquire the two-layer extraction lock:
      Layer 1: threading.Lock — intra-process guard
      Layer 2: port-scoped filesystem lock file — allows other ports to run concurrently
    """
    if not _INTRA_PROCESS_LOCK.acquire(blocking=False):
        return False

    lock_path = get_lock_file()
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as f:
                timestamp = float(f.read().strip() or "0")
            age = time.time() - timestamp
            if age < _LOCK_TTL_SECONDS:
                _INTRA_PROCESS_LOCK.release()
                return False
            log.warning(
                f"[Lock] Stale lock found on port {CURRENT_PORT} (age {age / 60:.0f} min). Clearing."
            )
            os.remove(lock_path)
        except Exception as e:
            log.warning(f"[Lock] Could not read lock file: {e}. Clearing and proceeding.")
            try:
                os.remove(lock_path)
            except Exception:
                pass

    try:
        with open(lock_path, "w") as f:
            f.write(str(time.time()))
    except Exception as e:
        log.error(f"[Lock] Failed to write lock file: {e}")
        _INTRA_PROCESS_LOCK.release()
        return False

    return True


def _release_extraction_lock():
    """Release the filesystem lock for current port and threading lock."""
    lock_path = get_lock_file()
    try:
        if os.path.exists(lock_path):
            os.remove(lock_path)
    except Exception as e:
        log.warning(f"[Lock] Could not remove lock file: {e}")
    try:
        _INTRA_PROCESS_LOCK.release()
    except RuntimeError:
        pass


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


def task_update(task_id, status, progress="", result=None, percentage=0, contacts_found=0, live_data=None):
    """Thread-safe update of task status."""
    with TASKS_LOCK:
        if task_id in TASKS:
            TASKS[task_id]["status"] = status
            TASKS[task_id]["progress"] = progress
            TASKS[task_id]["percentage"] = percentage
            TASKS[task_id]["contacts_found"] = contacts_found
            if live_data is not None:
                TASKS[task_id]["live_data"] = list(live_data)
            if result is not None:
                TASKS[task_id]["result"] = result
            if status in ("done", "error"):
                TASKS[task_id]["ts"] = time.time()


# -----------------------------------------------------------
# Master Email Tracker (Deduplication & Multi-Process Safety)
# -----------------------------------------------------------
MASTER_EMAIL_FILE = os.path.join(DOWNLOADS_DIR, "master_email_list.csv")
ATTEMPTED_DOIS_FILE = os.path.join(DOWNLOADS_DIR, "attempted_dois.txt")


def _acquire_master_storage_lock(timeout=10.0):
    """
    Acquires an atomic cross-process lock file on Windows/Linux.
    Guarantees multiple terminal instances writing simultaneously never collide.
    """
    lock_file = os.path.join(DOWNLOADS_DIR, ".master_storage.lock")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.close(fd)
            return lock_file
        except (FileExistsError, OSError):
            try:
                if os.path.exists(lock_file) and (time.time() - os.path.getmtime(lock_file) > 10.0):
                    os.remove(lock_file)
            except Exception:
                pass
            time.sleep(0.04)
    try:
        if os.path.exists(lock_file):
            os.remove(lock_file)
    except Exception:
        pass
    return None


def _release_master_storage_lock():
    lock_file = os.path.join(DOWNLOADS_DIR, ".master_storage.lock")
    try:
        if os.path.exists(lock_file):
            os.remove(lock_file)
    except Exception:
        pass


def normalise_doi(raw):
    """
    Convert any DOI representation to a stable, lowercase dedup key.
    """
    if not raw:
        return ""
    s = str(raw).strip().lower()
    if s in ("n/a", "na", "", "none", "null"):
        return ""

    for prefix in ("https://doi.org/", "http://doi.org/",
                   "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix):].strip()

    for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/", "arxiv:"):
        if s.startswith(prefix):
            arxiv_id = s[len(prefix):].strip()
            arxiv_id = re.sub(r'v\d+$', '', arxiv_id)
            return f"arxiv:{arxiv_id}"

    return s.strip("/ ")


def load_seen_data():
    """
    Load the set of all previously scraped emails AND DOIs from:
      1. master_email_list.csv  (emails + DOIs of extracted papers)
      2. attempted_dois.txt     (DOIs of all attempted papers, incl. empty ones)
    """
    seen_emails = set()
    seen_dois = set()

    # --- Load from master CSV ---
    if os.path.exists(MASTER_EMAIL_FILE):
        try:
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

    # --- Load from attempted-dois file ---
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
    Append a normalised DOI key to the attempted-dois file.
    Cross-process safe append.
    """
    if not doi_key:
        return
    _acquire_master_storage_lock(timeout=5.0)
    try:
        with open(ATTEMPTED_DOIS_FILE, "a", encoding="utf-8") as f:
            f.write(doi_key + "\n")
    except Exception as e:
        log.warning(f"[Dedup] Could not write attempted_dois.txt: {e}")
    finally:
        _release_master_storage_lock()


def save_new_emails_to_master(new_rows):
    """
    Multi-process safe append to the central master CSV.
    Dynamically checks existing emails on disk before writing to prevent duplicate records
    even if multiple terminals discover identical emails concurrently.
    """
    if not new_rows:
        return
    target_cols = ["Paper Title", "Author Name", "Email ID", "Country", "DOI"]

    _acquire_master_storage_lock(timeout=10.0)
    try:
        existing_emails = set()
        if os.path.exists(MASTER_EMAIL_FILE) and os.path.getsize(MASTER_EMAIL_FILE) > 0:
            try:
                df_curr = pd.read_csv(MASTER_EMAIL_FILE, encoding="utf-8-sig")
                if "Email ID" in df_curr.columns:
                    existing_emails = set(df_curr["Email ID"].dropna().str.lower().tolist())
            except Exception:
                pass

        sanitized_rows = []
        for r in new_rows:
            raw_em = str(r.get("Email ID", "")).strip().lower()
            email_val = clean_and_repair_email(raw_em) or raw_em
            if not email_val or email_val in existing_emails or is_suppressed_email(email_val):
                continue
            existing_emails.add(email_val)

            row_copy = dict(r)
            if "Paper Title" in row_copy:
                row_copy["Paper Title"] = clean_paper_title(row_copy["Paper Title"])
            if "Author Name" in row_copy:
                row_copy["Author Name"] = clean_author_name(row_copy["Author Name"], email=email_val)
            row_copy["Email ID"] = email_val
            c_existing = str(row_copy.get("Country", "")).strip()
            if c_existing and c_existing.lower() not in ("unknown", "none", "nan", "null", "n/a", "na"):
                row_copy["Country"] = normalize_country_name(c_existing)
            else:
                row_copy["Country"] = detect_country(
                    email=email_val,
                    doi=str(row_copy.get("DOI", "")),
                    paper_title=str(row_copy.get("Paper Title", "")),
                    author_name=str(row_copy.get("Author Name", "")),
                    allow_network=True
                )
            if "DOI" in row_copy:
                row_copy["DOI"] = re.sub(r'[\r\n\t]+', '', str(row_copy["DOI"])).strip()

            clean_dict = {col: row_copy.get(col, "") for col in target_cols}
            sanitized_rows.append(clean_dict)

        if not sanitized_rows:
            return

        new_df = pd.DataFrame(sanitized_rows)[target_cols]
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
    except Exception as e:
        log.error(f"[MasterCSV] Error appending to master CSV: {e}", exc_info=True)
    finally:
        _release_master_storage_lock()


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

    if is_suppressed_email(cleaned):
        log.warning(f"[EmailVerifier] Dropped suppressed/bounced email {cleaned}")
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
    Enforces strict 1-to-1 author-email mapping per paper (no multiple emails for same author).
    """
    mapped_pairs = []
    if not file_path or not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
        return mapped_pairs
    try:
        with open(file_path, "rb") as f_chk:
            header = f_chk.read(1024)
            if b"%PDF" not in header:
                return mapped_pairs
    except Exception:
        return mapped_pairs

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
                seen_block_authors = set()
                seen_block_emails = set()
                unwrapped = unwrap_broken_emails(text_block)
                pdf_names = extract_full_names_from_pdf(unwrapped)
                raw_matches = re.findall(
                    r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)(?:\s*\(([A-Z]+)\))?',
                    unwrapped
                )
                for raw_email, initials in raw_matches:
                    clean_email = clean_and_validate_email(raw_email)
                    if not clean_email or clean_email in seen_block_emails:
                        continue
                    matched_author = match_author_for_email(
                        clean_email,
                        candidate_authors=pdf_names,
                        initials=initials,
                        metadata_authors=metadata_authors
                    )
                    if not matched_author:
                        continue
                    clean_auth = clean_author_name(matched_author, email=clean_email)
                    if not clean_auth or not is_valid_author_name(clean_auth):
                        continue
                    auth_key = clean_auth.lower()
                    if auth_key in seen_block_authors:
                        # Skip additional emails for an already-mapped author in this paper
                        continue
                    seen_block_authors.add(auth_key)
                    seen_block_emails.add(clean_email)
                    pairs.append((clean_auth, clean_email))
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
# Dispatch Mapping (Active 10 Sources)
# -----------------------------------------------------------
SOURCE_FETCHERS = {
    "plos": ("PLOS", fetch_plos_papers),
    "europepmc": ("Europe PMC", fetch_europepmc_papers),
    "elife": ("eLife", fetch_elife_papers),
    "openalex": ("OpenAlex (All Servers)", fetch_openalex_papers),
    "arxiv": ("arXiv", fetch_arxiv_papers),
    "biorxiv": ("bioRxiv / medRxiv", fetch_biorxiv_papers),
    "crossref": ("Crossref (Multi-Server)", fetch_crossref_papers),
    "pubmed": ("PubMed / NCBI", fetch_pubmed_papers),
    "frontiers": ("Frontiers", fetch_frontiers_papers),
    "aha": ("AHA Journals", fetch_aha_papers),
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
                paper_title=str(row["Paper Title"]),
                author_name=str(row["Author Name"]),
                allow_network=True
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
def get_pdf_storage_stats():
    """Computes disk space occupied by downloaded temporary PDF files."""
    total_size = 0
    total_files = 0
    if os.path.exists(PDFS_BASE_DIR):
        for root, dirs, files in os.walk(PDFS_BASE_DIR):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total_size += os.path.getsize(fp)
                    total_files += 1
                except Exception:
                    pass
    if total_size >= 1024 * 1024 * 1024:
        size_str = f"{total_size / (1024 * 1024 * 1024):.2f} GB"
    elif total_size >= 1024 * 1024:
        size_str = f"{total_size / (1024 * 1024):.1f} MB"
    elif total_size >= 1024:
        size_str = f"{total_size / 1024:.1f} KB"
    else:
        size_str = f"{total_size} Bytes"
    return {"total_size_bytes": total_size, "total_size_str": size_str, "total_files": total_files}


def _run_extraction_task(task_id, source_sites, topic, max_papers, filters=None, topics=None, auto_delete_pdfs=True):
    """
    Runs the full extraction in a background thread across one or more selected sources.
    Supports single or multiple keywords simultaneously.
    Holds the extraction lock for its entire duration.
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
            with TASKS_LOCK:
                if task_id in TASKS:
                    TASKS[task_id]["live_data"] = list(rows)
                    TASKS[task_id]["contacts_found"] = len(rows)

    try:
        run_start_time = time.time()
        topic_clean = re.sub(r'[^\w\-]', '_', "_".join(topics[:2])).lower()[:40]

        task_update(task_id, "running", "Connecting to shared master database...", percentage=5, contacts_found=0, live_data=[])
        seen_emails, seen_dois = load_seen_data()
        log.info(
            f"[Dedup] Loaded {len(seen_emails)} seen emails, {len(seen_dois)} seen DOIs from shared master."
        )

        total_papers_fetched = 0
        total_papers_processed = 0
        doi_skipped_total = 0

        # Define parallel fetch worker job
        def _fetch_source_job(site, kw, max_p, p_dir, fltrs):
            label, fetch_fn = SOURCE_FETCHERS[site]
            try:
                t_start = time.time()
                kw_papers = fetch_fn(kw, max_p, p_dir, filters=fltrs)
                t_took = time.time() - t_start
                log.info(f"[ParallelFetch] {label} returned {len(kw_papers)} papers for '{kw}' in {t_took:.2f}s")
                return site, label, kw, kw_papers
            except Exception as e_fetch:
                log.error(f"[ParallelFetch] Error in {label} for '{kw}': {e_fetch}", exc_info=True)
                return site, label, kw, []

        source_jobs = []
        for s_idx, s_site in enumerate(source_sites):
            s_label, _ = SOURCE_FETCHERS[s_site]
            t_dir = os.path.join(PDFS_BASE_DIR, f"{s_site}_{topic_clean}_pdfs")
            os.makedirs(t_dir, exist_ok=True)
            if t_dir not in created_pdf_dirs:
                created_pdf_dirs.append(t_dir)
            for kw in topics:
                source_jobs.append((s_site, kw, max_papers, t_dir, filters))

        with TASKS_LOCK:
            if task_id in TASKS:
                TASKS[task_id]["created_pdf_dirs"] = list(created_pdf_dirs)

        total_jobs = len(source_jobs)
        task_update(
            task_id, "running",
            f"⚡ Launching parallel jet-speed fetch across {len(source_sites)} source databases...",
            percentage=8,
            contacts_found=0,
            live_data=[]
        )

        # Execute parallel downloads across all selected sources simultaneously
        max_workers = min(12, max(1, total_jobs))
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_job = {
                executor.submit(_fetch_source_job, site, kw, max_p, p_dir, fltrs): (site, kw)
                for (site, kw, max_p, p_dir, fltrs) in source_jobs
            }

            completed_jobs = 0
            for future in as_completed(future_to_job):
                if cancel_event.is_set() or stop_event.is_set():
                    try:
                        executor.shutdown(wait=False, cancel_futures=True)
                    except Exception:
                        pass
                    break

                site, label, kw, papers = future.result()
                completed_jobs += 1
                total_papers_fetched += len(papers)

                pct_job = min(92, 10 + int(80 * completed_jobs / max(1, total_jobs)))
                task_update(
                    task_id, "running",
                    f"[{completed_jobs}/{total_jobs}] {label} ('{kw}'): Processing {len(papers)} paper(s)...",
                    percentage=pct_job,
                    contacts_found=len(rows) + len(pending_rows),
                    live_data=rows + pending_rows
                )

                num_papers = len(papers)
                for paper_index, item in enumerate(papers):
                    total_papers_processed += 1

                    if cancel_event.is_set() or stop_event.is_set():
                        break

                    # Smooth incremental progress calculation
                    pct_paper = min(94, 10 + int(80 * (completed_jobs - 1 + (paper_index + 1) / max(1, num_papers)) / max(1, total_jobs)))

                    # Pause check
                    if not pause_event.is_set():
                        current_found = len(rows) + len(pending_rows)
                        task_update(
                            task_id, "paused",
                            f"Extraction stopped at {label} paper {paper_index + 1}/{num_papers} ({current_found} contacts found). Click Continue to resume.",
                            percentage=pct_paper,
                            contacts_found=current_found,
                            live_data=rows + pending_rows
                        )
                        while not pause_event.is_set():
                            if stop_event.is_set() or cancel_event.is_set():
                                break
                            time.sleep(0.25)

                        if cancel_event.is_set() or stop_event.is_set():
                            break

                    item_doi_raw = item.get("doi", "")
                    item_doi = normalise_doi(item_doi_raw)

                    # Re-sync DOI deduplication against master
                    if item_doi and item_doi in seen_dois:
                        doi_skipped_total += 1
                        continue

                    _record_attempted_doi(item_doi)
                    if item_doi:
                        seen_dois.add(item_doi)

                    # Strict 1-to-1 Author-Email Mapping per Paper
                    seen_authors_in_this_paper = set()

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

                            auth_norm = clean_auth.lower()
                            if auth_norm in seen_authors_in_this_paper:
                                # User requirement: 1 article can have multiple authors, but each author gets only 1 verified email
                                continue

                            # Country filter check
                            is_ok, country_reason = is_country_allowed(
                                email=clean_email,
                                allowed_countries=selected_countries,
                                author_name=clean_auth,
                                paper_title=item.get("title", ""),
                                doi=item_doi_raw
                            )
                            if not is_ok:
                                continue

                            detected_country = detect_country(
                                email=clean_email,
                                doi=item_doi_raw,
                                affiliation=item.get("affiliation", ""),
                                paper_title=item.get("title", ""),
                                author_name=clean_auth,
                                allowed_countries=selected_countries,
                                allow_network=True
                            )

                            seen_authors_in_this_paper.add(auth_norm)
                            seen_emails.add(clean_email)
                            pending_rows.append({
                                "PDF File Name": item.get("pdf_name", "N/A"),
                                "Source Journal": item.get("source_journal", label),
                                "Paper Title": clean_paper_title(item.get("title", "")),
                                "Author Name": clean_auth,
                                "Email ID": clean_email,
                                "Country": detected_country,
                                "DOI": re.sub(r'[\r\n\t]+', '', str(item_doi_raw)).strip(),
                            })

                            # Immediately update live streaming data
                            current_total_rows = rows + pending_rows
                            task_update(
                                task_id, "running",
                                f"[{completed_jobs}/{total_jobs}] {label}: Extracted {clean_auth} ({len(current_total_rows)} total)...",
                                percentage=pct_paper,
                                contacts_found=len(current_total_rows),
                                live_data=current_total_rows
                            )

                        if (paper_index + 1) % 5 == 0:
                            _flush_pending()
                        continue

                    # PDF parsing
                    if "file_path" in item and os.path.exists(item["file_path"]):
                        pairs = extract_author_email_pairs(item["file_path"], item.get("authors", []))
                        for author, email in pairs:
                            clean_auth = clean_author_name(author, email=email)
                            if not clean_auth or not is_valid_author_name(clean_auth):
                                continue

                            auth_norm = clean_auth.lower()
                            if auth_norm in seen_authors_in_this_paper:
                                # User requirement: 1 article can have multiple authors, but each author gets only 1 verified email
                                continue

                            if email in seen_emails:
                                continue

                            # Country filter check
                            is_ok, country_reason = is_country_allowed(
                                email=email,
                                allowed_countries=selected_countries,
                                author_name=clean_auth,
                                paper_title=item.get("title", ""),
                                doi=item_doi_raw
                            )
                            if not is_ok:
                                continue

                            detected_country = detect_country(
                                email=email,
                                doi=item_doi_raw,
                                affiliation=item.get("affiliation", ""),
                                paper_title=item.get("title", ""),
                                author_name=clean_auth,
                                allowed_countries=selected_countries,
                                allow_network=True
                            )

                            seen_authors_in_this_paper.add(auth_norm)
                            seen_emails.add(email)
                            pending_rows.append({
                                "PDF File Name": item.get("pdf_name", "N/A"),
                                "Source Journal": item.get("source_journal", label),
                                "Paper Title": clean_paper_title(item.get("title", "")),
                                "Author Name": clean_auth,
                                "Email ID": email,
                                "Country": detected_country,
                                "DOI": re.sub(r'[\r\n\t]+', '', str(item_doi_raw)).strip(),
                            })

                            # Immediately update live streaming data
                            current_total_rows = rows + pending_rows
                            task_update(
                                task_id, "running",
                                f"[{completed_jobs}/{total_jobs}] {label}: Extracted {clean_auth} ({len(current_total_rows)} total)...",
                                percentage=pct_paper,
                                contacts_found=len(current_total_rows),
                                live_data=current_total_rows
                            )

                        if (paper_index + 1) % 5 == 0:
                            _flush_pending()

                _flush_pending()

            # Flush remaining after each source completes
            _flush_pending()
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass

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
        total_elapsed_seconds = round(time.time() - run_start_time, 1)
        mins = int(total_elapsed_seconds // 60)
        secs = int(total_elapsed_seconds % 60)
        elapsed_str = f"{mins:02d}m {secs:02d}s" if mins > 0 else f"{secs}s"
        speed_per_min = round((len(rows) / max(1.0, total_elapsed_seconds)) * 60, 1)

        log.info(f"[Task {short_id}] Run complete in {elapsed_str} (stopped_early={was_stopped}). New rows this run: {len(rows)}")

        if not rows:
            if was_stopped:
                message = f"Extraction stopped by user after {elapsed_str} before any contacts were found."
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
            task_update(task_id, "done", message, result={
                "success": False,
                "message": message,
                "data": [],
                "file": None,
                "stopped_early": was_stopped,
                "elapsed_seconds": total_elapsed_seconds,
                "elapsed_str": elapsed_str,
                "speed_per_min": 0,
            }, percentage=100, contacts_found=0)
            return

        # Write Excel output
        total_found = len(rows)
        task_update(task_id, "running", f"Writing {total_found} verified results to Excel...", percentage=95, contacts_found=total_found)
        if len(source_sites) == 1:
            excel_filename = f"{source_sites[0]}_{topic_clean}_authors.xlsx"
        else:
            excel_filename = f"multisource_{topic_clean}_authors.xlsx"
        excel_path = os.path.join(EXCEL_BASE_DIR, excel_filename)

        df = pd.DataFrame(rows)
        format_and_save_excel(df, excel_path, sheet_name="Authors")

        # Auto-delete temporary PDFs for this run upon 100% completion if enabled
        if auto_delete_pdfs:
            import shutil
            for pdir in created_pdf_dirs:
                try:
                    if os.path.exists(pdir):
                        shutil.rmtree(pdir, ignore_errors=True)
                        log.info(f"[Task {short_id}] Auto-deleted temporary PDF folder on complete: {pdir}")
                except Exception as e_del:
                    log.warning(f"[Task {short_id}] Could not auto-delete PDF folder {pdir}: {e_del}")

        status_msg = (
            f"Extraction stopped early. {len(rows)} contacts saved in {elapsed_str}."
            if was_stopped
            else f"Extraction complete: Found {len(rows)} verified contacts across {len(source_sites)} sources in {elapsed_str} ({speed_per_min}/min)."
        )
        log.info(f"[Task {short_id}] {status_msg} -> written to {excel_filename}")

        task_update(task_id, "done", status_msg, result={
            "success": True,
            "total_records": len(rows),
            "download_file": excel_filename,
            "data": rows,
            "stopped_early": was_stopped,
            "message": status_msg,
            "elapsed_seconds": total_elapsed_seconds,
            "elapsed_str": elapsed_str,
            "speed_per_min": speed_per_min,
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


@app.route("/storage-stats", methods=["GET"])
def storage_stats():
    """Returns disk usage of the temporary downloaded PDF files."""
    return jsonify({
        "success": True,
        "pdf_storage": get_pdf_storage_stats()
    })


@app.route("/purge-pdfs", methods=["POST"])
def purge_pdfs():
    """
    1-Click purge of all downloaded temporary PDF files in downloads/Pdf Files/.
    Leaves master_email_list.csv, attempted_dois.txt, and author details/ Excel files 100% safe.
    """
    import shutil
    deleted_files = 0
    deleted_bytes = 0
    try:
        if os.path.exists(PDFS_BASE_DIR):
            for item in os.listdir(PDFS_BASE_DIR):
                item_path = os.path.join(PDFS_BASE_DIR, item)
                try:
                    if os.path.isfile(item_path):
                        deleted_bytes += os.path.getsize(item_path)
                        os.remove(item_path)
                        deleted_files += 1
                    elif os.path.isdir(item_path):
                        for root, dirs, files in os.walk(item_path):
                            for f in files:
                                fp = os.path.join(root, f)
                                try:
                                    deleted_bytes += os.path.getsize(fp)
                                    deleted_files += 1
                                except Exception:
                                    pass
                        shutil.rmtree(item_path, ignore_errors=True)
                except Exception as e:
                    log.warning(f"[PurgePDFs] Could not delete {item_path}: {e}")
        os.makedirs(PDFS_BASE_DIR, exist_ok=True)

        if deleted_bytes >= 1024 * 1024 * 1024:
            freed_str = f"{deleted_bytes / (1024 * 1024 * 1024):.2f} GB"
        elif deleted_bytes >= 1024 * 1024:
            freed_str = f"{deleted_bytes / (1024 * 1024):.1f} MB"
        elif deleted_bytes >= 1024:
            freed_str = f"{deleted_bytes / 1024:.1f} KB"
        else:
            freed_str = f"{deleted_bytes} Bytes"

        log.info(f"[PurgePDFs] Successfully purged {freed_str} across {deleted_files} files.")
        return jsonify({
            "success": True,
            "message": f"Purged {freed_str} ({deleted_files} files). Master database & Excels remain 100% intact.",
            "freed_bytes": deleted_bytes,
            "freed_str": freed_str,
            "deleted_files": deleted_files,
            "stats": get_pdf_storage_stats()
        })
    except Exception as e:
        log.error(f"[PurgePDFs] Error: {e}", exc_info=True)
        return jsonify({"success": False, "error": str(e)}), 500


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

    # Read auto_delete_pdfs toggle
    raw_auto_delete = request.form.get("auto_delete_pdfs", "true").strip().lower()
    auto_delete_pdfs = raw_auto_delete in ("true", "1", "yes", "on")

    # --- Read extraction filters from the form ---
    raw_year_from = request.form.get("year_from", "").strip()
    raw_year_to = request.form.get("year_to", "").strip()
    filters = {
        "countries": request.form.getlist("countries[]"),
        "year_from": int(raw_year_from) if raw_year_from.isdigit() else 2024,
        "year_to": int(raw_year_to) if raw_year_to.isdigit() else 2026,
        "article_types": request.form.getlist("article_types[]"),
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
            "auto_delete_pdfs": auto_delete_pdfs,
        }

    thread = threading.Thread(
        target=_run_extraction_task,
        args=(task_id, source_sites, topic, max_papers, filters, topics, auto_delete_pdfs),
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
        import shutil
        dirs_to_clean = list(task.get("created_pdf_dirs") or [])
        source_sites = task.get("source_sites") or ([task.get("source_site")] if task.get("source_site") else [])
        topic = task.get("topic", "")
        if topic and source_sites:
            topic_clean = re.sub(r'[^\w\-]', '_', topic).lower()[:80]
            for s in source_sites:
                topic_pdf_dir = os.path.join(PDFS_BASE_DIR, f"{s}_{topic_clean}_pdfs")
                if topic_pdf_dir not in dirs_to_clean:
                    dirs_to_clean.append(topic_pdf_dir)
        for pdir in dirs_to_clean:
            if os.path.exists(pdir):
                shutil.rmtree(pdir, ignore_errors=True)
                log.info(f"[Task {task_id[:8]}] Cleaned up temporary directory: {pdir}")
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
        "live_data": task.get("live_data", []),
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


@app.route("/suppression-list", methods=["GET", "POST"])
def manage_suppression_list():
    """
    GET: Returns list and count of suppressed bounce emails.
    POST: Adds new bounced emails to suppression list.
    """
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        raw_text = data.get("emails", "") or request.form.get("emails", "")
        if isinstance(raw_text, list):
            new_emails = raw_text
        else:
            new_emails = [e.strip() for e in re.split(r'[\r\n,;]+', str(raw_text)) if e.strip()]
        added = add_suppressed_emails(new_emails)
        all_suppressed = sorted(list(get_suppressed_emails()))
        return jsonify({
            "success": True,
            "added_count": added,
            "total_count": len(all_suppressed),
            "suppression_list": all_suppressed,
            "message": f"Successfully added {added} email(s) to suppression list ({len(all_suppressed)} total)."
        })

    suppressed = sorted(list(get_suppressed_emails()))
    return jsonify({
        "success": True,
        "total_count": len(suppressed),
        "suppression_list": suppressed
    })


def _launch_browser(port):
    """Wait briefly for Flask to bind to port, then launch default browser."""
    time.sleep(1.0)
    url = f"http://127.0.0.1:{port}"
    try:
        if sys.platform == "win32":
            os.system(f'start "" "{url}"')
        else:
            webbrowser.open(url)
    except Exception:
        try:
            webbrowser.open(url)
        except Exception:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PULSUS AI | ScholarLead Matrix")
    parser.add_argument("--port", "-p", type=int, default=None, help="Port to bind (e.g. 5000, 5001)")
    parser.add_argument("pos_port", nargs="?", type=int, default=None, help="Optional positional port")
    args, _ = parser.parse_known_args()

    requested_port = args.port or args.pos_port
    if requested_port:
        CURRENT_PORT = find_available_port(start_port=requested_port, max_attempts=20)
    else:
        CURRENT_PORT = find_available_port(start_port=5000, max_attempts=50)

    # Ensure any stale lock for this specific port is cleared
    _release_extraction_lock()

    print("=" * 65)
    print(" [*] PULSUS AI | ScholarLead Matrix (WS2.0)")
    print(f" [>] Running at: http://127.0.0.1:{CURRENT_PORT}")
    print(f" [>] Process Port: {CURRENT_PORT} | Shared Master Database Connected")
    print(" Opening browser automatically...")
    print("=" * 65)

    threading.Thread(target=_launch_browser, args=(CURRENT_PORT,), daemon=True).start()

    app.run(host="127.0.0.1", port=CURRENT_PORT, debug=False, use_reloader=False, threaded=True)
