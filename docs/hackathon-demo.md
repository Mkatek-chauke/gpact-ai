# GPACT AI: Hackathon Demo Package

This package demonstrates one complete, deliberately synthetic flow. The included sample is not government policy and must not be used to guide real decisions.

## End-to-end scenario

1. Sign in with an authorised Manager or Administrator account. For a local-only demo, enable `DEMO_AUTH_ENABLED=1`, then use **Settings → Account and access → Manager**. Never use demo role switching in production.
2. Select **Upload Documents** and upload [the synthetic retention procedure](../demo-assets/records-retention-demo.txt). Set:
   - Title: `Synthetic Demo Records Retention Procedure`
   - Owner: `GPACT demo team (fictional)`
   - Version: `1.0`
   - Status: `Current`
   - Classification: `Public`
   - Repository: `Government DMS (simulated)`
   - Document type: `Policy`
3. Ask: **“How long are closed case files retained under this demo procedure?”**
4. Confirm the answer states five years and cites section **1.1 Retention Period**, version **1.0**.
5. Open citation **C1**. Confirm the document page shows the synthetic disclaimer, source text, owner, repository, version, status, and classification.
6. Ask: **“What is the moon made of?”** With no relevant source in the corpus, the assistant must return its exact no-evidence response and no citations.

The app already supports local text uploads and linked source pages. The automated test `test_demo_upload_question_citation_and_source_open` exercises this whole flow against the provided file.

## Architecture overview

```text
PDF / DOCX / TXT / Markdown / approved public pages
        ↓
Text extraction (OCR for scans only when Tesseract + Poppler are installed)
        ↓
PII pattern checks + owner/date/status/version/classification metadata
        ↓
Section-aware chunks → current prototype's JSON store or Postgres JSONB
        ↓
Role/classification filter → lexical retrieval over authorised chunks
        ↓
Optional Groq phrasing for eligible public excerpts; citation validation
        ↓
Local extractive fallback or exact no-evidence response
        ↓
Cited answer → linked document viewer for verification
```

**Current prototype:** PDF, DOCX, TXT, and Markdown upload; bounded public-page/import flows; section citations; role-filtered retrieval; draft/superseded warnings; optional Groq; local extractive fallback. The prototype's Neon store uses JSONB with lexical retrieval. It does **not** currently have a vector store or production SharePoint/OpenText connectors.

**Pilot target:** add embeddings and hybrid lexical/vector retrieval in `pgvector` (or an approved managed vector service), retain original files in approved object storage, and implement repository adapters for department DMS/intranet sources. Treat Groq as an optional model adapter; select and host models only after SITA/security approval. Entra ID app roles map users to approved classification clearances; ingestion is restricted to authorised managers. A document is filtered by clearance before ranking, and generated answers must cite retrieved excerpts.

## Scalability, security, governance, and SITA pilot

- **Scale:** move ingestion to a queued worker with retries, checksums, deduplication, incremental indexing, and version-aware updates. Use managed Postgres/vector indexing, object storage, backups, observability, and per-department tenancy boundaries. Load-test ingestion, concurrent queries, and response latency.
- **Security and POPIA:** the local JSON store and local demo roles are demonstration-only. Before operational use, configure Entra, persistent storage, TLS/encryption at rest, managed secrets, least privilege, rate limits, audit retention/rotation, backup/restore, and incident response. Complete a POPIA impact assessment, MISS/classification mapping, threat model, and independent security review. Do not ingest classified, confidential, or personal data into this prototype.
- **Content governance:** appoint a source owner for each repository; define approval, current/superseded/draft states, review dates, retention, takedown, and correction workflows. Keep provenance and version metadata with every chunk; monitor citation validity, no-evidence rate, stale-source warnings, retrieval quality, and user feedback.
- **SITA pathway:** (1) agree a pilot charter, data owner, classification boundary, hosting and residency requirements; (2) select one department and a small approved public/synthetic corpus; (3) connect a read-only simulated DMS and intranet adapter; (4) validate access controls, citations, OCR, audit, privacy, and load with department users; (5) obtain SITA/security approvals and operational ownership before expanding. Keep the assistant advisory: officials make decisions.

## Five-minute pitch and live demo

**0:00–0:40 — The problem**

“Officials spend too long finding the current policy and checking whether an answer is supported. A confident but uncited answer creates compliance risk. GPACT is designed to make the evidence visible, not replace the official.”

**0:40–1:10 — The solution**

“GPACT indexes authorised documents with ownership, version, status, and classification metadata. It retrieves only content the user can access, answers from those excerpts, and links every citation to the source. If the evidence is missing, it says so.”

**1:10–3:30 — Live demo**

- Upload the synthetic retention procedure using the metadata above.
- Ask the retention question; point out the answer, section citation, version, owner, and source link.
- Open C1 and verify the exact source text and synthetic disclaimer.
- Ask **“What is the moon made of?”** Show that GPACT declines with the no-evidence response and no source citations.

**3:30–4:20 — Architecture and safeguards**

“The current demo has local/Postgres storage, lexical retrieval, role-based classification filters, and an optional model adapter with an extractive fallback. It does not claim live SharePoint/OpenText integration or vector search yet. Those are explicit pilot work, along with Entra, persistent audit controls, and security approval.”

**4:20–5:00 — Pilot ask**

“We propose a bounded SITA pilot with one department, approved non-sensitive documents, named content owners, and measurable acceptance criteria: correct source/version citations, safe access boundaries, transparent declines, and useful response times. Expand only after the department, SITA, and security reviewers approve the evidence.”

**Demo contingency:** if the AI provider is unavailable, the UI labels the local extractive fallback. Continue the demo and explain that the source-grounded flow is functioning; do not describe the fallback as model-generated text.
