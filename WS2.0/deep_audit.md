# Deep Audit: Academic Email Extraction Project
### Analysis-Only — No code changes, no implementation recommendations

---

## 1. Project Understanding

This is a **Flask-based web application** whose core purpose is:

1. Accept a user's search topic and a source site selection from a browser form.
2. Dispatch to one of eight "fetcher" scripts that query an external API or website, download PDFs, and return structured metadata records.
3. In the main background worker (`_run_extraction_task` in `app.py`), read each PDF with `pdfplumber`, run regex-based email extraction, attempt author-name matching, deduplicate against a master CSV, then write results to both the master CSV and a per-run Excel file.

**Source sites currently supported:**

| Key | Fetcher | Strategy |
|---|---|---| 
| `plos` | `plos_fetcher.py` | Searches PLOS API → downloads PDF directly |
| `elife` | `elife_fetcher.py` | Searches eLife API → downloads PDF |
| `europepmc` | `europepmc_fetcher.py` | Searches EuropePMC API → downloads PDF |
| `imedpub` | `imedpub_fetcher.py` | Scrapes iMedPub search HTML → follows article links → finds PDF |
| `openalex` | `openalex_fetcher.py` | Queries OpenAlex REST API → downloads Open Access PDF |
| `arxiv` | `arxiv_fetcher.py` | Queries arXiv API → tries HTML abstract first; PDF only as fallback |
| `biorxiv` | `biorxiv_fetcher.py` | Queries Crossref (prefix 10.1101) → constructs bioRxiv/medRxiv PDF URL |
| `crossref` | `crossref_fetcher.py` | Queries Crossref API → batch-resolves PDFs via OpenAlex |

**Two fetchers exist but are NOT registered:**
- `medrxiv_fetcher.py` — a separate standalone medRxiv fetcher using EuropePMC cursor pagination.
- `preprints_fetcher.py` — a generic preprint fetcher using EuropePMC cursor pagination.

Neither is imported in `app.py` or listed in `SOURCE_FETCHERS`. They are dead code.

---

## 2. Current Email Extraction Flow

```
Browser Form → POST /start-extraction
                ↓
        [Thread spawned] _run_extraction_task(task_id, source, topic, max_papers)
                ↓
        load_seen_data()            ← reads master_email_list.csv into memory
        (seen_emails set, seen_dois set)
                ↓
        fetcher_func(topic, max_papers, topic_pdf_dir)
            → returns list of dicts: {file_path, pdf_name, title, authors, doi, [_html_emails]}
                ↓
        For each item:
            → DOI-level duplicate check against seen_dois
            → if _html_emails present: validate, author-match, add to rows
            → else: pdfplumber reads PDF pages 1–2
                   → unwrap_broken_emails() (regex stitching)
                   → extract_full_names_from_pdf() (capitalized word groups)
                   → regex scan for email patterns
                   → clean_and_validate_email() per email
                   → author-name fuzzy match
                   → fallback: assign to metadata_authors[0]
                   → add to rows if not in seen_emails
                ↓
        save_new_emails_to_master(rows)   ← append-write to master CSV
        write Excel file
                ↓
        Browser polls /status/<task_id> every 3 s
        When done → /result/<task_id> → renders table + Excel download link
```

---

## 3. Issues Found

### 3.1 — Email Extraction Reliability

---

**ISSUE-01: Only first 2 PDF pages are scanned**
- **Where:** `app.py` line 188 — `for page in pdf.pages[:min(2, len(pdf.pages))]`
- **Why it's a problem:** Corresponding author emails in many journals appear on the last page (e.g., Acknowledgements, Author Contributions, Conflict of Interest sections). In multi-column or two-column PDFs (PLOS, eLife), author blocks wrap in ways that push emails beyond page 2.
- **Impact:** Significant email loss. Real data not collected.
- **Severity:** **High** — Data Quality

---

**ISSUE-02: `unwrap_broken_emails` uses fragile regex stitching**
- **Where:** `app.py` lines 156–163
- **Why it's a problem:** The regex patterns try to glue hyphenated line-breaks, whitespace-split `@` symbols, and domain fragments, but only handle a narrow set of patterns. Multi-column PDF text extracted by `pdfplumber` frequently has column interleaving where text from column 1 and column 2 is interleaved by line, making these patterns fail or produce false stitches.
- **Impact:** Missed emails or malformed email strings passed to the validator.
- **Severity:** **High** — Data Quality, Reliability

---

**ISSUE-03: Name-matching fallback silently assigns wrong author**
- **Where:** `app.py` lines 227–229 — "Fallback: if no name match, use the first metadata author"
- **Why it's a problem:** When a paper's email (e.g., `corresponding@lab.edu`) has no username similarity to any author name, the code silently assigns the email to `metadata_authors[0]` — which is almost never the corresponding author. The first author in a metadata list is typically the first listed author, not the corresponding author.
- **Impact:** Systematic incorrect Author Name ↔ Email pairing. This is not a minor cosmetic issue; it corrupts the relationship between email and person.
- **Severity:** **Critical** — Data Quality (incorrect records permanently stored)

---

**ISSUE-04: arXiv placeholder file written to disk even when not used**
- **Where:** `arxiv_fetcher.py` lines 152–154
- **Why it's a problem:** When HTML emails are found, a placeholder file `b"HTML_EMAIL_EXTRACTED"` is written to disk as if it were a PDF. The `file_path` is stored in the record, so the downloads folder accumulates thousands of fake "PDF" files over time. These cannot be parsed if someone later tries to re-read them.
- **Impact:** Disk space waste; misleading file inventory; potential confusion if someone audits the downloads folder.
- **Severity:** **Medium** — Maintainability, Data Integrity

---

**ISSUE-05: iMedPub fetcher uses brittle regex HTML parsing with no pagination**
- **Where:** `imedpub_fetcher.py` lines 44–46 — `re.findall(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', html, re.I)`
- **Why it's a problem:** This captures ALL anchor tags on the page, not specifically article links, then filters by URL patterns that are easily changed if iMedPub updates its site structure. There is no pagination: only one search results page is ever loaded, limiting results to what appears on that single page.
- **Impact:** Low yield from iMedPub; very sensitive to site layout changes (can break completely overnight).
- **Severity:** **High** — Reliability, Maintainability

---

**ISSUE-06: `clean_and_validate_email` domain-blocklist is hardcoded and incomplete**
- **Where:** `app.py` lines 147–153
- **Why it's a problem:** The blocklist contains 8 specific domains. As new sources are added, their internal platform emails (e.g., `@biorxiv.org`, `@crossref.org`, `@orcid.org`) are not always present. A real example visible in the master CSV is `line@www.bjd-abd.com` — likely a navigation element, not a person's email, which was NOT blocked.
- **Impact:** Garbage/internal emails enter the dataset.
- **Severity:** **High** — Data Quality

---

**ISSUE-07: email_verifier.py is imported nowhere and unused**
- **Where:** `extractors/email_verifier.py` — file exists with SMTP/DNS verification logic, but is never imported or called in `app.py` or any fetcher.
- **Why it's a problem:** All emails that pass the regex and domain-blocklist check are added to the master list without any actual deliverability verification. The infrastructure for verification exists but is disconnected.
- **Impact:** Unverified, potentially dead emails accumulate.
- **Severity:** **Medium** — Data Quality

---

**ISSUE-08: Author name extraction from PDF text is extremely noisy**
- **Where:** `app.py` lines 166–179 — `extract_full_names_from_pdf()`
- **Why it's a problem:** The function grabs any sequence of "Capitalized Words." Academic papers are full of capitalized proper nouns that are not names: journal names, institution names, section headers, abbreviations. The blacklist contains only 12 phrases. In practice, hundreds of false-positive "author names" will be collected, diluting the real author list and making the name-matching step less accurate.
- **Impact:** Incorrect author attribution; algorithm is biased toward matching emails to institution-derived false "names."
- **Severity:** **Medium** — Data Quality

---

### 3.2 — Deduplication

---

**ISSUE-09: `seen_dois` is loaded and populated in-memory only for the current run**
- **Where:** `app.py` lines 269, 313–314, 332–333
- **Why it's a problem:** `seen_dois` is loaded from the master CSV at the start of each run, which is correct. However, the in-memory `seen_dois` is never written back to master CSV separately — the DOI is only considered "seen" if at least one email row was extracted from it. If a paper has no valid emails extracted but is processed, its DOI is **not** added to the master CSV, so the same paper will be downloaded and parsed again on every future run for the same topic.
- **Impact:** Repeated costly PDF downloads of papers that will never yield emails.
- **Severity:** **High** — Performance, Scalability

---

**ISSUE-10: DOI normalization is inconsistent between loading and saving**
- **Where:** `load_seen_data()` (`app.py` lines 94–102) strips `https://doi.org/` prefix; but the DOI stored in the CSV rows (`app.py` line 329, and in fetchers) is sometimes the raw string with the prefix, sometimes without, sometimes an arXiv URL like `http://arxiv.org/abs/...`.
- **Why it's a problem:** On the next run, `load_seen_data()` will normalize to lowercase and strip `https://doi.org/`, but an arXiv DOI stored as `http://arxiv.org/abs/1234.5678v2` will normalize differently (`http://arxiv.org/abs/1234.5678v2` stays as-is since it has no `doi.org/` prefix), making the DOI check inconsistent.
- **Impact:** Papers from arXiv and similar sources whose DOI is an arXiv URL may never match the deduplification check, causing re-processing on every run.
- **Severity:** **High** — Data Quality, Deduplication Reliability

---

**ISSUE-11: Email deduplication is case-sensitive during the run (in-memory set) but normalized to lowercase in the CSV**
- **Where:** `app.py` line 292: `clean_and_validate_email()` returns `.lower()`, so the email set should be lowercase. But the in-memory `seen_emails` set is populated from the CSV via `.str.lower()` as well. This part is consistent. HOWEVER, the HTML email path in arxiv_fetcher.py line 54 returns `.lower()` emails too, so this particular path is fine. The risk emerges when emails extracted from PDF are not yet cleaned — `raw_matches` on line 195 can contain mixed-case strings that pass `clean_and_validate_email()` since it lowercases, so this is handled. **Low risk here but worth noting it is not obvious.**
- **Severity:** **Low** — Data Quality (minor)

---

**ISSUE-12: The global `ACTIVE_EXTRACTION` lock does NOT prevent a user from opening two browser tabs and starting two simultaneous extractions**
- **Where:** `app.py` lines 421–424 — `ACTIVE_EXTRACTION.acquire(blocking=False)`
- **Why it's a problem:** The lock correctly blocks concurrent extractions within a single Flask process. However, if the app is run with a multi-worker WSGI server (e.g., gunicorn with `--workers 2`), the lock is process-local (a Python threading lock), not a cross-process lock. Two workers would each have their own `ACTIVE_EXTRACTION`, and both could start simultaneously, causing concurrent writes to `master_email_list.csv`.
- **Impact:** CSV corruption via interleaved writes under production deployment; not a problem in single-process `python app.py`.
- **Severity:** **High** — Reliability (production risk), Data Integrity

---

### 3.3 — Architecture

---

**ISSUE-13: Two fetchers are dead code — `medrxiv_fetcher.py` and `preprints_fetcher.py`**
- **Where:** Both files exist in `extractors/` but are not imported in `app.py` and not in `SOURCE_FETCHERS`.
- **Why it's a problem:** These represent work done but inaccessible from the app. They have a different function signature (`fetch_medrxiv_papers(query, max_papers, output_dir, start_year, end_year)`) than the rest (`fetch_*_papers(topic, limit, target_dir)`), meaning they CANNOT be registered without a wrapper since the dispatcher calls `fetcher_func(topic, max_papers, topic_pdf_dir)`.
- **Impact:** Dead code causing confusion; inconsistent API contracts across fetchers.
- **Severity:** **Medium** — Maintainability

---

**ISSUE-14: Fetcher return-dict schema is not enforced — inconsistent keys across fetchers**
- **Where:** All fetcher files return a list of dicts, but the keys differ:
  - `medrxiv_fetcher.py` includes `"pdf_url"` key; others do not.
  - `europepmc_fetcher.py` does not include `"source_journal"` key.
  - `medrxiv_fetcher.py` formats DOI as `"https://doi.org/{doi}"` while `crossref_fetcher.py` stores raw DOI without prefix.
- **Why it's a problem:** `app.py` does `item.get("source_journal", source_label)` as a fallback — a silent patch. New fetchers will silently use wrong defaults if they omit keys. No schema validation exists.
- **Impact:** Silent data errors when adding new sources; hard to debug missing fields.
- **Severity:** **Medium** — Maintainability, Data Quality

---

**ISSUE-15: PDF files are never cleaned up**
- **Where:** `app.py` lines 265–266 — `topic_pdf_dir` is created in `downloads/Pdf Files/`; PDF files are written but never deleted.
- **Why it's a problem:** Every run for the same topic creates new numbered PDFs (`arxiv_paper_1.pdf`, etc.) in the same folder, overwriting previous files silently if the name collides, OR accumulating if names don't collide. Over time, the `downloads/` folder can grow to gigabytes.
- **Impact:** Disk exhaustion on long-running systems; overwritten files cause incorrect audit trails.
- **Severity:** **Medium** — Scalability

---

**ISSUE-16: Task memory is not bounded — `TASKS` dict grows indefinitely during a session**
- **Where:** `app.py` lines 39, 395 — `_cleanup_stale_tasks()` only runs after each completed task.
- **Why it's a problem:** If the server runs for days, completed tasks accumulate in `TASKS` until the next task completes and triggers cleanup. Under very frequent usage, this is a memory leak.
- **Impact:** Memory growth over time; low severity for low-usage scenarios.
- **Severity:** **Low** — Scalability

---

**ISSUE-17: No logging framework — all output goes to `print()`**
- **Where:** Throughout all files.
- **Why it's a problem:** `print()` output goes to stdout only and is not persisted, structured, or queryable. If a run fails, there is no log file to inspect after the fact. Error messages in except blocks are often completely swallowed (`except Exception: continue`).
- **Impact:** Extremely difficult to debug failed extractions or investigate data quality issues retroactively.
- **Severity:** **High** — Maintainability, Debuggability

---

**ISSUE-18: No retry logic for API calls that fail transiently**
- **Where:** Most fetchers break or return `[]` on any non-200 status code with no retry. E.g., `europepmc_fetcher.py` line 22: `if resp.status_code != 200: return []`.
- **Why it's a problem:** A transient 503 or rate-limit response results in an empty run — the user gets nothing even though the source is available.
- **Impact:** False-empty extraction runs; user frustration; data missed for no real reason.
- **Severity:** **Medium** — Reliability

---

**ISSUE-19: Contact email (personal student email) hardcoded in fetchers**
- **Where:** `crossref_fetcher.py` line 6: `CONTACT_EMAIL = "23r25a6702@mlrit.ac.in"` and `biorxiv_fetcher.py` line 60: `"mailto": "23r25a6702@mlrit.ac.in"`.
- **Why it's a problem:** This email is used in API `mailto` parameters (Crossref polite pool, OpenAlex). This is a real student's academic email. If the email expires (graduation), or if the institution blocks it, API calls that rely on the polite pool may degrade or fail. Also, it is a personal identifier embedded in code.
- **Impact:** Operational risk (API access degraded if email expires), privacy concern.
- **Severity:** **Medium** — Security/Privacy, Reliability

---

**ISSUE-20: No error recovery after partial extraction crash**
- **Where:** `app.py` lines 383–394.
- **Why it's a problem:** If the extraction crashes after 7 out of 10 papers are processed, the 7 rows already extracted are lost — `save_new_emails_to_master` is only called after the full loop. The `finally` block releases the lock, but does not save partial results.
- **Impact:** Data loss on any crash mid-run.
- **Severity:** **High** — Reliability, Data Loss

---

## 4. Duplicate-Data Risks

### 4.1 Exact Duplicate Emails
- **Detected?** Yes, by `seen_emails` set loaded from master CSV.
- **Gap:** Only within the current process's lifetime. If master CSV is manually edited, corrupted, or two processes run concurrently, duplicates can slip in.

### 4.2 Same Email with Different Formatting
- **Detected?** Partially. `clean_and_validate_email()` lowercases the result. But `John.Doe@UNIVERSITY.EDU` and `john.doe@university.edu` would both normalize to lowercase → detected. Email with `+tag` (e.g., `john+arxiv@uni.edu`) is treated as distinct from `john@uni.edu` → **NOT detected as duplicate**.
- **Gap:** Plus-tagged emails (`+tag` style) and alias emails are not normalized.

### 4.3 Same Author Appearing Multiple Times
- **Detected?** Not at all. An author who publishes in both arXiv and PLOS will have two rows with different `PDF File Name` and `DOI` but the same `Author Name` and `Email ID`. The email deduplication prevents the same email from appearing twice, but only the first run captures the record. If the email was already seen, the second paper by the same author is silently skipped — meaning newer publications by that author never produce records.

### 4.4 Same Publication Appearing Multiple Times
- **Detected?** Yes, by DOI matching — IF the DOI is present and consistently formatted.
- **Gap:** See ISSUE-10. arXiv papers use `http://arxiv.org/abs/...` as their DOI. A paper on both arXiv and CrossRef has TWO different DOIs in the system (the arXiv URL and the actual DOI). Both could be fetched and processed independently, producing duplicate rows with different `DOI` values but the same `Email ID` — which the email-level check would catch. But the `Author Name` assignment could differ between the two runs.

### 4.5 Same PDF Downloaded Multiple Times
- **Detected?** No. PDF files are named `{source}_{topic}_pdfs/arxiv_paper_1.pdf`. If the user runs the same topic + source combination again, the same numbered filenames are created again, overwriting the prior files. There is no content-level deduplication of PDFs.

### 4.6 Same Email Associated with Different Publications
- **Detected?** Partially. The email deduplication means only the first publication's data is stored. If the same email appears in a second paper, the second paper's title, DOI, and author assignment are silently discarded. The master CSV will never accumulate: "john@uni.edu published Paper A AND Paper B."

### 4.7 Multiple Emails Associated with the Same Author
- **Detected?** No. If an author uses `j.doe@lab1.edu` in one paper and `johndoe@lab2.edu` in another, both are stored as separate rows with potentially different `Author Name` values (due to the name-matching imprecision), with no linkage between them.

### 4.8 Duplicate Data Across Different Scraping Runs
- **Detected?** By email. If a new run is started for a different topic but involves the same papers (e.g., broad topic overlap), the DOI check will suppress the paper, and the email check will suppress individual emails. However, if even one new email is found in the "seen" paper, the DOI check skips the whole paper before trying, meaning that new email is also lost.

### 4.9 Duplicates Already in `master_email_list.csv`
- See Section 5 for direct observations from the actual file.

---

## 5. Master Email List Audit

### 5.1 Observed Structure
The CSV has six columns:
```
PDF File Name | Source Journal | Paper Title | Author Name | Email ID | DOI
```

### 5.2 Direct Observations from the Actual File

**Observation A — Same PDF name appears across different topics**
> Row 2 and Row 13 and Row 23 all contain `arxiv_paper_1.pdf` — but they are clearly from different papers (different titles, different authors, different DOIs). This is because the PDF numbering resets each run. The `PDF File Name` column is **not a reliable unique identifier** — it is a local filename that is overwritten between runs.

**Observation B — Confirmed incorrect/malformed email present**
> Row 15: `haoran.ren@monash.eduqi.fang` — This is a fused email; two emails (`haoran.ren@monash.edu` and `qi.fang@...`) were merged by the PDF text extractor. `clean_and_validate_email()` has logic to reject TLDs longer than 7 characters, but `eduqi` is 5 characters, so it **passed validation** and was stored permanently in the master list.

**Observation C — Suspiciously generic email confirmed in data**
> Row 47: `line@www.bjd-abd.com` — attributed to "Diabetes Update 2023." This appears to be a navigation-bar email or a placeholder email scraped from a journal abstract page, not a real author email. It is in the master list permanently.

**Observation D — Author name is a non-person entity**
> Row 62–63: `Author Name` = `"Research Portal\nQuality"` — This is a multi-line cell value that broke CSV parsing and is stored as a two-line pseudo-name. The author is clearly not a person; it appears to be a metadata field or institutional label from the article page.

**Observation E — Same author with two different emails in the same CSV**
> Row 49 and Row 50: Author `HeeSook Kim`, DOI `10.4093/jkd.2013.14.4.194` — stored twice, once with `kimhs02041@hotmail.com` and once with `khs0204@dongnam.ac.kr`. This is legitimate data but shows the system has no concept of "one row per author" — it can produce multiple rows per author per paper.

**Observation F — DOI format is inconsistent**
> Rows from arXiv use `http://arxiv.org/abs/...` and `https://doi.org/10...`. Rows from Crossref use raw DOIs like `10.1038/...`. Rows from bioRxiv use `10.1101/...`. This inconsistency is permanent in the stored data.

**Observation G — Source Journal sometimes reflects the fetcher name, not the real journal**
> Row 11–12: Source Journal = `"bioRxiv / medRxiv"` — this is the fetcher's `source_label`, not the actual journal where the preprint was submitted. This makes the column unreliable for identifying the true publishing venue.

### 5.3 Missing Metadata
The master CSV contains **no timestamp** (when the record was collected), **no scraping run ID** (which run produced it), **no topic/keyword** that was used to find it, and **no version identifier** (arXiv papers have version suffixes like `v1`, `v2`). All of these would be needed for any meaningful audit, re-scraping strategy, or provenance tracking.

### 5.4 No Primary Key / Unique Constraint
There is no column or combination of columns that serves as a reliable unique identifier. `Email ID` is the closest thing but cannot serve as a primary key because:
- The same email can legitimately appear in multiple papers (same author).
- The same email can be incorrectly matched to the wrong paper.
- Emails can be malformed (as shown above) and still pass validation.

---

## 6. Publication/PDF/DOI Uniqueness Analysis

### 6.1 What is Currently Captured

| Field | Available in fetcher records? | Stored in CSV? | Reliable? |
|---|---|---|---|
| DOI | Yes (most fetchers) | Yes | Partially — inconsistent format, sometimes arXiv URL, sometimes raw DOI |
| Article Title | Yes (all fetchers) | Yes | Partially — HTML entities, truncation, encoding issues |
| Author List | Yes (metadata) | Yes (one row per author) | Partially — name format varies by fetcher |
| Journal Name | Yes (most fetchers) | Yes (`Source Journal`) | Inconsistent — sometimes fetcher label, sometimes real journal name |
| PDF URL | Not stored in CSV | Not stored | N/A |
| Publication Date | Not captured by any fetcher | Not stored | Not available |
| PDF Filename | Yes (local name) | Yes | Unreliable — resets each run |
| arXiv ID | Partially (used as DOI fallback) | Partially (as DOI field) | Inconsistent |
| PMCID | Used in EuropePMC fetcher for PDF URL construction | Not stored | Not available |
| Version number | Not captured | Not stored | Not available |

### 6.2 DOI Availability and Reliability

- **PLOS:** DOI always present (it IS the article ID in their API).
- **eLife:** DOI usually present; fallback constructed as `10.7554/eLife.{id}` which is reliable.
- **EuropePMC:** DOI present in API response; PMCID also available but not stored.
- **arXiv:** DOI field may contain the actual DOI (from `rel="related"` link) or fall back to the arXiv abstract URL. This means arXiv records have two possible "DOI-like" identifiers.
- **bioRxiv/medRxiv:** DOI is always a `10.1101/...` formatted string — very reliable.
- **Crossref:** DOI is always present and reliable (it IS the primary key of Crossref's database).
- **OpenAlex:** DOI usually present; `"N/A"` string used as fallback (not `None`), which means `"N/A"` becomes a DOI value that looks populated but is meaningless.
- **iMedPub:** DOI extracted via regex from article HTML; may fail to match, returning `"N/A"`.

### 6.3 Duplicate Cases the Current System Cannot Distinguish

1. **Same paper, different DOI representations** — arXiv URL vs. actual publisher DOI for the same paper.
2. **Preprint + Published version of the same paper** — bioRxiv preprint and its final journal version have entirely different DOIs.
3. **Same paper indexed by multiple sources** — OpenAlex, Crossref, and EuropePMC all index many of the same papers. If the same paper is found via three different fetcher runs, the DOI check may catch it OR may not (if DOI formats differ).
4. **Two papers by the same author at the same institution** — appear identical in email and institution domain but have different titles and DOIs.
5. **Corrected/retracted papers** — a paper that was retracted and has a new DOI for the retraction notice could appear as two separate entries.

---

## 7. Edge Cases and Failure Scenarios

| Scenario | Effect |
|---|---|
| PDF is a scanned image (no text layer) | `pdfplumber` returns empty string. Zero emails extracted. Paper DOI not stored. Re-downloaded on every run. |
| PDF is password-protected | `pdfplumber` raises an exception. Swallowed by `except Exception: pass`. Empty result. |
| Paper has no authors in API metadata (metadata_authors is empty list) | `extract_full_names_from_pdf()` provides PDF-guessed names. Name matching unreliable. |
| API returns 429 rate-limit response | Most fetchers treat non-200 as immediate failure, return `[]`. Entire run returns nothing. |
| `master_email_list.csv` is locked by Excel (Windows file lock) | `save_new_emails_to_master()` raises `PermissionError`. Exception is NOT caught in the worker thread → task crashes. Partial data lost. |
| Two browser tabs start extraction simultaneously (single-worker Flask dev server) | Second request immediately gets HTTP 429 with error message. Clean behavior. |
| `master_email_list.csv` exists but has only a header row (0 data rows) | `load_seen_data()` returns empty sets. Works correctly. |
| `master_email_list.csv` has a row with no DOI value (empty cell) | `df["DOI"].dropna()` skips it. Works correctly. |
| `downloads/` folder does not exist at startup | `os.makedirs(..., exist_ok=True)` handles this. Works correctly. |
| User provides `max_papers = 0` | `int("0") = 0`. Fetcher requested for 0 papers. Most fetchers loop 0 times. Returns `[]`. Task completes with "no emails" message. Minor but unexpected behavior. |
| Topic contains special characters (e.g., `/`, `&`, `#`) | `topic_clean = topic.replace(" ", "_").lower()` does not sanitize these. Special chars used in folder names → OS error creating folder on Windows (`/`, `\`, `:` are invalid in Windows filenames). |
| Very long topic string (e.g., 500 characters) | Used directly as folder name → OS path length limit exceeded on Windows (MAX_PATH = 260). `os.makedirs` raises `FileNotFoundError`. |
| arXiv HTML page contains emails from JavaScript libraries (e.g., `dompurify@2.3.5`) | Mitigated by `VALID_EMAIL_RE` requiring alphabetic TLD. Generally handled. |
| eLife paper has no `pdf` key in response | Falls back to `f"https://elifesciences.org/articles/{article_id}.pdf"` — may 404. `requests.get` returns non-200; silently skipped. |
| OpenAlex returns `"N/A"` string as DOI | Stored as DOI value. On next run, `"n/a"` (normalized) is in `seen_dois`. Any other paper that also has `"N/A"` DOI would be incorrectly identified as "already seen." This is a potential **false-positive dedup collision**. |
| `biorxiv_fetcher.py` `source_journal` field uses `server` variable | `server` is the loop variable from `for server in SERVERS`. After the loop, `server` holds the last server tried (whether it succeeded or not). If `biorxiv` succeeded, `server = "biorxiv"` → `"Biorxiv"`. If `medrxiv` succeeded, `server = "medrxiv"` → `"Medrxiv"`. This is actually correct — but only because success breaks out of the inner loop, and `server` holds the correct value at that point. A subtle fragility. |

---

## 8. Scalability and Production Risks

### Thousands of articles
- In-memory deduplication (`seen_emails` set, `seen_dois` set) loaded from CSV on each run becomes slow as the CSV grows. Reading and parsing a 100,000-row CSV into Python sets is feasible but takes seconds.
- The single-threaded PDF processing within one run means 1,000 papers processed serially could take hours.

### Hundreds of thousands of emails / millions of records
- The master CSV becomes extremely slow to append-read. `pandas.read_csv()` on a 500K-row CSV will use significant RAM.
- Excel output (`openpyxl`) for a single run with thousands of rows is already slow; there is a nested `max()` call in the column-width calculation (line 372–374) that is O(rows × cols) — acceptable for small files but a bottleneck at scale.
- The `downloads/Pdf Files/` folder accumulates all PDFs indefinitely. At 1 MB per PDF × 10,000 papers = 10 GB of PDF files.

### Repeated scraping jobs for the same topic
- The DOI-level dedup skips already-seen papers but does NOT skip re-downloading them — wait, actually it does: the DOI check happens before the fetcher is called? No — the fetcher downloads PDFs first, then `app.py` does the DOI check on the returned records. So PDFs are downloaded even for already-seen papers; only processing is skipped. This means re-running the same topic re-downloads all the PDFs it already has, wasting bandwidth and time.
- This is a significant inefficiency at scale.

### File system limits
- Windows NTFS supports ~4 billion files per directory, so file count is not an issue. But the MAX_PATH (260 characters) issue with long topic names is a real production risk (ISSUE in Section 7).

---

## 9. Risk Summary (Prioritized)

| Priority | Issue | Severity | Category |
|---|---|---|---|
| 1 | **ISSUE-03** — Fallback assigns wrong author to email | **Critical** | Data Correctness |
| 2 | **ISSUE-20** — No partial save: crash loses all extracted data | **High** | Data Loss |
| 3 | **ISSUE-10** — DOI format inconsistency breaks deduplication | **High** | Dedup Reliability |
| 4 | **ISSUE-09** — PDFs with no emails are re-downloaded every run | **High** | Performance |
| 5 | **ISSUE-01** — Only first 2 PDF pages scanned | **High** | Data Completeness |
| 6 | **ISSUE-02** — Broken email stitching fails for multi-column PDFs | **High** | Data Quality |
| 7 | **ISSUE-06** — Incomplete domain blocklist admits garbage emails | **High** | Data Quality |
| 8 | **ISSUE-17** — No persistent logging; silent exception swallowing | **High** | Debuggability |
| 9 | **ISSUE-12** — Lock is process-local; breaks under multi-worker WSGI | **High** | Production Reliability |
| 10 | **ISSUE-05** — iMedPub fetcher: brittle regex, no pagination | **High** | Source Reliability |
| 11 | **ISSUE-04** — Fake placeholder PDFs written to disk (arXiv) | **Medium** | Disk / Integrity |
| 12 | **ISSUE-07** — `email_verifier.py` exists but is never used | **Medium** | Data Quality |
| 13 | **ISSUE-08** — PDF name extractor produces too many false positives | **Medium** | Data Quality |
| 14 | **ISSUE-13** — Two fetchers are dead code with incompatible signatures | **Medium** | Maintainability |
| 15 | **ISSUE-14** — No schema enforcement for fetcher return dicts | **Medium** | Maintainability |
| 16 | **ISSUE-15** — PDF files never cleaned up; disk grows indefinitely | **Medium** | Scalability |
| 17 | **ISSUE-18** — No retry on transient API failures | **Medium** | Reliability |
| 18 | **ISSUE-19** — Personal student email hardcoded in API calls | **Medium** | Security/Privacy |
| 19 | **ISSUE-16** — `TASKS` dict is an unbounded in-memory store | **Low** | Scalability |
| 20 | **ISSUE-11** — `+tag` emails not de-aliased | **Low** | Data Quality (minor) |

### Confirmed data-quality issues already in `master_email_list.csv`
- `haoran.ren@monash.eduqi.fang` — fused malformed email that passed validation (Row 15)
- `line@www.bjd-abd.com` — likely a navigation/placeholder email, not a person (Row 47)
- `Author Name = "Research Portal\nQuality"` — non-person entity stored as author (Rows 62–63)
- `PDF File Name` is not a unique identifier — same filename maps to completely different papers across runs

---

## 10. Questions That Need to Be Answered Before Designing a Solution

1. **What is the primary use case for the master email list?** Is it used for outreach (sending emails), research, or just collection? This determines how important Author Name correctness is vs. just Email ID correctness.

2. **Is "one email per author per paper" or "one email per author (globally)" the correct record model?** Currently a mix of both creates confusion.

3. **Should the system track papers it has already ATTEMPTED (even if no emails were found), to avoid re-downloading them?** This requires storing "seen but empty" DOIs separately.

4. **Is DOI the intended primary key for publication deduplication, or should something else (title hash, PMCID, arXiv ID) serve that role?**

5. **How should the arXiv dual-identifier problem be handled?** arXiv papers have an arXiv ID AND sometimes a publisher DOI. Should both be stored and cross-referenced?

6. **Should preprint and published versions of the same paper be treated as the same record or different records?**

7. **Is the existing `master_email_list.csv` considered clean/trusted, or should it be treated as potentially dirty data that needs re-validation?**

8. **Is deliverability verification (the `email_verifier.py` file) intended to be part of the extraction pipeline, or is it a standalone tool?** If it should be part of the pipeline, at what point should it run?

9. **What is the expected scale?** Will this process hundreds of papers per month or tens of thousands? The answer significantly affects which architectural patterns are appropriate.

10. **Should the system support resuming a failed extraction run from where it crashed?** This requires persistent state tracking per run.

11. **Should PDFs be retained long-term, or are they only needed transiently for email extraction?** If transient, a cleanup policy is needed.

12. **When the same email appears in multiple papers, should ALL paper associations be recorded, or only the first?** The current system records only the first occurrence and silently discards subsequent ones.

13. **What should happen when an author has multiple email addresses across papers?** Should they be linked (treated as the same person) or treated as independent records?

14. **Is it acceptable for the `Source Journal` field to contain the fetcher's label (e.g., "bioRxiv / medRxiv") rather than the actual journal name?** Many rows currently use the fetcher label, not the real journal.

15. **Under what conditions should an extraction result be considered a "failure"?** Currently any non-200 API response results in a complete empty run with no user-facing error detail beyond a generic message.
