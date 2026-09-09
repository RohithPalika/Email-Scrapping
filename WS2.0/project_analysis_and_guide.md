# Project Analysis & Guide

Here is a simple breakdown of how this project works, what each file does, and how you can add new websites to extract data from.

> [!NOTE]
> I noticed you were trying to run `pip install -r requirements.txt` and it failed because the file was missing. I have created the `requirements.txt` file for you. You can now successfully run the command to install the required packages!

## 1. What This Project Does

This project is a **Web Application** that searches for academic papers on specific topics, downloads their PDFs, and automatically reads through them to extract the **Author Names** and **Email Addresses**. Finally, it saves all this extracted information into an Excel file for you.

## 2. What Each File Does (In Simple Words)

*   **[`app.py`](file:///c:/Users/sampa/WS2.0/app.py)**
    This is the "brain" or the main engine of the project. It runs the web server. When you search for something on the webpage, this file takes your request, asks a "fetcher" to go get the PDFs, then reads those PDFs to find emails, and saves the results in an Excel file.
*   **[`extractors/`](file:///c:/Users/sampa/WS2.0/extractors/) (Folder)**
    This folder contains "fetcher" scripts (like `plos_fetcher.py` or `openalex_fetcher.py`). Think of these as specialized workers. Each script knows exactly how to talk to one specific website to search for papers and download them. 
*   **[`templates/index.html`](file:///c:/Users/sampa/WS2.0/templates/index.html)**
    This is the visual webpage you see in your browser. It has the search bar, the dropdown to pick a website, and the extract button.
*   **`downloads/` (Folder)**
    This folder is automatically created by the app. It temporarily stores the downloaded PDFs and the final generated Excel files.

---

## 3. How to Add a New Site to Extract Data

If you want to add a new website (e.g., `MyNewSite`), you only need to do 3 simple steps:

### Step 1: Create a new Fetcher Script
Go into the `extractors/` folder and create a new file, for example, `mynewsite_fetcher.py`.
Inside this file, you need to write a function that takes a topic, downloads PDFs from the site, and returns a list of dictionaries. 

Here is the exact format your code should follow:

```python
import os
import requests

def fetch_mynewsite_papers(topic, limit, target_dir):
    # 1. Write code here to search 'MyNewSite' for the 'topic'
    # 2. Download the PDFs into 'target_dir'
    
    records = []
    
    # 3. For each downloaded paper, add its info to the list
    records.append({
        "file_path": "path/to/the/downloaded/paper.pdf",
        "pdf_name": "paper_1.pdf",
        "title": "Title of the Paper",
        "authors": ["Author One", "Author Two"],
        "doi": "10.1234/example.doi", # The document ID
        "source_journal": "MyNewSite Journal" # Optional
    })
    
    return records
```

### Step 2: Register it in `app.py`
Open `app.py` and tell it about your new fetcher.

First, import your new function at the top of the file:
```python
from extractors.mynewsite_fetcher import fetch_mynewsite_papers
```

Next, scroll down to the `SOURCE_FETCHERS` dictionary (around line 241) and add your new site:
```python
SOURCE_FETCHERS = {
    "plos":      ("PLOS", fetch_plos_papers),
    # ... other sites ...
    "mynewsite": ("My New Site", fetch_mynewsite_papers), # <-- Add this line
}
```

### Step 3: Add it to the Webpage Dropdown
Open `templates/index.html` and find the dropdown menu (`<select name="source_site">`). Add a new `<option>` for your site:

```html
<select name="source_site" id="source_site">
    <option value="plos">PLOS</option>
    <!-- ... other options ... -->
    <option value="mynewsite">My New Site</option> <!-- <-- Add this line -->
</select>
```

That's it! Now when you go to the webpage, you can select "My New Site", and `app.py` will automatically use your new script to get the PDFs and extract the emails.
