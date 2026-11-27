# GPACT AI — Government Knowledge & Policy Assistant

A runnable demonstration of a source-grounded government policy assistant. It includes a conversational interface, citations linked to source documents, version/status warnings, simulated repositories, classification-aware retrieval, and public document ingestion.

> **Safety and deployment status:** The included corpus is synthetic/public-style sample content. Local demo roles are not authentication and are disabled on Vercel. Production sign-in requires Microsoft Entra configuration. The repository connectors are not yet implemented; do not ingest or store classified, confidential, or personal information. The assistant informs users and does not make government decisions.

## Run locally with the JSON store

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:LOCAL_JSON_STORE = "1"
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). `LOCAL_JSON_STORE=1` forces the local JSON corpus and extractive answers even if a database URL is present in `.env`. For a local-only role preview, set `DEMO_AUTH_ENABLED=1`; the server then provides fixed Citizen, Officer, Manager, and Administrator role limits. Demo authentication is never enabled on Vercel.

Copy `.env.example` to `.env` for local settings. Never commit `.env` or paste secrets into source files.

## Microsoft Entra sign-in

Production use requires an Entra app registration and these server environment variables: `ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID`, `ENTRA_CLIENT_SECRET`, `ENTRA_REDIRECT_URI`, and a high-entropy `FLASK_SECRET_KEY`. Register the exact redirect URI `https://<your-domain>/auth/callback`; use the local URI shown in `.env.example` for local sign-in. Configure app roles named `Citizen`, `Officer`, `Manager`, and `Administrator`, or set `ENTRA_GROUP_ROLE_MAP` to a JSON mapping of Entra group object IDs to those roles. Unrecognized or missing roles receive Citizen access. Never put client secrets in browser code.

The session stores only a normalized user ID, display name, and role; it does not store provider tokens. All state-changing browser requests require a session CSRF token. Vercel production requests fail closed and show a setup error until Entra is configured. Configure every variable in Vercel Project Settings and redeploy after changes.

## Use Neon Postgres as the document store

Neon is a hosted Postgres service. Configure the full Neon pooled Postgres connection URI as `DATABASE_URL_POOLED` (preferred) or `DATABASE_URL`. The URI should include the `postgresql://` scheme, database role, password, host, and database name. Install the Python driver and load the value into your process environment:

```powershell
pip install -r requirements.txt
neon env pull
$env:DATABASE_URL = (Get-Content .env.local | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1).Split('=', 2)[1].Trim('"')
python app.py
```

If Neon writes the URL to `.env`, use that file instead. Keep `.env` and `.env.local` private; they are gitignored. The app creates a `government_documents` table, seeds it from the included JSON corpus when the database is empty, and then reads/writes documents in Postgres. Without a Neon URL it falls back to the local JSON store. Use a Neon pooled connection string for web deployments.

## Deploy on Vercel

The repo includes `vercel.json`, a Flask/Python entrypoint, and CDN-served assets under `public/static/`. In Vercel's **New Project** screen, select this GitHub repo, keep Root Directory as `./`, choose the **Flask** preset, leave Output Directory blank, and deploy. The configured Build Command is `npm run build`.

Add the Neon **pooled** connection string as `DATABASE_URL_POOLED` or `DATABASE_URL` in Vercel Project Settings → Environment Variables, for Production and Preview as appropriate. Protected document reads, chat, rate limits, and audit events require persistent Postgres; APIs return HTTP 503 when it is missing rather than silently using bundled or ephemeral data. Redeploy after adding/changing it. Never commit or paste `.env` values into source code.

Vercel hosting and Neon are cloud services; requests and any indexed public documents are processed/stored off your laptop. Only use public or approved material. Do not deploy classified or confidential information.

## Optional Groq and Neo4j integrations

These are not required for Neon Postgres or for the local extractive-answer mode. Neo4j remains an optional metadata mirror; Groq is used only for eligible public-source answer phrasing.

## Included features

- Synthetic governance documents, metadata, versions, and status examples in the local corpus.
- Follow-up questions, plain-language answers, and linked citations identifying document, section, and version.
- Retrieval is filtered by the authenticated role's fixed classification limit before ranking; unknown classification values fail closed. Draft and superseded material is visibly flagged.
- “I don't know” responses when no sufficiently relevant authorized evidence is found.
- Upload PDF, DOCX, TXT, and Markdown files, or import a public web page. Metadata includes owner, date, status, repository, version, and classification.
- Scanned PDFs use `pytesseract` and `pdf2image`; install the Tesseract OCR and Poppler binaries and add them to `PATH` to enable OCR.
- Obvious email, phone-number, and national-ID patterns and direct requests for employee identifiers or home addresses are blocked before retrieval. Conversation history and non-public passages are not sent to Groq.
- The ingestion dialog includes a `gov.za` importer for public Acts, Bills, draft bills, notices, papers, reports, comments and other public documents.
- Gov.za imports are restricted to the `gov.za`/`www.gov.za` hosts, HTTPS, public classification, 15 MB PDFs, one to five documents per request, and readable PDF text.
- The ingestion dialog can also load the current national-department website directory from gov.za and import up to two policy-related PDFs from one selected department per run. It stays on the directory-listed HTTPS host, checks robots.txt, and marks publication currency as `Unverified`; check the original source before relying on it.
- Citations for imported public PDFs open the original PDF, with the department page retained as additional source context. Scanned/image-only PDFs need OCR tooling to be configured; otherwise they cannot be indexed as text.
- Chat searches only indexed project documents that the user is authorised to access; it does not browse live websites or use hardcoded source URLs. Retrieval tolerates common spelling and wording differences. When configured, Groq phrases answers only from retrieved excerpts; if AI generation fails citation checks or is unavailable, the UI identifies the extractive fallback. If no relevant evidence is found, the assistant returns: "I couldn't find supporting information for that question in the official government sources currently available."
- The department importer is deliberately bounded to one policy-related page and two PDFs per request; check original sources before relying on them.
- No question history is persisted, and source/query content is not written to application logs. Decision requests are answered with policy information rather than an approval/rejection.
- Uploaded PDFs are limited to 200 pages; scanned PDFs are limited to 40 pages and OCR uses one worker, 150 DPI, and a 30-second conversion timeout. Tesseract and Poppler must be installed locally for scanned-PDF OCR.

## Not yet implemented

- SharePoint Graph and OpenText Content Suite REST connectors; current repository names are metadata labels, not live integrations.
- pgvector search; the current Neon-backed document store uses JSONB with lexical retrieval.
- Automated retention/rotation for database-backed rate-limit and audit records.
- Automated policy comparison, conflict detection, multilingual generation, and grounded checklist/memo drafting.
- Production OCR verification on a machine with Tesseract and Poppler installed.

The local JSON store and in-memory development rate limits are intended for demonstration, not multi-user production. Vercel uses database-backed rate limits and audit events keyed by a one-way hash of the identity; question text is not stored in those records. Do not treat this build as operationally compliant until the missing connectors, vector search, audit retention/rotation, organizational encryption policies, and production identity configuration are implemented and independently validated.
