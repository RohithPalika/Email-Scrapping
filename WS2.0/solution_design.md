# Solution Design: 10 High-Priority Issues
## Academic Email Extraction Project
### Research · Design · Implementation Plan

> **This is a design document only. No code has been modified.**
> Proceed to implementation only after reviewing and approving this plan.

---

## A. Executive Summary

The project is a working Flask web application that extracts academic author emails from research PDFs. It functions correctly in its happy path but carries ten high-severity defects that affect data correctness, data completeness, deduplication reliability, crash safety, and production reliability.

**The recommended direction is surgical, not architectural.** The pipeline design (fetchers → app.py worker → master CSV) is sound and should be preserved. Every one of the ten issues can be resolved with targeted, isolated changes. No full rewrite is needed.

**Three issues form a high-dependency cluster that must be addressed together:**
- ISSUE-03 (wrong author fallback) + ISSUE-20 (no partial save) + ISSUE-17 (no logging) — because fixing the fallback can surface previously hidden crashes, the partial save must exist to protect data when that happens, and logging must be in place to diagnose what went wrong.

**Two issues form a second dependency cluster:**
- ISSUE-09 (re-downloads of empty papers) + ISSUE-10 (DOI normalisation) — because ISSUE-10's fix is a prerequisite for ISSUE-09's dedup to work correctly.

The remaining five issues (ISSUE-01, 02, 06, 12, 05) are largely independent and lower risk to implement.

**The existing `master_email_list.csv` must be preserved as-is.** Known dirty records (fused email, placeholder email, non-person author) will not be retroactively cleaned at implementation time — a separate one-time cleanup pass should be a manual decision after the new validation logic is in place.

---

## B. Current Architecture Impact

The ten issues touch four distinct layers of the application:

```
┌─────────────────────────────────────────────────────────────┐
│  Layer                  │  Issues                           │
│─────────────────────────│───────────────────────────────────│
│  Fetcher scripts        │  ISSUE-05 (iMedPub)               │
│  app.py — PDF parsing   │  ISSUE-01 (pages), ISSUE-02       │
│                         │  (stitching), ISSUE-03 (fallback) │
│  app.py — email filter  │  ISSUE-06 (blocklist)             │
│  app.py — deduplication │  ISSUE-09 (empty PDFs),           │
│                         │  ISSUE-10 (DOI normalisation)     │
│  app.py — task worker   │  ISSUE-17 (logging),              │
│                         │  ISSUE-20 (partial save)          │
│  app.py — concurrency   │  ISSUE-12 (process-local lock)    │
└─────────────────────────────────────────────────────────────┘
```

Components that must remain **untouched**:
- All source fetchers except `imedpub_fetcher.py`
- Flask routes (`/`, `/start-extraction`, `/status/<id>`, `/result/<id>`, `/download/<filename>`)
- The background-task mechanism (UUID, TASKS dict, polling)
- Excel output logic
- `load_seen_data()` internal structure (only its normalisation helper changes)
- `master_email_list.csv` schema (no column additions during this phase)

---

## C. Issue-by-Issue Analysis

---

### ISSUE-03 — Fallback Assigns Wrong Author to Email

**Current Problem**

In `extract_author_email_pairs()` (`app.py` lines 227–229), when no author name can be matched to an email address by username similarity or initials, the code falls back to assigning the email to `metadata_authors[0]` — the first author in the metadata list, who is statistically the first (not corresponding) author. This silently creates permanently incorrect Author↔Email pairs in the master CSV.

**Root Cause**

The fallback was added to avoid discarding valid emails that use non-name usernames (e.g., `corresponding@lab.edu`). The intent was good; the implementation is wrong. `metadata_authors[0]` is ordered by the API's author listing, not by "most likely to be the corresponding author."

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Remove the fallback entirely** — discard unmatched emails | Simple; clean data; no wrong assignments | Loses real corresponding-author emails that have generic usernames | Medium — real data loss for generic-username emails | High — existing logic unchanged except removal |
| **B: Store unmatched emails with `Author Name = "Unknown"`** | Preserves the email; honest about uncertainty; filterable | "Unknown" rows lower data quality appearance; user must post-process | Low — no wrong data, just incomplete | High — only the fallback label changes |
| **C: Try to match against last author** — last author is often the PI/corresponding author | Slightly better heuristic than first author | Still wrong more often than not; adds false confidence | Medium — can produce wrong matches | Medium — logic change needed |
| **D: Use a keyword heuristic before falling back** — check if the word "corresponding" or "∗" appears near the email in the PDF text, and only fall back if such a marker is found | Much more accurate fallback; uses real journal conventions | Requires text-window analysis around the email; more complex | Low if implemented carefully | Medium — requires changes to PDF parsing context |

**Recommended Approach: B — Store unmatched emails with `Author Name = "Unknown"`**

Approach A loses real data. Approach C just moves the wrong guess from position 0 to the last position — the problem is the guess itself. Approach D is the most accurate but adds meaningful complexity. Approach B is the safest choice: it retains the email (valuable), is honest about the uncertainty (preserves data integrity), and leaves a clearly filterable marker. The user can later review "Unknown" rows and fill in manually or with a post-processing step.

**Implementation Plan**

In `extract_author_email_pairs()` (`app.py`), replace lines 227–229:
```python
# BEFORE (wrong):
if not matched_author and metadata_authors:
    matched_author = metadata_authors[0]
    print(f"    [Match fallback] ...")

# AFTER (correct):
if not matched_author:
    matched_author = "Unknown"
    # Log this for review — do not silently corrupt data
```

The `_run_extraction_task` HTML-path fallback at line 296 (`matched_author = item["authors"][0] if item["authors"] else "Unknown"`) has the same problem and must be changed to `"Unknown"` as well.

**Affected Files/Functions**
- `app.py` — `extract_author_email_pairs()` (lines 227–229)
- `app.py` — `_run_extraction_task()` (line 296, HTML email path)

**Regression Risks**
- Excel output rows that previously showed a first-author name will now show "Unknown" — this is the desired correction, not a regression.
- No fetcher code changes; no CSV schema changes; no route changes.
- Existing records in master CSV with wrong author names are NOT retroactively changed (backward compatible).

**Testing Requirements**
- Unit test: feed `extract_author_email_pairs()` a PDF with generic email (`info@lab.edu`), no matching authors — verify result is `("Unknown", "info@lab.edu")` not `(first_author, "info@lab.edu")`.
- Integration test: run extraction for a topic on arXiv; verify no row in output has `Author Name = first_metadata_author` for emails that clearly don't match the name.
- Regression: existing matched emails (e.g., `torressalinas@gmail.com` → `Daniel Torres-Salinas`) must still match correctly.

**Rollback Considerations**
- Minimal risk. A one-line code revert restores the original behaviour. No CSV changes are made by this fix.

---

### ISSUE-20 — No Partial Save: Crash Loses All Extracted Data

**Current Problem**

In `_run_extraction_task()` (`app.py` lines 276–361), `save_new_emails_to_master(rows)` is only called once after the entire paper loop completes. If an exception is raised mid-loop (e.g., `PermissionError` when the CSV is open in Excel, or a network error on paper 8 of 10), all rows extracted so far are lost. The `finally` block only releases the lock, it does not save.

**Root Cause**

The save-once design was likely chosen to avoid partial writes, but the tradeoff (all-or-nothing) means any crash causes total data loss for that run.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Save after every N papers** — append rows in batches | Partial data saved incrementally; crash loses at most N papers | Requires careful handling of `seen_emails` / `seen_dois` sync with what was already saved | Medium | High — only worker loop changes |
| **B: Save immediately after each email row is extracted** — one CSV append per row | Maximum protection; crash loses at most 1 row | Many small writes; CSV append overhead per row; `seen_emails` already updated in-memory so dedup is fine | Low | High |
| **C: Save in `finally` block with whatever rows were accumulated** — move `save_new_emails_to_master(rows)` to `finally` | Almost no code change; saves whatever was collected before crash | If `rows` is empty due to crash at row-building stage, nothing is saved; does NOT help if crash is inside the accumulation | Low | High — one-line move |
| **D: Write to a temporary staging CSV first, then atomically rename** | Atomic from filesystem perspective; protects master CSV | More complex; Windows `os.replace()` works atomically; adds staging file management | Medium | Medium |

**Recommended Approach: C (move save to `finally`) + A (batch flush every 5 papers)**

Approach C alone is the minimum-viable fix and can be done safely in 2 lines. Combining it with Approach A (flush every 5 papers) covers the common crash scenario (mid-loop exception) with minimal complexity. There is no need for Approach D's atomic rename because `pandas` append-write to CSV already works at the OS level without corruption risk on Windows for sequential writes.

Specifically:
1. Move `save_new_emails_to_master(rows)` into the `finally` block so it always fires.
2. Additionally call `save_new_emails_to_master(new_batch)` and clear `new_batch` every 5 papers inside the loop, so a crash mid-run saves progress.
3. The `seen_emails` and `seen_dois` in-memory sets are already updated per-paper in the existing loop, so the dedup state stays consistent with what has been appended.

**Affected Files/Functions**
- `app.py` — `_run_extraction_task()` (lines 276–395, specifically the loop and the `finally` block)
- `app.py` — `save_new_emails_to_master()` (called more frequently, but function itself unchanged)

**Regression Risks**
- `save_new_emails_to_master` already handles the "file is empty" / header logic correctly — multiple calls are safe.
- If the batch save is called and then the run succeeds, the final `finally` save will try to save an already-empty `rows` list — the function handles empty lists with an early return (`if not new_rows: return`) so this is safe.
- No fetcher changes; no route changes; no schema changes.

**Testing Requirements**
- Failure injection test: mock `pdfplumber.open()` to raise on paper 5 of 10. Verify master CSV contains rows from papers 1–4 (or whichever batch completed).
- Success path regression: run full 10-paper extraction; verify result is identical to pre-fix output (all rows appear exactly once).
- CSV lock test (Windows): open master CSV in Excel, start an extraction, verify a useful error is logged and partial results are attempted to be saved before task marks as "error."

**Rollback Considerations**
- Rollback is a simple revert of the `finally` block and batch flush. The extra rows already written to master CSV in a partial save are valid data and do not need to be removed.

---

### ISSUE-10 — DOI Format Inconsistency Breaks Deduplication

**Current Problem**

The `load_seen_data()` function normalises DOIs by stripping `https://doi.org/` and lowercasing. However, DOIs stored in the CSV can be:
- `https://doi.org/10.1038/...` (OpenAlex, eLife)
- `http://arxiv.org/abs/1234.5678v2` (arXiv fallback)
- `10.1101/...` (bioRxiv)
- `10.1038/...` (Crossref)
- `N/A` (iMedPub, OpenAlex fallback)

The normalisation function only strips the `https://doi.org/` prefix. It does not handle `http://arxiv.org/abs/` or any other non-standard format. As a result, arXiv papers re-fetched on the next run will not match seen DOIs, causing re-download and re-processing.

**Root Cause**

There is no single DOI normalisation function used consistently at both storage time and retrieval time.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Create a `normalise_doi()` helper used at both save and load time** | Single source of truth; fixes the root cause permanently | Must apply normalisation to the item DOI at storage time without changing the raw value stored in CSV | Low | High |
| **B: Normalise only at load time, adding more prefix patterns** | No change to stored CSV data | Stored data stays inconsistent; future load logic must grow with each new format | Medium (pattern drift) | High |
| **C: Normalise at both save AND load time (store the clean DOI)** | Master CSV gets clean DOI values going forward; best long-term | Old rows in CSV have inconsistent DOIs; new dedup check won't find them | Medium (migration gap for old rows) | Medium |

**Recommended Approach: A — Shared `normalise_doi()` helper at BOTH load and save time**

Create a `normalise_doi(raw)` helper function in `app.py` that:
1. Lowercases the string.
2. Strips `https://doi.org/` and `http://doi.org/` prefixes.
3. Strips `https://arxiv.org/abs/` and `http://arxiv.org/abs/` prefixes, converting them to `arxiv:{id}` for a stable canonical key.
4. Returns `""` (empty string) for `"N/A"`, `"n/a"`, or blank values, so they never enter the seen_dois set.

This helper is then called:
- In `load_seen_data()` when building `seen_dois` (replacing the current inline `.str.replace()`).
- In `_run_extraction_task()` when computing `item_doi` for the duplicate check.
- The raw DOI (not the normalised key) is still stored in the CSV rows — so the stored data looks the same as before, preserving backward compatibility.

The normalisation only affects the **in-memory dedup key**, not what is written to CSV.

**Affected Files/Functions**
- `app.py` — new `normalise_doi()` helper function (to be added near `load_seen_data`)
- `app.py` — `load_seen_data()` (replace inline normalisation)
- `app.py` — `_run_extraction_task()` (line 280, replace `item_doi` calculation)

**Regression Risks**
- Existing CSV rows are not modified — backward compatible.
- The `"N/A"` → `""` change means papers with no DOI are no longer incorrectly added to `seen_dois` — this is the correct behaviour and prevents false-positive dedup collisions.
- arXiv papers that were previously stored with `http://arxiv.org/abs/1234v2` as their DOI will now normalise to `arxiv:1234v2` in the key — these will NOT match any previously seen_dois entries built from the old format. This means a one-time re-download may happen for arXiv papers already in master CSV. This is acceptable; the old records are not deleted, and the email dedup will still prevent duplicate email entries.

**Testing Requirements**
- Unit test `normalise_doi()` for each of the 6 DOI formats found in the real CSV.
- Verify `"N/A"` and `""` both return `""` (not added to seen_dois).
- Integration test: after running an arXiv extraction, run the same topic again — verify same papers are NOT re-processed (DOI dedup fires correctly).
- Verify `https://doi.org/10.1038/...` normalises to `10.1038/...` (same as Crossref format).

**Rollback Considerations**
- The helper function is additive. Removing it and reverting the two call sites restores original behaviour. No CSV data changes.

---

### ISSUE-09 — PDFs with No Emails Are Re-Downloaded Every Run

**Current Problem**

When a paper's PDF is downloaded but yields zero valid emails (scanned PDF, image-only, no email on first 2 pages), its DOI is never added to `seen_dois` in the master CSV. On the next run for the same topic, the fetcher re-downloads the same PDF, and the same zero-email outcome repeats. This wastes bandwidth and time.

**Root Cause**

The DOI is only added to `seen_dois` (via a saved row) if at least one email is extracted. Papers that are "attempted but empty" leave no trace in the master CSV.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Add a separate `seen_dois_attempted.txt` file** — one DOI per line, written after every PDF attempt | Zero changes to master CSV schema; simple append-only file; fast lookup with a set | Another file to manage; does not survive manual deletion | Low | High |
| **B: Add a `"EMPTY"` sentinel row to master CSV for papers with no emails** | Uses existing infrastructure; single file | Pollutes the master CSV with non-email rows; complicates downstream processing | Medium | Medium |
| **C: Add a `Scrape Status` column to master CSV** | Rich auditing; knows WHY a paper was skipped | Schema change — breaks backward compatibility with existing 78 rows | High | Low |
| **D: Store attempted DOIs in a JSON file** | Structured; extensible | Slightly more complex than a flat text file; no real advantage here | Low | High |

**Recommended Approach: A — Separate `seen_dois_attempted.txt` file**

This is the simplest, lowest-risk approach. A plain text file with one normalised DOI per line is appended to after every paper attempt (whether emails were found or not). At the start of each run, this file is loaded into `seen_dois` alongside the master CSV DOIs.

**Dependency**: This fix MUST be implemented after ISSUE-10 (DOI normalisation), because the normalised DOI must be used when writing to and reading from this file. Otherwise the same inconsistency problem recurs.

**Implementation Plan**

1. Define `ATTEMPTED_DOIS_FILE = os.path.join(DOWNLOADS_DIR, "attempted_dois.txt")`.
2. In `load_seen_data()`, after loading from master CSV, also read this file and add each line to `seen_dois`.
3. In `_run_extraction_task()`, after processing each paper (whether emails found or not), append `normalise_doi(item_doi)` to `attempted_dois.txt` if it is non-empty.
4. The `finally` block should NOT clear this file — it is a permanent running log.

**Affected Files/Functions**
- `app.py` — `load_seen_data()` (add read of attempted_dois.txt)
- `app.py` — `_run_extraction_task()` (append to attempted_dois.txt after each paper)
- `app.py` — module level (new constant `ATTEMPTED_DOIS_FILE`)

**Regression Risks**
- If `attempted_dois.txt` does not exist on first run, `load_seen_data()` must handle the missing file gracefully (same as it handles missing master CSV — just return empty set). This is a one-line check.
- Papers already in master CSV with valid DOIs: their normalised DOIs are already in `seen_dois` from the CSV, so they will still be correctly skipped even without being in `attempted_dois.txt`.
- The file can grow to tens of thousands of lines over time — reading it at startup is O(n) but a 10,000-line text file is ~300 KB and loads in milliseconds.

**Testing Requirements**
- Test: run extraction that yields zero emails for all papers; restart app; run same topic again — verify fetcher is NOT called for those papers (task completes immediately with "all already attempted").
- Test: run successful extraction; verify attempted_dois.txt contains all processed DOIs (both email-bearing and empty).
- Edge case: `attempted_dois.txt` missing — verify app starts normally with empty set.

**Rollback Considerations**
- Delete `attempted_dois.txt`. Remove the two code additions. The system returns to its original behaviour of re-downloading empty papers.

---

### ISSUE-01 — Only First 2 PDF Pages Are Scanned

**Current Problem**

In `extract_author_email_pairs()` (`app.py` line 188), `pdf.pages[:min(2, len(pdf.pages))]` limits parsing to the first 2 pages. Corresponding author emails frequently appear on the last page in Acknowledgements, Author Contributions, or Conflict of Interest sections, especially in PLOS, eLife, and Frontiers journals.

**Root Cause**

A conservative limit was set, likely to avoid long parse times on large PDFs.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Scan first 3 pages + last 2 pages** | Catches both front-matter and back-matter emails without scanning entire PDF | Slightly longer parse; pages may overlap for short PDFs | Low | High |
| **B: Scan all pages** | Maximum email coverage | Very slow for 50+ page PDFs; memory spike for large files | Medium | High |
| **C: Scan first 3 pages + last page only** | Good balance; most journals put corresponding info on last page | May miss page N-1 in some formats | Low | High |
| **D: Scan until emails are found, then stop** | Dynamic; fast for papers where emails are on page 1 | Complex logic; may stop too early if one email is found but more exist | Medium | Medium |

**Recommended Approach: A — First 3 pages + last 2 pages**

This is the optimal balance. Academic journals consistently put author contact info at two locations: the front matter (pages 1–3) and the back matter (last 1–2 pages). Scanning 5 pages maximum is fast (under 2 seconds for typical PDFs) and covers the vast majority of real-world layouts.

**Implementation Plan**

In `extract_author_email_pairs()`, replace the page selection logic:
```python
# BEFORE:
for page in pdf.pages[:min(2, len(pdf.pages))]:

# AFTER:
pages_to_scan = pdf.pages[:min(3, len(pdf.pages))]
if len(pdf.pages) > 3:
    # Append last 2 pages (may overlap with first 3 for short PDFs — dedup by index)
    last_pages = pdf.pages[max(3, len(pdf.pages)-2):]
    pages_to_scan = list(pages_to_scan) + list(last_pages)
for page in pages_to_scan:
```

**Affected Files/Functions**
- `app.py` — `extract_author_email_pairs()` (line 188, page selection slice)

**Regression Risks**
- Parsing additional pages increases extraction time per paper. For a 10-paper run, overhead is minimal (2–5 seconds total).
- The `unwrap_broken_emails()` and email regex run on concatenated text from all scanned pages — more text means more processing but the logic is unchanged.
- Risk of extracting MORE emails from back-matter (references, footnotes). The `clean_and_validate_email()` domain blocklist becomes more important here. ISSUE-06 (blocklist expansion) should be fixed together or immediately after.

**Testing Requirements**
- Test with a real PLOS paper where the corresponding author email is on the last page — verify it is now found.
- Regression: verify papers where emails ARE on page 1 still work identically.
- Performance: measure wall-clock time for a 10-paper extraction before and after — document the delta.

**Rollback Considerations**
- Trivial: revert the 5-line change. No data changes.

---

### ISSUE-02 — Broken Email Stitching Fails for Multi-Column PDFs

**Current Problem**

`unwrap_broken_emails()` (`app.py` lines 156–163) uses 7 regex patterns to reassemble emails fragmented by PDF line-wrapping. Multi-column PDF text extracted by `pdfplumber` suffers from column interleaving — text from both columns appears on the same logical "line" in extracted output — which these regex patterns were not designed to handle.

**Root Cause**

`pdfplumber`'s default `extract_text()` joins columns horizontally in reading order, which causes column 1's end-of-line text to be immediately followed by column 2's start-of-line text. This produces fragments that look like: `john.doe@uni|Some Author From Column2|.edu`.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Use `extract_text(layout=True)` instead of default** — pdfplumber has a layout-aware mode that attempts to preserve 2-column structure | Directly addresses multi-column interleaving; pdfplumber built-in | `layout=True` is slower and sometimes produces unexpected whitespace; existing code already uses `layout=False` explicitly | Low-Medium | High |
| **B: Add more regex patterns to `unwrap_broken_emails()`** | No dependency change; incremental improvement | Whack-a-mole approach; will never fully handle all interleaving variants | Low | High |
| **C: Use `pdfplumber`'s word-level extraction and reconstruct emails from word tokens** | Most accurate reconstruction; works on any column layout | Significantly more complex; requires custom token assembly logic | High | Medium |
| **D: Extract text from each column independently using bounding-box extraction** | Best-in-class accuracy for multi-column; `pdfplumber` supports bbox cropping | Requires knowing column boundaries; varies by journal; complex calibration | High | Low |
| **E: Switch to `pdfminer.six` for extraction** — pdfminer has better multi-column heuristics | Better baseline for multi-column PDFs | Adds a dependency; different API; significant refactor | Medium | Low |

**Recommended Approach: A (layout=True) + B (add 3–4 targeted regex patterns)**

`extract_text(layout=True)` is the best single change with the highest payoff for the least risk. It is already used on line 189 (`extract_text(layout=False)`) — the `False` was presumably chosen for performance. Changing it to `True` directly uses pdfplumber's built-in column-layout heuristic.

Combining this with adding 3–4 additional regex patterns in `unwrap_broken_emails()` handles cases that layout=True still misses (hyphenated line breaks, `@` split across lines). These two changes together address the majority of real-world failures without adding any new dependencies.

**Implementation Plan**

1. Change `page.extract_text(layout=False)` to `page.extract_text(layout=True)` at line 189.
2. Add the following patterns to `unwrap_broken_emails()`:
   - Re-stitch domain split at TLD: `re.sub(r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+)\s+([a-zA-Z]{2,})\b', r'\1.\2', text)` — catches `john@uni .edu` type splits.
   - Collapse whitespace inside email: `re.sub(r'([a-zA-Z0-9._%+-]+)\s*@\s*([a-zA-Z0-9.-]+)\s*\.\s*([a-zA-Z]{2,})', r'\1@\2.\3', text)` (this pattern already exists but can be extended for multi-space variants).

**Affected Files/Functions**
- `app.py` — `extract_author_email_pairs()` line 189 (`layout=False` → `layout=True`)
- `app.py` — `unwrap_broken_emails()` lines 156–163 (add patterns)

**Regression Risks**
- `layout=True` is more CPU-intensive. For small PDFs (2–3 pages) this is negligible. For very large PDFs the performance difference may be a few seconds per page. Acceptable given ISSUE-01 already expands to 5 pages maximum.
- Existing email extraction for single-column PDFs (arXiv) should be unaffected or improved by `layout=True`.
- New regex patterns must be carefully ordered to avoid false replacements. Test each pattern independently on known-good text.

**Testing Requirements**
- Test with a real PLOS or eLife PDF (2-column) — verify emails previously missed are now found.
- Test with an arXiv single-column PDF — verify results are identical to before.
- Regex unit tests for each new pattern in `unwrap_broken_emails()` with known input→expected output.
- Test with a PDF that has a hyphen-wrapped email domain (`john.doe@university`↵`ofexample.edu`).

**Rollback Considerations**
- Revert `layout=True` to `layout=False` and remove the added regex patterns. No data changes.

---

### ISSUE-06 — Incomplete Domain Blocklist Admits Garbage Emails

**Current Problem**

`clean_and_validate_email()` (`app.py` lines 147–153) has a hardcoded list of 8 domains to reject. The confirmed real-world failures in master CSV (`line@www.bjd-abd.com`, `haoran.ren@monash.eduqi.fang`) show the blocklist is insufficient both in coverage (missing source-site domains) and in pattern detection (fused emails slipping through).

**Root Cause**

Two distinct problems share this issue:
1. Domain blocklist is too short and not updated when new sources are added.
2. Fused-email detection relies only on TLD length (>7 chars) which misses shorter fused TLDs like `eduqi` (5 chars).

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Expand the hardcoded blocklist** | Simple; no new dependencies | Still requires manual updates every time a source is added; stays incomplete | Low | High |
| **B: Pattern-based heuristics for fused emails** — detect `text.text@domain` immediately followed by another capitalised segment | Catches the `monash.eduqi.fang` class of fusion | Heuristics can have false positives on legitimate hyphenated domains | Low-Medium | High |
| **C: Load blocklist from an external config file** | Updatable without code change; maintainable | Adds a file dependency; startup failure if file missing | Low | High |
| **D: Use a TLD whitelist instead of domain blocklist** — only accept emails whose TLD is in a known-valid TLD list | Most rigorous; future-proof | TLD list needs maintenance; very long list; some valid uncommon TLDs may be rejected | Medium | High |
| **E: Detect consecutive dot-separated segments after the TLD as a fusion indicator** | Targeted fix for `monash.eduqi.fang` pattern | Requires analysing domain structure more deeply | Low | High |

**Recommended Approach: A + B + E**

Three complementary lightweight fixes:
1. **Expand the blocklist** to cover all currently known source-site domains: add `biorxiv.org`, `medrxiv.org`, `crossref.org`, `orcid.org`, `doi.org`, `pubmed.ncbi.nlm.nih.gov`, `frontiersin.org`, `mdpi.com`, `arxiv.org`, `ssrn.com`.
2. **Improve fused-email detection (Approach E)**: after splitting on `@`, check if the domain portion contains more than one sequence of consecutive letters-only segments after the last valid TLD position. For example, `monash.eduqi.fang` → after splitting `eduqi.fang`, the TLD candidate `fang` is 4 chars (valid), but `eduqi` is not a real SLD pattern. A more robust check: reject if the domain matches `r'[a-z]+\.[a-z]{2,6}[a-z]{2,}` — i.e., characters directly appended to a normal TLD without a dot separator.
3. **Add a "common navigation/placeholder" pattern detector**: reject emails where the local-part is a generic word: `info`, `admin`, `support`, `contact`, `editor`, `editorial`, `webmaster`, `noreply`, `no-reply`, `help`, `line`, `mail`, `office`, `submit`, `submission`.

**Implementation Plan**

In `clean_and_validate_email()`:
1. Extend the `ignored` list with the new domains.
2. After the TLD length check (line 137), add: detect fused domain by checking if the domain after the TLD boundary contains additional alphabetic segments.
3. Add a `GENERIC_LOCAL_PARTS` set and check `username in GENERIC_LOCAL_PARTS`.

**Affected Files/Functions**
- `app.py` — `clean_and_validate_email()` (lines 147–154)

**Regression Risks**
- Adding `mdpi.com` and `frontiersin.org` to the blocklist means authors who used `name@mdpi.com` or `name@frontiersin.org` will be filtered. However, these are platform/internal emails, not personal academic emails — this is the correct behaviour.
- The generic local-part check could theoretically reject a legitimate email like `admin@mylab.edu`. This is acceptable: a person named "Admin" is unlikely, and the practical risk is negligible.
- Existing dirty records in master CSV (e.g., `line@www.bjd-abd.com`) are NOT retroactively removed by this fix. A manual cleanup pass of the existing CSV is needed separately.

**Testing Requirements**
- Unit test each new blocked domain — verify the email is rejected.
- Unit test `line@www.bjd-abd.com` — should now be rejected (generic local-part `line`).
- Unit test `haoran.ren@monash.eduqi.fang` — should now be rejected (fused domain detection).
- Regression: test `torressalinas@gmail.com`, `loet@leydesdorff.net` — should still pass.
- Test that `admin@stanford.edu` is rejected (generic local-part) but `john.admin@stanford.edu` is not.

**Rollback Considerations**
- Revert the extended blocklist and new checks in `clean_and_validate_email()`. No CSV changes.

---

### ISSUE-17 — No Persistent Logging; Silent Exception Swallowing

**Current Problem**

All diagnostic output uses `print()`. Exceptions in most fetchers are caught with `except Exception: continue` — swallowed entirely. If the app runs as a background service and a run fails, there is no log file to inspect. The existing `print()` calls are also not structured, making automated parsing impossible.

**Root Cause**

No logging framework was configured. `print()` was used as a quick substitute.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Replace `print()` with Python's stdlib `logging` module** | No new dependencies; structured levels (DEBUG/INFO/WARNING/ERROR); file handler configurable; works in all Python versions | Requires replacing every `print()` call (approximately 40–50 across all files) | Low | High |
| **B: Use a third-party logging library (loguru, structlog)** | More features (JSON output, automatic traceback capture, etc.) | Adds a dependency; `pip install loguru`; more change than needed for this project | Low | Medium |
| **C: Add a `RotatingFileHandler` to the existing print-based output** — redirect stdout to a file | Zero code changes to print statements; just add a handler | stdout is shared between Flask's request log and extraction log; messy; no log levels | Low | High (but poor quality) |

**Recommended Approach: A — stdlib `logging` module with a file handler**

Python's `logging` module is the right tool. No new dependencies. The key changes are:

1. **`app.py`**: At module level, configure a logger:
   ```python
   import logging
   logging.basicConfig(
       level=logging.INFO,
       format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
       handlers=[
           logging.FileHandler("extraction.log"),
           logging.StreamHandler()  # keep console output
       ]
   )
   log = logging.getLogger("extraction")
   ```
2. Replace all `print(f"...")` calls in `app.py` with `log.info(...)`, `log.warning(...)`, or `log.error(...)`.
3. Change `except Exception: continue` patterns in fetchers to `except Exception as e: log.warning(f"...: {e}")`.
4. In `_run_extraction_task`'s except block, use `log.error(...); log.debug(traceback.format_exc())` to capture full stack traces to the log file.

**Note on fetchers**: Rather than import the root logger into each fetcher (which would create circular concerns), give each fetcher its own named child logger: `log = logging.getLogger("extraction.arxiv")` etc.

**Dependency on ISSUE-20**: Once ISSUE-20 is fixed (partial save in finally), the log messages around save operations become especially important for diagnosing crash-recovery scenarios. These two fixes reinforce each other.

**Affected Files/Functions**
- `app.py` — module level (logger config) + all `print()` calls
- All fetcher files — `print()` → `log.warning/info/error` (approximately 5–8 calls per file)
- New file: `extraction.log` (auto-created by `FileHandler`)

**Regression Risks**
- The logging framework itself has no functional impact on extraction behaviour.
- Console output format changes (timestamps added) — this is purely cosmetic.
- If `extraction.log` cannot be created (permissions issue), `FileHandler` raises during startup — should be wrapped in a try/except with fallback to StreamHandler only.

**Testing Requirements**
- Run a successful extraction — verify `extraction.log` contains INFO-level progress messages.
- Inject a failure (invalid topic) — verify ERROR-level message appears in log with traceback.
- Verify the log file uses rotation or is bounded (consider `RotatingFileHandler` with `maxBytes=5MB, backupCount=3`).

**Rollback Considerations**
- Removing the logging configuration and replacing `log.xxx()` calls with `print()` is straightforward. The `extraction.log` file is additive and can be deleted.

---

### ISSUE-12 — Lock Is Process-Local; Breaks Under Multi-Worker WSGI

**Current Problem**

`ACTIVE_EXTRACTION = threading.Lock()` is a Python threading lock — it only prevents concurrent extractions within a single OS process. If Flask is deployed with a multi-worker WSGI server (e.g., `gunicorn --workers 2`), two separate Python processes each have their own `ACTIVE_EXTRACTION`, and both can start an extraction simultaneously, causing concurrent writes to `master_email_list.csv`.

**Root Cause**

Threading locks are not shared across OS process boundaries. This is a fundamental property of Python's `threading.Lock`.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Use a filesystem-based lock file** — write a `extraction.lock` file; check for its existence before starting | Works across processes and across machines; zero new dependencies; survives server restarts (with stale-lock detection) | Must handle stale lock cleanup if process crashes without releasing | Low | High |
| **B: Use `filelock` or `fasteners` library** — cross-process file locking with proper advisory lock semantics | More robust than manual lock file; handles stale locks automatically | Adds a dependency | Low | High |
| **C: Use Redis-based distributed lock** | Production-grade; survives restarts; TTL-based expiry | Requires Redis server; massive overengineering for a single-machine app | Medium | Low |
| **D: Document that the app must be deployed with single worker only** | Zero code changes | Does not fix the problem; hides a real risk | None (non-fix) | — |
| **E: Use `multiprocessing.Lock` shared via Manager** | Cross-process in a single machine; no file system dependency | Requires a `multiprocessing.Manager` process running; complex startup | High | Low |

**Recommended Approach: A — Filesystem lock file with stale-lock detection**

This is the lowest-dependency, most portable solution. A lock file at `downloads/extraction.lock` is created when an extraction starts and deleted in the `finally` block. Before starting, check if the file exists AND the PID stored inside it is still a running process (to handle stale locks from crashes).

Implementation:
1. Replace `ACTIVE_EXTRACTION = threading.Lock()` with a filesystem lock helper.
2. `acquire_extraction_lock()` → write PID to `extraction.lock`; return True. If file exists and PID is alive, return False. If file exists and PID is dead (stale), delete and re-acquire.
3. `release_extraction_lock()` → delete `extraction.lock`.
4. Call these in `start_extraction()` and the `finally` block of `_run_extraction_task()`.

**Note**: The existing threading lock should be KEPT as well for intra-process protection (prevents two simultaneous requests within the same worker). The filesystem lock adds the cross-process layer. Both locks together provide complete protection.

**Affected Files/Functions**
- `app.py` — `ACTIVE_EXTRACTION` (lines 45, 421–424, 394) → replaced by filesystem lock functions
- `app.py` — `_run_extraction_task()` `finally` block → call `release_extraction_lock()`
- `app.py` — `start_extraction()` route → call `acquire_extraction_lock()` instead of `.acquire(blocking=False)`
- New constant: `LOCK_FILE = os.path.join(DOWNLOADS_DIR, "extraction.lock")`

**Regression Risks**
- If the app is ALWAYS run with `python app.py` (single process, current usage), the filesystem lock adds negligible overhead (two file I/O operations per extraction).
- If `DOWNLOADS_DIR` is read-only (permissions issue), lock file creation fails. Must be caught and handled gracefully.
- Stale lock detection using PID check (`os.kill(pid, 0)`) is portable on Windows with a try/except around `psutil.pid_exists(pid)` — or use the simpler approach of writing a timestamp and treating locks older than 2 hours as stale.

**Testing Requirements**
- Single-process test: verify only one extraction can run at a time (existing behaviour preserved).
- Simulate stale lock: create `extraction.lock` with a dead PID; start an extraction — verify it proceeds (stale lock cleared).
- Concurrent test: start two extractions within milliseconds in a test — verify second is rejected with 429.

**Rollback Considerations**
- Remove the filesystem lock functions and restore `ACTIVE_EXTRACTION = threading.Lock()`. The `extraction.lock` file can be deleted. No CSV changes.

---

### ISSUE-05 — iMedPub Fetcher: Brittle Regex, No Pagination

**Current Problem**

`imedpub_fetcher.py` uses `re.findall(r'<a\s+href="([^"]+)"[^>]*>([^<]+)</a>', html, re.I)` to parse ALL anchor tags on iMedPub's search result page, then applies fragile URL-pattern filters to guess which ones are articles. This breaks immediately if iMedPub changes its HTML structure. There is also no pagination — only one search results page is fetched.

**Root Cause**

Raw regex on HTML is inherently fragile. BeautifulSoup was not used because it requires a separate install, but it is already standard in the Python web-scraping ecosystem and far more robust.

**Possible Approaches**

| Approach | Advantages | Disadvantages | Risk | Compatibility |
|---|---|---|---|---|
| **A: Switch to BeautifulSoup for HTML parsing** | Robust against minor HTML changes; cleaner selectors; widely used | Adds `beautifulsoup4` dependency; `pip install beautifulsoup4` | Low | High |
| **B: Use iMedPub's OAI-PMH endpoint if available** | Standards-based; structured; pagination built-in | iMedPub may not expose OAI-PMH; requires research to confirm | Unknown | Unknown |
| **C: Use iMedPub's sitemap XML** | Machine-readable; stable; no HTML parsing | May not support keyword search; lists all articles regardless of topic | Medium | Medium |
| **D: Query Crossref/OpenAlex filtered by iMedPub's publisher name** | Structured API; no scraping; pagination already implemented | iMedPub papers may not all be indexed in Crossref; depends on publisher registration | Medium | High |
| **E: Keep regex but add CSS selector emulation with targeted patterns** | No new dependencies | Still fragile; does not add pagination | Low | High (but low quality) |

**Recommended Approach: A — BeautifulSoup + pagination**

`beautifulsoup4` with `html.parser` (built into Python stdlib) is the standard solution for structured HTML scraping. It adds one dependency but that dependency is already installed by most Python environments and is production-stable.

**Implementation Plan**

Rewrite `imedpub_fetcher.py` using the following approach:

1. **Parse search results** with `BeautifulSoup(html, "html.parser")`.
2. **Find article links** by targeting `<a>` tags inside likely article-listing containers (e.g., `div[class*="article"]`, `div[class*="result"]`, or `h2 > a`, `h3 > a`). This is more specific than catching all anchors.
3. **Add pagination**: iMedPub typically uses `?page=2` or `&page=2` query parameters. After the first page, check for a "Next" page link in the parsed HTML and follow it until `limit` papers are found or no next page exists.
4. **Author extraction**: use `soup.find_all("meta", {"name": "citation_author"})` which is standard for academic sites using Google Scholar meta tags — this is already done in the regex version but will be more reliable via BeautifulSoup.
5. **DOI extraction**: use `soup.find("meta", {"name": "citation_doi"})` — more reliable than the current regex.

The download, PDF validation, and return-dict format remain identical to the current implementation — only the HTML parsing logic changes.

**Research Note**: iMedPub is a small publisher. If the site is frequently down or blocks scrapers, option D (query OpenAlex filtered by publisher) should be considered as a fallback. OpenAlex supports `filter=institutions.display_name:iMedPub` queries. This would be a more stable long-term approach if iMedPub scraping continues to be unreliable.

**Affected Files/Functions**
- `extractors/imedpub_fetcher.py` — entire `fetch_imedpub_papers()` function (rewritten)
- `requirements.txt` — add `beautifulsoup4`
- All other fetchers — UNTOUCHED

**Regression Risks**
- `beautifulsoup4` with `html.parser` is a safe dependency (no C extensions, no lxml required). If iMedPub's HTML structure changes dramatically, BeautifulSoup will still parse successfully but selectors may need updating — this is no worse than the current situation and is easier to fix.
- Other fetchers are completely unaffected.
- The return dict format is unchanged — `app.py` does not need any modification for this fetcher.

**Testing Requirements**
- Live test: run iMedPub extraction for topic "diabetes" — verify at least 5 papers are returned (vs. likely 0 with the broken current version).
- Verify DOI extraction works for a known iMedPub article.
- Verify pagination: request 20 papers — verify results come from at least 2 pages.
- Verify PDF download still works (this part of the code is unchanged).
- Regression for other fetchers: run PLOS and arXiv extractions and verify they are completely unaffected.

**Rollback Considerations**
- Replace the new `fetch_imedpub_papers()` with the original implementation. Remove `beautifulsoup4` from requirements if not used elsewhere. No CSV or data changes.

---

## D. Cross-Issue Dependencies

```
ISSUE-10 (DOI normalisation)
    └──▶ ISSUE-09 (attempted DOIs file)
              └──▶ Effectiveness of dedup improves together

ISSUE-17 (logging)
    └──▶ ISSUE-20 (partial save)
              └──▶ Log messages become essential context for diagnosing partial-save events

ISSUE-03 (wrong author fallback)
    └──▶ ISSUE-20 (partial save)
              └──▶ The fallback change may cause new code paths to surface errors;
                   partial save protects data if those paths crash

ISSUE-01 (more pages scanned)
    └──▶ ISSUE-02 (multi-column stitching)
              └──▶ Scanning more pages increases surface area for fragmented emails;
                   ISSUE-02 fix reduces the false-fragment rate from those additional pages

ISSUE-01 (more pages scanned)
    └──▶ ISSUE-06 (blocklist)
              └──▶ Scanning back-matter increases risk of collecting platform/editorial emails;
                   blocklist expansion is more important after more pages are scanned
```

**Implementation ordering implications:**

| Fix FIRST | Then fix | Reason |
|---|---|---|
| ISSUE-10 | ISSUE-09 | DOI normalisation must be correct before the attempted-DOI file is populated |
| ISSUE-17 | ISSUE-20 | Logging must be in place before partial-save debugging is meaningful |
| ISSUE-06 | ISSUE-01 | Stronger blocklist before scanning more pages |
| ISSUE-02 | ISSUE-01 | Stitching improvements before expanding page count |

---

## E. Overall Implementation Roadmap

### Phase 0 — Safety / Baseline (Before ANY code changes)

1. **Backup `master_email_list.csv`** → copy to `downloads/master_email_list_backup_YYYYMMDD.csv`.
2. **Backup `downloads/`** folder (or zip it).
3. **Record current test cases**: document 3–5 real extraction runs (topic, source, expected email count) to use as regression baselines.
4. **Establish a git commit** of the current working codebase as the rollback point.
5. **Verify the app currently runs** (`python app.py` on port 5000, one successful extraction from any source).

---

### Phase 1 — Lowest-Risk Fixes (Independent, no data impact)

**Group together in one commit:**

| Issue | Change | Risk |
|---|---|---|
| ISSUE-17 | Add `logging` configuration to `app.py`; replace `print()` calls | Low |
| ISSUE-06 | Expand `clean_and_validate_email()` blocklist + fused-email detection | Low |
| ISSUE-03 | Change fallback to `"Unknown"` in two locations | Low |

**Test after Phase 1:**
- Run 2 extractions (e.g., arXiv + PLOS) and compare results to Phase 0 baseline.
- Check `extraction.log` exists and contains expected output.
- Verify no existing valid emails are rejected by the expanded blocklist.

---

### Phase 2 — PDF Extraction Improvements (Moderate impact, isolated)

**Group together in one commit:**

| Issue | Change | Risk |
|---|---|---|
| ISSUE-06 | (Already done in Phase 1) | — |
| ISSUE-02 | `layout=True` + additional regex in `unwrap_broken_emails()` | Low-Medium |
| ISSUE-01 | Expand page scan to first 3 + last 2 pages | Low |

**Note**: ISSUE-06 MUST precede ISSUE-01 (already handled by doing it in Phase 1).

**Test after Phase 2:**
- Run extraction on a known 2-column journal (PLOS/eLife) — verify more emails found.
- Verify no regressions for arXiv (single-column) extraction.
- Measure and record extraction time per paper.

---

### Phase 3 — Deduplication / Persistence

**Sequential (not parallel — each depends on the previous):**

| Step | Issue | Change | Risk |
|---|---|---|---|
| 3a | ISSUE-10 | Add `normalise_doi()` helper; update `load_seen_data()` and `_run_extraction_task()` | Low |
| 3b | ISSUE-09 | Add `attempted_dois.txt` logic (after 3a) | Low |
| 3c | ISSUE-20 | Move `save_new_emails_to_master()` to `finally`; add batch flush every 5 papers | Low |

**Test after Phase 3:**
- Run extraction for a topic; kill the server mid-run; verify partial rows are in master CSV.
- Restart and re-run same topic — verify already-attempted DOIs are not re-processed.
- Verify `attempted_dois.txt` is created and grows correctly.

---

### Phase 4 — Reliability / Production

| Issue | Change | Risk |
|---|---|---|
| ISSUE-12 | Replace threading lock with filesystem lock (+ keep threading lock) | Low-Medium |

**Test after Phase 4:**
- Simulate concurrent start (two requests within 100ms) — verify second is rejected.
- Create stale `extraction.lock` with a dead PID — verify it is cleared on next start attempt.

---

### Phase 5 — iMedPub Fetcher Rewrite

| Issue | Change | Risk |
|---|---|---|
| ISSUE-05 | Rewrite `imedpub_fetcher.py` with BeautifulSoup | Low (isolated) |

**Why phase 5 is last:** It touches only one fetcher, is fully isolated from the rest of the pipeline, and requires installing `beautifulsoup4`. Doing it last means any issues with this change cannot affect the fixes made in Phases 1–4.

**Test after Phase 5:**
- Live iMedPub extraction test.
- Verify all other fetchers (arXiv, PLOS, eLife, OpenAlex, bioRxiv, Crossref, EuropePMC) are completely unaffected.

---

## F. Regression Protection Plan

**The following must continue to work identically after all phases:**

| Component | Regression check |
|---|---|
| All 8 source fetchers | Run one extraction per source; verify emails are found and appear in output |
| Master CSV append | Verify new rows are appended, not overwriting existing rows |
| Excel download | Verify `.xlsx` file downloads correctly from browser |
| Task status polling | Verify browser spinner updates with progress messages |
| DOI dedup | Verify running same topic twice does NOT produce duplicate email rows in CSV |
| Email dedup | Verify same email from two different papers appears only once in master CSV |
| `load_seen_data()` | Verify it correctly reads existing 78-row master CSV and returns non-empty sets |
| Flask routes | All 5 routes (`/`, `/start-extraction`, `/status/`, `/result/`, `/download/`) respond correctly |
| ACTIVE_EXTRACTION lock (intra-process) | Two rapid browser submissions → second gets immediate 429 error |

---

## G. Final Risk Assessment

After all 10 fixes are implemented, the following risks **remain**:

| Remaining Risk | Severity | Notes |
|---|---|---|
| Existing dirty records in master CSV (3 confirmed bad rows) | Medium | Requires a separate one-time manual or scripted cleanup pass — not addressed by any of the 10 fixes |
| `"Unknown"` author rows accumulate for generic-username emails | Low | Acceptable; these are now honestly labelled instead of being incorrectly attributed |
| arXiv papers in master CSV with old DOI format won't match normalised keys | Low | One-time re-download risk; email dedup prevents actual duplicates |
| ISSUE-02 (multi-column stitching) still won't catch all column-interleave patterns | Low | Addressed the most common cases; edge cases may persist in extreme layouts |
| `extraction.log` grows indefinitely without rotation | Low | Use `RotatingFileHandler` with `maxBytes=5_000_000, backupCount=3` in Phase 1 |
| iMedPub site structure may change again | Low | Now mitigated by BeautifulSoup selectors vs. raw regex; still depends on site stability |
| `attempted_dois.txt` can grow without bound | Very Low | Text file with one line per paper; 10,000 papers ≈ 300 KB; not a real concern for years |
| ISSUE-03 change surfaces "Unknown" in existing Excel outputs | Informational | Expected and correct behaviour; communicate to downstream users |

**What is now safe:**
- Data correctness (ISSUE-03 fixed — no more wrong author assignments)
- Crash safety (ISSUE-20 fixed — partial saves protect data)
- Deduplication reliability (ISSUE-09 + ISSUE-10 fixed — normalised DOIs, attempted-paper tracking)
- Production deployment (ISSUE-12 fixed — cross-process lock)
- Email quality (ISSUE-06 fixed — stronger validation)
- Debuggability (ISSUE-17 fixed — persistent log file)
- PDF coverage (ISSUE-01 + ISSUE-02 — more pages, better stitching)
- iMedPub reliability (ISSUE-05 — BeautifulSoup parsing)
