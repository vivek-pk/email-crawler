import imaplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
import re
import asyncio
import aiohttp
from urllib.parse import urlparse, urljoin
from html.parser import HTMLParser
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set
from datetime import datetime
import socket
import dns.resolver
import dns.reversename
import logging
import argparse
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


# ── Link extractor ──────────────────────────────────────────────

class HTMLLinkExtractor(HTMLParser):
    """Extract all href links from HTML content."""

    def __init__(self):
        super().__init__()
        self.links: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            attrs_dict = dict(attrs)
            href = attrs_dict.get('href', '')
            if href and href != '#' and href != 'javascript:':
                self.links.append(href)


def extract_links_from_html(html_text: str) -> List[str]:
    """Parse HTML and return list of URLs found in href attributes."""
    extractor = HTMLLinkExtractor()
    extractor.feed(html_text)
    return extractor.links


def extract_links_from_text(text: str) -> List[str]:
    """Regex-based URL finder for plain-text emails."""
    url_pattern = re.compile(
        r'https?://[^\s<>\"\')\]],;\]}]+'
        r'(?:[^)>\s\"\']*\([^)]*\))*'
        r'(?:[^,.\s<>\"\']*)',
        re.IGNORECASE
    )
    return url_pattern.findall(text)


def normalize_url(url: str) -> str:
    """Clean and normalise a URL found in an email."""
    url = url.strip().rstrip('.,;:!?)')
    if url.startswith('www.'):
        url = 'https://' + url
    if url and not url.startswith(('http://', 'https://', 'ftp://', 'mailto:')):
        url = 'https://' + url
    # Strip fragment for dedup
    parsed = urlparse(url)
    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return clean


# ── Email fetcher ───────────────────────────────────────────────

@dataclass
class EmailMessage:
    uid: str
    subject: str
    sender: str
    date: str
    body_text: str = ""
    body_html: str = ""
    links: List[str] = field(default_factory=list)
    normalised_links: List[str] = field(default_factory=list)


class EmailCrawler:
    """Connect to IMAP, fetch emails, extract links."""

    def __init__(
        self,
        imap_server: str,
        imap_port: int,
        username: str,
        password: str,
        use_ssl: bool = True,
        folder: str = "INBOX",
        max_emails: int = 50,
        seen_marker: Optional[str] = None,
    ):
        self.imap_server = imap_server
        self.imap_port = imap_port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.folder = folder
        self.max_emails = max_emails
        self.seen_marker = seen_marker  # optional: only fetch unseen

    def connect(self) -> imaplib.IMAP4_SSL:
        logger.info("Connecting to IMAP server %s:%d ...", self.imap_server, self.imap_port)
        conn = imaplib.IMAP4_SSL(self.imap_server, self.imap_port) if self.use_ssl else imaplib.IMAP4(self.imap_server, self.imap_port)
        conn.login(self.username, self.password)
        logger.info("Logged in as %s", self.username)
        return conn

    def _decode_header_value(self, raw: str) -> str:
        """Decode a MIME header value to unicode string."""
        parts = decode_header(raw)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or 'utf-8', errors='replace'))
            else:
                decoded.append(part)
        return ' '.join(decoded)

    def _get_body(self, msg: email.message.Message) -> tuple:
        """Extract plain-text and HTML body from a MIME message."""
        text_body = ""
        html_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get('Content-Disposition', ''))
                if 'attachment' in disposition:
                    continue
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or 'utf-8'
                try:
                    decoded = payload.decode(charset, errors='replace')
                except Exception:
                    decoded = payload.decode('utf-8', errors='replace')
                if content_type == 'text/plain':
                    text_body = decoded
                elif content_type == 'text/html':
                    html_body = decoded
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                charset = msg.get_content_charset() or 'utf-8'
                try:
                    decoded = payload.decode(charset, errors='replace')
                except Exception:
                    decoded = payload.decode('utf-8', errors='replace')
                if content_type == 'text/plain':
                    text_body = decoded
                elif content_type == 'text/html':
                    html_body = decoded
        return text_body, html_body

    def fetch_emails(self) -> List[EmailMessage]:
        conn = self.connect()
        conn.select(self.folder, readonly=True)

        criterion = '(UNSEEN)' if self.seen_marker else '(ALL)'
        status, data = conn.search(None, criterion)
        if status != 'OK':
            logger.warning("Search returned status=%s", status)
            conn.logout()
            return []

        email_ids = data[0].split()
        email_ids = email_ids[-self.max_emails:]  # newest N

        results: List[EmailMessage] = []
        for eid in email_ids:
            try:
                status, msg_data = conn.fetch(eid, '(RFC822)')
                if status != 'OK':
                    continue
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                text_body, html_body = self._get_body(msg)

                text_links = extract_links_from_text(text_body) if text_body else []
                html_links = extract_links_from_html(html_body) if html_body else []
                all_links = list(dict.fromkeys(text_links + html_links))  # dedupe, preserve order

                norm_links = [normalize_url(l) for l in all_links if normalize_url(l)]
                norm_links = list(dict.fromkeys(norm_links))  # dedupe normalised

                em = EmailMessage(
                    uid=eid.decode(),
                    subject=self._decode_header_value(msg.get('Subject', '')),
                    sender=self._decode_header_value(msg.get('From', '')),
                    date=str(msg.get('Date', '')),
                    body_text=text_body[:500],
                    body_html=html_body[:500],
                    links=all_links,
                    normalised_links=norm_links,
                )
                results.append(em)
            except Exception as exc:
                logger.warning("Failed to fetch email %s: %s", eid, exc)

        conn.logout()
        logger.info("Fetched %d email(s) from %s", len(results), self.folder)
        return results


# ── URL crawler ─────────────────────────────────────────────────

@dataclass
class UrlResult:
    url: str
    status_code: int
    title: str
    final_url: str
    redirects: List[str] = field(default_factory=list)
    error: str = ""
    content_length: int = 0
    response_time_ms: float = 0.0
    links_on_page: int = 0
    is_pdf: bool = False
    is_download: bool = False


class UrlCrawler:
    """Asynchronously crawl a list of URLs and collect metadata."""

    def __init__(
        self,
        max_concurrent: int = 10,
        timeout_seconds: int = 15,
        user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
        max_redirects: int = 5,
    ):
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent
        self.max_redirects = max_redirects

    async def crawl_one(self, session: aiohttp.ClientSession, url: str, index: int = 0, total: int = 0) -> UrlResult:
        start = asyncio.get_event_loop().time()
        redirects: List[str] = []
        try:
            logger.info("[CRAWL %d/%d] %s", index, total, url)
            async with session.get(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                headers={'User-Agent': self.user_agent},
            ) as resp:
                history = resp.history
                redirects = [str(h.url) for h in history]
                content_type = resp.headers.get('Content-Type', '')
                is_pdf = 'pdf' in content_type
                content_dispo = resp.headers.get('Content-Disposition', '')
                is_download = 'attachment' in content_dispo or any(
                    ext in url.lower() for ext in ('.pdf', '.doc', '.docx', '.xls', '.xlsx', '.zip', '.exe', '.msi')
                )

                text = await resp.text(errors='replace')
                title = ''
                m = re.search(r'<title[^>]*>(.*?)</title>', text, re.IGNORECASE | re.DOTALL)
                if m:
                    title = m.group(1).strip()

                title_links = extract_links_from_html(text)

                elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000
                logger.info("[CRAWL %d/%d] %s -> %d (%.0fms, %d links, %dKB)",
                           index, total, url, resp.status, elapsed_ms, len(title_links), len(text.encode('utf-8'))//1024)
                return UrlResult(
                    url=url,
                    status_code=resp.status,
                    title=title,
                    final_url=str(resp.url),
                    redirects=redirects,
                    content_length=len(text.encode('utf-8')),
                    response_time_ms=round(elapsed_ms, 1),
                    links_on_page=len(title_links),
                    is_pdf=is_pdf,
                    is_download=is_download,
                )
        except asyncio.TimeoutError:
            return UrlResult(url=url, status_code=0, title='', final_url=url, error='Timeout')
        except aiohttp.ClientError as exc:
            return UrlResult(url=url, status_code=0, title='', final_url=url, error=str(exc))
        except Exception as exc:
            return UrlResult(url=url, status_code=0, title='', final_url=url, error=str(exc))

    async def crawl_all(self, urls: List[str]) -> List[UrlResult]:
        if not urls:
            logger.info("No URLs to crawl.")
            return []

        # Remove duplicates
        urls = list(dict.fromkeys(urls))
        logger.info("Crawling %d unique URL(s) ...", len(urls))

        semaphore = asyncio.Semaphore(self.max_concurrent)
        results: List[UrlResult] = []

        async with aiohttp.ClientSession() as session:
            async def _crawl(url: str, idx: int):
                async with semaphore:
                    return await self.crawl_one(session, url, index=idx, total=len(urls))

            tasks = [_crawl(u, i) for i, u in enumerate(urls, 1)]
            gathered = await asyncio.gather(*tasks)
            results = list(gathered)

        # Sort: errors first, then by status code
        results.sort(key=lambda r: (r.status_code == 0, r.status_code))
        logger.info("Finished crawling %d URL(s) — %d succeeded, %d failed",
                     len(results),
                     sum(1 for r in results if r.status_code),
                     sum(1 for r in results if not r.status_code))
        return results


# ── Threat / Quarantine Analyzer ────────────────────────────────

TYPHOSQUAT_DOMAINS: Dict[str, List[str]] = {
    'login': ['login0', 'log1n', '1ogin', 'log-in', 'loginn', 'logim'],
    'account': ['account0', 'acct', 'aaccount', 'acount', 'ccount'],
    'verify': ['verif', 'verf', 'vrfy', 'ver1fy', 'vefify'],
    'secure': ['secur', 'secure0', 'se cure', 's ecure', 'secur3'],
    'signin': ['signin0', 'sign-in', 'signinn', 's1gnin', 'sgnin'],
    'apple': ['ap ple', 'ap p1e', 'app1e', 'aple', 'appie', 'appIe'],
    'google': ['goggle', 'gooogle', 'goo gle', 'g00gle', 'goo9le'],
    'microsoft': ['microsft', 'm1crosoft', 'microsoft0', 'micr0soft'],
    'amazon': ['arnazon', 'amaz0n', 'amazn', 'amaz0n', 'arnazon'],
    'bank': ['b4nk', 'banc', 'barrk', 'bank0', 'bnak'],
    'paypal': ['paypai', 'paypa1', 'paypal', 'paypal0', 'paypay'],
    'office': ['off1ce', '0ffice', 'office365login', 'offcie'],
    'microsoft': ['microsft', 'm1crosoft', 'm1crosof t'],
    'verify': ['ver1fy', 'verfiy', 'verifiy', 'verfy'],
    'update': ['ud ate', 'up date', 'udpate', 'upddate', 'updat3'],
    'notification': ['notifcation', 'notifiction', 'notifcat ion', 'notif1cation'],
    'password': ['passw0rd', 'pass word', 'passwrd', 'passw0rd', 'pass1word'],
    'reset': ['r eset', 'res et', 'rset', 'rse t', 'r3set'],
}

SUSPICIOUS_TLDS = {
    '.xyz', '.top', '.click', '.link', '.work', '.buzz', '.trade',
    '.review', '.online', '.site', '.store', '.club', '.date',
    '.icu', '.gq', '.ml', '.ga', '.cf', '.tk', '.pw', '.cc', '.su',
}

PHISHING_SUBJECT_KEYWORDS = [
    'urgent', 'action required', 'immediate', 'alert', 'verify now',
    'suspended', 'terminated', 'restricted', 'confirm immediately',
    'your account will be closed', 'final notice', 'last warning',
    'unusual activity', 'unauthorized access', 'fraudulent',
    'payment failed', 'invoice overdue', 'invoice attached',
    'wire transfer', 'gift card', 'bitcoin', 'crypto',
    'refund', 'claim now', 'you have won', 'congratulations',
    'limited time', 'expires in', 'act now', 'act immediately',
]

SUSPICIOUS_BODY_PATTERNS = [
    r'click\s+(here|this\s+link)',
    r'verify\s+(your\s+)?(account|email|identity)',
    r'confirm\s+(your\s+)?(account|password|details)',
    r'update\s+(your\s+)?(password|payment|billing)',
    r'your\s+account\s+(has\s+been|will\s+be|is\s+restricted)',
    r'unusual\s+(login|activity|access)',
    r'password\s+(reset|expired|update)',
    r'payment\s+(failed|issue|dispute)',
    r'unauthorized\s+(access|transaction|login)',
    r'send\s+(us|me|them)\s+(your\s+)?(password|code|verification)',
    r'submit\s+(your\s+)?(details|information|documents)',
    r'wire\s+(transfer|payment)',
    r'gift\s+(card|cards)',
    r'bitcoin|crypto|cryptocurrency',
]

class EmailThreatScore:
    """Single-email threat assessment."""
    def __init__(self):
        self.score: int = 0
        self.max_score: int = 100
        self.factors: List[Dict[str, object]] = []

    def add(self, factor: str, severity: str, delta: int, detail: str = ""):
        self.factors.append({
            'factor': factor,
            'severity': severity,
            'delta': delta,
            'detail': detail,
        })
        if severity == 'critical':
            self.score += delta * 2
        elif severity == 'high':
            self.score += delta * 1.5
        else:
            self.score += delta
        self.score = min(self.score, self.max_score)

    @property
    def risk_level(self) -> str:
        if self.score >= 80:
            return 'CRITICAL'
        elif self.score >= 60:
            return 'HIGH'
        elif self.score >= 40:
            return 'MEDIUM'
        elif self.score >= 20:
            return 'LOW'
        return 'MINIMAL'

    def to_dict(self) -> Dict:
        return {
            'score': self.score,
            'risk_level': self.risk_level,
            'factors': self.factors,
        }


class TyposquatChecker:
    """Detect domain typosquatting / look-alike attacks."""

    def __init__(self):
        self._known: Dict[str, List[str]] = {}
        for word, variants in TYPHOSQUAT_DOMAINS.items():
            self._known[word] = variants

    def check(self, domain: str) -> List[Dict[str, str]]:
        hits: List[Dict[str, str]] = []
        domain_lower = domain.lower().rstrip('.')
        # Direct dictionary lookup
        for base, variants in self._known.items():
            if base in domain_lower:
                for v in variants:
                    if v in domain_lower:
                        hits.append({
                            'type': 'typosquat',
                            'detail': f"Contains known variant of '{base}' -> '{v}'",
                            'domain': domain,
                        })
        # Transposition (swap adjacent chars)
        if len(domain_lower) >= 4:
            for i in range(len(domain_lower) - 1):
                swapped = domain_lower[:i] + domain_lower[i+1] + domain_lower[i] + domain_lower[i+2:]
                if swapped != domain_lower and '.' in swapped:
                    hits.append({
                        'type': 'transposition',
                        'detail': f"Possible transposition of '{domain_lower}' -> '{swapped}'",
                        'domain': domain,
                    })
        # Homoglyph substitution (0/o, 1/l/i, etc.)
        homograph_map = {'0': 'o', '1': 'l', '3': 'e', '5': 's', '7': 't', '8': 'b', '9': 'g'}
        trans = domain_lower.translate(str.maketrans(homograph_map))
        if trans != domain_lower and any(k in domain_lower for k in homograph_map):
            hits.append({
                'type': 'homograph',
                'detail': f"Potential homoglyph — decoded form: '{trans}'",
                'domain': domain,
            })
        # Remove duplicates
        seen: set = set()
        unique = []
        for h in hits:
            key = (h['type'], h['detail'])
            if key not in seen:
                seen.add(key)
                unique.append(h)
        return unique


class DnsChecker:
    """DNS-based checks: SPF, DKIM, DMARC hints, reverse DNS, MX record."""

    def __init__(self):
        self._timeout = 3

    def check_spf(self, domain: str) -> Dict[str, object]:
        result = {'domain': domain, 'passed': False, 'record': '', 'status': 'unknown'}
        try:
            records = dns.resolver.resolve(domain, 'TXT', lifetime=self._timeout)
            for rdata in records:
                rec = str(rdata).strip('"')
                if rec.startswith('v=spf1'):
                    result['record'] = rec
                    result['passed'] = '-all' not in rec and '~all' in rec
                    break
        except Exception:
            result['status'] = 'no-txt-records'
        return result

    def check_dkim(self, domain: str) -> Dict[str, object]:
        result = {'domain': domain, 'passed': False, 'status': 'unknown'}
        try:
            records = dns.resolver.resolve(domain, 'TXT', lifetime=self._timeout)
            for rdata in records:
                rec = str(rdata).strip('"')
                if rec.startswith('v=DKIM1'):
                    result['passed'] = True
                    break
        except Exception:
            result['status'] = 'no-dkim'
        return result

    def check_dmarc(self, domain: str) -> Dict[str, object]:
        result = {'domain': domain, 'passed': False, 'policy': '', 'status': 'unknown'}
        try:
            records = dns.resolver.resolve(f'_dmarc.{domain}', 'TXT', lifetime=self._timeout)
            for rdata in records:
                rec = str(rdata).strip('"')
                if rec.startswith('v=DMARC1'):
                    result['passed'] = True
                    if 'p=none' in rec:
                        result['policy'] = 'none (p=none — not enforced)'
                    elif 'p=quarantine' in rec:
                        result['policy'] = 'quarantine'
                    elif 'p=reject' in rec:
                        result['policy'] = 'reject'
                    break
        except Exception:
            result['status'] = 'no-dmarc-record'
        return result

    def check_reverse_dns(self, domain: str) -> Dict[str, object]:
        result = {'domain': domain, 'matched': False, 'rdns': ''}
        try:
            ips = dns.resolver.resolve(domain, 'A', lifetime=self._timeout)
            for ip in ips:
                rdns_name = dns.reversename.from_address(ip.address)
                rdns = dns.resolver.resolve(rdns_name, 'PTR', lifetime=self._timeout)[0].to_text().rstrip('.')
                if domain in rdns:
                    result['matched'] = True
                result['rdns'] = rdns
        except Exception:
            pass
        return result

    def check_mx(self, domain: str) -> bool:
        try:
            dns.resolver.resolve(domain, 'MX', lifetime=self._timeout)
            return True
        except Exception:
            return False

    def run_checks(self, domain: str) -> Dict[str, object]:
        return {
            'spf': self.check_spf(domain),
            'dkim': self.check_dkim(domain),
            'dmarc': self.check_dmarc(domain),
            'reverse_dns': self.check_reverse_dns(domain),
            'has_mx': self.check_mx(domain),
        }


class UrlThreatAnalyzer:
    """Analyze individual URLs for threat indicators."""

    def __init__(self):
        self.typosquat = TyposquatChecker()

    def analyze(self, url: str, redirect_chain: List[str] = None) -> EmailThreatScore:
        score = EmailThreatScore()
        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower().lstrip('www.').rstrip('/')
            scheme = parsed.scheme.lower()
            path = parsed.path.lower()
        except Exception:
            score.add('parse_error', 'high', 15, f'Could not parse URL: {url}')
            return score

        # 1. Scheme check
        if scheme == 'http':
            score.add('insecure_protocol', 'medium', 10, 'URL uses HTTP (not HTTPS)')

        # 2. Long / suspicious path
        if len(path) > 100:
            score.add('long_path', 'medium', 5, f'URL path is {len(path)} chars')
        if len(parsed.geturl()) > 200:
            score.add('long_url', 'medium', 5, f'Full URL is {len(parsed.geturl())} chars')

        # 3. IP address in URL
        if netloc and re.match(r'^\d{1,3}(\.\d{1,3}){3}', netloc):
            score.add('ip_in_url', 'high', 15, 'URL points directly to an IP address')

        # 4. IP in path
        if re.search(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', path):
            score.add('ip_in_path', 'medium', 10, 'IP address found in URL path')

        # 5. Typosquatting
        typos = self.typosquat.check(netloc)
        for t in typos:
            sev = 'critical' if t['type'] == 'typosquat' else 'high'
            score.add(t['type'], sev, 20, t['detail'])

        # 6. Suspicious TLD
        for tld in SUSPICIOUS_TLDS:
            if netloc.endswith(tld):
                score.add('suspicious_tld', 'medium', 10, f'Domain uses suspicious TLD: {tld}')
                break

        # 7. Too many subdomains
        parts = netloc.split('.')
        if len(parts) > 4:
            score.add('deep_subdomains', 'medium', 8, f'Domain has {len(parts)-1} subdomain levels')

        # 8. URL shortener detection
        shorteners = {'bit.ly', 'tinyurl.com', 't.co', 'short.link', 'ow.ly', 'is.gd', 'cutt.us'}
        for s in shorteners:
            if s in netloc:
                score.add('url_shortener', 'medium', 12, f'Uses URL shortener: {s}')
                break

        # 9. Encoding tricks
        if '%' in netloc and not netloc.startswith('xn--'):
            score.add('url_encoding', 'medium', 8, 'URL contains percent-encoding in hostname')

        # 10. Redirect chain analysis
        if redirect_chain:
            if len(redirect_chain) > 3:
                score.add('deep_redirects', 'high', 15, f'URL redirects {len(redirect_chain)} times')
            else:
                score.add('redirect', 'low', 5, f'URL redirects {len(redirect_chain)} time(s)')
            # Check if final domain differs
            if redirect_chain:
                final = urlparse(redirect_chain[-1]).netloc.lower()
                if final and final != netloc:
                    score.add('redirect_domain_mismatch', 'high', 18,
                               f'Original: {netloc} -> Final: {final}')

        # 11. File download indicators
        dl_exts = {'.exe', '.bat', '.cmd', '.scr', '.vbs', '.js', '.msi', '.dll', '.com', '.pif',
                    '.hta', '.ws', '.wsh', '.ps1', '.jar'}
        for ext in dl_exts:
            if path.endswith(ext):
                score.add('dangerous_extension', 'critical', 20, f'URL points to executable: {ext}')
                break

        return score


class QuarantineAnalyzer:
    """
    Full email threat analysis — simulates how a quarantine / email security
    gateway scans and scores incoming messages.

    Checks performed:
      - Header analysis: From/Reply-To mismatch, missing/invalid headers,
        X-Originating-IP analysis, return-path check
      - SPF / DKIM / DMARC validation via DNS
      - Subject-line phishing keyword detection
      - Body text social-engineering pattern detection
      - URL threat analysis (typosquatting, shorteners, IP URLs, redirects)
      - Attachment risk indicators
      - Overall quarantine decision (quarantine / approve / block / review)
    """

    def __init__(self, verbose: bool = False):
        self.dns = DnsChecker()
        self.url_analyzer = UrlThreatAnalyzer()
        self.verbose = verbose

    def _log(self, msg: str):
        if self.verbose:
            logger.info("[QUARANTINE] %s", msg)

    def analyze_email(self, msg: email.message.Message, em: EmailMessage) -> Dict:
        """Run full threat analysis on a single email."""
        overall = EmailThreatScore()
        header_analysis = {}
        dns_analysis: Dict[str, Dict] = {}
        url_analyses: Dict[str, Dict] = {}
        body_analysis = {}

        # ── 1. Header analysis ───────────────────────────────────────
        from_addr = self._decode_header(msg.get('From', ''))
        reply_to = self._decode_header(msg.get('Reply-To', ''))
        return_path = self._decode_header(msg.get('Return-Path', ''))
        subject = self._decode_header(msg.get('Subject', ''))

        header_analysis['from'] = from_addr
        header_analysis['reply_to'] = reply_to
        header_analysis['return_path'] = return_path
        header_analysis['subject'] = subject

        # From vs Reply-To mismatch
        if reply_to and from_addr and reply_to != from_addr:
            overall.add('from_reply_mismatch', 'critical', 20,
                        f"Reply-To ({reply_to}) differs from From ({from_addr})")

        # Return-Path check
        if return_path and from_addr:
            rp_domain = return_path.split('@')[-1].lower() if '@' in return_path else ''
            from_domain = from_addr.split('@')[-1].lower() if '@' in from_addr else ''
            if rp_domain and from_domain and rp_domain != from_domain:
                overall.add('return_path_mismatch', 'high', 15,
                             f"Return-Path domain ({rp_domain}) != From domain ({from_domain})")

        # Missing standard headers
        for h in ['Date', 'Message-ID']:
            if not msg.get(h):
                overall.add('missing_header', 'medium', 5, f'Missing header: {h}')

        # X-Originating-IP
        x_orig = msg.get('X-Originating-IP', '')
        if x_orig:
            x_orig_clean = x_orig.split('[')[-1].rstrip(']') if '[' in x_orig else x_orig
            if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', x_orig_clean):
                header_analysis['originating_ip'] = x_orig_clean
                # Check if IP is in private range
                parts = x_orig_clean.split('.')
                if parts[0] in ('10', '192', '172') or (parts[0] == '127' and parts[1] == '0'):
                    overall.add('private_ip_in_header', 'high', 12,
                                 f"Email claims originating IP is private: {x_orig_clean}")

        # X-Mailer detection
        x_mailer = msg.get('X-Mailer', '')
        if x_mailer:
            header_analysis['x_mailer'] = self._decode_header(x_mailer)
            suspicious_mailers = ['bmail', 'phpmailer', 'swiftmailer', 'javamail']
            for s in suspicious_mailers:
                if s.lower() in x_mailer.lower():
                    overall.add('suspicious_mailer', 'low', 5,
                                 f"X-Mailer contains '{s}'")
                    break

        # Priority / urgency in headers
        priority = msg.get('Priority', '')
        urgency = msg.get('X-Priority', '')
        if priority or urgency:
            header_analysis['priority'] = f"{priority}/{urgency}"
            if 'urgent' in priority.lower() or '1' in urgency or 'high' in urgency.lower():
                overall.add('header_urgency', 'low', 3, 'High urgency header present')

        # ── 2. DNS / Authentication checks (on From domain) ──────────
        from_domain = from_addr.split('@')[-1].lower() if '@' in from_addr else ''
        if from_domain and from_domain != '':
            self._log(f"Running DNS checks for domain: {from_domain}")
            dns_analysis = self.dns.run_checks(from_domain)

            if dns_analysis['spf']['status'] == 'unknown':
                overall.add('spf_missing', 'high', 15, 'No SPF record found for sender domain')

            if dns_analysis['dkim']['status'] == 'unknown':
                overall.add('dkim_missing', 'medium', 10, 'No DKIM record found for sender domain')

            if dns_analysis['dmarc']['status'] == 'unknown':
                overall.add('dmarc_missing', 'high', 15, 'No DMARC record found for sender domain')

            if not dns_analysis['has_mx']:
                overall.add('no_mx_record', 'critical', 20, f'Sender domain {from_domain} has no MX record')

            rdns = dns_analysis['reverse_dns']
            if rdns['rdns'] and not rdns['matched']:
                overall.add('rdns_mismatch', 'medium', 8,
                             f"Reverse DNS for sender does not match: {rdns['rdns']}")

        # ── 3. Subject line analysis ─────────────────────────────────
        if subject:
            subject_lower = subject.lower()
            phishing_hits = []
            for kw in PHISHING_SUBJECT_KEYWORDS:
                if kw in subject_lower:
                    phishing_hits.append(kw)
            if phishing_hits:
                overall.add('phishing_subject', 'high', 15,
                             f"Subject contains phishing keywords: {', '.join(phishing_hits)}")
            body_analysis['subject'] = subject
            body_analysis['phishing_keywords'] = phishing_hits

        # ── 4. Body text analysis ────────────────────────────────────
        body_text = (em.body_text or '') + ' ' + (em.body_html or '')
        body_text_lower = body_text.lower()
        body_hits = []
        for pattern in SUSPICIOUS_BODY_PATTERNS:
            if re.search(pattern, body_text_lower, re.IGNORECASE):
                body_hits.append(pattern)
        if body_hits:
            overall.add('suspicious_body_patterns', 'medium', 15,
                         f"Body matches {len(body_hits)} suspicious pattern(s)")
        body_analysis['suspicious_patterns'] = len(body_hits)
        body_analysis['suspicious_pattern_count'] = len(body_hits)

        # Check for email address requests
        if re.search(r'(send\s+me|reply\s+with|forward\s+this)\s+(.*)(password|code|pin|ssn|social\s+security|account\s+number|bank)', body_text_lower, re.IGNORECASE):
            overall.add('data_harvest', 'critical', 25, 'Body requests sensitive personal information')

        # Excessive urgency
        urgency_words = [r'immediately', r'within\s+24\s+hours', r'right\s+now', r'as\s+soon\s+as\s+possible',
                         r'before\s+you\s+lose', r'act\s+now', r'do\s+not\s+delay']
        urgency_count = sum(1 for u in urgency_words if re.search(u, body_text_lower))
        if urgency_count >= 2:
            overall.add('excessive_urgency', 'medium', 10, f"Body contains {urgency_count} urgency indicators")

        # ── 5. URL threat analysis ───────────────────────────────────
        url_analyses: Dict[str, Dict] = {}
        if em.normalised_links:
            self._log(f"Analyzing {len(em.normalised_links)} URL(s)...")
            for url in em.normalised_links:
                score = self.url_analyzer.analyze(url)
                url_analyses[url] = score.to_dict()
                overall_score = score.score
                if overall_score >= 50:
                    overall.add('threat_url', 'critical', 10,
                                f'URL scored {overall_score}/100: {url[:100]}')
                elif overall_score >= 30:
                    overall.add('suspicious_url', 'medium', 5,
                                f'URL scored {overall_score}/100: {url[:100]}')

        # ── 6. Sender domain risk ────────────────────────────────────
        if from_domain:
            for tld in SUSPICIOUS_TLDS:
                if from_domain.endswith(tld):
                    overall.add('suspicious_sender_tld', 'medium', 12,
                                 f'Sender domain uses suspicious TLD: {tld}')
                    break

        # ── 7. Cross-check: domain mismatch across all fields ─────────
        domains_found: Set[str] = set()
        domains_found.add(from_domain)
        if reply_to:
            rt_domain = reply_to.split('@')[-1].lower() if '@' in reply_to else ''
            domains_found.add(rt_domain)
        if return_path:
            rp_d = return_path.split('@')[-1].lower() if '@' in return_path else ''
            domains_found.add(rp_d)
        for url in em.normalised_links:
            try:
                d = urlparse(url).netloc.lower().lstrip('www.').rstrip('/')
                if d:
                    domains_found.add(d)
            except Exception:
                pass

        if len(domains_found) > 1 and None in domains_found:
            domains_found.discard(None)
        if len(domains_found) > 1 and '' in domains_found:
            domains_found.discard('')
        if len(domains_found) > 2:
            overall.add('domain_diversity', 'medium', 8,
                         f"Email references {len(domains_found)} distinct domains")

        # ── 8. Final assessment ──────────────────────────────────────
        risk = overall.risk_level
        if risk == 'CRITICAL':
            decision = 'BLOCK'
        elif risk == 'HIGH':
            decision = 'QUARANTINE'
        elif risk == 'MEDIUM':
            decision = 'REVIEW'
        else:
            decision = 'PASS'

        result = {
            'email_uid': em.uid,
            'from': from_addr,
            'subject': subject,
            'threat_score': overall.to_dict(),
            'decision': decision,
            'header_analysis': header_analysis,
            'dns_analysis': dns_analysis,
            'url_analyses': url_analyses,
            'body_analysis': body_analysis,
            'recommendation': self._recommendation(decision, overall),
        }

        self._log(f"Email '{subject[:50]}' -> Score: {overall.score}/100 | Risk: {risk} | Decision: {decision}")
        return result

    def _recommendation(self, decision: str, overall: EmailThreatScore) -> str:
        recs: List[str] = []
        for f in overall.factors:
            if f['severity'] in ('critical', 'high'):
                recs.append(f"{f['factor']}: {f['detail']}")
        if not recs:
            return 'No immediate action required.'
        return 'Flags: ' + '; '.join(recs[:5])

    def _decode_header(self, raw: str) -> str:
        return decode_header(raw)[0][0] if raw else ''

    def analyze_emails_batch(self, emails: List[EmailMessage]) -> List[Dict]:
        """Run full quarantine analysis on a list of emails."""
        results = []
        for em in emails:
            try:
                # Sanitize header values to prevent newline injection
                def safe(s):
                    return s.replace('\n', ' ').replace('\r', '') if s else ''

                from email.message import EmailMessage as EM
                em_msg = EM()
                em_msg['From'] = safe(em.sender)
                em_msg['Subject'] = safe(em.subject)
                em_msg['Date'] = safe(em.date)
                em_msg['Reply-To'] = safe(em.sender)
                result = self.analyze_email(em_msg, em)
                results.append(result)
            except Exception as exc:
                logger.warning("Threat analysis failed for email %s: %s", em.uid, exc)
                results.append({'error': str(exc), 'uid': em.uid})
        return results


# ── Report / output ─────────────────────────────────────────────

def format_quarantine_report(analyses: List[Dict]):
    """Print quarantine-style analysis report."""
    print("\n" + "=" * 80)
    print("  QUARANTINE / THREAT ANALYSIS REPORT")
    print("  Generated:", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    print("=" * 80)

    # Sort by score descending
    analyses.sort(key=lambda a: a.get('threat_score', {}).get('score', 0), reverse=True)

    for a in analyses:
        score_info = a.get('threat_score', {})
        score = score_info.get('score', 0)
        risk = score_info.get('risk_level', 'UNKNOWN')
        decision = a.get('decision', 'N/A')
        subject = a.get('subject', 'N/A')
        from_addr = a.get('from', 'N/A')

        print(f"\n{'─' * 70}")
        print(f"  [{decision}] {subject[:60]}")
        print(f"  From:  {from_addr}")
        print(f"  Score: {score}/100  |  Risk: {risk}")
        print(f"  Recommendation: {a.get('recommendation', 'N/A')}")

        # Flags
        factors = score_info.get('factors', [])
        if factors:
            print(f"  Flags ({len(factors)}):")
            for f in factors[:8]:
                sev_icon = {'critical': '🔴', 'high': '🟠', 'medium': '🟡', 'low': '🟢'}.get(f['severity'], '⚪')
                print(f"    {sev_icon} [{f['severity'].upper():8}] {f['factor']}: {f['detail']}")

        # DNS results
        dns = a.get('dns_analysis', {})
        if dns:
            spf = dns.get('spf', {})
            dkim = dns.get('dkim', {})
            dmarc = dns.get('dmarc', {})
            print(f"\n  Authentication (DNS):")
            spf_status = spf.get('status', 'unknown')
            dkim_status = dkim.get('status', 'unknown')
            dmarc_status = dmarc.get('status', 'unknown')
            print(f"    SPF:  {spf_status}{' — ' + spf.get('record','') if spf.get('record') else ''}")
            print(f"    DKIM: {dkim_status}")
            print(f"    DMARC: {dmarc_status}{' — ' + dmarc.get('policy','') if dmarc.get('policy') else ''}")

        # URL scores
        url_scores = a.get('url_analyses', {})
        if url_scores:
            print(f"\n  URL Threat Scores:")
            for url, uinfo in list(url_scores.items())[:5]:
                uscore = uinfo.get('score', 0)
                ucolor = '🔴' if uscore >= 50 else '🟠' if uscore >= 30 else '🟡' if uscore >= 15 else '🟢'
                print(f"    {ucolor} {uscore:3d}/100  {url[:80]}")
                for f in uinfo.get('factors', [])[:2]:
                    print(f"        → {f['factor']}: {f['detail']}")

    # Overall summary
    total = len(analyses)
    if total == 0:
        return
    decisions = {}
    scores = []
    for a in analyses:
        d = a.get('decision', 'UNKNOWN')
        decisions[d] = decisions.get(d, 0) + 1
        s = a.get('threat_score', {}).get('score', 0)
        scores.append(s)

    print(f"\n{'=' * 80}")
    print("  QUARANTINE SUMMARY")
    print("=" * 80)
    print(f"  Total emails analyzed : {total}")
    print(f"  Average threat score  : {sum(scores) / total:.1f}/100")
    print(f"  Highest score         : {max(scores)}/100")
    print(f"  Lowest score          : {min(scores)}/100")
    print(f"\n  Decisions:")
    for d in ['BLOCK', 'QUARANTINE', 'REVIEW', 'PASS']:
        count = decisions.get(d, 0)
        if count:
            print(f"    {d:<12}: {count}")
    print("=" * 80)


def format_combined_report(emails: List[EmailMessage], results: List[UrlResult],
                           analyses: List[Dict]):
    """Print both URL crawl results and threat analysis."""
    format_quarantine_report(analyses)
    print("\n" + "-" * 80)
    print("  URL CRAWL RESULTS")
    print("-" * 80)
    header = f"{'#':<4} {'Status':<7} {'Time':<8} {'Size':>8}  {'PDF':<4} {'Download':<10} URL"
    print(header)
    print("-" * 80)
    for idx, r in enumerate(results, 1):
        status = str(r.status_code) if r.status_code else "ERR"
        time_ms = f"{r.response_time_ms:.0f}ms"
        size_kb = f"{r.content_length / 1024:.1f}kb" if r.content_length else "N/A"
        pdf_flag = "YES" if r.is_pdf else "no"
        dl_flag = "YES" if r.is_download else "no"
        print(f"{idx:<4} {status:<7} {time_ms:<8} {size_kb:>8}  {pdf_flag:<4} {dl_flag:<10} {r.url}")
        if r.title:
            print(f"       Title: {r.title[:60]}")

    ok = [r for r in results if r.status_code >= 200 and r.status_code < 400]
    redirects = [r for r in results if r.redirects]
    pdfs = [r for r in results if r.is_pdf]
    slow = [r for r in results if r.response_time_ms > 5000]
    print("\n" + "=" * 80)
    print("  URL CRAWL SUMMARY")
    print("=" * 80)
    print(f"  Total URLs crawled      : {len(results)}")
    print(f"  Successful (2xx/3xx)    : {len(ok)}")
    print(f"  Errors                  : {len(results) - len(ok)}")
    print(f"  With redirects          : {len(redirects)}")
    print(f"  PDFs                    : {len(pdfs)}")
    print(f"  Downloads               : {sum(1 for r in results if r.is_download)}")
    print(f"  Slow (>5s)              : {len(slow)}")
    if slow:
        print("  Slow URLs:")
        for r in slow:
            print(f"    - {r.url} ({r.response_time_ms:.0f}ms)")
    print("=" * 80)


def save_json(emails: List[EmailMessage], results: List[UrlResult],
              analyses: List[Dict], filepath: str):
    """Serialize results to JSON."""
    data = {
        'generated_at': datetime.now().isoformat(),
        'emails': [],
        'urls': [],
        'threat_analysis': [],
    }
    for em in emails:
        data['emails'].append({
            'uid': em.uid,
            'subject': em.subject,
            'sender': em.sender,
            'date': em.date,
            'links': em.normalised_links,
        })
    for r in results:
        data['urls'].append({
            'url': r.url,
            'status_code': r.status_code,
            'title': r.title,
            'final_url': r.final_url,
            'redirects': r.redirects,
            'error': r.error,
            'content_length': r.content_length,
            'response_time_ms': r.response_time_ms,
            'links_on_page': r.links_on_page,
            'is_pdf': r.is_pdf,
            'is_download': r.is_download,
        })
    for a in analyses:
        data['threat_analysis'].append(a)
    with open(filepath, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Results saved to %s", filepath)


# ── Main ────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description='Email Link Crawler — fetch emails, extract links, crawl URLs'
    )
    # IMAP
    parser.add_argument('--imap-server', default='imap.gmail.com')
    parser.add_argument('--imap-port', type=int, default=993)
    parser.add_argument('--username', required=True, help='Email address')
    parser.add_argument('--password', required=True, help='App password / OAuth token')
    parser.add_argument('--folder', default='INBOX')
    parser.add_argument('--max-emails', type=int, default=50, help='Max emails to fetch')
    parser.add_argument('--unseen-only', action='store_true', help='Only fetch unread emails')
    # Crawling
    parser.add_argument('--max-concurrent', type=int, default=10, help='Max concurrent HTTP requests')
    parser.add_argument('--timeout', type=int, default=15, help='HTTP request timeout in seconds')
    parser.add_argument('--max-redirects', type=int, default=5)
    # Threat analysis
    parser.add_argument('--threat', '-t', action='store_true', help='Run quarantine/threat analysis')
    parser.add_argument('--analyze', '-a', action='store_true', help='Run both URL crawl + threat analysis')
    parser.add_argument('--dns-check', action='store_true', help='Only run DNS/authentication checks')
    parser.add_argument('--no-lookup', action='store_true', help='Skip DNS/URL lookups (faster, offline mode)')
    # Output
    parser.add_argument('--output', '-o', help='Save JSON report to file')
    parser.add_argument('--verbose', '-v', action='store_true', help='Verbose logging')
    return parser.parse_args()


async def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 1. Fetch emails and extract links
    email_crawler = EmailCrawler(
        imap_server=args.imap_server,
        imap_port=args.imap_port,
        username=args.username,
        password=args.password,
        folder=args.folder,
        max_emails=args.max_emails,
        seen_marker='UNSEEN' if args.unseen_only else None,
    )

    emails = email_crawler.fetch_emails()
    if not emails:
        print("No emails found. Exiting.")
        return

    # 2. Collect all unique URLs
    all_urls: List[str] = []
    for em in emails:
        all_urls.extend(em.normalised_links)

    all_urls = list(dict.fromkeys(all_urls))

    # Determine mode
    do_threat = args.threat or args.analyze
    do_dns = args.dns_check or args.analyze
    do_crawl = args.analyze or (not args.threat and all_urls and not args.no_lookup)
    do_offline_threat = args.threat and args.no_lookup

    if do_threat or do_dns:
        # ── Threat / Quarantine Analysis ─────────────────────────────
        print("\n" + "=" * 80)
        print("  RUNNING QUARANTINE / THREAT ANALYSIS")
        print("=" * 80)

        quar = QuarantineAnalyzer(verbose=args.verbose)

        if do_dns and all_urls:
            # DNS checks on all domains found in emails
            all_domains: Set[str] = set()
            for em in emails:
                from_addr = em.sender.split('@')[-1].lower() if '@' in em.sender else ''
                if from_addr:
                    all_domains.add(from_addr)
                for url in em.normalised_links:
                    try:
                        d = urlparse(url).netloc.lower().lstrip('www.').rstrip('/')
                        if d:
                            all_domains.add(d)
                    except Exception:
                        pass

            if all_domains:
                print(f"\n  Checking DNS for {len(all_domains)} domain(s)...")
                for domain in sorted(all_domains):
                    print(f"\n  Domain: {domain}")
                    checks = quar.dns.run_checks(domain)
                    spf = checks['spf']
                    dkim = checks['dkim']
                    dmarc = checks['dmarc']
                    print(f"    SPF:    {spf.get('status', 'unknown')}{' — ' + spf.get('record', '') if spf.get('record') else ''}")
                    print(f"    DKIM:   {dkim.get('status', 'unknown')}")
                    print(f"    DMARC:  {dmarc.get('status', 'unknown')}{' — ' + dmarc.get('policy', '') if dmarc.get('policy') else ''}")
                    rdns = checks['reverse_dns']
                    if rdns['rdns']:
                        match_str = "✓ match" if rdns['matched'] else "✗ no match"
                        print(f"    rDNS:   {rdns['rdns']} ({match_str})")
                    mx_str = "✓ has MX" if checks['has_mx'] else "✗ no MX"
                    print(f"    MX:     {mx_str}")

        if do_threat:
            # Full email threat analysis
            print(f"\n  Analyzing {len(emails)} email(s) for threats...")
            analyses = quar.analyze_emails_batch(emails)
            format_quarantine_report(analyses)

            if do_crawl and all_urls:
                # Also crawl URLs
                url_crawler = UrlCrawler(
                    max_concurrent=args.max_concurrent,
                    timeout_seconds=args.timeout,
                    max_redirects=args.max_redirects,
                )
                results = await url_crawler.crawl_all(all_urls)
                # Try to merge URL results back into analyses
                # (simplified: just print both reports)
                print("\n\n")
                format_combined_report(emails, results, analyses)

                if args.output:
                    save_json(emails, results, analyses, args.output)
                    print(f"\nFull JSON report saved to: {args.output}")
            else:
                if args.output:
                    save_json(emails, [], analyses, args.output)
                    print(f"\nFull JSON report saved to: {args.output}")
        elif do_crawl and all_urls:
            # URL crawl only, no threat analysis
            url_crawler = UrlCrawler(
                max_concurrent=args.max_concurrent,
                timeout_seconds=args.timeout,
                max_redirects=args.max_redirects,
            )
            results = await url_crawler.crawl_all(all_urls)
            format_combined_report(emails, results, analyses if do_threat else [])
            if args.output:
                save_json(emails, results, analyses if do_threat else [], args.output)
                print(f"\nFull JSON report saved to: {args.output}")

    elif do_crawl and all_urls:
        # Original mode: URL crawl only
        url_crawler = UrlCrawler(
            max_concurrent=args.max_concurrent,
            timeout_seconds=args.timeout,
            max_redirects=args.max_redirects,
        )
        results = await url_crawler.crawl_all(all_urls)
        format_report(emails, results)
        if args.output:
            save_json(emails, results, [], args.output)
            print(f"\nFull JSON report saved to: {args.output}")
    else:
        print("No URLs found in emails. Use --threat for offline analysis.")


if __name__ == '__main__':
    asyncio.run(main())
