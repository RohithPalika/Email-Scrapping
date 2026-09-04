# Project Analysis: Academic Paper Email Extractor

This document provides a detailed, simple-words explanation of the project located in your workspace, how it extracts email addresses, how you can apply these concepts to website scraping, and how to set up the project.

## 1. What This Project Is and What It Does

This project is a **Web Application** (built with a Python framework called Flask) designed to search for academic research papers, download them as PDFs, read the text inside those PDFs, and figure out the email addresses of the authors who wrote them. 

Finally, it saves all this information neatly into an Excel file for you to download.

### Project Structure Overview:
*   **[`app.py`](file:///c:/Users/sampa/WS2.0/app.py)**: This is the brain of the project. It handles the web interface, processes your search request, tells the other files to go download papers, and does the heavy lifting of reading the PDFs and guessing which email belongs to which author.
*   **[`extractors/`](file:///c:/Users/sampa/WS2.0/extractors/)**: This folder contains separate "worker" scripts (`plos_fetcher.py`, `elife_fetcher.py`, etc.). Each script is trained to talk to a specific scientific database (like PLOS or Europe PMC) to search for a topic and download the PDF files.
*   **[`templates/`](file:///c:/Users/sampa/WS2.0/templates/)**: Contains the `index.html` file, which is the visual webpage you see in your browser with the search bar and buttons.
*   **`downloads/`**: A folder created automatically when the app runs. It temporarily stores the downloaded PDFs and the final Excel files.

---

## 2. How the Email Extraction Works (Step-by-Step)

The project **does not** scrape emails directly from the HTML code of a webpage. Instead, it downloads the actual PDF document of the paper and reads the text inside it. 

Here is exactly what the code in [`app.py`](file:///c:/Users/sampa/WS2.0/app.py) does to get emails:

1.  **Read the PDF:** It uses a tool called `pdfplumber` to open the downloaded PDF and read the raw text from the first two pages (where author details usually are).
2.  **Fix Broken Text:** Sometimes, when text is copied from a PDF, an email like `john.doe@example.com` gets broken across two lines. The code has a step (`unwrap_broken_emails`) that uses "Regular Expressions" (RegEx - a way to search for text patterns) to glue these broken emails back together.
3.  **Find Author Names:** It looks at the text and the metadata of the paper to build a list of all potential author names. It looks for Capitalized Words next to each other, filtering out common phrases like "Medical College" or "Open Access".
4.  **Find ANY Email:** It scans the whole text for anything that looks like an email address (e.g., `text@text.text`).
5.  **Filter Bad Emails:** It cleans up the emails and throws away generic ones that belong to the publishing platforms (like `@plos.org` or `@ncbi.nlm.nih.gov`), ensuring only real author emails are kept.
6.  **The Matching Game (The Smart Part):** Now it has a list of names and a list of emails. It tries to match them using two rules:
    *   **Rule 1 (Name Match):** It checks if the author's name is inside the email. For example, if the author is "John Doe", it checks if the email starts with `johndoe`, `john.doe`, or `jdoe`. If it matches, it pairs them up!
    *   **Rule 2 (Initials Match):** Sometimes papers list emails with initials, like `j.doe@example.com (JD)`. The code looks for these initials in the text and matches them to the author with the same initials.

---

## 3. How to Apply This to Web Scraping

If you want to build a script to scrape emails directly from website info (instead of PDFs), you can borrow the "Regex" patterns and logic from this project. 

Here is how you would do it:

### Step A: Get the text from the website
Instead of using `pdfplumber` to read a PDF, you would use a tool like `BeautifulSoup` to grab the text from a webpage.
```python
import requests
from bs4 import BeautifulSoup

# Get the website
response = requests.get("https://example.com/some-article")
soup = BeautifulSoup(response.text, 'html.parser')

# Extract all the text from the page
page_text = soup.get_text()
```

### Step B: Use the Regex from this project to find emails
You can copy the exact formula this project uses to hunt for emails in the text you just scraped.
```python
import re

# This is the exact pattern used in app.py to find emails
email_pattern = r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)'

# Find all emails in the website text
found_emails = re.findall(email_pattern, page_text)

print(found_emails) 
```

### Step C: Clean and Filter
Just like the project does, you should clean the emails (remove trailing commas/periods) and filter out generic ones (like `info@website.com` or `admin@website.com`).

---

## 4. How to Setup and Run This Project

To run this project on your Windows computer, follow these simple steps:

### Step 1: Open your terminal
Open Command Prompt, PowerShell, or the terminal inside your code editor (like VS Code), and make sure you are in the project folder:
`cd c:\Users\sampa\WS2.0`

### Step 2: Install required libraries
The project relies on some external Python tools. You need to install them. Run this command:
```bash
pip install Flask pdfplumber pandas openpyxl requests
```
*(Note: Since there is no `requirements.txt` file, I've listed the packages I found being used in the code).*

### Step 3: Run the application
Start the Flask web server by running the main Python file:
```bash
python app.py
```

### Step 4: Open in your browser
Once the terminal says the server is running, open your web browser (like Chrome or Edge) and go to:
`http://127.0.0.1:5000`

You should see the user interface where you can select a journal, type a topic, and start extracting!
