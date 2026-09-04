import os
import re
import time
import uuid
import threading
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
# Background Task Store
# -----------------------------------------------------------
# Maps task_id → {"status": ..., "progress": ..., "result": ...}
# This is the fix for the browser timeout (Bug 1).
TASKS = {}
TASKS_LOCK = threading.Lock()

# --- Concurrency guard: only ONE extraction may run at a time ---
# Prevents race conditions when two requests try to write to master_email_list.csv
# simultaneously, which would corrupt the file with duplicate or interleaved rows.
ACTIVE_EXTRACTION = threading.Lock()

def _cleanup_stale_tasks():
    """Remove tasks that finished or were abandoned more than 30 minutes ago."""
    cutoff = time.time() - 1800   # 30 minutes
    with TASKS_LOCK:
        stale = [
            tid for tid, t in TASKS.items()
            if t.get("status") in ("done", "error") and t.get("ts", 0) < cutoff
        ]
        for tid in stale:
            TASKS.pop(tid, None)
    if stale:
        print(f"[Tasks] Cleaned up {len(stale)} stale task(s).")

def task_update(task_id, status, progress="", result=None):
    """Thread-safe update of task status."""
    with TASKS_LOCK:
        if task_id in TASKS:
            TASKS[task_id]["status"] = status
            TASKS[task_id]["progress"] = progress
            if result is not None:
                TASKS[task_id]["result"] = result
            if status in ("done", "error"):
                TASKS[task_id]["ts"] = time.time()  # Timestamp for cleanup

# -----------------------------------------------------------
# Master Email Tracker (Deduplication)
# -----------------------------------------------------------
MASTER_EMAIL_FILE = os.path.join(DOWNLOADS_DIR, "master_email_list.csv")

def load_seen_data():
    """
    Load the set of all previously scraped emails AND DOIs from master CSV.

    Returns (seen_emails, seen_dois) — two separate sets.

    Why two sets?
    - seen_emails: skip individual duplicate emails within one paper
    - seen_dois:   skip the ENTIRE PAPER before downloading its PDF
                   This is the key optimization — no wasted downloads!
    """
    seen_emails = set()
    seen_dois = set()
    if os.path.exists(MASTER_EMAIL_FILE):
        try:
            df = pd.read_csv(MASTER_EMAIL_FILE)
            if "Email ID" in df.columns:
                seen_emails = set(df["Email ID"].dropna().str.lower().tolist())
            if "DOI" in df.columns:
                # Normalise DOIs: lowercase, strip https://doi.org/ prefix
                seen_dois = set(
                    df["DOI"].dropna()
                    .str.lower()
                    .str.replace("https://doi.org/", "", regex=False)
                    .str.strip()
                    .tolist()
                )
        except Exception:
            pass
    return seen_emails, seen_dois

def save_new_emails_to_master(new_rows):
    """Append newly found email rows to the master CSV file."""
    if not new_rows:
        return
    new_df = pd.DataFrame(new_rows)
    # Write header when file is new OR when it exists but is empty (0 bytes).
    # Bug fix: previously only checked os.path.exists — a 0-byte file exists
    # but has no header, causing all subsequent reads to fail silently.
    file_is_empty = (not os.path.exists(MASTER_EMAIL_FILE)
                     or os.path.getsize(MASTER_EMAIL_FILE) == 0)
    new_df.to_csv(MASTER_EMAIL_FILE, mode="a", index=False, header=file_is_empty)

# -----------------------------------------------------------
# Email Utilities
# -----------------------------------------------------------
def clean_and_validate_email(raw_email):
    if not raw_email:
        return None
    cleaned = raw_email.strip(" \t\n\r*\u2020\u2021\u00a7#()[]{}<>,;:'\"")
    cleaned = re.split(r'[\s\(\[\{]', cleaned)[0].rstrip(".,;:")

    # Must match a standard email pattern
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', cleaned):
        return None

    domain = cleaned.split("@")[-1]
    tld = domain.split(".")[-1]

    # Reject fused/malformed emails, e.g. 'user@monash.eduqi.fang' (two emails merged)
    # A real TLD is short (2-7 chars). Anything longer is a fused domain.
    if len(tld) > 7:
        return None

    # Reject if domain has consecutive dots or is too long
    if ".." in domain or len(domain) > 50 or domain.count(".") > 4:
        return None
    if len(cleaned) > 80:
        return None

    # Reject platform/internal domains
    ignored = [
        "plos.org", "elifesciences.org", "europepmc.org", "ebi.ac.uk",
        "ncbi.nlm.nih.gov", "crossref.org", "orcid.org", "adobe.com",
        "openalex.org", "imedpub.com"
    ]
    if any(d in cleaned.lower() for d in ignored):
        return None
    return cleaned.lower()

def unwrap_broken_emails(text):
    text = re.sub(r'([\w.-]+)\s*@\s*([\w.-]+)\s*\.\s*([a-zA-Z]{2,})', r'\1@\2.\3', text)
    text = re.sub(r'(\b[\w]+)\s*\.\s*([\w]+)\s*\.\s*([\w]+@)', r'\1.\2.\3', text)
    text = re.sub(r'(\b[\w]+)\s*\.\s*([\w]+@)', r'\1.\2', text)
    text = re.sub(r'(\w+)-\s*\n\s*(\w+)', r'\1-\2', text)
    text = re.sub(r'([\w.+-]+)@\s*\n\s*([\w.-]+)', r'\1@\2', text)
    text = re.sub(r'([\w.-]+)\.\s*\n\s*([a-zA-Z]{2,})', r'\1.\2', text)
    text = re.sub(r'([\w.+-]+@[\w.-]+)\s*\n\s*([\w.-]+\.[a-zA-Z]{2,})', r'\1\2', text)
    return text

def extract_full_names_from_pdf(text):
    candidate_names = []
    potential_blocks = re.findall(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)', text)
    blacklist = {
        "Indian Journal", "Medical Research", "Medical College", "Research Centre",
        "Artificial Intelligence", "All Rights", "Creative Commons", "Open Access",
        "Author Response", "Authors Response", "Received January", "Accepted February",
        "Published June", "Conflicts Of", "Financial Support", "Public Health", "BMJ Open"
    }
    for name in potential_blocks:
        if name not in blacklist and len(name.split()) in [2, 3, 4]:
            if not any(b.lower() in name.lower() for b in ["department", "university", "hospital", "institute"]):
                candidate_names.append(name.strip())
    return list(dict.fromkeys(candidate_names))

def extract_author_email_pairs(file_path, metadata_authors):
    mapped_pairs = []
    try:
        with pdfplumber.open(file_path) as pdf:
            if len(pdf.pages) == 0:
                return mapped_pairs
            raw_text = ""
            for page in pdf.pages[:min(2, len(pdf.pages))]:
                raw_text += (page.extract_text(layout=False) or "") + "\n"

            text = unwrap_broken_emails(raw_text)
            pdf_names = extract_full_names_from_pdf(text)
            all_candidate_authors = list(dict.fromkeys(metadata_authors + pdf_names))

            raw_matches = re.findall(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)(?:\s*\(([A-Z]+)\))?', text)

            for raw_email, initials in raw_matches:
                clean_email = clean_and_validate_email(raw_email)
                if not clean_email:
                    continue

                username = clean_email.split('@')[0].lower()
                clean_username = re.sub(r'[^a-z]', '', username)
                matched_author = None

                for author in all_candidate_authors:
                    name_parts = [re.sub(r'[^a-z]', '', p.lower()) for p in author.split() if len(p) >= 2]
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
                        author_initials = "".join([p[0].upper() for p in author.split() if p])
                        if initials.upper() == author_initials:
                            matched_author = author
                            break

                # --- Fallback: if no name match, use the first metadata author ---
                # Reason: emails like 'corresponding@lab.edu' are valid but won't
                # match any name pattern. Discarding them loses real data.
                if not matched_author and metadata_authors:
                    matched_author = metadata_authors[0]
                    print(f"    [Match fallback] {clean_email} → {matched_author} (no username match)")

                if matched_author and clean_email not in [p[1] for p in mapped_pairs]:
                    mapped_pairs.append((matched_author, clean_email))

    except Exception as e:
        print(f"Error parsing PDF ({file_path}): {e}")
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
def _run_extraction_task(task_id, source_site, topic, max_papers):
    """
    Runs the full extraction in a background thread.
    Holds ACTIVE_EXTRACTION lock for its entire duration so that
    no second extraction can start and corrupt the master CSV.
    """
    try:
        source_label, fetcher_func = SOURCE_FETCHERS[source_site]
        topic_clean = topic.replace(" ", "_").lower()

        topic_pdf_dir = os.path.join(PDFS_BASE_DIR, f"{source_site}_{topic_clean}_pdfs")
        os.makedirs(topic_pdf_dir, exist_ok=True)

        task_update(task_id, "running", "Loading previously seen emails...")
        seen_emails, seen_dois = load_seen_data()
        print(f"[Dedup] Loaded {len(seen_emails)} seen emails, {len(seen_dois)} seen DOIs.")

        task_update(task_id, "running", f"Fetching papers from {source_label}...")
        downloaded = fetcher_func(topic, max_papers, topic_pdf_dir)

        task_update(task_id, "running", f"Extracting emails from {len(downloaded)} papers...")
        rows = []

        doi_skipped_count = 0
        for item in downloaded:
            item_doi = item.get("doi", "").lower().replace("https://doi.org/", "").strip()

            # --- DOI-LEVEL DUPLICATE CHECK (BEFORE any download/parse) ---
            if item_doi and item_doi in seen_dois:
                print(f"[Dedup] Paper already collected (DOI match), skipping: {item.get('title','')[:55]}")
                doi_skipped_count += 1
                continue

            # HTML-First shortcut: if fetcher pre-extracted emails, skip PDF parse
            if "_html_emails" in item:
                for email in item["_html_emails"]:
                    clean_email = clean_and_validate_email(email)
                    if not clean_email or clean_email in seen_emails:
                        continue

                    # Match email to author by username similarity
                    matched_author = item["authors"][0] if item["authors"] else "Unknown"
                    clean_uname = re.sub(r'[^a-z]', '', email.split("@")[0].lower())
                    for author in item["authors"]:
                        name_parts = [re.sub(r'[^a-z]', '', p.lower()) for p in author.split() if len(p) >= 2]
                        if any(len(p) >= 3 and p in clean_uname for p in name_parts):
                            matched_author = author
                            break

                    row = {
                        "PDF File Name": item["pdf_name"],
                        "Source Journal": item.get("source_journal", source_label),
                        "Paper Title": item["title"],
                        "Author Name": matched_author,
                        "Email ID": clean_email,
                        "DOI": item["doi"]
                    }
                    rows.append(row)
                    seen_emails.add(clean_email)
                    seen_dois.add(item_doi)   # Mark DOI as collected
                continue  # Skip PDF parse for HTML-extracted items

            # Standard path: extract from PDF
            pairs = extract_author_email_pairs(item["file_path"], item["authors"])
            for author, email in pairs:
                if email in seen_emails:
                    print(f"[Dedup] Skipped duplicate: {email}")
                    continue
                row = {
                    "PDF File Name": item["pdf_name"],
                    "Source Journal": item.get("source_journal", source_label),
                    "Paper Title": item["title"],
                    "Author Name": author,
                    "Email ID": email,
                    "DOI": item["doi"]
                }
                rows.append(row)
                seen_emails.add(email)
                seen_dois.add(item_doi)   # Mark DOI as collected

        if not rows:
            # Build a clear, specific message so the user knows exactly what happened
            if doi_skipped_count > 0 and doi_skipped_count == len(downloaded):
                message = (
                    f"All {doi_skipped_count} papers for '{topic}' on {source_label} are already "
                    f"in your collection. ✅ Try a different keyword, or increase the count to get fresh papers."
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
                "file": None
            })
            return

        # Save new emails to master list
        save_new_emails_to_master(rows)
        print(f"[Dedup] Added {len(rows)} new emails to master list.")

        # Write Excel
        task_update(task_id, "running", f"Writing {len(rows)} results to Excel...")
        excel_filename = f"{source_site}_{topic_clean}_authors.xlsx"
        excel_path = os.path.join(EXCEL_BASE_DIR, excel_filename)
        df = pd.DataFrame(rows)
        with pd.ExcelWriter(excel_path, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Authors')
            ws = writer.sheets['Authors']
            for col in ws.columns:
                max_len = max(len(str(cell.value or '')) for cell in col)
                ws.column_dimensions[col[0].column_letter].width = max(max_len + 3, 14)

        task_update(task_id, "done", "", result={
            "success": True,
            "total_records": len(rows),
            "download_file": excel_filename,
            "data": rows
        })

    except Exception as e:
        print(f"[Task {task_id}] CRASHED: {e}")
        import traceback; traceback.print_exc()
        task_update(task_id, "error", str(e), result={
            "success": False,
            "message": f"Extraction failed: {str(e)}",
            "data": [],
            "file": None
        })
    finally:
        # Always release the lock — even if extraction crashed mid-way
        ACTIVE_EXTRACTION.release()
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
    """
    source_site = request.form.get("source_site", "plos").lower()
    topic = request.form.get("topic", "").strip()
    max_papers = int(request.form.get("max_papers", 10))

    if not topic or source_site not in SOURCE_FETCHERS:
        return jsonify({"error": "Invalid request."}), 400

    # Reject if another extraction is already running
    if not ACTIVE_EXTRACTION.acquire(blocking=False):
        return jsonify({
            "error": "An extraction is already in progress. Please wait for it to finish."
        }), 429

    task_id = str(uuid.uuid4())
    with TASKS_LOCK:
        TASKS[task_id] = {
            "status": "starting",
            "progress": "Initialising extraction...",
            "result": None,
            "ts": time.time()
        }

    thread = threading.Thread(
        target=_run_extraction_task,
        args=(task_id, source_site, topic, max_papers),
        daemon=True
    )
    thread.start()

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
        "progress": task["progress"]
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
