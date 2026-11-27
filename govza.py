import html
import io
import ipaddress
import re
import socket
import time
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.robotparser import RobotFileParser
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from pypdf import PdfReader


BASE_URL = "https://www.gov.za/"
CATEGORIES = {
    "Acts": "documents/acts",
    "Bills": "documents/bills",
    "Draft bills": "documents/draft-bills",
    "Notices": "documents/notices",
    "White papers": "documents/white-papers",
    "Green papers": "documents/green-papers",
    "Annual reports": "documents/annual-report",
    "Other public documents": "documents/other-documents",
    "Public comments": "documents/public-comment",
}
CATEGORY_PREFIXES = {
    "Acts": "/documents/acts/",
    "Bills": "/documents/bills/",
    "Draft bills": "/documents/draft-bills/",
    "Notices": "/documents/notices/",
    "White papers": "/documents/white-papers/",
    "Green papers": "/documents/green-papers/",
    "Annual reports": "/documents/annual-reports/",
    "Other public documents": "/documents/other/",
    "Public comments": "/documents/public-comment/",
}
USER_AGENT = "GPACT-AI-Policy-Demo/1.0 (public-document-ingestion)"
DEPARTMENT_DIRECTORY = urljoin(BASE_URL, "about-government/government-system/national-departments")
POLICY_TERMS = re.compile(
    r"\b(policy|policies|legislation|regulation|circular|framework|guideline|standard|"
    r"directive|gazette|act|white paper|green paper|strategy)\b",
    re.I,
)


class DirectoryLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            label = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            self.links.append((self._href, label))
            self._href = None
            self._text = []


class SameHostRedirectHandler(HTTPRedirectHandler):
    def __init__(self, expected_host):
        super().__init__()
        self.expected_host = expected_host

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme != "https" or parsed.hostname != self.expected_host:
            raise ValueError("The official site redirected outside its verified HTTPS host.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _validate_official_host_url(raw_url, expected_host=None):
    parsed = urlparse(raw_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or (expected_host and parsed.hostname != expected_host)
    ):
        raise ValueError("Only verified official HTTPS department URLs can be imported.")
    try:
        addresses = {
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise ValueError("The official department host could not be resolved.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("The official department host is not publicly routable.")
    return parsed


def list_national_departments():
    html_text = fetch_text(DEPARTMENT_DIRECTORY, timeout=10)
    parser = DirectoryLinkParser()
    parser.feed(html_text)
    departments = []
    seen = set()
    for href, label in parser.links:
        if not href:
            continue
        absolute = urljoin(DEPARTMENT_DIRECTORY, html.unescape(href).strip())
        parsed = urlparse(absolute)
        host = parsed.hostname or ""
        if not (
            parsed.scheme in {"http", "https"}
            and parsed.username is None
            and parsed.password is None
            and (
                host.endswith(".gov.za")
                or host.endswith(".mil.za")
                or host.endswith(".judiciary.org.za")
            )
            and host not in {"gov.za", "www.gov.za"}
        ):
            continue
        absolute = parsed._replace(scheme="https", fragment="").geturl()
        if absolute in seen or not label or len(label) < 3:
            continue
        seen.add(absolute)
        departments.append({"name": label, "url": absolute})
    return departments


def _robots_parser(root_url, host):
    robots_url = urljoin(root_url, "/robots.txt")
    request = Request(robots_url, headers={"User-Agent": USER_AGENT})
    opener = build_opener(SameHostRedirectHandler(host))
    try:
        with opener.open(request, timeout=4) as response:
            content = response.read(500_000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            content = ""
        else:
            raise ValueError("The department site's robots.txt could not be checked.") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("The department site's robots.txt could not be checked.") from exc
    parser = RobotFileParser(robots_url)
    parser.parse(content.splitlines())
    return parser


def _fetch_official_page(raw_url, robots, host, maximum_bytes=2_000_000):
    parsed = _validate_official_host_url(raw_url, host)
    if not robots.can_fetch(USER_AGENT, parsed.path or "/"):
        raise ValueError("The department site's robots.txt disallows this page.")
    request = Request(raw_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    opener = build_opener(SameHostRedirectHandler(host))
    try:
        with opener.open(request, timeout=6) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if response.status != 200 or "text/html" not in content_type:
                raise ValueError("The department page did not return HTML.")
            return response.read(maximum_bytes).decode("utf-8", errors="replace")
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("The official department page could not be fetched.") from exc


def _fetch_official_pdf(raw_url, robots, host, maximum_bytes=15_000_000):
    parsed = _validate_official_host_url(raw_url, host)
    if not robots.can_fetch(USER_AGENT, parsed.path or "/"):
        raise ValueError("The department site's robots.txt disallows this PDF.")
    request = Request(raw_url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"})
    opener = build_opener(SameHostRedirectHandler(host))
    try:
        with opener.open(request, timeout=8) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            length = response.headers.get("Content-Length")
            if length and int(length) > maximum_bytes:
                raise ValueError("The department PDF exceeds the 15 MB import limit.")
            data = response.read(maximum_bytes + 1)
            if len(data) > maximum_bytes:
                raise ValueError("The department PDF exceeds the 15 MB import limit.")
            if "pdf" not in content_type and not data.startswith(b"%PDF"):
                raise ValueError("The department attachment is not a PDF.")
            return data
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("The official department PDF could not be fetched.") from exc


def _department_links(html_text, base_url, host):
    parser = DirectoryLinkParser()
    parser.feed(html_text)
    links = []
    for href, label in parser.links:
        absolute = urldefrag(urljoin(base_url, html.unescape(href).strip()))[0]
        parsed = urlparse(absolute)
        if parsed.scheme != "https" or parsed.hostname != host:
            continue
        if absolute not in {link["url"] for link in links}:
            links.append({"url": absolute, "title": re.sub(r"\s+", " ", label).strip()})
    return links


def import_department_site(site_url, limit=2):
    limit = max(1, min(int(limit), 2))
    approved = list_national_departments()
    approved_site = next((site for site in approved if site["url"] == site_url), None)
    if not approved_site:
        raise ValueError("Choose a department website listed in the official gov.za directory.")
    site_url = approved_site["url"]
    parsed = _validate_official_host_url(site_url)
    host = parsed.hostname
    robots = _robots_parser(site_url, host)
    home_html = _fetch_official_page(site_url, robots, host)
    candidates = []
    pages = []
    for link in _department_links(home_html, site_url, host):
        if ".pdf" in urlparse(link["url"]).path.lower():
            candidates.append({**link, "page_url": site_url})
        elif POLICY_TERMS.search(link["title"] + " " + link["url"]):
            pages.append(link)
    for page in pages[:1]:
        try:
            page_html = _fetch_official_page(page["url"], robots, host)
        except ValueError:
            continue
        for link in _department_links(page_html, page["url"], host):
            if ".pdf" in urlparse(link["url"]).path.lower() and POLICY_TERMS.search(
                page["title"] + " " + link["title"] + " " + link["url"]
            ):
                candidates.append({**link, "page_url": page["url"], "context": page["title"]})

    imported = []
    seen = set()
    for item in candidates:
        if item["url"] in seen or not POLICY_TERMS.search(
            item.get("context", "") + " " + item["title"] + " " + item["url"]
        ):
            continue
        seen.add(item["url"])
        try:
            pdf_bytes = _fetch_official_pdf(item["url"], robots, host)
        except ValueError:
            continue
        title = item["title"]
        if title.lower() in {"pdf", "download", "view", "document"} or not title:
            title = urlparse(item["url"]).path.rsplit("/", 1)[-1]
        imported.append({
            "title": title[:200],
            "source_url": item["url"],
            "page_url": item["page_url"],
            "owner": approved_site["name"],
            "pdf": pdf_bytes,
        })
        if len(imported) >= limit:
            break
        time.sleep(0.1)
    return imported


def validate_gov_url(raw_url):
    parsed = urlparse(raw_url)
    if parsed.scheme != "https":
        raise ValueError("Gov.za URLs must use HTTPS.")
    if parsed.hostname not in {"gov.za", "www.gov.za"}:
        raise ValueError("Only gov.za and www.gov.za URLs can be imported.")
    return parsed


def fetch_text(raw_url, timeout=45):
    validate_gov_url(raw_url)
    request = Request(raw_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError("The public gov.za page could not be fetched.")
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                raise ValueError("The gov.za category page did not return HTML.")
            return response.read(4_000_000).decode("utf-8", errors="replace")
    except TimeoutError as exc:
        raise ValueError("Gov.za timed out. Try again or use a smaller document limit.") from exc
    except URLError as exc:
        raise ValueError("Gov.za could not be reached. Check the network and try again.") from exc


def parse_category_items(html_text, limit=5, prefix=None):
    items = []
    pattern = re.compile(
        r'<a\s+href="([^"]+)"[^>]*>\s*([^<>]+?)\s*</a>',
        re.I,
    )
    for href, title in pattern.findall(html_text):
        href = html.unescape(href).strip()
        title = re.sub(r"\s+", " ", html.unescape(title)).strip()
        if not href.startswith("/documents/"):
            continue
        if prefix and not href.startswith(prefix):
            continue
        path_parts = href.strip("/").split("/")
        if len(path_parts) < 3:
            continue
        if title in {"", "Document", "Title", "Date"}:
            continue
        if len(title) < 10:
            continue
        if not any(character.isalnum() for character in title):
            continue
        absolute = urljoin(BASE_URL, href)
        if absolute not in {item["url"] for item in items}:
            items.append({"title": title, "url": absolute})
            if len(items) >= limit:
                break
    return items


def parse_detail(html_text):
    pdf_links = []
    for href in re.findall(r'href="([^"]+\.pdf[^"]*)"', html_text, re.I):
        href = html.unescape(href)
        if href.startswith(("http://", "https://")):
            absolute = href
        else:
            absolute = urljoin(BASE_URL, href)
        if absolute not in pdf_links:
            pdf_links.append(absolute)

    title_match = re.search(r"<h1[^>]*>\s*(.*?)\s*</h1>", html_text, re.I | re.S)
    title = ""
    if title_match:
        title = re.sub(r"<[^>]+>", " ", title_match.group(1))
        title = re.sub(r"\s+", " ", html.unescape(title)).strip()

    date_match = re.search(
        r"(\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b)",
        html_text,
        re.I,
    )
    date = date_match.group(1) if date_match else ""
    return title, date, pdf_links


def fetch_pdf(raw_url, maximum_bytes=15_000_000):
    validate_gov_url(raw_url)
    request = Request(raw_url, headers={"User-Agent": USER_AGENT, "Accept": "application/pdf,*/*"})
    try:
        with urlopen(request, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > maximum_bytes:
                raise ValueError("The gov.za PDF exceeds the 15 MB import limit.")
            data = response.read(maximum_bytes + 1)
            if len(data) > maximum_bytes:
                raise ValueError("The gov.za PDF exceeds the 15 MB import limit.")
            if "pdf" not in content_type.lower() and not data.startswith(b"%PDF"):
                raise ValueError("The gov.za attachment is not a PDF.")
    except TimeoutError as exc:
        raise ValueError("Gov.za timed out while downloading the PDF. Try again.") from exc
    except URLError as exc:
        raise ValueError("The gov.za PDF could not be reached.") from exc
    return data


def extract_pdf_text(pdf_bytes):
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if not text.strip():
        raise ValueError("The gov.za PDF has no readable text. OCR is needed for scanned copies.")
    return text.strip()


def import_category(category, limit=5):
    if category not in CATEGORIES:
        raise ValueError("Choose an approved gov.za document category.")
    limit = max(1, min(int(limit), 10))
    category_url = urljoin(BASE_URL, CATEGORIES[category])
    html_text = fetch_text(category_url)
    items = parse_category_items(html_text, limit, CATEGORY_PREFIXES[category])
    imported = []
    for item in items:
        detail_html = fetch_text(item["url"])
        detail_title, detail_date, pdf_links = parse_detail(detail_html)
        title = detail_title or item["title"]
        for pdf_url in pdf_links:
            pdf_bytes = fetch_pdf(pdf_url)
            imported.append({
                "title": title,
                "source_url": pdf_url,
                "page_url": item["url"],
                "document_date": detail_date,
                "pdf": pdf_bytes,
            })
        time.sleep(0.5)
    return imported
