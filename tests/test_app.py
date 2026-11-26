import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import app as application
import rag
import govza
import storage


class AssistantApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_path = Path(self.temporary.name) / "documents.json"
        self.data_path.write_text(application.DATA_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        self.data_file_patch = patch.object(application, "DATA_FILE", self.data_path)
        self.data_file_patch.start()
        self.database_url_patch = patch.object(storage, "database_url", return_value=None)
        self.database_url_patch.start()
        self.groq_key_patch = patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=False)
        self.groq_key_patch.start()
        self.client = application.app.test_client()

    def tearDown(self):
        self.groq_key_patch.stop()
        self.database_url_patch.stop()
        self.data_file_patch.stop()
        self.temporary.cleanup()

    def test_public_answer_has_citation_and_source_is_openable(self):
        response = self.client.post("/api/chat", json={"question": "What must happen before official records are destroyed?"})
        self.assertEqual(response.status_code, 200)
        result = response.get_json()
        self.assertTrue(result["citations"])
        self.assertIn("[C1]", result["answer"])
        citation = result["citations"][0]
        source = self.client.get(f'/api/source/{citation["document_id"]}')
        self.assertEqual(source.status_code, 200)
        self.assertEqual(source.get_json()["version"], citation["version"])

    def test_classification_filters_documents_and_retrieval(self):
        documents = application.load_documents()
        documents.append({
            "id": "secret-sample",
            "title": "Synthetic Restricted Sample",
            "section": "1 — Sample",
            "version": "1.0",
            "status": "Current",
            "classification": "Secret",
            "clearance_rank": application.CLEARANCES["Secret"],
            "owner": "Test",
            "date": "2025-01-01",
            "repository": "Policy Library",
            "document_type": "Test",
            "text": "Synthetic secret access test material.",
        })
        application.save_documents(documents)
        public_docs = self.client.get("/api/documents").get_json()["documents"]
        self.assertNotIn("Responsible Use of AI Framework", [doc["title"] for doc in public_docs])
        blocked = self.client.get(
            "/api/source/secret-sample",
            headers={"X-User-Role": "Official", "X-User-Clearance": "Secret"},
        )
        self.assertEqual(blocked.status_code, 404)
        allowed = self.client.get(
            "/api/source/secret-sample",
            headers={"X-User-Role": "Administrator", "X-User-Clearance": "Secret"},
        )
        self.assertEqual(allowed.status_code, 200)
        public_answer = self.client.post(
            "/api/chat",
            json={"question": "What oversight is required when using AI?"},
        ).get_json()
        self.assertEqual(public_answer["citations"], [])
        analyst_answer = self.client.post(
            "/api/chat",
            json={"question": "What oversight is required when using AI?"},
            headers={"X-User-Role": "Policy Analyst", "X-User-Clearance": "Confidential"},
        ).get_json()
        self.assertTrue(analyst_answer["citations"])
        self.assertEqual(analyst_answer["citations"][0]["classification"], "Internal")

    def test_unknown_question_says_no_evidence(self):
        response = self.client.post(
            "/api/chat",
            json={"question": "What is the moon made of?"},
        )
        result = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(result["citations"], [])
        self.assertIn("couldn't find", result["answer"])

    def test_follow_up_reuses_recent_user_question_for_retrieval(self):
        response = self.client.post(
            "/api/chat",
            json={
                "question": "When should it be ready?",
                "history": [{"role": "user", "content": "What should be in an annual procurement plan?"}],
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["citations"][0]["title"], "Circular 07/2025: Procurement Planning")

    def test_draft_evidence_is_flagged(self):
        response = self.client.post(
            "/api/chat",
            json={"question": "According to the draft travel approval guidance, who approves domestic travel?"},
            headers={"X-User-Role": "Official", "X-User-Clearance": "Internal"},
        )
        result = response.get_json()
        self.assertTrue(result["citations"])
        self.assertTrue(any("draft" in warning.lower() for warning in result["warnings"]))

    def test_superseded_source_is_flagged_with_current_version(self):
        response = self.client.post(
            "/api/chat",
            json={"question": "What did the superseded records policy say about records disposal?"},
        )
        result = response.get_json()
        self.assertTrue(any("superseded" in warning.lower() for warning in result["warnings"]))
        self.assertTrue(any(citation["status"] == "Superseded" for citation in result["citations"]))

    def test_govza_category_parser_returns_detail_links_only(self):
        html = '''
        <a href="/documents/acts">Acts</a>
        <a href="/documents/acts/public-administration-act-7-of-2025">Public Administration Act 7 of 2025</a>
        <a href="/sites/default/files/test.pdf">Test PDF</a>
        '''
        items = govza.parse_category_items(html, limit=5, prefix="/documents/acts/")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Public Administration Act 7 of 2025")
        self.assertEqual(items[0]["url"], "https://www.gov.za/documents/acts/public-administration-act-7-of-2025")

    def test_govza_detail_parser_finds_pdf_and_title(self):
        html = '''
        <h1>Public Administration Act 7 of 2025</h1>
        01 Apr 2026
        <a href="/sites/default/files/gcis_document/202604/public-administration.pdf">PDF</a>
        '''
        title, date, pdf_links = govza.parse_detail(html)
        self.assertEqual(title, "Public Administration Act 7 of 2025")
        self.assertEqual(date, "01 Apr 2026")
        self.assertEqual(pdf_links, ["https://www.gov.za/sites/default/files/gcis_document/202604/public-administration.pdf"])

    def test_postgres_connection_is_optional_and_reads_neon_env(self):
        self.database_url_patch.stop()
        try:
            with patch.dict(os.environ, {}, clear=True):
                self.assertIsNone(storage.database_url())
            with patch.dict(os.environ, {"DATABASE_URL": "postgresql://pooled.example/db"}, clear=True):
                self.assertEqual(storage.database_url(), "postgresql://pooled.example/db")
            with patch.dict(os.environ, {
                "DATABASE_URL": "postgresql://direct.example/db",
                "DATABASE_URL_POOLED": "postgresql://pooled.example/db",
            }, clear=True):
                self.assertEqual(storage.database_url(), "postgresql://pooled.example/db")
            with patch.dict(os.environ, {"NEON_DATABASE_URL": "postgresql://neon.example/db"}, clear=True):
                self.assertEqual(storage.database_url(), "postgresql://neon.example/db")
        finally:
            self.database_url_patch.start()

    def test_vercel_ingestion_requires_persistent_database(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=False), patch.object(storage, "database_url", return_value=None):
            response = self.client.post("/api/ingest-url", json={"url": "http://localhost"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("DATABASE_URL", response.get_json()["error"])

    def test_public_assets_are_served_from_vercel_public_directory(self):
        script = self.client.get("/static/app.js")
        stylesheet = self.client.get("/static/styles.css")
        try:
            self.assertEqual(script.status_code, 200)
            self.assertIn(b"function ask", script.data)
            self.assertEqual(stylesheet.status_code, 200)
        finally:
            script.close()
            stylesheet.close()

    def test_text_upload_is_indexed_with_metadata(self):
        response = self.client.post(
            "/api/ingest",
            data={
                "file": (BytesIO(b"Section 1\n\nRecords must be reviewed annually."), "review.txt"),
                "owner": "Test department",
                "version": "1.0",
                "date": "2025-05-01",
                "repository": "Policy Library",
                "classification": "Public",
                "status": "Current",
                "document_type": "Policy",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 201)
        document_id = response.get_json()["document"]["id"]
        source = self.client.get(f"/api/source/{document_id}")
        self.assertEqual(source.status_code, 200)
        self.assertEqual(source.get_json()["owner"], "Test department")

    def test_upload_rejects_likely_personal_information(self):
        response = self.client.post(
            "/api/ingest",
            data={
                "file": (BytesIO(b"Contact the records officer at 082 123 4567."), "contact.txt"),
                "owner": "Test department",
                "version": "1.0",
                "date": "2025-05-01",
                "repository": "Policy Library",
                "classification": "Public",
                "status": "Current",
                "document_type": "Policy",
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("personal information", response.get_json()["error"].lower())

    def test_web_ingestion_rejects_non_public_hosts(self):
        response = self.client.post("/api/ingest-url", json={"url": "https://127.0.0.1/private"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("publicly routable", response.get_json()["error"])

    def test_external_llm_is_skipped_for_internal_content_and_personal_queries(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-key"}), patch.object(rag, "groq_answer") as groq:
            internal = self.client.post(
                "/api/chat",
                json={"question": "What oversight is required when using AI?"},
                headers={"X-User-Role": "Policy Analyst", "X-User-Clearance": "Confidential"},
            ).get_json()
            self.assertEqual(internal["provider"], "Local extractive retrieval")
            groq.assert_not_called()

            personal = self.client.post(
                "/api/chat",
                json={"question": "What must happen before official records are destroyed at 082 123 4567?"},
            ).get_json()
            self.assertEqual(personal["provider"], "Local extractive retrieval")
            self.assertTrue(personal["citations"])
            groq.assert_not_called()


if __name__ == "__main__":
    unittest.main()
