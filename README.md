# Email Link Crawler — Quarantine Analyzer

Scans emails from any IMAP inbox, extracts all URLs, crawls them, and runs a **quarantine-style threat analysis** simulating how an email security gateway flags suspicious messages.

```
┌─────────────┐     ┌─────────────┐     ┌─────────────────┐     ┌──────────┐
│  IMAP       │────▶│  URL        │────▶│  Quarantine     │────▶│  Report  │
│  Inbox      │     │  Crawler    │     │  Analyzer       │     │  (UI)    │
└─────────────┘     └─────────────┘     └─────────────────┘     └──────────┘
```

## Features

### Email Scanning
- Fetches emails via IMAP (Gmail, Outlook, custom servers)
- Extracts links from both **plain-text** and **HTML** emails
- Supports unread-only mode, custom folders, configurable email count

### URL Crawling
- Concurrent async HTTP crawling with configurable parallelism
- Records status codes, titles, response times, content sizes
- Detects PDFs, file downloads, redirects
- Browser-identical User-Agent (Chrome 109 on Windows)

### Quarantine / Threat Analysis
Simulates an email security gateway with multi-layer scoring:

| Check | Description |
|---|---|
| **Header Analysis** | From vs Reply-To mismatch, Return-Path domain check, X-Originating-IP, X-Mailer spoofing, urgency headers |
| **DNS Authentication** | SPF record lookup, DKIM verification, DMARC policy check (none/quarantine/reject), reverse DNS match, MX records |
| **Subject Line** | 30+ phishing keywords ("urgent", "verify now", "account suspended", etc.) |
| **Body Text** | 15+ regex patterns for social engineering, sensitive data requests, urgency indicators |
| **URL Threats** | Typosquatting dictionary + transposition + homoglyph detection, suspicious TLDs, URL shorteners, IP-in-URL, redirect chains, executable downloads |
| **Cross-Check** | Domain diversity analysis (do From, Reply-To, and all URLs use different domains?) |

**Risk levels:** MINIMAL → LOW → MEDIUM → HIGH → CRITICAL  
**Decisions:** PASS → REVIEW → QUARANTINE → BLOCK

## Quick Start

### Prerequisites

- Python 3.10+
- pip

### Installation

```bash
# Clone the repo
git clone https://github.com/vivek-pk/email-crawler.git
cd email-crawler

# Install dependencies
pip install -r requirements.txt
```

### Run the Web UI (Recommended)

```bash
# Option 1: Using the start script
./start.sh              # runs on port 8500

# Option 2: Directly
python3 server.py       # runs on port 8500
```

Open **http://localhost:8500** in your browser.

### Run from Command Line

```bash
python3 main.py --username you@gmail.com --password your-app-password
```

## Usage

### Web UI Options

| Setting | Description |
|---|---|
| IMAP Server | e.g. `imap.gmail.com` |
| IMAP Port | Usually `993` |
| Email Address | Your email address |
| App Password | Gmail App Password (see below) |
| Folder | Default `INBOX` |
| Max Emails | How many recent emails to scan |
| Concurrent Crawls | Parallel HTTP requests (default 10) |
| Mode | URL Crawl Only / Threat Analysis / Full / DNS-only |

### CLI Commands

```bash
# Full scan — fetch emails, crawl all URLs, run threat analysis
python3 main.py --username you@gmail.com --password xxx --analyze -o report.json

# URL crawl only (no threat analysis)
python3 main.py --username you@gmail.com --password xxx

# Threat analysis only (no URL crawling, faster)
python3 main.py --username you@gmail.com --password xxx --threat --no-lookup

# DNS authentication checks only
python3 main.py --username you@gmail.com --password xxx --dns-check

# Custom IMAP server (Outlook)
python3 main.py --username you@outlook.com --password xxx --imap-server outlook.office365.com --folder Junk

# Save JSON report
python3 main.py --username you@gmail.com --password xxx --analyze -o scan_results.json

# Only scan unread emails
python3 main.py --username you@gmail.com --password xxx --unseen-only
```

### CLI Arguments

| Flag | Description | Default |
|---|---|---|
| `--username` | Email address | *(required)* |
| `--password` | App password / token | *(required)* |
| `--imap-server` | IMAP server host | `imap.gmail.com` |
| `--imap-port` | IMAP server port | `993` |
| `--folder` | IMAP folder to scan | `INBOX` |
| `--max-emails` | Max emails to fetch | `50` |
| `--unseen-only` | Only unread emails | `false` |
| `--max-concurrent` | Max HTTP concurrency | `10` |
| `--timeout` | HTTP timeout (seconds) | `15` |
| `--analyze` | Full scan (crawling + threat) | — |
| `--threat` | Threat analysis only | — |
| `--dns-check` | DNS authentication only | — |
| `--no-lookup` | Skip DNS/URL lookups | `false` |
| `--output` | Save JSON to file | — |
| `--verbose` | Debug logging | `false` |

## Gmail Setup

Gmail blocks standard passwords — you must create an **App Password**:

1. Go to https://myaccount.google.com → **Security**
2. Enable **2-Step Verification** (required)
3. Go to https://myaccount.google.com/apppasswords
4. Create a new app password named "Email Crawler"
5. Copy the 16-character password into the tool

> **Tip:** The UI shows a live log panel so you can see each step as it happens.

## Threat Scoring

Each check adds weighted points to a 0-100 score:

| Severity | Weight | Example |
|---|---|---|
| Critical | 2× | Reply-To mismatch, IP in URL, typosquatting |
| High | 1.5× | Missing DMARC, suspicious TLD, redirect mismatch |
| Medium | 1× | Phishing keywords, HTTP URLs, URL shorteners |
| Low | 1× | X-Mailer hints, urgency headers |

**Risk thresholds:**
- 0-19: **MINIMAL** — PASS
- 20-39: **LOW** — PASS
- 40-59: **MEDIUM** — REVIEW
- 60-79: **HIGH** — QUARANTINE
- 80-100: **CRITICAL** — BLOCK

## Output

### JSON Report

```json
{
  "generated_at": "2026-01-01T12:00:00",
  "emails": [
    {
      "uid": "41142",
      "subject": "Your invoice is attached",
      "sender": "billing@example.com",
      "date": "Mon, 1 Jan 2026 10:00:00 +0000",
      "links": ["http://example.com/invoice.pdf"]
    }
  ],
  "urls": [
    {
      "url": "http://example.com/invoice.pdf",
      "status_code": 200,
      "title": "Invoice January",
      "is_pdf": true,
      "response_time_ms": 342
    }
  ],
  "threat_analysis": [
    {
      "email_uid": "41142",
      "from": "billing@example.com",
      "subject": "Your invoice is attached",
      "threat_score": { "score": 45, "risk_level": "MEDIUM", "factors": [...] },
      "decision": "REVIEW",
      "dns_analysis": { ... },
      "url_analyses": { ... }
    }
  ]
}
```

## File Structure

```
email-crawler/
├── main.py            # Core crawler + threat engine (CLI)
├── server.py          # Web server + UI handler
├── start.sh           # Quick launcher script
├── requirements.txt   # Python dependencies
└── ui/
    └── index.html     # Web interface (dark theme)
```

## IMAP Server Reference

| Provider | Server | Port |
|---|---|---|
| Gmail | `imap.gmail.com` | 993 |
| Outlook / Hotmail | `outlook.office365.com` | 993 |
| Yahoo Mail | `imap.mail.yahoo.com` | 993 |
| iCloud Mail | `imap.mail.me.com` | 993 |
| GMX | `email.gmx.com` | 993 |

## License

MIT
