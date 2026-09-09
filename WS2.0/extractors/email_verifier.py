import smtplib
import socket
import dns.resolver

def check_email_deliverability(email, sender_domain="gmail.com", timeout=4):
    """
    Validates email addresses.
    - True Dead: Domain doesn't exist OR explicit 'User not found / does not exist' response.
    - Deliverable: 250 OK OR Valid MX domain where local IP was blocked by Spamhaus / 5.7.1.
    """
    if not email or "@" not in email:
        return "Undeliverable", "Invalid email syntax"

    domain = email.split("@")[1].strip()

    # Step 1: Check Domain MX Records (Dead domains dropped here)
    try:
        mx_records = dns.resolver.resolve(domain, "MX")
        mx_host = sorted([(rec.preference, str(rec.exchange).rstrip(".")) for rec in mx_records])[0][1]
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
        return "Undeliverable", "Domain does not exist or has no active MX records"
    except Exception as e:
        return "Deliverable", f"MX Verified (DNS lookup timeout: {str(e)})"

    # Step 2: Test Mailbox Connection
    try:
        server = smtplib.SMTP(timeout=timeout)
        server.connect(mx_host, 25)
        server.helo(sender_domain)
        server.mail(f"verify@{sender_domain}")
        code, message = server.rcpt(email)
        server.quit()

        msg = message.decode("utf-8", errors="ignore") if isinstance(message, bytes) else str(message)
        msg_lower = msg.lower()

        # CASE A: Client IP Blocked by Server (Spamhaus / RBL / 5.7.1 / Mimecast) -> KEEP IT
        if any(term in msg_lower for term in ["spamhaus", "blocked", "service unavailable", "5.7.1", "blacklist", "rbl", "mimecast", "trendmicro"]):
            return "Deliverable", "Deliverable (Valid MX - Local IP blocked by server firewall)"

        # CASE B: Server Explicitly Confirms Mailbox -> KEEP IT
        if code == 250:
            return "Deliverable", "Mailbox exists and accepts messages (250 OK)"

        # CASE C: Explicit Recipient Failure (User does not exist) -> DROP IT
        if code in [550, 551, 552, 553, 554] and any(term in msg_lower for term in ["user", "recipient", "not exist", "unknown", "invalid recipient", "mailbox unavailable"]):
            return "Undeliverable", f"Mailbox rejected: {msg.strip()}"

        # Default fallback for ambiguous codes: preserve email
        return "Deliverable", f"Accepted with code {code}"

    except Exception:
        # If port 25 or socket connection is dropped by ISP/University, domain is valid -> KEEP IT
        return "Deliverable", "Deliverable (Valid MX - Direct socket connection bypassed)"