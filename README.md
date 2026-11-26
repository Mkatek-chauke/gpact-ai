# GPACT AI — Government Knowledge & Policy Assistant

A runnable demonstration of a source-grounded government policy assistant. It includes a conversational interface, citations linked to source documents, version/status warnings, simulated repositories, classification-aware retrieval, and public document ingestion.

> **Demo safety:** The included corpus is synthetic/public-style sample content. Do not upload classified, confidential, or personal information. The identity/clearance selector is for demonstration only; production deployments must use trusted SSO identity and centrally enforced MISS access controls. This assistant informs users and does not make government decisions.

## Run locally with the JSON store

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000). Without `DATABASE_URL`, the app uses the local JSON corpus and extractive answers.

## Use Neon Postgres as the document store

Neon is a hosted Postgres service. Configure the full Neon pooled Postgres connection URI as `DATABASE_URL_POOLED` (preferred) or `DATABASE_URL`. The URI should include the `postgresql://` scheme, database role, password, host, and database name. Install the Python driver and load the value into your process environment:

```powershell
pip install -r requirements.txt
neon env pull
$env:DATABASE_URL = (Get-Content .env.local | Where-Object { $_ -match '^DATABASE_URL=' } | Select-Object -First 1).Split('=', 2)[1].Trim('"')
python app.py
```

If Neon writes the URL to `.env`, use that file instead. Keep `.env` and `.env.local` private; they are gitignored. The app creates a `government_documents` table, seeds it from the included JSON corpus when the database is empty, and then reads/writes documents in Postgres. Without a Neon URL it falls back to the local JSON store. Use a Neon pooled connection string for web deployments.

The Neon project is linked to organization `org-tiny-salad-44594756`, project `crimson-voice-69979260`, branch `production`; `neon.ts` has no paid services or branch policies configured.

## Deploy on Vercel

The repo includes `vercel.json`, a Flask/Python entrypoint, and CDN-served assets under `public/static/`. In Vercel's **New Project** screen, select this GitHub repo, keep Root Directory as `./`, choose the **Flask** preset, leave Output Directory blank, and deploy. The configured Build Command is `npm run build`.

For persistent uploads and gov.za imports, connect Neon Postgres to the Vercel project or add the Neon **pooled** connection string as the `DATABASE_URL` environment variable in Vercel Project Settings → Environment Variables, for Production and Preview as appropriate. Redeploy after adding/changing it. Never commit or paste `.env` values into source code. Without `DATABASE_URL`, chat can use the bundled sample corpus, but serverless ingestion returns HTTP 503 rather than attempting to write to Vercel's temporary filesystem.

Vercel hosting and Neon are cloud services; requests and any indexed public documents are processed/stored off your laptop. Only use public or approved material. Do not deploy classified or confidential information.

## Optional Groq and Neo4j integrations

These are not required for Neon Postgres or for the local extractive-answer mode. Neo4j remains an optional metadata mirror; Groq is used only for eligible public-source answer phrasing.

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
