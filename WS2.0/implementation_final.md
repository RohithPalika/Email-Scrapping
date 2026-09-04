# Email Scraping: Reliable & Scalable Implementation Plan

## Part 1: Analysis of the Existing Code

After reading every file in your project, here is an honest breakdown of what is working well and what the current weaknesses are.

### ✅ What the Existing Code Does Well
| Component | What It Does | Quality |
|---|---|---|
| `app.py` — `clean_and_validate_email()` | Cleans up messy emails, removes junk | Excellent |
| `app.py` — `unwrap_broken_emails()` | Fixes emails split across PDF lines | Excellent |
| `app.py` — `extract_author_email_pairs()` | Matches email to author using Name + Initials rules | Good |
| `europepmc_fetcher.py` | Uses official API + fallback PDF URLs | Good |
| `openalex_fetcher.py` | Uses OpenAlex API to find open access PDF links | Good |

### ❌ Current Problems & Weaknesses

**Problem 1 — No delay between PDF downloads**
In `elife_fetcher.py`, `plos_fetcher.py`, and `imedpub_fetcher.py`, the code downloads PDFs in a fast loop with zero waiting time. This will get your IP blocked quickly by anti-bot security systems.

**Problem 2 — iMedPub fetcher uses fragile HTML scraping**
The `imedpub_fetcher.py` uses `re.findall()` to scan raw HTML text for links. This breaks the moment iMedPub changes its website design (which happens frequently).

**Problem 3 — No retry logic**
If a PDF download fails once (e.g., due to a temporary network error), the code simply moves on and loses that record forever.

**Problem 4 — Single User-Agent string**
All fetchers use the same browser signature (`Mozilla/5.0 Chrome/122`). Security systems recognize this as a bot pattern over time.

**Problem 5 — No coverage for your 25-website list**
The image you provided shows 25+ preprint servers. The current project only covers 5 of them directly.

---

## Part 2: The Best Implementation Approach

After analyzing all approaches, the selected strategy is a **3-Layer Hybrid System**:

```
Layer 1 (PREFERRED): Official REST APIs     → arXiv, bioRxiv, Crossref
         ↓ if no PDF found
Layer 2 (FALLBACK):  OpenAlex Super-Index  → covers ALL 25 websites
         ↓ read the PDF
Layer 3 (EXTRACTION): Existing pdfplumber  → already works perfectly
```

**Why this is the best approach:**
- ✅ No API keys needed for any of the three layers
- ✅ Completely free, no payment required
- ✅ Resistant to blocks (APIs don't block you; they are designed for developers)
- ✅ Scales to 1000s of papers per day
- ✅ Reuses all existing logic in `app.py` unchanged
- ✅ Covers all 25+ websites from your image through OpenAlex

---

## Part 3: Mapping Your 25 Websites to the Plan

Here is how every website in your image will be covered:

| Website from Your Image | Coverage Method |
|---|---|
| **arXiv.org** | New `arxiv_fetcher.py` using official arXiv API |
| **bioRxiv** | New `biorxiv_fetcher.py` using official bioRxiv API |
| **medRxiv** | Covered by the same `biorxiv_fetcher.py` (they share the same API) |
| **ChemRxiv, AgriXiv, ESSoAr, SSRN** | Covered by `openalex_fetcher.py` (already exists!) |
| **preprints.com, PeerJ, PsyArXiv, SocArXiv** | Covered by `openalex_fetcher.py` |
| **ResearchGate (RG)**, **SSRN** | Covered by `crossref_fetcher.py` |
| **Earth ArXiv, engrXiv, SportRxiv** | All indexed by OpenAlex — already covered! |
| **INA-Rxiv, LawArXiv, NutriXiv** | All indexed by OpenAlex — already covered! |
| **RePEC, SciELO, RIO** | Covered by `crossref_fetcher.py` |
| **iMedPub** | Existing fetcher (will be improved with delay + retry) |
| **BITSS, ChinaXiv, MindRxiv, Cogprints** | Covered by `openalex_fetcher.py` |
| **LIS Scholarship Archive, FocUS Archive** | Covered by Crossref + OpenAlex |
| **Humanities Commons** | Covered by `openalex_fetcher.py` |
| **paleo-rXiv** | Covered by `openalex_fetcher.py` |

> **Key Insight:** OpenAlex (already in your project) aggregates data from ALL of these preprint servers. So by improving the existing `openalex_fetcher.py`, we essentially cover all 25 websites in one shot!

---

## Part 4: Proposed Changes to the Codebase

### New Files to Create

---

#### [NEW] `extractors/arxiv_fetcher.py`
Uses the **official free arXiv API** (`http://export.arxiv.org/api/query`).
- Parses the XML response to get title, authors, and a direct PDF link
- Includes a mandatory **3-second delay** between requests
- Has a **retry mechanism** (tries 3 times if download fails)

---

#### [NEW] `extractors/biorxiv_fetcher.py`
Uses the **official free bioRxiv API** (`https://api.biorxiv.org/details/`).
- Fetches papers from **both bioRxiv AND medRxiv** using the same script
- Includes polite 1-second delay between downloads
- Has retry logic for failed PDF downloads

---

#### [NEW] `extractors/crossref_fetcher.py`
Uses the **free Crossref REST API** (`https://api.crossref.org/works`).
- Includes your email in the `User-Agent` header to join the "Polite Pool" for maximum speed
- Covers many of the smaller preprint servers (RePEC, SciELO, SSRN, etc.)
- Fetches DOI metadata, then uses OpenAlex to get the actual PDF link

---

### Files to Modify

---

#### [MODIFY] [`app.py`](file:///c:/Users/sampa/WS2.0/app.py)
Add the 3 new fetchers to the `SOURCE_FETCHERS` dictionary:
```python
from extractors.arxiv_fetcher import fetch_arxiv_papers
from extractors.biorxiv_fetcher import fetch_biorxiv_papers
from extractors.crossref_fetcher import fetch_crossref_papers

SOURCE_FETCHERS = {
    "plos":       ("PLOS",        fetch_plos_papers),
    "elife":      ("eLife",       fetch_elife_papers),
    "europepmc":  ("Europe PMC",  fetch_europepmc_papers),
    "imedpub":    ("iMedPub",     fetch_imedpub_papers),
    "openalex":   ("OpenAlex (All Servers)", fetch_openalex_papers),
    # NEW:
    "arxiv":      ("arXiv",       fetch_arxiv_papers),
    "biorxiv":    ("bioRxiv / medRxiv", fetch_biorxiv_papers),
    "crossref":   ("Crossref (Multi-Server)", fetch_crossref_papers),
}
```

---

#### [MODIFY] [`templates/index.html`](file:///c:/Users/sampa/WS2.0/templates/index.html)
Add 3 new options to the dropdown:
```html
<option value="arxiv">arXiv.org</option>
<option value="biorxiv">bioRxiv / medRxiv</option>
<option value="crossref">Crossref (Multi-Server)</option>
```

---

#### [IMPROVE] [`extractors/imedpub_fetcher.py`](file:///c:/Users/sampa/WS2.0/extractors/imedpub_fetcher.py)
Add a **1-second delay** and **retry logic** to prevent it from getting blocked, since it is the only fetcher that does direct HTML scraping.

---

## Part 5: Anti-Blocking Rules Baked into Every New Script

Every new fetcher script will follow these rules automatically:

| Rule | How It's Coded |
|---|---|
| Polite delays | `time.sleep(3)` for arXiv, `time.sleep(1)` for others |
| Retry on failure | Try 3 times before giving up on a PDF |
| Identify ourselves | `User-Agent` header with a descriptive app name |
| Crossref "Polite Pool" | `mailto:youremail@gmail.com` in the User-Agent |
| Check for valid PDF | Check first 10 bytes for `%PDF` before saving |

---

## Part 6: Setup Instructions (One-Time)

Run this single command to install all required Python libraries:
```bash
pip install flask pdfplumber pandas openpyxl requests
```
Then start the server:
```bash
python app.py
```
Open browser: `http://127.0.0.1:5000`

---

> [!IMPORTANT]
> **Ready to proceed?**
> If you approve this plan, I will immediately write the code for all 3 new fetcher scripts (`arxiv_fetcher.py`, `biorxiv_fetcher.py`, `crossref_fetcher.py`) and update `app.py` and `index.html`.
>
> Just confirm one thing: **What Gmail address should I put inside the Crossref fetcher** so you get maximum speed from the Polite Pool? I will use a placeholder `your_email@gmail.com` until you provide it.

## Addressing Your Concern (Unauthorized Scraping & Payments)

You asked if scraping emails directly from the HTML webpage (Method B) causes issues with unauthorized scraping or requires payments.

**The short answer:** 
1.  **No Payments:** The websites in your image (arXiv, bioRxiv, SSRN, preprints, etc.) are **Open Access** preprint servers. They are completely free. You will never be asked to pay to read their papers.
2.  **Scraping Issues (Getting Blocked):** While they are free, these websites hate aggressive "bots" scraping their HTML because it slows down their servers for real humans. If you use Method B and scrape their HTML pages too fast, their security systems (like Cloudflare) will simply **block your IP address**. You won't be in legal trouble or charged money, but your scraper will stop working.

Because of this, Method B (direct HTML scraping) across 25 different websites is the **worst** way to do this. It is fragile, prone to getting blocked, and requires writing 25 different scripts.

---

## The Best Implementation Plan

Instead of fighting security systems and writing 25 different scrapers, we will use the "Front Door". We will use **Authorized Public APIs**. 

APIs are special doors that these websites provide specifically for developers to download data legally, freely, and without getting blocked. 

Here is the best strategy:

### Phase 1: Use the "OpenAlex" Super-Database (Already in your project!)
Notice that your project already has a file called `openalex_fetcher.py`. **OpenAlex** is a massive, free, legal database that indexes almost *all* the websites in your image (arXiv, bioRxiv, SSRN, etc.).

Instead of going to 25 websites individually, we just ask OpenAlex: *"Give me 100 papers about [Topic], and give me the direct links to their PDFs."*
1.  OpenAlex legally gives us the PDF links for free.
2.  Our app downloads the PDFs.
3.  Our app reads the PDFs and extracts the emails using the existing logic (Method A). 

### Phase 2: Add Specific APIs (If needed)
If you want to target specific websites from your image directly (like arXiv or bioRxiv) to guarantee you only get papers from them, we will use their **Official Free APIs**, not HTML scraping.

We will create new fetchers for the most important ones:
*   `arxiv_fetcher.py` (Using the official free arXiv API)
*   `biorxiv_fetcher.py` (Using the official free bioRxiv/medRxiv API)
*   `crossref_fetcher.py` (Crossref is another massive free database that handles many of the others).

### Phase 3: The PDF Extraction Fallback
Almost all of these preprint servers include the author's email inside the actual PDF document. By using APIs to get the PDF link, and then using `pdfplumber` to read the PDF (which your project already does perfectly), we avoid 100% of the HTML scraping blocks.

---

## Proposed Changes to the Codebase

To make this work seamlessly for your outreach list, I propose we add the following fetchers using official APIs:

### 1. Update `app.py`
Add the new API fetchers to the `SOURCE_FETCHERS` dictionary.

### 2. Create `extractors/arxiv_fetcher.py`
#### [NEW] [arxiv_fetcher.py](file:///c:/Users/sampa/WS2.0/extractors/arxiv_fetcher.py)
This script will use the `http://export.arxiv.org/api/query` API to legally and freely search for papers, get the PDF links, and save them for the main app to extract emails.

### 3. Create `extractors/biorxiv_fetcher.py`
#### [NEW] [biorxiv_fetcher.py](file:///c:/Users/sampa/WS2.0/extractors/biorxiv_fetcher.py)
This script will use the `https://api.biorxiv.org/details/` API to legally fetch papers from both bioRxiv and medRxiv.

### 4. Update `templates/index.html`
#### [MODIFY] [index.html](file:///c:/Users/sampa/WS2.0/templates/index.html)
Add "arXiv", "bioRxiv", and "OpenAlex (All Servers)" to the dropdown menu so you can easily select them from the website interface.

---

## User Review Required

> [!IMPORTANT]
> **Does this strategy sound good to you?** 
> By using APIs (like OpenAlex and arXiv API) combined with your existing PDF email extractor, we bypass all the blocks associated with HTML scraping, keep it 100% free, and can cover almost all the websites in your image. 
> 
> If you approve, I will write the code for the `arxiv` and `biorxiv` fetchers right now and wire them up to your website UI!



include this feature:
3. The Automated Master File (The Best Solution)

How it works: We tell app.py to keep a running list—let's call it master_email_list.csv (which opens easily in Excel).
What it does:
Every time you click "Extract Data", the app quietly opens master_email_list.csv and memorizes all the emails you have ever scraped.
When it finds a new email in a PDF, it asks: "Is this email in my memory?"
If YES: It ignores it and throws it in the trash.
If NO: It saves it to your new Excel sheet AND adds it to the master_email_list.csv for the future.
Why Solution 3 is the Best
It is 100% automatic. You never have to worry about duplicates again. You will only ever get fresh, new, unseen emails in your download folder, and the script handles the memory for you behind the scenes.

Would you like me to include this "Master List Deduplication" feature when I write the new code?

11:54 AM
12:02 PM
No, it will not create any storage issues or slow down your computer at all!

To put it in perspective: a .csv file is just a plain text file. Even if you scrape 100,000 email addresses, the file size would only be about 2 or 3 Megabytes (MB). That is smaller than a single photo taken on your smartphone!

Here is a simple breakdown of the Pros and Cons of using a Master CSV file:

👍 Pros (The Good Stuff)
Zero Storage Worries: As mentioned, text files are tiny. You could store millions of emails and it wouldn't even dent your hard drive.
100% Automatic: You never have to think about duplicates. You can run the scraper 100 times, and it will only ever give you fresh contacts.
Always Accessible: Because it is a .csv file, you can double-click it at any time and it will open right up in Excel so you can see your entire history of scraped emails.
No Extra Software: We don't need to install complicated database servers. It just uses basic Python, which is already running the app.
👎 Cons (The Minor Drawbacks)
Slightly Slower Start: When you click "Extract Data", the app has to spend about half a second reading the master file before it starts downloading new papers. (Half a second is practically invisible, but it is technically a con).
Accidental Deletion: If you accidentally go into your folders and delete the master_email_list.csv file, the app loses its memory. It will just create a new, empty one the next time you run it, but it won't remember the old emails you scraped.
The Verdict: For an outreach automation tool, this is the standard, safest, and most lightweight way to handle duplicates. The benefits massively outweigh the cons.

Would you like me to go ahead and add this feature to our Implementation Plan?

12:02 PM

