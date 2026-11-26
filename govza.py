import html
import io
import re
import time
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

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


def validate_gov_url(raw_url):
    parsed = urlparse(raw_url)
    if parsed.scheme != "https":
        raise ValueError("Gov.za URLs must use HTTPS.")
    if parsed.hostname not in {"gov.za", "www.gov.za"}:
        raise ValueError("Only gov.za and www.gov.za URLs can be imported.")
    return parsed


def fetch_text(raw_url):
    validate_gov_url(raw_url)
    request = Request(raw_url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})
    try:
        with urlopen(request, timeout=45) as response:
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
