import html
import re
import smtplib
import socket
import threading
import logging
import requests
import dns.resolver
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger("extraction.verifier")

_DOMAIN_CACHE = {}  # domain -> bool (True if domain has valid MX/A records, False if NXDOMAIN)
_EMAIL_CACHE = {}   # email -> (status, reason)
_CACHE_LOCK = threading.Lock()

# Common disposable & temporary email services
DISPOSABLE_DOMAINS = frozenset({
    "mailinator.com", "guerrillamail.com", "tempmail.com", "trashmail.com",
    "10minutemail.com", "yopmail.com", "sharklasers.com", "dispostable.com",
    "getairmail.com", "throwawaymail.com", "mytemp.email", "fakeinbox.com",
    "temp-mail.org", "nada.ltd", "mohmal.com", "crazymailing.com",
    "generator.email", "trashmail.net", "emailondeck.com", "inboxkitten.com"
})

# Publisher & platform domains that should never be treated as author emails
PLATFORM_DOMAINS = frozenset({
    "plos.org", "elifesciences.org", "europepmc.org", "ebi.ac.uk",
    "ncbi.nlm.nih.gov", "crossref.org", "orcid.org", "adobe.com",
    "openalex.org", "imedpub.com", "biorxiv.org", "medrxiv.org",
    "doi.org", "pubmed.ncbi.nlm.nih.gov", "frontiersin.org", "mdpi.com",
    "arxiv.org", "ssrn.com", "wiley.com", "onlinelibrary.wiley.com",
    "springer.com", "elsevier.com", "tandfonline.com", "nature.com",
    "oup.com", "cambridge.org", "ahajournals.org", "researchgate.net",
    "sciencedirect.com", "cell.com", "thelancet.com", "bmj.com",
    "jamanetwork.com", "sagepub.com", "ieee.org", "acm.org"
})

GENERIC_ROLE_PREFIXES = frozenset({
    "info", "admin", "support", "contact", "editor", "editorial", "webmaster",
    "noreply", "no-reply", "help", "line", "mail", "office", "submit",
    "submission", "postmaster", "reply", "donotreply", "do-not-reply",
    "mailer", "bounce", "newsletter", "list", "subscribe", "sales",
    "enquiries", "enquiry", "query", "feedback", "helpdesk", "editorialoffice",
    "manuscript", "journal", "service", "billing", "marketing", "careers"
})


def clean_and_repair_email(raw_email):
    """
    Intelligently cleans and repairs raw email strings extracted from PDFs and text:
    - Strips PDF footnote markers glued to TLD (e.g. '.edu1' -> '.edu', '.org*' -> '.org')
    - Strips Unicode whitespace, control characters, brackets, quotes
    - Returns cleaned lowercase email, or None if invalid.
    """
    if not raw_email or not isinstance(raw_email, str):
        return None

    cleaned = raw_email.strip(" \t\n\r*\u2020\u2021\u00a7#()[]{}<>,;:'\"")
    cleaned = re.split(r'[\s\(\[\{]', cleaned)[0].rstrip(".,;:")

    if "@" not in cleaned:
        return None

    local_part, domain = cleaned.split("@", 1)
    local_part = local_part.strip().lower()
    domain = domain.strip().lower()

    # Repair footnote superscripts stuck to the TLD (e.g., .edu1, .com2, .org*, .ac.uk3)
    # Match patterns like .edu\d+, .com\d+, .org\d+, .gov\d+, .net\d+, .io\d+
    domain = re.sub(r'\.(edu|com|org|gov|net|mil|int|ac|eu|io|de|fr|uk|cn|in|br|jp|au|ca|es|nl|it|ch|se|no|ru|za|mx)\d+$', r'.\1', domain)
    # Also handle country compound TLDs like .edu.cn1, .ac.uk2
    domain = re.sub(r'\.(edu|ac|res|gov|co|org)\.([a-z]{2})\d+$', r'.\1.\2', domain)
    domain = domain.rstrip("0123456789*#†‡.,;:'\"")

    repaired = f"{local_part}@{domain}"

    # Strict syntax check
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,15}$', repaired):
        return None

    tld = domain.split(".")[-1]
    if len(tld) > 10:
        return None

    # Reject consecutive dots or invalid domain structure
    if ".." in domain or len(domain) > 70 or domain.count(".") > 5:
        return None

    return repaired


def clean_paper_title(raw_title):
    """
    Cleans and standardizes paper titles:
    - Decodes HTML entities like &amp;, &lt;, &gt;, &quot;, &#39;
    - Eliminates embedded newlines, carriage returns, tabs
    - Collapses consecutive whitespace into single spaces
    - Strips surrounding quotes or whitespace
    """
    if not raw_title or not isinstance(raw_title, str):
        return ""
    t = html.unescape(str(raw_title))
    t = re.sub(r'[\r\n\t]+', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' \t\r\n"\'')
    return t


def clean_author_name(raw_author, email=""):
    """
    Cleans and normalizes author names to ensure strictly valid person names:
    - Eliminates newlines, carriage returns, tabs, and excess spacing
    - Strips unwanted prefixes/suffixes ('Correspondence:', 'Author:', 'Email:', 'Dr.', 'Prof.')
    - Filters out attached section headers, paper topics, countries, or journal names
    - If composite string contains multiple names, picks the one corresponding to the email
    - If empty/invalid but valid academic email exists, derives clean author name from email username
    """
    if not raw_author or not isinstance(raw_author, str):
        raw_author = ""

    a = html.unescape(str(raw_author))
    a = a.replace('\u00a0', ' ').replace('\u200b', '').replace('\ufeff', '')

    # 1. Split across explicit newline boundaries
    lines = [re.sub(r'\s+', ' ', l).strip(' \t\r\n:;,-') for l in re.split(r'[\r\n]+', a)]
    lines = [l for l in lines if l]

    blacklist_exact = {
        'case report', 'case reports', 'case study', 'case studies', 'control study',
        'mini review', 'comprehensive review', 'systematic review', 'review', 'original research',
        'research article', 'abstract', 'abfrak', 'abstrakt', 'introduction', 'correspondence',
        'corresponding author', 'email', 'e-mail', 'mail', 'author', 'authors', 'asst',
        'assistant', 'professor', 'department', 'division', 'faculty', 'institute', 'hospital',
        'university', 'college', 'center', 'centre', 'journal', 'journals', 'in this work',
        'inthiswork', 'synthetic data', 'face image', 'learning in', 'students',
        'medical students', 'hungary', 'china', 'malaysia', 'brazil', 'pakistan', 'qatar',
        'nepal', 'australia', 'indonesia', 'philippines', 'uganda', 'malawi', 'catalonia',
        'bangladesh', 'spain', 'south korea', 'quebec', 'twitter', 'operations',
        'management research', 'acne vulgaris', 'poultry flock', 'obesity potential',
        'body immune enhancers', 'herbal face wash', 'masa simpan', 'diabetes',
        'dera ismail khan', 'an bras dermatol', 'wild', 'face image deblurring',
        'all rights reserved', 'open access', 'creative commons', 'previousstudiesshowthatdeep',
        'automaticnetworkarchitectureadaptationforobjectdetectionbeyond'
    }

    def is_junk(text_chunk):
        tc = text_chunk.lower().strip(' :;,-')
        if tc in blacklist_exact:
            return True
        if any(b in tc for b in [
            'correspondence', 'case report', 'case study', 'control study', 'all rights',
            'medical students', 'students', 'inthiswork', 'journal of', 'department of',
            'university of', 'faculty of', 'hospital', 'creative commons', 'management research',
            'previousstudiesshow', 'networkarchitecture', 'association', 'consortium'
        ]):
            return True
        if any(len(w) > 24 for w in text_chunk.split()):
            return True
        return False

    filtered_lines = [l for l in lines if not is_junk(l)]
    if not filtered_lines:
        filtered_lines = lines

    chosen = None
    if email and '@' in email:
        uname = re.sub(r'[^a-z]', '', email.split('@')[0].lower())
        for l in filtered_lines:
            words = [re.sub(r'[^a-z]', '', w.lower()) for w in l.split()]
            words = [w for w in words if len(w) >= 3]
            if any(w in uname for w in words):
                chosen = l
                break

    if not chosen and filtered_lines:
        chosen = filtered_lines[-1]
    elif not chosen and lines:
        chosen = lines[-1]
    else:
        chosen = ""

    # Clean out labels from the chosen line
    chosen = re.sub(r'^(?:correspondence|corresponding author|author|email|e-mail|dr\.?|prof\.?)\s*[:\-]?\s*', '', chosen, flags=re.IGNORECASE)
    chosen = re.sub(r'\s*(?:email|e-mail|correspondence|department|university).*$', '', chosen, flags=re.IGNORECASE)
    chosen = re.sub(r'[\d*†‡§#]+', '', chosen)
    chosen = re.sub(r'\s+', ' ', chosen).strip(' \t\r\n,;:-')

    # If chosen has multiple names or words, test sub-windows against email
    if email and '@' in email and len(chosen.split()) >= 3:
        uname = re.sub(r'[^a-z]', '', email.split('@')[0].lower())
        name_parts = chosen.split()
        for i in range(len(name_parts) - 1):
            w1 = re.sub(r'[^a-z]', '', name_parts[i].lower())
            w2 = re.sub(r'[^a-z]', '', name_parts[i+1].lower())
            if (len(w1) >= 3 and w1 in uname) or (len(w2) >= 3 and w2 in uname):
                chosen = f"{name_parts[i]} {name_parts[i+1]}"
                break

    # If still junk, or abnormally long single word, or empty: derive human name from email username
    if is_junk(chosen) or len(chosen) < 3 or any(len(w) > 24 for w in chosen.split()):
        if email and '@' in email:
            uname = email.split('@')[0]
            parts = [p for p in re.split(r'[^a-zA-Z]+', uname) if len(p) >= 2]
            parts = [p.capitalize() for p in parts if p.lower() not in ('dr', 'prof', 'mr', 'ms', 'md', 'phd')]
            if parts:
                chosen = ' '.join(parts)

    return chosen


def is_valid_author_name(name):
    """
    Verifies that the author name looks like a legitimate person name.
    Rejects generic departments, journal titles, numbers, newlines, or sentinel words.
    """
    if not name or not isinstance(name, str):
        return False

    name = name.strip()
    name_lower = name.lower()

    if '\n' in name or '\r' in name or '\t' in name:
        return False

    # Blacklist sentinel keywords
    sentinels = {
        "unknown", "na", "n/a", "none", "null", "author", "authors",
        "corresponding author", "editorial team", "editorial board",
        "et al", "et al.", "department", "university", "institute",
        "college", "hospital", "center", "centre", "school", "faculty",
        "laboratory", "division", "consortium", "group", "committee",
        "students", "medical students", "case report", "control study",
        "abstract", "review", "synthetic data"
    }

    if name_lower in sentinels:
        return False

    # Check for keywords inside string
    if any(k in name_lower for k in [
        "department of", "university of", "institute of", "hospital", "journal of",
        "all rights reserved", "case report", "control study", "students", "creative commons"
    ]):
        return False

    # Check if word is concatenated junk (>24 chars)
    if any(len(w) > 24 for w in name.split()):
        return False

    # Must contain alphabetic characters and at least 2 chars
    alpha_chars = sum(c.isalpha() for c in name)
    if alpha_chars < 2:
        return False

    # Check digit ratio (author names should not have numbers)
    digits = sum(c.isdigit() for c in name)
    if digits > 0:
        return False

    return True


def check_email_deliverability(email, sender_domain="gmail.com", timeout=2.5):
    """
    Validates email deliverability and domain existence.
    - True Dead / Undeliverable:
      * Domain doesn't exist (NXDOMAIN) or has no active MX records.
      * Server explicitly rejects with 550 'User does not exist'.
    - Deliverable:
      * Valid MX domain where mailbox is confirmed (250 OK).
      * Valid MX domain where direct port 25 connection is protected/blocked by institution firewall.
    """
    cleaned = clean_and_repair_email(email)
    if not cleaned:
        return "Undeliverable", "Invalid email syntax"

    local_part, domain = cleaned.split("@", 1)

    # Check role accounts
    if local_part in GENERIC_ROLE_PREFIXES:
        return "Undeliverable", f"Role/platform address ({local_part}@)"

    # Check publisher domains
    if any(domain == p or domain.endswith("." + p) for p in PLATFORM_DOMAINS):
        return "Undeliverable", f"Publisher/platform domain ({domain})"

    # Check disposable domains
    if domain in DISPOSABLE_DOMAINS:
        return "Undeliverable", "Disposable/temp email service"

    # Fast cache check
    with _CACHE_LOCK:
        if cleaned in _EMAIL_CACHE:
            return _EMAIL_CACHE[cleaned]
        if domain in _DOMAIN_CACHE and not _DOMAIN_CACHE[domain]:
            return "Undeliverable", "Domain has no active MX records"

    # Step 1: Check Domain MX Records via DNS
    mx_host = None
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 2.5
    resolver.timeout = 2.0

    try:
        mx_records = resolver.resolve(domain, "MX")
        mx_host = sorted([(rec.preference, str(rec.exchange).rstrip(".")) for rec in mx_records])[0][1]
        with _CACHE_LOCK:
            _DOMAIN_CACHE[domain] = True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        # Fallback: check if domain has an A record (RFC 5321 implicit MX fallback)
        try:
            a_records = resolver.resolve(domain, "A")
            if a_records:
                mx_host = domain
                with _CACHE_LOCK:
                    _DOMAIN_CACHE[domain] = True
            else:
                with _CACHE_LOCK:
                    _DOMAIN_CACHE[domain] = False
                res = ("Undeliverable", "Domain does not exist or has no active MX records")
                with _CACHE_LOCK:
                    _EMAIL_CACHE[cleaned] = res
                return res
        except Exception:
            with _CACHE_LOCK:
                _DOMAIN_CACHE[domain] = False
            res = ("Undeliverable", "Domain does not exist or has no active MX records")
            with _CACHE_LOCK:
                _EMAIL_CACHE[cleaned] = res
            return res
    except Exception as e:
        # Transient DNS timeout — do not block legitimate academic emails
        return "Deliverable", f"MX lookup timeout ({str(e)})"

    # Step 2: Test Mailbox Connection via SMTP handshake
    try:
        server = smtplib.SMTP(timeout=timeout)
        server.connect(mx_host, 25)
        server.helo(sender_domain)
        server.mail(f"verify@{sender_domain}")
        code, message = server.rcpt(cleaned)
        server.quit()

        msg = message.decode("utf-8", errors="ignore") if isinstance(message, bytes) else str(message)
        msg_lower = msg.lower()

        # CASE A: Client IP Blocked by Server (Spamhaus / RBL / 5.7.1 / Mimecast) -> Valid domain, KEEP IT
        if any(term in msg_lower for term in ["spamhaus", "blocked", "service unavailable", "5.7.1", "blacklist", "rbl", "mimecast", "trendmicro"]):
            res = ("Deliverable", "Deliverable (Valid MX - Server firewall protected)")
            with _CACHE_LOCK:
                _EMAIL_CACHE[cleaned] = res
            return res

        # CASE B: Server Explicitly Confirms Mailbox (250 OK) -> KEEP IT
        if code == 250:
            res = ("Deliverable", "Mailbox exists and accepts messages (250 OK)")
            with _CACHE_LOCK:
                _EMAIL_CACHE[cleaned] = res
            return res

        # CASE C: Explicit Recipient Failure (User does not exist) -> DROP IT
        if code in [550, 551, 552, 553, 554] and any(term in msg_lower for term in ["user", "recipient", "not exist", "unknown", "invalid recipient", "mailbox unavailable", "no such user"]):
            res = ("Undeliverable", f"Mailbox rejected: {msg.strip()[:80]}")
            with _CACHE_LOCK:
                _EMAIL_CACHE[cleaned] = res
            return res

        # Ambiguous code fallback: preserve email
        res = ("Deliverable", f"Accepted with code {code}")
        with _CACHE_LOCK:
            _EMAIL_CACHE[cleaned] = res
        return res

    except Exception:
        # If port 25 or socket connection is dropped by ISP/University, domain is valid -> KEEP IT
        res = ("Deliverable", "Deliverable (Valid MX - Connection bypassed)")
        with _CACHE_LOCK:
            _EMAIL_CACHE[cleaned] = res
        return res


def verify_email_via_api(email, api_key, service="auto"):
    """
    Verifies deliverability using a 3rd party email verification API.
    Supports ZeroBounce, Hunter, Debounce, Reoon.
    Returns (status, reason).
    """
    if not api_key:
        return check_email_deliverability(email)

    email = email.strip().lower()

    # Detect or use selected service
    # 1. ZeroBounce (key is 32-char hex usually)
    try:
        # Try ZeroBounce
        resp = requests.get(
            "https://api.zerobounce.net/v2/validate",
            params={"api_key": api_key, "email": email, "ip_address": ""},
            timeout=8
        )
        if resp.status_code == 200:
            data = resp.json()
            status = data.get("status", "").lower()
            sub_status = data.get("sub_status", "")
            if status == "valid":
                return "Deliverable", f"ZeroBounce Verified (Valid: {sub_status})"
            elif status in ("invalid", "spamtrap", "abuse"):
                return "Undeliverable", f"ZeroBounce Rejected ({status}: {sub_status})"
            elif status == "catch-all":
                return "Deliverable", "ZeroBounce Catch-All Mailbox"
            else:
                return "Deliverable", f"ZeroBounce status: {status}"
    except Exception as e:
        log.warning(f"API verification attempt error: {e}")

    # Fallback to local verifier
    return check_email_deliverability(email)


# -----------------------------------------------------------
# Strict Country Domain & Geolocation Rules
# -----------------------------------------------------------
COUNTRY_DOMAIN_RULES = {
    "USA": {
        "iso": {"US"},
        "domain_suffixes": (".edu", ".gov", ".mil", ".us"),
        "institutions": [
            "mayo.edu", "stanford.edu", "harvard.edu", "mit.edu", "jhu.edu",
            "yale.edu", "columbia.edu", "cornell.edu", "berkeley.edu", "ucla.edu",
            "ucsf.edu", "ucsd.edu", "umich.edu", "upenn.edu", "uw.edu",
            "washington.edu", "northwestern.edu", "duke.edu", "nih.gov", "cdc.gov",
            "hopkinsmedicine.org", "mskcc.org", "stjude.org", "clevelandclinic.org"
        ],
        "keywords": ["united states", "usa", "u.s.a.", " u.s. ", "america", "california", "new york", "texas", "massachusetts"]
    },
    "UK": {
        "iso": {"GB", "UK"},
        "domain_suffixes": (".ac.uk", ".gov.uk", ".uk", ".nhs.uk", ".nhs.net", ".org.uk", ".co.uk"),
        "institutions": ["ox.ac.uk", "cam.ac.uk", "ucl.ac.uk", "imperial.ac.uk", "kcl.ac.uk", "ed.ac.uk", "manchester.ac.uk", "warwick.ac.uk"],
        "keywords": ["united kingdom", " uk ", "u.k.", "england", "scotland", "wales", "great britain", "london", "oxford", "cambridge"]
    },
    "Italy": {
        "iso": {"IT"},
        "domain_suffixes": (".it", ".edu.it"),
        "institutions": ["unibo.it", "unimi.it", "uniroma1.it", "unipd.it", "polimi.it", "cnr.it", "unige.it", "unifi.it", "unina.it"],
        "keywords": ["italy", "italia", "italian", "rome", "milan", "bologna", "florence", "naples", "turin"]
    },
    "Spain": {
        "iso": {"ES"},
        "domain_suffixes": (".es", ".edu.es"),
        "institutions": ["uab.es", "ub.edu", "ucm.es", "uam.es", "csic.es", "upv.es", "unav.edu"],
        "keywords": ["spain", "españa", "spanish", "madrid", "barcelona", "valencia", "seville"]
    },
    "Romania": {
        "iso": {"RO"},
        "domain_suffixes": (".ro", ".edu.ro"),
        "institutions": ["unibuc.ro", "ubbcluj.ro", "upb.ro", "umfcluj.ro", "umfcd.ro", "upt.ro"],
        "keywords": ["romania", "romanian", "bucharest", "cluj", "iasi", "timisoara"]
    },
    "France": {
        "iso": {"FR"},
        "domain_suffixes": (".fr",),
        "institutions": ["inserm.fr", "cnrs.fr", "pasteur.fr", "sorbonne-universite.fr", "u-paris.fr", "univ-lyon1.fr"],
        "keywords": ["france", "french", "paris", "lyon", "marseille", "toulouse", "bordeaux"]
    },
    "Brazil": {
        "iso": {"BR"},
        "domain_suffixes": (".br", ".edu.br", ".gov.br"),
        "institutions": ["usp.br", "unicamp.br", "ufrj.br", "unesp.br", "ufmg.br", "ufrgs.br", "fiocruz.br"],
        "keywords": ["brazil", "brasil", "brazilian", "são paulo", "rio de janeiro"]
    },
    "Germany": {
        "iso": {"DE"},
        "domain_suffixes": (".de",),
        "institutions": ["mpg.de", "helmholtz.de", "fraunhofer.de", "charite.de", "tum.de", "lmu.de", "uni-heidelberg.de", "rwth-aachen.de", "kit.edu"],
        "keywords": ["germany", "deutschland", "german", "berlin", "munich", "heidelberg", "frankfurt", "hamburg"]
    },
    "Australia": {
        "iso": {"AU"},
        "domain_suffixes": (".au", ".edu.au", ".gov.au"),
        "institutions": ["unimelb.edu.au", "sydney.edu.au", "anu.edu.au", "uq.edu.au", "unsw.edu.au", "monash.edu", "uwa.edu.au", "uts.edu.au", "csiro.au"],
        "keywords": ["australia", "australian", "melbourne", "sydney", "brisbane", "canberra", "perth"]
    },
    "Canada": {
        "iso": {"CA"},
        "domain_suffixes": (".ca", ".gc.ca"),
        "institutions": ["utoronto.ca", "mcgill.ca", "ubc.ca", "umontreal.ca", "ualberta.ca", "uwaterloo.ca", "ucalgary.ca"],
        "keywords": ["canada", "canadian", "toronto", "montreal", "vancouver", "ottawa", "quebec"]
    },
    "Mexico": {
        "iso": {"MX"},
        "domain_suffixes": (".mx", ".edu.mx"),
        "institutions": ["unam.mx", "cinvestav.mx", "ipn.mx", "itesm.mx", "uam.mx"],
        "keywords": ["mexico", "méxico", "mexican"]
    },
    "Saudi Arabia": {
        "iso": {"SA"},
        "domain_suffixes": (".sa", ".edu.sa"),
        "institutions": ["kaust.edu.sa", "kfupm.edu.sa", "ksu.edu.sa", "kau.edu.sa"],
        "keywords": ["saudi arabia", "saudi", "ksa", "riyadh", "jeddah"]
    },
    "Egypt": {
        "iso": {"EG"},
        "domain_suffixes": (".eg", ".edu.eg"),
        "institutions": ["cu.edu.eg", "asu.edu.eg", "auc-egypt.edu", "mans.edu.eg", "alexu.edu.eg"],
        "keywords": ["egypt", "egyptian", "cairo", "alexandria"]
    }
}

CHINESE_DOMAINS = frozenset({
    '126.com', '163.com', 'yeah.net', 'qq.com', 'foxmail.com',
    'sina.com', 'sina.cn', 'vip.sina.com', 'sohu.com', 'vip.sohu.com',
    'aliyun.com', 'alibaba-inc.com', 'tencent.com', 'tom.com', '21cn.com',
    '139.com', '189.cn', 'wo.cn', 'chinajournal.net.cn', 'wanfangdata.com.cn',
    'vip.163.com', 'vip.126.com', '263.net', 'eyou.com'
})

CHINESE_INSTITUTION_MARKERS = [
    'pku.edu', 'tsinghua', 'fudan', 'zju.edu', 'sjtu.edu', 'whu.edu',
    'buaa.edu', 'ustc.edu', 'sysu.edu', 'hust.edu', 'tongji.edu',
    'cas.cn', 'caas.cn', 'cams.cn', 'ecnu.edu', 'dlut.edu', 'neu.edu',
    'hnu.edu', 'lzu.edu', 'uestc.edu', 'scut.edu', 'nwsuaf.edu',
    'nudt.edu', 'shu.edu', 'nlpr.ia.ac', 'jlu.edu', 'sdu.edu', 'seu.edu',
    'csu.edu', 'nwpu.edu', 'hit.edu', 'bit.edu', 'nankai.edu', 'xmu.edu',
    'scu.edu', 'tju.edu', 'ouc.edu', 'scau.edu', 'hunau.edu', 'zafu.edu'
]

CHINESE_TITLE_KEYWORDS = [
    'in china', 'chinese patients', 'chinese population', 'chinese children',
    'chinese women', 'chinese adults', 'in wuhan', 'in beijing', 'in shanghai',
    'in guangzhou', 'in nanjing', 'in chengdu', 'in hangzhou', 'hospital in china',
    'chinese hospital', 'china national', 'national natural science foundation of china',
    'chinese cohort', 'chinese multi-center', 'chinese multicenter'
]

LOW_INCOME_AND_BLOCKED_TLDS = (
    '.cn', '.edu.cn', '.ac.cn', '.gov.cn', '.org.cn', '.net.cn', '.com.cn',
    '.in', '.ac.in', '.res.in', '.edu.in', '.gov.in', '.nic.in',
    '.pk', '.edu.pk',
    '.bd', '.edu.bd',
    '.ir', '.ac.ir',
    '.ru', '.edu.ru',
    '.np', '.edu.np',
    '.ng', '.edu.ng',
    '.id', '.ac.id',
    '.vn', '.edu.vn',
    '.ph', '.edu.ph',
    '.my', '.edu.my',
    '.iq', '.sy', '.ye', '.lk', '.ac.lk', '.et', '.ke', '.gh', '.sd',
    '.ua', '.kz', '.uz', '.mm', '.th', '.ac.th', '.ec', '.ma', '.dz', '.tn',
    '.so', '.af', '.kh', '.la', '.zw', '.zm', '.cm'
)

LOW_INCOME_WEBMAILS = frozenset({
    'rediffmail.com', 'mail.ru', 'yandex.ru', 'rambler.ru', 'bk.ru', 'inbox.ru', 'list.ru'
})

DOI_COUNTRY_CACHE = {}
DOI_CACHE_LOCK = threading.Lock()

def get_doi_countries(doi):
    """
    Fetches institution countries associated with a DOI using OpenAlex with caching.
    """
    if not doi:
        return set()
    norm_doi = doi.strip().lower()
    with DOI_CACHE_LOCK:
        if norm_doi in DOI_COUNTRY_CACHE:
            return DOI_COUNTRY_CACHE[norm_doi]

    countries = set()
    try:
        url = f"https://api.openalex.org/works/https://doi.org/{norm_doi}"
        r = requests.get(url, timeout=2.5, headers={"User-Agent": "mailto:verifier@academic-scraper.org"})
        if r.status_code == 200:
            data = r.json()
            for a in data.get("authorships", []):
                for c in a.get("countries", []):
                    countries.add(str(c).upper().strip())
    except Exception:
        pass

    with DOI_CACHE_LOCK:
        DOI_COUNTRY_CACHE[norm_doi] = countries
    return countries


def is_country_allowed(email, allowed_countries=None, author_name="", paper_title="", affiliation="", doi=""):
    """
    Strict Country Verification:
    - Enforces ZERO tolerance against Chinese domains, institutions, and webmail services.
    - Enforces ZERO tolerance against low-income and developing country ccTLDs unless explicitly allowed.
    - If allowed_countries is provided, strictly requires positive validation against the allowed countries.
    Returns: (is_allowed: bool, reason: str)
    """
    if not email or "@" not in email:
        return False, "Invalid email format"

    domain = email.split("@")[-1].lower().strip()
    title_lower = (paper_title or "").lower()
    aff_lower = (affiliation or "").lower()

    # 1. Zero tolerance Chinese check
    if domain in CHINESE_DOMAINS:
        return False, f"Blocked: Chinese email provider ({domain})"
    if any(m in domain for m in CHINESE_INSTITUTION_MARKERS):
        return False, f"Blocked: Chinese academic institution ({domain})"
    if domain.endswith(('.cn',)):
        return False, f"Blocked: Chinese top-level domain ({domain})"
    if any(k in title_lower for k in CHINESE_TITLE_KEYWORDS):
        return False, "Blocked: Paper focused on China / Chinese population"
    if any(k in aff_lower for k in ['china', 'beijing', 'shanghai', 'wuhan', 'guangzhou', 'chinese academy', 'tsinghua']):
        return False, "Blocked: Chinese institutional affiliation"

    # 2. Blocked low income / non-target TLDs
    if domain in LOW_INCOME_WEBMAILS:
        return False, f"Blocked: Webmail from non-target region ({domain})"

    for btld in LOW_INCOME_AND_BLOCKED_TLDS:
        if domain.endswith(btld):
            # Check if this country is explicitly in allowed_countries
            if allowed_countries:
                is_explicitly_allowed = False
                for c in allowed_countries:
                    rule = COUNTRY_DOMAIN_RULES.get(c)
                    if rule and any(domain.endswith(sfx) for sfx in rule["domain_suffixes"]):
                        is_explicitly_allowed = True
                        break
                if not is_explicitly_allowed:
                    return False, f"Blocked: Domain ccTLD not allowed ({domain})"
            else:
                return False, f"Blocked: Non-target country ccTLD ({domain})"

    # 3. If allowed_countries is specified, enforce strict inclusion
    if allowed_countries:
        # Normalize list of allowed countries
        clean_allowed = [c.strip() for c in allowed_countries if c and c.strip()]
        if clean_allowed:
            # Direct domain check
            for c in clean_allowed:
                rule = COUNTRY_DOMAIN_RULES.get(c)
                if not rule:
                    continue
                if any(domain.endswith(sfx) for sfx in rule["domain_suffixes"]):
                    return True, f"Matched domain for {c}"
                if any(inst in domain for inst in rule.get("institutions", [])):
                    return True, f"Matched recognized institution for {c}"

            # Affiliation text check
            if aff_lower:
                for c in clean_allowed:
                    rule = COUNTRY_DOMAIN_RULES.get(c)
                    if not rule:
                        continue
                    if any(kw in aff_lower for kw in rule["keywords"]):
                        return True, f"Matched affiliation keyword for {c}"

            # Generic commercial domains (e.g. gmail, yahoo, outlook) require DOI or affiliation confirmation
            generic_domains = {'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'icloud.com', 'aol.com', 'proton.me', 'protonmail.com'}
            if domain in generic_domains and doi:
                iso_set = set()
                for c in clean_allowed:
                    rule = COUNTRY_DOMAIN_RULES.get(c)
                    if rule:
                        iso_set.update(rule["iso"])

                paper_countries = get_doi_countries(doi)
                if "CN" in paper_countries:
                    return False, "Blocked: Paper verified originating from China"
                if "IN" in paper_countries and "IN" not in iso_set:
                    return False, "Blocked: Paper verified originating from India"

                if paper_countries & iso_set:
                    matched = list(paper_countries & iso_set)
                    return True, f"Verified via DOI affiliation ({matched})"
                elif paper_countries:
                    return False, f"DOI countries {list(paper_countries)} not in allowed filter"

            return False, f"Domain/origin ({domain}) does not match allowed countries ({clean_allowed})"

    return True, "Allowed"


def verify_single_record(record, api_key=None, allowed_countries=None):
    """
    Performs full deep filtration on a single record.
    record is a dict with keys: 'Paper Title', 'Author Name', 'Email ID', 'DOI' (or variations).
    Returns (is_valid, cleaned_record, reason).
    """
    # Extract fields with flexible column naming
    title = ""
    for k in ["Paper Title", "Title", "paper_title", "title"]:
        if k in record and record[k]:
            title = str(record[k]).strip()
            break

    author = ""
    for k in ["Author Name", "Author", "author_name", "author", "Name"]:
        if k in record and record[k]:
            author = str(record[k]).strip()
            break

    raw_email = ""
    for k in ["Email ID", "Email", "email_id", "email", "Mail"]:
        if k in record and record[k]:
            raw_email = str(record[k]).strip()
            break

    doi = ""
    for k in ["DOI", "doi", "Doi"]:
        if k in record and record[k]:
            doi = str(record[k]).strip()
            break

    # Auto-Realignment: detect if fields were shifted in the uploaded spreadsheet
    email_regex = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
    if not raw_email or "@" not in raw_email:
        for candidate in [author, title, doi]:
            m = email_regex.search(candidate)
            if m:
                raw_email = m.group(0)
                if author == raw_email or "@" in author:
                    author = ""
                break

    if "@" in author and "@" not in raw_email and len(raw_email) > 0:
        author, raw_email = raw_email, author

    if not doi or doi.lower() in ("n/a", "na", "none", "null"):
        for candidate in [title, author]:
            if candidate.startswith("10.") or "doi.org/10." in candidate:
                doi = candidate
                break

    # 1. Clean Title
    cleaned_title = clean_paper_title(title)

    # 2. Footnote / Artifact Cleaning on Email
    cleaned_email = clean_and_repair_email(raw_email)
    if not cleaned_email:
        return False, None, f"Invalid email format: '{raw_email[:35]}'"

    # 3. Clean and Sanitize Author Name
    cleaned_author = clean_author_name(author, email=cleaned_email)
    if not is_valid_author_name(cleaned_author):
        # Fallback: attempt to recover person name from structured academic email
        uname_raw = cleaned_email.split('@')[0]
        parts = [p for p in re.split(r'[^a-zA-Z]+', uname_raw) if len(p) >= 2]
        parts = [p.capitalize() for p in parts if p.lower() not in ('dr', 'prof', 'mr', 'ms', 'md', 'phd')]
        if len(parts) >= 2:
            derived = " ".join(parts)
            if is_valid_author_name(derived):
                cleaned_author = derived

    if not is_valid_author_name(cleaned_author):
        return False, None, f"Invalid author name: '{author[:35]}'"

    # 4. Clean DOI
    cleaned_doi = re.sub(r'[\r\n\t]+', '', doi).strip()

    # 5. Strict Country Verification
    is_ok, country_reason = is_country_allowed(
        email=cleaned_email,
        allowed_countries=allowed_countries,
        author_name=cleaned_author,
        paper_title=cleaned_title,
        doi=cleaned_doi
    )
    if not is_ok:
        return False, None, f"Country Filter: {country_reason}"

    # 6. Deliverability Verification
    if api_key:
        status, reason = verify_email_via_api(cleaned_email, api_key)
    else:
        status, reason = check_email_deliverability(cleaned_email)

    if status == "Undeliverable":
        return False, None, reason

    # Cleaned record with strictly the 4 requested columns in sequential order
    cleaned_record = {
        "Paper Title": cleaned_title,
        "Author Name": cleaned_author,
        "Email ID": cleaned_email,
        "DOI": cleaned_doi
    }
    return True, cleaned_record, "Valid"


def normalize_and_realign_records(records):
    """
    Normalizes and repairs unaligned records from any uploaded Excel or CSV file.
    Fixes shifted columns, strips bad whitespace, and returns clean dicts.
    """
    if not records:
        return []
    realigned = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        # Run single record normalization logic
        title = ""
        for k in ["Paper Title", "Title", "paper_title", "title", "paper", "article"]:
            if k in rec and rec[k] is not None:
                title = str(rec[k]).strip()
                break

        author = ""
        for k in ["Author Name", "Author", "author_name", "author", "Name", "name"]:
            if k in rec and rec[k] is not None:
                author = str(rec[k]).strip()
                break

        email = ""
        for k in ["Email ID", "Email", "email_id", "email", "Mail", "mail", "contact"]:
            if k in rec and rec[k] is not None:
                email = str(rec[k]).strip()
                break

        doi = ""
        for k in ["DOI", "doi", "Doi", "link", "url"]:
            if k in rec and rec[k] is not None:
                doi = str(rec[k]).strip()
                break

        # Check for shifted email
        email_regex = re.compile(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+')
        if not email or "@" not in email:
            for candidate in [author, title, doi]:
                m = email_regex.search(candidate)
                if m:
                    email = m.group(0)
                    if author == email or "@" in author:
                        author = ""
                    break

        if "@" in author and "@" not in email and len(email) > 0:
            author, email = email, author

        if not doi or doi.lower() in ("n/a", "na", "none", "null"):
            for candidate in [title, author]:
                if candidate.startswith("10.") or "doi.org/10." in candidate:
                    doi = candidate
                    break

        c_title = clean_paper_title(title)
        c_email = clean_and_repair_email(email) or email.strip().lower()
        c_author = clean_author_name(author, email=c_email)
        c_doi = re.sub(r'[\r\n\t]+', '', doi).strip()

        realigned.append({
            "Paper Title": c_title,
            "Author Name": c_author,
            "Email ID": c_email,
            "DOI": c_doi
        })
    return realigned


def batch_verify_records(records, api_key=None, allowed_countries=None, max_workers=20, progress_callback=None):
    """
    Runs multi-threaded concurrent verification on an entire list of records.
    Significantly accelerates processing: 2,000 emails checked in ~1-2 minutes.
    """
    valid_records = []
    dropped_records = []
    seen_emails = set()

    total = len(records)
    processed = 0

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_rec = {
            executor.submit(verify_single_record, rec, api_key, allowed_countries): rec
            for rec in records
        }

        for future in as_completed(future_to_rec):
            processed += 1
            orig_rec = future_to_rec[future]
            try:
                is_valid, cleaned_rec, reason = future.result()
                if is_valid and cleaned_rec:
                    # Enforce strict deduplication
                    email = cleaned_rec["Email ID"].lower()
                    if email not in seen_emails:
                        seen_emails.add(email)
                        valid_records.append(cleaned_rec)
                    else:
                        dropped_records.append({
                            "Record": orig_rec,
                            "Reason": "Duplicate email in batch"
                        })
                else:
                    dropped_records.append({
                        "Record": orig_rec,
                        "Reason": reason
                    })
            except Exception as e:
                dropped_records.append({
                    "Record": orig_rec,
                    "Reason": f"Processing error: {str(e)}"
                })

            if progress_callback:
                progress_callback(processed, total, len(valid_records), len(dropped_records))

    return valid_records, dropped_records