# GPACT AI — Government Knowledge & Policy Assistant

A runnable, local-first demonstration of a source-grounded government policy assistant. It includes a conversational interface, citations linked to source documents, version/status warnings, simulated repositories, classification-aware retrieval, document ingestion, and optional Groq and Neo4j integrations.

> **Demo safety:** The included corpus is synthetic/public-style sample content. Do not upload classified, confidential, or personal information. The identity/clearance selector is for demonstration only; production deployments must use trusted SSO identity and centrally enforced MISS access controls. This assistant informs users and does not make government decisions.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). The app works without external service credentials using its local corpus and extractive answers.

## Optional integrations

Set environment variables before starting the app:

```powershell
$env:GROQ_API_KEY = "your-key"
$env:GROQ_MODEL = "llama-3.3-70b-versatile"
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USERNAME = "neo4j"
$env:NEO4J_PASSWORD = "your-password"
python app.py
```

Groq is used only to phrase answers from retrieved excerpts; source references are validated and unsupported/unreferenced output falls back to extractive answers. Neo4j receives document and chunk metadata when configured. The JSON corpus remains the local source of truth.

## Included features

- Two simulated repositories with synthetic governance guidance, metadata, versions, and document statuses.
- Follow-up questions, plain-language answers, and linked citations identifying document, section, and version.
- Retrieval is filtered by the selected demo clearance before ranking; draft and superseded material is visibly flagged.
- “I don't know” responses when no sufficiently relevant authorized evidence is found.
- Upload PDF, DOCX, TXT, and Markdown files, or import a public web page. Metadata includes owner, date, status, repository, version, and classification.
- Scanned PDFs use `pytesseract` and `pdf2image`; install the Tesseract OCR and Poppler binaries and add them to `PATH` to enable OCR.
- Obvious email, phone-number, and national-ID patterns are rejected from ingested content and kept out of Groq prompts; conversation history is not sent to Groq.
- The ingestion dialog includes a `gov.za` importer for public Acts, Bills, draft bills, notices, papers, reports, comments and other public documents.
- Gov.za imports are restricted to the `gov.za`/`www.gov.za` hosts, HTTPS, public classification, 15 MB PDFs, one to five documents per request, and readable PDF text.
- No question history is persisted, and source/query content is not written to application logs.

The local JSON store is intended for demonstration, not multi-user production. Configure authentication, encryption, retention, audit controls, repository connectors, and deployment-specific policy before operational use.
