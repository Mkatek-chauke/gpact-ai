import json
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import socket
import threading
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlencode, urlparse

import psycopg
from dotenv import load_dotenv
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from auth import ROLE_ORDER, demo_auth_enabled, entra_configuration, principal_from_claims, register_entra_oauth
from email_service import EmailDeliveryError, resend_is_configured, send_password_reset_email
from integrations import groq_web_search_enabled, save_chunks_to_neo4j
from rag import PERSONAL_DATA_RE, PERSONAL_DATA_REQUEST_RE, answer_question, make_chunks
from govza import (
    CATEGORIES as GOVZA_CATEGORIES,
    import_category,
    import_department_site,
    list_national_departments,
)
import storage

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local")
DATA_FILE = ROOT / "data" / "documents.json"
ACCOUNTS_FILE = ROOT / "data" / "accounts.json"
UPLOAD_DIR = Path(tempfile.gettempdir()) / "gpact-ai-uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
app = Flask(__name__, static_folder=str(ROOT / "public" / "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY") or secrets.token_urlsafe(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = bool(os.getenv("VERCEL"))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 8
ENTRA_CONFIG = entra_configuration()
ENTRA_OAUTH = register_entra_oauth(app)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)
lock = threading.Lock()
rate_limit_lock = threading.Lock()
local_rate_limits = {}


@app.errorhandler(psycopg.Error)
def handle_database_error(error):
    logger.error("Postgres request failed (%s)", type(error).__name__)
    if request.path.startswith("/api/"):
        return jsonify({
            "error": "The database is unavailable. Check DATABASE_URL_POOLED in the Vercel project settings."
        }), 503
    return "Database unavailable. Check the configured Postgres connection.", 503


@app.before_request
def require_persistent_storage_for_serverless_writes():
    if (
        os.getenv("VERCEL")
        and request.method == "POST"
        and request.path.startswith("/api/ingest")
        and not storage.database_url()
    ):
        return jsonify({
            "error": "Document ingestion requires persistent storage on Vercel. Configure DATABASE_URL."
        }), 503


CLEARANCES = {"Public": 0, "Internal": 1, "Restricted": 2}
CLASSIFICATIONS = {"Public", "Internal", "Restricted"}
ROLES = set(ROLE_ORDER)
ROLE_MAX_CLEARANCE = {"Citizen": 0, "Officer": 1, "Manager": 2, "Administrator": 2}
STATUSES = {"Current", "Superseded", "Draft", "Unverified"}
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MANAGE_ROLES = {"Manager", "Administrator"}
MAX_PDF_PAGES = 200
MAX_OCR_PAGES = 40
RATE_LIMITS = {
    "chat": (30, 60),
    "ingest_upload": (10, 60),
    "ingest_url": (10, 60),
    "ingest_govza": (5, 60),
    "ingest_department": (5, 60),
}
SOURCE_FILTERS = {
    "all",
    "policies",
    "gazettes",
    "legislation",
    "grants_benefits",
    "department_websites",
}

LOCAL_RESET_TOKENS = {}


def load_local_accounts():
    if not ACCOUNTS_FILE.exists():
        return []
    try:
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def save_local_account(account):
    accounts = [item for item in load_local_accounts() if item.get("email") != account["email"]]
    accounts.append(account)
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = ACCOUNTS_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(ACCOUNTS_FILE)


def current_principal():
    principal = session.get("principal")
    if not isinstance(principal, dict) or principal.get("role") not in ROLES:
        return None
    return principal


def effective_clearance():
    principal = current_principal()
    if not principal:
        return None
    return ROLE_MAX_CLEARANCE[principal["role"]]


def audit_event(event_type, resource_id=None, outcome="success"):
    principal = current_principal()
    if principal:
        storage.record_audit_event(principal["id"], event_type, resource_id, outcome)


def local_rate_limit_allowed(principal_id, action, limit, window_seconds):
    actor_hash = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()
    current_window = int(time.time() // window_seconds)
    with rate_limit_lock:
        key = (actor_hash, action)
        previous_window, count = local_rate_limits.get(key, (current_window, 0))
        count = count + 1 if previous_window == current_window else 1
        local_rate_limits[key] = (current_window, count)
        return count <= limit


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_local_auth_csrf():
    expected = session.get("_csrf_token")
    supplied = request.headers.get("X-CSRF-Token")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


@app.before_request
def require_authentication_and_csrf():
    if request.endpoint in {
        "static", "healthz", "auth_login", "auth_callback", "auth_account",
        "local_register", "local_login", "local_forgot", "local_reset",
    }:
        return None

    if demo_auth_enabled():
        if current_principal() is None:
            if request.path.startswith("/api/") and request.endpoint not in {
                "session_info", "local_register", "local_login", "local_forgot", "local_reset",
            }:
                return jsonify({"error": "Sign in to use GPACT AI."}), 401
    elif not ENTRA_CONFIG or not ENTRA_OAUTH:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Entra authentication is not configured for this deployment."}), 503
        return render_template("auth_setup.html"), 503
    elif current_principal() is None:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Sign in with your department account."}), 401
        return redirect(url_for("auth_login"))

    if request.path.startswith("/api/ingest") and current_principal()["role"] not in MANAGE_ROLES:
        audit_event("ingestion_denied", request.endpoint, "denied")
        return jsonify({"error": "A Manager or Administrator role is required to ingest documents."}), 403

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("_csrf_token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            return jsonify({"error": "The request failed CSRF validation. Refresh the page and try again."}), 403
    persistent_endpoints = {
        "list_documents",
        "get_source",
        "get_source_content",
        "chat",
        *RATE_LIMITS.keys(),
    }
    if (
        os.getenv("VERCEL")
        and request.endpoint in persistent_endpoints
        and not storage.database_url()
    ):
        return jsonify({
            "error": "Persistent Postgres is required for production document access, rate limits, and audit records."
        }), 503
    rate_limit = RATE_LIMITS.get(request.endpoint)
    if rate_limit:
        limit, window_seconds = rate_limit
        principal = current_principal()
        allowed = storage.check_rate_limit(
            principal["id"], request.endpoint, limit, window_seconds
        )
        if allowed is None:
            allowed = local_rate_limit_allowed(
                principal["id"], request.endpoint, limit, window_seconds
            )
        if not allowed:
            audit_event("rate_limit", request.endpoint, "limited")
            return jsonify({"error": "Too many requests. Wait briefly and try again."}), 429, {
                "Retry-After": str(window_seconds)
            }
    return None


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    if isinstance(error, HTTPException):
        return error
    logger.exception("Unhandled request error (%s)", type(error).__name__)
    if request.path.startswith("/api/"):
        return jsonify({"error": "The request could not be completed. Check the server logs."}), 500
    return "The request could not be completed. Check the server logs.", 500


def load_documents():
    if storage.database_url():
        documents = storage.load_documents()
        if not documents:
            with lock:
                documents = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            storage.save_documents(documents)
    else:
        with lock:
            documents = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    for document in documents:
        document["clearance_rank"] = CLEARANCES.get(document.get("classification"))
        document.setdefault("version_family", document.get("title", "").strip().casefold())
    return documents


def save_documents(documents):
    if storage.database_url():
        storage.save_documents(documents)
        return
    with lock:
        temporary = DATA_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(documents, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DATA_FILE)


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
        page_count = len(reader.pages)
        if page_count > MAX_PDF_PAGES:
            raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES}-page ingestion limit.")
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        if text.strip():
            return text
        if page_count > MAX_OCR_PAGES:
            raise ValueError(
                f"Scanned PDFs are limited to {MAX_OCR_PAGES} pages for local OCR. Split the document before indexing."
            )
        try:
            import pytesseract
            from pdf2image import convert_from_path
            from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFPopplerTimeoutError
        except ImportError as exc:
            raise ValueError(
                "This PDF appears to be scanned. Install the OCR dependencies to enable text recognition."
            ) from exc
        try:
            images = convert_from_path(
                str(path),
                first_page=1,
                last_page=page_count,
                dpi=150,
                thread_count=1,
                timeout=30,
            )
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
    if classification not in CLASSIFICATIONS:
        raise ValueError("Classification must be Public, Internal, or Restricted.")
    if status not in STATUSES:
        raise ValueError("Status must be Current, Superseded, Draft, or Unverified.")
    version = safe_text(values.get("version", "1.0"), "Version", 40)
    repository = safe_text(values.get("repository", "Department Intranet (simulated)"), "Repository", 100)
    if repository not in {
        "Policy Library",
        "Government Gazette",
        "SharePoint",
        "OpenText Content Suite",
        "Government DMS (simulated)",
        "Department Intranet (simulated)",
    }:
        raise ValueError("Choose a supported repository.")
    return {
        "id": document_id,
        "title": safe_text(title, "Document title", 200),
        "section": safe_text(values.get("section", "Document"), "Section", 150),
        "version": version,
        "version_family": safe_text(
            values.get("version_family", title.strip().casefold()), "Version family", 200
        ),
        "status": status,
        "classification": classification,
        "clearance_rank": CLEARANCES[classification],
        "owner": safe_text(values.get("owner", "Department document owner"), "Owner", 120),
        "date": safe_text(values.get("date", date.today().isoformat()), "Date", 30),
        "uploaded_at": datetime.now(timezone.utc).isoformat(),
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
    principal = current_principal()
    if principal is None:
        return render_template("home.html", demo_auth_enabled=demo_auth_enabled())
    return render_template("index.html", demo_auth_enabled=demo_auth_enabled())


@app.get("/healthz")
def healthz():
    return jsonify({
        "status": "ok",
        "authentication": "entra" if ENTRA_OAUTH else ("local-demo" if demo_auth_enabled() else "unconfigured"),
        "persistent_storage_configured": bool(storage.database_url()),
    })


@app.get("/auth/login")
def auth_login():
    if demo_auth_enabled():
        return redirect(url_for("auth_account"))
    if not ENTRA_OAUTH or not ENTRA_CONFIG:
        return render_template("auth_setup.html"), 503
    return ENTRA_OAUTH.entra.authorize_redirect(ENTRA_CONFIG["redirect_uri"])


@app.get("/auth/account")
def auth_account():
    if not demo_auth_enabled():
        return redirect(url_for("auth_login"))
    reset_token = request.args.get("token", "").strip()
    if current_principal() is not None and not reset_token:
        return redirect(url_for("home"))
    return render_template(
        "auth_account.html",
        csrf_token=csrf_token(),
        reset_token=reset_token,
    )


@app.get("/auth/callback")
def auth_callback():
    if not ENTRA_OAUTH:
        return render_template("auth_setup.html"), 503
    try:
        token = ENTRA_OAUTH.entra.authorize_access_token()
        claims = token.get("userinfo") or ENTRA_OAUTH.entra.parse_id_token(token)
        principal = principal_from_claims(dict(claims))
    except Exception as exc:
        logger.warning("Entra sign-in failed (%s)", type(exc).__name__)
        return "Sign-in failed. Check Entra configuration and try again.", 401
    session.clear()
    session["principal"] = principal
    session.permanent = True
    csrf_token()
    audit_event("sign_in")
    return redirect(url_for("home"))


@app.post("/auth/logout")
def auth_logout():
    session.clear()
    if demo_auth_enabled():
        session["demo_signed_out"] = True
        return redirect(url_for("auth_account"))
    return redirect(url_for("home"))


@app.get("/api/session")
def session_info():
    principal = current_principal()
    if principal is None and demo_auth_enabled():
        principal = {"id": "local-demo", "name": "Local development", "role": "Citizen"}
        session["principal"] = principal
    return jsonify({
        "user": {
            "id": principal["id"],
            "name": principal["name"],
            "role": principal["role"],
            "clearance": ROLE_MAX_CLEARANCE[principal["role"]],
        },
        "csrf_token": csrf_token(),
        "demo": demo_auth_enabled(),
        "live_search_enabled": (
            groq_web_search_enabled()
            and bool(os.getenv("GROQ_API_KEY", "").strip())
        ),
    })


@app.post("/api/auth/register")
def local_register():
    if not demo_auth_enabled():
        return jsonify({"error": "Local registration is disabled."}), 404
    if not validate_local_auth_csrf():
        return jsonify({"error": "The request failed CSRF validation. Refresh the page and try again."}), 403
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "Citizen"))
    if len(name) < 2 or len(name) > 160:
        return jsonify({"error": "Enter your full name."}), 400
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must contain at least 8 characters."}), 400
    if role not in ROLES:
        return jsonify({"error": "Choose a valid role."}), 400
    if any(account.get("email") == email for account in load_local_accounts()):
        return jsonify({"error": "An account with this email already exists."}), 409
    account = {
        "id": str(uuid.uuid4()),
        "name": name,
        "email": email,
        "password_hash": generate_password_hash(password),
        "role": role,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_local_account(account)
    session.clear()
    session["principal"] = {"id": account["id"], "name": name, "role": role}
    csrf_token()
    return jsonify({"message": "Registration complete.", "redirect": "/"})


@app.post("/api/auth/login")
def local_login():
    if not demo_auth_enabled():
        return jsonify({"error": "Local login is disabled."}), 404
    if not validate_local_auth_csrf():
        return jsonify({"error": "The request failed CSRF validation. Refresh the page and try again."}), 403
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    account = next(
        (item for item in load_local_accounts() if item.get("email") == email),
        None,
    )
    if not account or not check_password_hash(str(account.get("password_hash", "")), password):
        return jsonify({"error": "Email or password is incorrect."}), 401
    session.clear()
    session["principal"] = {
        "id": account["id"],
        "name": account["name"],
        "role": account["role"] if account.get("role") in ROLES else "Citizen",
    }
    csrf_token()
    return jsonify({"message": "Signed in.", "redirect": "/"})


@app.post("/api/auth/forgot")
def local_forgot():
    if not demo_auth_enabled():
        return jsonify({"error": "Password recovery is disabled."}), 404
    if not validate_local_auth_csrf():
        return jsonify({"error": "The request failed CSRF validation. Refresh the page and try again."}), 403
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        return jsonify({"error": "Enter a valid email address."}), 400
    if not resend_is_configured():
        logger.error("Password reset email is unavailable because Resend is not configured.")
        return jsonify({
            "error": "Password reset email is not configured. Contact support.",
        }), 503

    base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    parsed_base_url = urlparse(base_url)
    if (
        not parsed_base_url.hostname
        or parsed_base_url.username
        or parsed_base_url.password
        or parsed_base_url.query
        or parsed_base_url.fragment
        or parsed_base_url.path not in ("", "/")
        or (
            parsed_base_url.scheme not in {"https", "http"}
            or (
                parsed_base_url.scheme != "https"
                and parsed_base_url.hostname not in {"localhost", "127.0.0.1"}
            )
        )
    ):
        logger.error("Password reset email is unavailable because PUBLIC_BASE_URL is invalid.")
        return jsonify({
            "error": "Password reset email is not configured. Contact support.",
        }), 503

    account = next(
        (item for item in load_local_accounts() if item.get("email") == email),
        None,
    )
    if account:
        token = secrets.token_urlsafe(32)
        token_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
        LOCAL_RESET_TOKENS[token_key] = {
            "email": email,
            "expires": time.time() + 30 * 60,
        }
        reset_url = (
            f"{base_url}{url_for('auth_account')}?"
            f"{urlencode({'token': token})}"
        )
        try:
            send_password_reset_email(email, reset_url)
        except EmailDeliveryError as exc:
            LOCAL_RESET_TOKENS.pop(token_key, None)
            logger.error("Password reset email delivery failed: %s", exc)
            return jsonify({
                "error": "Password reset email could not be sent. Please try again later.",
            }), 503
    return jsonify({
        "message": (
            "If an account exists for this email, reset instructions will arrive shortly. "
            "Check your inbox and spam folder."
        ),
    })


@app.post("/api/auth/reset")
def local_reset():
    if not demo_auth_enabled():
        return jsonify({"error": "Password recovery is disabled."}), 404
    if not validate_local_auth_csrf():
        return jsonify({"error": "The request failed CSRF validation. Refresh the page and try again."}), 403
    payload = request.get_json(silent=True) or {}
    token = str(payload.get("token", "")).strip()
    password = str(payload.get("password", ""))
    if len(password) < 8:
        return jsonify({"error": "Password must contain at least 8 characters."}), 400
    token_key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    reset = LOCAL_RESET_TOKENS.get(token_key)
    if not reset or reset["expires"] < time.time():
        LOCAL_RESET_TOKENS.pop(token_key, None)
        return jsonify({"error": "This reset token is invalid or has expired."}), 400
    account = next(
        (item for item in load_local_accounts() if item.get("email") == reset["email"]),
        None,
    )
    if not account:
        return jsonify({"error": "This account no longer exists."}), 400
    account["password_hash"] = generate_password_hash(password)
    save_local_account(account)
    LOCAL_RESET_TOKENS.pop(token_key, None)
    return jsonify({"message": "Password updated. You can now sign in.", "redirect": "/auth/account"})


@app.post("/api/demo-role")
def demo_role():
    if not demo_auth_enabled():
        return jsonify({"error": "Demo role switching is disabled."}), 404
    payload = request.get_json(silent=True) or {}
    role = str(payload.get("role", ""))
    if role not in ROLES:
        return jsonify({"error": "Choose a configured demo role."}), 400
    session["principal"] = {"id": "local-demo", "name": "Local development", "role": role}
    session.pop("demo_signed_out", None)
    return jsonify({"user": session["principal"], "clearance": ROLE_MAX_CLEARANCE[role]})


@app.get("/api/documents")
def list_documents():
    clearance = effective_clearance()
    if clearance is None:
        return jsonify({"error": "Sign in with an authorized department account."}), 403
    documents = load_documents()
    visible = [{
        key: document.get(key) for key in (
            "id", "title", "version", "status", "classification",
            "owner", "date", "uploaded_at", "repository", "document_type",
        )
    } for document in documents if isinstance(document.get("clearance_rank"), int)
        and document["clearance_rank"] <= clearance]
    return jsonify({"documents": visible, "repositories": sorted({d["repository"] for d in visible})})


@app.get("/api/source/<document_id>")
def get_source(document_id):
    if "text/html" in request.headers.get("Accept", "").lower():
        return redirect(url_for("document_viewer", document_id=document_id))
    document, status = authorized_source(document_id, "source_access")
    if status == 403:
        return jsonify({"error": "Sign in with an authorized department account."}), 403
    if status == 404:
        return jsonify({"error": "Source not found or not authorised."}), 404
    return jsonify({key: value for key, value in document.items() if key != "clearance_rank"})


@app.get("/api/source/<document_id>/content")
def get_source_content(document_id):
    document, status = authorized_source(document_id, "source_content_access")
    if status == 403:
        return jsonify({"error": "Sign in with an authorized department account."}), 403
    if status == 404:
        return jsonify({"error": "Source not found or not authorised."}), 404
    return Response(
        document.get("text", ""),
        content_type="text/plain; charset=utf-8",
        headers={"X-Content-Type-Options": "nosniff"},
    )


def authorized_source(document_id, event_type):
    clearance = effective_clearance()
    if clearance is None:
        return None, 403
    document = next((item for item in load_documents() if item["id"] == document_id), None)
    if (
        not document
        or not isinstance(document.get("clearance_rank"), int)
        or document["clearance_rank"] > clearance
    ):
        audit_event(event_type, document_id, "denied")
        return None, 404
    audit_event(event_type, document_id)
    return document, None


def document_content_blocks(text):
    blocks = []
    paragraph = []

    def flush_paragraph():
        if paragraph:
            blocks.append({"type": "paragraph", "text": " ".join(paragraph)})
            paragraph.clear()

    for line in text.splitlines():
        line = line.strip()
        if not line:
            flush_paragraph()
            continue
        heading = re.match(r"^(\d+(?:\.\d+)*)(?:[.)])?\s+(.+)$", line)
        if heading:
            flush_paragraph()
            blocks.append({
                "type": "heading",
                "number": heading.group(1),
                "text": heading.group(2).strip(),
            })
        else:
            paragraph.append(line)
    flush_paragraph()
    return blocks


def document_key_points(text, limit=4):
    key_points = []
    for block in document_content_blocks(text):
        if block["type"] != "paragraph":
            continue
        sentences = re.split(r"(?<=[.!?])\s+", block["text"])
        first_sentence = next((sentence.strip() for sentence in sentences if sentence.strip()), "")
        if first_sentence:
            key_points.append(first_sentence)
        if len(key_points) >= limit:
            break
    return key_points


def matches_source_filter(document, source_filter):
    repository = str(document.get("repository", "")).casefold()
    document_type = str(document.get("document_type", "")).casefold()
    title = str(document.get("title", "")).casefold()
    searchable = f"{title} {document_type} {repository}"
    if source_filter == "policies":
        return "policy" in repository or document_type == "policy"
    if source_filter == "gazettes":
        return "gazette" in repository
    if source_filter == "legislation":
        return bool(re.search(r"\b(act|bill|legislation|regulation|statute)\b", searchable))
    if source_filter == "grants_benefits":
        return bool(re.search(r"\b(grant|grants|benefit|benefits|nsfas|sassa)\b", searchable))
    if source_filter == "department_websites":
        return bool(document.get("source_url") or document.get("page_url")) or (
            "department" in repository or "intranet" in repository
        )
    return True


@app.get("/documents/<document_id>")
def document_viewer(document_id):
    document, status = authorized_source(document_id, "source_view")
    if status:
        abort(status)
    return render_template(
        "document_viewer.html",
        document=document,
        content_blocks=document_content_blocks(document.get("text", "")),
        key_points=document_key_points(document.get("text", "")),
    )


@app.get("/documents/<document_id>/download")
def download_document(document_id):
    document, status = authorized_source(document_id, "source_download")
    if status:
        abort(status)
    title = secure_filename(document.get("title", "policy-document")) or "policy-document"
    content = (
        f'{document.get("title", "Policy document")}\n'
        f'Type: {document.get("document_type", "Document")}\n'
        f'Version: {document.get("version", "Unknown")}\n'
        f'Owner: {document.get("owner", "Unknown")}\n'
        f'Classification: {document.get("classification", "Unknown")}\n'
        f'Publication date: {document.get("date", "Unknown")}\n\n'
        f'{document.get("text", "")}'
    )
    return Response(
        content,
        content_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{title}.txt"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.post("/api/chat")
def chat():
    payload = request.get_json(silent=True) or {}
    question = str(payload.get("question", "")).strip()
    if not question or len(question) > 2000:
        return jsonify({"error": "Enter a question of up to 2,000 characters."}), 400
    if PERSONAL_DATA_RE.search(question) or PERSONAL_DATA_REQUEST_RE.search(question):
        audit_event("privacy_block", outcome="denied")
    clearance = effective_clearance()
    if clearance is None:
        return jsonify({"error": "Sign in with an authorized department account."}), 403

    documents = load_documents()
    source_filters = payload.get("source_filters", ["all"])
    if (
        not isinstance(source_filters, list)
        or not source_filters
        or any(not isinstance(item, str) or item not in SOURCE_FILTERS for item in source_filters)
    ):
        return jsonify({"error": "Choose one or more valid source categories."}), 400
    if "all" not in source_filters:
        documents = [
            document
            for document in documents
            if any(matches_source_filter(document, item) for item in source_filters)
        ]
    document_id = payload.get("document_id")
    if document_id is not None:
        if not isinstance(document_id, str) or not document_id or len(document_id) > 200:
            return jsonify({"error": "Choose a valid document context."}), 400
        document, status = authorized_source(document_id, "document_chat_context")
        if status:
            return jsonify({"error": "Document not found or not authorised."}), status
        documents = [document]
    history = payload.get("history", [])
    if not isinstance(history, list):
        return jsonify({"error": "Conversation history must be a list."}), 400
    history = [
        {"role": item["role"], "content": item["content"][:300]}
        for item in history[-12:]
        if isinstance(item, dict)
        and item.get("role") == "user"
        and isinstance(item.get("content"), str)
    ]
    should_search_web = (
        document_id is None
        and ("all" in source_filters or "department_websites" in source_filters)
        and groq_web_search_enabled()
        and bool(os.getenv("GROQ_API_KEY", "").strip())
    )
    answer, passages, provider = answer_question(
        question,
        history,
        documents,
        clearance,
        document_context=document_id is not None,
        web_search_enabled=should_search_web,
    )
    audit_event(
        "chat",
        document_id,
        outcome="answered" if passages else "no_evidence",
    )
    current_versions = {}
    for document in documents:
        key = document.get("version_family", document["title"].casefold())
        if document["status"] == "Current":
            current_versions[key] = document["version"]
    citations = []
    warnings = []
    if provider.startswith("Local extractive retrieval (AI provider unavailable"):
        warnings.append("AI answer generation is unavailable; the response was extracted from the cited passages.")
    elif provider.startswith("Local extractive retrieval (AI answer rejected"):
        warnings.append("The AI answer failed citation checks; the response was extracted from the cited passages.")
    if "official web search unavailable" in provider.lower() or provider == "Groq web search unavailable":
        warnings.append("Live search is unavailable; only indexed project documents could support this response.")
    elif "no relevant official web results" in provider.lower() or provider == "Groq web search (no relevant official results)":
        warnings.append("No relevant official website results were found; indexed project documents were searched.")
    elif "web search skipped for non-public context" in provider.lower():
        warnings.append("Live search was skipped because matching project sources are not public.")
    for item in passages:
        citation = {
            "ref": item["ref"],
            "document_id": item["document_id"],
            "title": item["title"],
            "section": item["section"],
            "version": item["version"],
            "status": item["status"],
            "classification": item["classification"],
            "owner": item["owner"],
            "repository": item["repository"],
            "date": item["date"],
            "source_url": item.get("source_url"),
            "page_url": item.get("page_url"),
            "source_type": item.get("source_type", "project_document"),
        }
        citations.append(citation)
        if item["status"] == "Superseded":
            newer = current_versions.get(item.get("version_family", item["title"].casefold()))
            warnings.append(
                f'{item["title"]} version {item["version"]} is superseded'
                + (f" by current version {newer}." if newer else ".")
            )
        elif item["status"] == "Draft":
            warnings.append(f'{item["title"]} version {item["version"]} is a draft and is not approved.')
        elif item["status"] == "Unverified":
            warnings.append(
                f'{item["title"]} was found on an official government website, '
                "but its current publication status could not be verified."
            )
    related_documents = list({
        item["document_id"]: {
            "id": item["document_id"],
            "title": item["title"],
            "document_type": item.get("document_type", "Document"),
            "version": item["version"],
            "repository": item["repository"],
            "owner": item["owner"],
        }
        for item in passages
    }.values())
    confidence_score = (
        round(min(100, max(0, max(item["score"] for item in passages) * 100)))
        if passages else None
    )
    return jsonify({
        "answer": answer,
        "citations": citations,
        "related_documents": related_documents,
        "relevant_department": citations[0]["owner"] if citations else None,
        "confidence_score": confidence_score,
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
        audit_event("document_ingestion", document["id"])
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
        audit_event("web_ingestion", document["id"])
        return jsonify({"message": "Web page indexed.", "document": {k: v for k, v in document.items() if k != "text"}}), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Web ingestion failed")
        return jsonify({"error": "The web page could not be imported."}), 500


@app.get("/api/govza/categories")
def govza_categories():
    return jsonify({"categories": sorted(GOVZA_CATEGORIES)})


@app.get("/api/govza/departments")
def govza_departments():
    try:
        return jsonify({"departments": list_national_departments()})
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 502


@app.post("/api/ingest-department")
def ingest_department():
    payload = request.get_json(silent=True) or {}
    site_url = str(payload.get("url", "")).strip()
    try:
        limit = min(max(int(payload.get("limit", 2)), 1), 2)
    except (TypeError, ValueError):
        return jsonify({"error": "The document limit must be a number."}), 400
    try:
        imports = import_department_site(site_url, limit)
        if not imports:
            return jsonify({
                "error": "No policy-related PDFs were found on the selected site within the safe import limit."
            }), 404
        documents = load_documents()
        added = []
        for imported in imports:
            try:
                from govza import extract_pdf_text
                text = extract_pdf_text(imported["pdf"])
            except Exception as exc:
                logger.warning("Department PDF text extraction skipped: %s", exc)
                continue
            if not text or len(text) > 1_000_000 or PERSONAL_DATA_RE.search(text):
                continue
            document = {
                "id": uuid.uuid4().hex,
                "title": safe_text(imported["title"], "Document title", 200),
                "version_family": imported["title"].strip().casefold(),
                "section": "Full document",
                "version": "Website copy",
                "status": "Unverified",
                "classification": "Public",
                "clearance_rank": 0,
                "owner": safe_text(imported["owner"], "Department", 120),
                "date": date.today().isoformat(),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "repository": "Policy Library",
                "document_type": "Department policy/governance PDF",
                "source_url": imported["source_url"],
                "page_url": imported["page_url"],
                "text": text,
            }
            documents.append(document)
            added.append({key: value for key, value in document.items() if key != "text"})
        if not added:
            return jsonify({
                "error": "The PDFs had no usable text or contained likely personal information."
            }), 422
        save_documents(documents)
        for document in added:
            audit_event("department_import", document["id"])
        return jsonify({
            "message": f"Indexed {len(added)} public PDFs. Their publication currency is unverified.",
            "documents": added,
        }), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Department website import failed")
        return jsonify({"error": "The department website could not be imported."}), 500


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
                "version_family": imported["title"].strip().casefold(),
                "section": "Full document",
                "version": safe_text(payload.get("version", "Published"), "Version", 40),
                "status": status,
                "classification": "Public",
                "clearance_rank": 0,
                "owner": "South African Government",
                "date": iso_date,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
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
        for document in added:
            audit_event("govza_import", document["id"])
        return jsonify({"message": f"Indexed {len(added)} public gov.za PDF documents.", "documents": added}), 201
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        logger.exception("Gov.za import failed")
        return jsonify({"error": "The gov.za import could not be completed."}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
