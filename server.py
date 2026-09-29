#!/usr/bin/env python3
"""Lightweight HTTP server for the Email Link Crawler web UI.

No extra dependencies needed — uses Python stdlib + aiohttp (already installed).

Usage:
    python3 server.py [port]
    Default port: 8500
"""
import asyncio
import json
import mimetypes
import os
import sys
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

# ── API state ────────────────────────────────────────────────────
_scan_result = {}
_running = False
_running_lock = threading.Lock()


class APIHandler(SimpleHTTPRequestHandler):
    """Handles static file serving + /api/scan endpoint."""

    def __init__(self, *args, **kwargs):
        # Serve from ui/ directory for static files
        self.ui_dir = os.path.join(os.path.dirname(__file__), 'ui')
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        pass  # silence access logs

    def do_GET(self):
        path = urlparse(self.path).path
        if path == '/' or path == '/index.html':
            self.serve_file(os.path.join(self.ui_dir, 'index.html'), 'text/html')
        elif path.startswith('/api/'):
            self.serve_json(200, {'status': 'ok', 'message': 'Server running'})
        else:
            # Try to serve from ui/ dir
            filepath = os.path.join(self.ui_dir, path.lstrip('/'))
            if os.path.isfile(filepath):
                content_type = mimetypes.guess_type(filepath)[0] or 'application/octet-stream'
                self.serve_file(filepath, content_type)
            else:
                self.send_error(404, 'Not Found')

    def do_POST(self):
        path = urlparse(self.path).path
        if path == '/api/scan':
            self.handle_scan()
        else:
            self.send_error(404, 'Not Found')

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def handle_scan(self):
        global _running
        with _running_lock:
            if _running:
                self.serve_json(429, {'error': 'Scan already in progress. Wait for it to finish.'})
                return

        # Read request body
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            params = json.loads(body)
        except json.JSONDecodeError:
            self.serve_json(400, {'error': 'Invalid JSON body'})
            return

        # Validate required fields
        if not params.get('username') or not params.get('password'):
            self.serve_json(400, {'error': 'Username and password are required'})
            return

        # Run scan synchronously — return full results
        try:
            import asyncio
            result = asyncio.run(run_scan_logic_async(params))
        except Exception as e:
            import traceback
            result = {'error': str(e), 'traceback': traceback.format_exc()}

        self.serve_json(200, result)

    def serve_file(self, filepath, content_type):
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404, 'Not Found')

    def serve_json(self, code, data):
        body = json.dumps(data).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(body)


async def run_scan_logic_async(params: dict) -> dict:
    """Execute the scanning pipeline and return results."""
    import imaplib
    import email
    from email.header import decode_header
    import re
    from html.parser import HTMLParser
    from urllib.parse import urlparse
    from dataclasses import dataclass, field
    from typing import List, Dict, Set
    from datetime import datetime
    import dns.resolver
    import dns.reversename

    # ── Import crawler classes ───────────────────────────────────
    sys.path.insert(0, os.path.dirname(__file__))
    from main import (
        EmailCrawler, UrlCrawler, HTMLLinkExtractor,
        normalize_url, extract_links_from_text, extract_links_from_html,
        QuarantineAnalyzer, UrlResult, PlaywrightCrawler,
    )

    @dataclass
    class CrawlResult:
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

    # ── Extract parameters ────────────────────────────────────────
    imap_server = params.get('imap_server', 'imap.gmail.com')
    imap_port = params.get('imap_port', 993)
    username = params['username']
    password = params['password']
    folder = params.get('folder', 'INBOX')
    max_emails = params.get('max_emails', 20)
    unseen_only = params.get('unseen_only', False)
    max_concurrent = params.get('max_concurrent', 10)
    timeout = params.get('timeout', 15)
    mode = params.get('mode', 'full')
    crawler_engine = params.get('crawler_engine', 'aiohttp')
    pw_stealth = params.get('pw_stealth', True)
    no_lookup = params.get('no_lookup', False)

    results = {'emails': [], 'urls': [], 'threat_analysis': []}

    # ── Step 1: Fetch emails ──────────────────────────────────────
    try:
        import logging
        logging.getLogger().info(f"=== Email fetch params: unseen_only={unseen_only}, seen_marker={'UNSEEN' if unseen_only else 'None'}, folder={folder} ===")
        email_crawler = EmailCrawler(
            imap_server=imap_server,
            imap_port=imap_port,
            username=username,
            password=password,
            folder=folder,
            max_emails=max_emails,
            seen_marker='UNSEEN' if unseen_only else None,
        )
        emails = email_crawler.fetch_emails()
        logging.getLogger().info(f"=== Fetched {len(emails)} email(s) via IMAP ===")
        
        # Check if we got 0 emails
        if not emails and mode in ('full', 'crawl'):
            logging.getLogger().info("=== WARNING: 0 emails fetched. Check folder name or credentials. ===")
    except Exception as e:
        import traceback
        logging.getLogger().error(f"=== IMAP Error: {str(e)} ===")
        logging.getLogger().error(traceback.format_exc())
        results['error'] = f'Failed to connect to IMAP: {str(e)}'
        return results

    if not emails:
        # Return a hint to the user
        results['emails'] = []
        results['urls'] = []
        results['threat_analysis'] = []
        results['folder_hint'] = f"0 emails found in '{folder}'. Try '[Gmail]/INBOX' for Gmail, or check folder name."
        return results

    results['emails'] = [{
        'uid': em.uid,
        'subject': em.subject,
        'sender': em.sender,
        'date': em.date,
        'links': em.normalised_links,
    } for em in emails]

    # ── Step 2: Threat analysis ───────────────────────────────────
    do_threat = mode in ('threat', 'full')
    do_dns = mode in ('dns', 'full')

    if do_threat or do_dns:
        quar = QuarantineAnalyzer(verbose=False)
        analyses = quar.analyze_emails_batch(emails)
        results['threat_analysis'] = analyses

        if do_dns and not no_lookup:
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
                dns_results = {}
                for domain in sorted(all_domains):
                    checks = quar.dns.run_checks(domain)
                    dns_results[domain] = checks
                results['dns_checks'] = dns_results

    # ── Step 3: URL crawling ──────────────────────────────────────
    do_crawl = mode in ('full', 'crawl')
    if do_crawl:
        all_urls: List[str] = []
        for em in emails:
            all_urls.extend(em.normalised_links)
        all_urls = list(dict.fromkeys(all_urls))

        if all_urls:
            import logging
            logging.getLogger().info(f"=== Using crawler engine: {crawler_engine} ===")
            
            if crawler_engine == 'playwright':
                logging.getLogger().info(f"=== [PLAYWRIGHT] Crawling {len(all_urls)} URLs with real Chromium... ===")
                url_results = await PlaywrightCrawler(
                    max_concurrent=max(1, max_concurrent // 2),
                    timeout_seconds=timeout,
                    stealth=pw_stealth,
                ).crawl_all(all_urls)
            else:
                url_crawler = UrlCrawler(
                    max_concurrent=max_concurrent,
                    timeout_seconds=timeout,
                    delay_range=(params.get('delay_min', 0.1), params.get('delay_max', 0.8)),
                    randomize_headers=params.get('randomize_headers', True),
                    handle_cookies=not params.get('no_cookies', False),
                    simulate_human_timing=not params.get('no_human_timing', False),
                    rotate_user_agent=not params.get('same_user_agent', False),
                    user_agent=None,
                    simulate_js_execution=not params.get('no_js', False),
                    simulate_css_loading=not params.get('no_css', False),
                    simulate_cookie_consent=not params.get('no_cookies', False),
                    simulate_time_on_page=not params.get('no_time', False),
                    simulate_scroll=not params.get('no_scroll', False),
                )
                url_results = await url_crawler.crawl_all(all_urls)
            results['urls'] = [{
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
                'js_executed': r.js_executed,
                'css_loaded': r.css_loaded,
                'cookies_set': r.cookies_set,
                'time_on_page_ms': r.time_on_page_ms,
                'scroll_depth': r.scroll_depth,
                'has_dynamic_content': r.has_dynamic_content,
            } for r in url_results]

    return results


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8500
    host = '0.0.0.0'

    server = HTTPServer((host, port), APIHandler)
    print(f'  Email Link Crawler UI')
    print(f'  Server running on http://localhost:{port}')
    print(f'  Open http://localhost:{port} in your browser')
    print(f'  Press Ctrl+C to stop\n')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopped.')
        server.server_close()


if __name__ == '__main__':
    main()
