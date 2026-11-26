import json
import ipaddress
import logging
import os
import re
import socket
import threading
import uuid
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request
from werkzeug.utils import secure_filename

from integrations import save_chunks_to_neo4j
from rag import PERSONAL_DATA_RE, answer_question, make_chunks
from govza import CATEGORIES as GOVZA_CATEGORIES, import_category

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "data" / "documents.json"
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)
lock = threading.Lock()

CLEARANCES = {"Public": 0, "Internal": 1, "Confidential": 2, "Secret": 3}
ROLES = {"Official", "Policy Analyst", "Records Officer", "Administrator"}
ROLE_MAX_CLEARANCE = {
    "Official": "Internal",
    "Policy Analyst": "Confidential",
    "Records Officer": "Internal",
    "Administrator": "Secret",
}
STATUSES = {"Current", "Superseded", "Draft"}
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}


def load_documents():
    with lock:
        documents = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for document in documents:
        document["clearance_rank"] = CLEARANCES.get(document.get("classification"), len(CLEARANCES))
    return documents


def save_documents(documents):
    with lock:
        temporary = DATA_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DATA_FILE)


def effective_clearance(headers):
    role = headers.get("X-User-Role", "Official")
    requested = headers.get("X-User-Clearance", "Public")
    if role not in ROLES or requested not in CLEARANCES:
        return None
    return min(CLEARANCES[requested], CLEARANCES[ROLE_MAX_CLEARANCE[role]])


def safe_text(value, label, maximum=200):
    value = str(value or "").strip()
    if not value or len(value) > maximum:
        raise ValueError(f"{label} is required and must be at most {maximum} characters.")
    return value


def extract_text(path):
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md"):
        return path.read_text(encoding="utf-8")
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            return text
        try:
            import pytesseract
            from pdf2image import convert_from_path
            from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFPopplerTimeoutError
        except ImportError as exc:
            raise ValueError(
                "This PDF appears to be scanned. Install the OCR dependencies to enable text recognition."
            ) from exc
        try:
            images = convert_from_path(str(path))
            return "\n\n".join(pytesseract.image_to_string(image) for image in images)
        except (
            OSError,
            PDFInfoNotInstalledError,
            PDFPageCountError,
            PDFPopplerTimeoutError,
            pytesseract.TesseractError,
            pytesseract.TesseractNotFoundError,
        ) as exc:
            raise ValueError(
                "Scanned PDF OCR could not run. Install Tesseract OCR and Poppler and add them to PATH."
            ) from exc
    if suffix == ".docx":
        from docx import Document
        document = Document(str(path))
        return "\n\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())
    raise ValueError("Supported file types are PDF, DOCX, TXT, and Markdown.")


class VisibleTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden_depth += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data):
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def document_metadata(values, document_id, title):
    classification = safe_text(values.get("classification", "Public"), "Classification", 30)
    status = safe_text(values.get("status", "Current"), "Status", 30)
    if classification not in CLEARANCES:
        raise ValueError("Classification must be Public, Internal, Confidential, or Secret.")
    if status not in STATUSES:
        raise ValueError("Status must be Current, Superseded, or Draft.")
    version = safe_text(values.get("version", "1.0"), "Version", 40)
    repository = safe_text(values.get("repository", "Department Intranet (simulated)"), "Repository", 100)
    if repository not in {"Policy Library", "Government Gazette", "Department Intranet (simulated)"}:
        raise ValueError("Choose one of the configured simulated repositories.")
    return {
        "id": document_id,
        "title": safe_text(title, "Document title", 200),
        "section": safe_text(values.get("section", "Document"), "Section", 150),
        "version": version,
        "status": status,
        "classification": classification,
        "clearance_rank": CLEARANCES[classification],
        "owner": safe_text(values.get("owner", "Department document owner"), "Owner", 120),
        "date": safe_text(values.get("date", date.today().isoformat()), "Date", 30),
        "repository": repository,
        "document_type": safe_text(values.get("document_type", "Document"), "Document type", 50),
    }


def validate_public_url(raw_url):
    parsed = urlparse(raw_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Only public HTTPS URLs without embedded credentials can be imported.")
    if parsed.port not in (None, 443):
        raise ValueError("Web import is limited to the standard HTTPS port.")
    try:
        addresses = {
            ipaddress.ip_address(result[4][0])
            for result in socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise ValueError("The web host could not be resolved.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Only publicly routable web hosts can be imported.")
    return parsed


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@app.get("/")
def home():
    return render_template("index.html")


@app.get("/api/documents")
def list_documents():
    clearance = effective_clearance(request.headers)
    if clearance is None:
        return jsonify({"error": "Unknown demo role or access clearance."}), 403
    documents = load_documents()
    visible = [{
        key: document.get(key) for key in (
            "id", "title", "version", "status", "classification",
            "owner", "date", "repository", "document_type",
        )
    } for document in documents if document.get("clearance_rank", 0) <= clearance]
    return jsonify({"documents": visible, "repositories": sorted({d["repository"] for d in visible})})


@app.get("/api/source/<document_id>")
def get_source(document_id):
    clearance = effective_clearance(request.headers)
    if clearance is None:
        return jsonify({"error": "Unknown demo role or access clearance."}), 403
    document = next((item for item in load_documents() if item["id"] == document_id), None)
    if not document or document.get("clearance_rank", 0) > clearance:
        return jsonify({"error": "Source not found or not authorised."}), 404
    return jsonify({key: value for key, value in document.items() if key != "clearance_rank"})


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not question or len(question) > 2000:
        return jsonify({"error": "Enter a question of up to 2,000 characters."}), 400
    clearance = effective_clearance(request.headers)
    if clearance is None:
        return jsonify({"error": "Unknown demo role or access clearance."}), 403

    documents = load_documents()
    for document in documents:
        document["clearance_rank"] = CLEARANCES.get(document.get("classification"), 0)
    history = payload.get("history", [])
    if not isinstance(history, list):
        history = []
    answer, passages, provider = answer_question(question, history, documents, clearance)
    current_versions = {}
    for document in documents:
        key = document["title"].casefold()
        if document["status"] == "Current":
            current_versions[key] = document["version"]
    citations = []
    warnings = []
    for item in passages:
        citation = {
            "ref": item["ref"],
            "document_id": item["document_id"],
            "title": item["title"],
            "section": item["section"],
            "version": item["version"],
            "status": item["status"],
            "classification": item["classification"],
            "repository": item["repository"],
            "date": item["date"],
        }
        citations.append(citation)
        if item["status"] == "Superseded":
            newer = current_versions.get(item["title"].casefold())
            warnings.append(
                f'{item["title"]} version {item["version"]} is superseded'
                + (f" by current version {newer}." if newer else ".")
            )
        elif item["status"] == "Draft":
            warnings.append(f'{item["title"]} version {item["version"]} is a draft and is not approved.')
    return jsonify({
        "answer": answer,
        "citations": citations,
        "warnings": list(dict.fromkeys(warnings)),
        "disclaimer": "For information only. Verify the source and consult an authorised official before acting.",
        "provider": provider,
    })


@app.post("/api/ingest")
def ingest_upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "Choose a document to upload."}), 400
    filename = secure_filename(uploaded.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        return jsonify({"error": "Supported file types are PDF, DOCX, TXT, and Markdown."}), 400
    temp_path = UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
    try:
        uploaded.save(temp_path)
        text = extract_text(temp_path).strip()
        if not text:
            return jsonify({"error": "No readable text was found in the document."}), 400
        if PERSONAL_DATA_RE.search(text):
            return jsonify({"error": "Potential personal information detected. Redact it before indexing this document."}), 400
        if len(text) > 1_000_000:
            return jsonify({"error": "Extracted text exceeds the 1 MB document limit."}), 413
        values = request.form
        document = document_metadata(values, uuid.uuid4().hex, values.get("title") or Path(filename).stem)
        document["text"] = text
        documents = load_documents()
        documents.append(document)
        chunks = make_chunks(document)
        save_chunks_to_neo4j(document, chunks)
        save_documents(documents)
        return jsonify({"message": "Document indexed.", "document": {k: v for k, v in document.items() if k != "text"}}), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Document ingestion failed")
        return jsonify({"error": "Document could not be indexed. Check the file and configured integrations."}), 500
    finally:
        temp_path.unlink(missing_ok=True)


@app.post("/api/ingest-url")
def ingest_url():
    payload = request.get_json(silent=True) or {}
    raw_url = str(payload.get("url", "")).strip()
    try:
        parsed = validate_public_url(raw_url)
        req = Request(raw_url, headers={"User-Agent": "GPACT-AI-Demo/1.0"})
        opener = build_opener(SafeRedirectHandler)
        with opener.open(req, timeout=10) as response:
            if response.status != 200:
                return jsonify({"error": "The source page could not be fetched."}), 400
            content_type = response.headers.get("Content-Type", "")
            if "text/html" not in content_type:
                return jsonify({"error": "Only HTML web pages can be imported."}), 400
            raw_html = response.read(2_000_000).decode("utf-8", errors="replace")
        parser = VisibleTextParser()
        parser.feed(raw_html)
        text = "\n".join(parser.parts)
        if len(text.strip()) < 50:
            return jsonify({"error": "The page did not contain enough readable text."}), 400
        if PERSONAL_DATA_RE.search(text):
            return jsonify({"error": "Potential personal information detected. Choose a public page without personal information."}), 400
        title_match = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else parsed.hostname
        document = document_metadata(payload, uuid.uuid4().hex, title[:200])
        document["text"] = text[:1_000_000]
        document["source_url"] = raw_url
        documents = load_documents()
        documents.append(document)
        save_chunks_to_neo4j(document, make_chunks(document))
        save_documents(documents)
        return jsonify({"message": "Web page indexed.", "document": {k: v for k, v in document.items() if k != "text"}}), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Web ingestion failed")
        return jsonify({"error": "The web page could not be imported."}), 500


@app.get("/api/govza/categories")
def govza_categories():
    return jsonify({"categories": sorted(GOVZA_CATEGORIES)})


@app.post("/api/ingest-govza")
def ingest_govza():
    payload = request.get_json(silent=True) or {}
    category = str(payload.get("category", "")).strip()
    try:
        limit = min(max(int(payload.get("limit", 3)), 1), 5)
    except (TypeError, ValueError):
        return jsonify({"error": "The document limit must be a number."}), 400
    try:
        imports = import_category(category, limit)
        if not imports:
            return jsonify({"error": "No PDF attachments were found in this category."}), 404
        documents = load_documents()
        added = []
        for imported in imports:
            text = None
            try:
                from govza import extract_pdf_text
                text = extract_pdf_text(imported["pdf"])
            except Exception as exc:
                logger.warning("Gov.za PDF text extraction skipped: %s", exc)
                continue
            if not text or PERSONAL_DATA_RE.search(text):
                continue
            parsed_date = imported["document_date"]
            iso_date = date.today().isoformat()
            for day_format in ("%d %B %Y", "%d %b %Y"):
                try:
                    iso_date = __import__("datetime").datetime.strptime(parsed_date, day_format).date().isoformat()
                    break
                except ValueError:
                    pass
            status = "Draft" if "draft" in imported["title"].lower() or "draft" in category.lower() else "Current"
            document = {
                "id": uuid.uuid4().hex,
                "title": safe_text(imported["title"], "Document title", 200),
                "section": "Full document",
                "version": safe_text(payload.get("version", "Published"), "Version", 40),
                "status": status,
                "classification": "Public",
                "clearance_rank": 0,
                "owner": "South African Government",
                "date": iso_date,
                "repository": "Government Gazette",
                "document_type": category,
                "source_url": imported["source_url"],
                "page_url": imported["page_url"],
                "text": text[:1_000_000],
            }
            documents.append(document)
            added.append({key: value for key, value in document.items() if key != "text"})
        if not added:
            return jsonify({"error": "The PDFs had no usable text or contained likely personal information."}), 422
        save_documents(documents)
        return jsonify({"message": f"Indexed {len(added)} public gov.za PDF documents.", "documents": added}), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Gov.za import failed")
        return jsonify({"error": "The gov.za import could not be completed."}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
