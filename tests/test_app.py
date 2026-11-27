import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import app as application
import integrations
import rag
import govza
import storage
import psycopg


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

    def test_department_directory_parser_uses_official_government_hosts_only(self):
        directory_html = '''
        <a href="https://www.education.gov.za/">Basic Education</a>
        <a href="https://www.gov.za/about-government/contact-directory">Minister</a>
        <a href="https://example.com/policy">Unverified external site</a>
        '''
        with patch.object(govza, "fetch_text", return_value=directory_html):
            departments = govza.list_national_departments()
        self.assertEqual(departments, [{
            "name": "Basic Education",
            "url": "https://www.education.gov.za/",
        }])

    def test_department_directory_api_returns_official_site_options(self):
        expected = [{"name": "Basic Education", "url": "https://www.education.gov.za/"}]
        with patch.object(application, "list_national_departments", return_value=expected):
            response = self.client.get("/api/govza/departments")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["departments"], expected)

    def test_department_import_only_accepts_directory_sites_and_policy_pdfs(self):
        home_page = '''
        <a href="/policies">Policies</a>
        <a href="/files/policy-framework.pdf">Policy framework</a>
        <a href="/files/annual-report.pdf">Annual report</a>
        '''
        policies_page = '<a href="/files/policy-manual.pdf">Download</a>'
        approved = [{"name": "Basic Education", "url": "https://www.education.gov.za/"}]
        with patch.object(govza, "list_national_departments", return_value=approved), patch.object(
            govza, "_validate_official_host_url", side_effect=govza.urlparse
        ) as validate_url, patch.object(govza, "_robots_parser", return_value=object()), patch.object(
            govza, "_fetch_official_page", side_effect=[home_page, policies_page]
        ), patch.object(govza, "_fetch_official_pdf", return_value=b"%PDF sample"):
            imported = govza.import_department_site("https://www.education.gov.za/", limit=5)
        self.assertEqual(len(imported), 2)
        self.assertEqual(
            {item["source_url"] for item in imported},
            {
                "https://www.education.gov.za/files/policy-framework.pdf",
                "https://www.education.gov.za/files/policy-manual.pdf",
            },
        )
        self.assertTrue(validate_url.called)

        with patch.object(govza, "list_national_departments", return_value=approved):
            with self.assertRaisesRegex(ValueError, "official gov.za directory"):
                govza.import_department_site("https://example.com/")

    def test_unverified_department_sources_are_warned_about(self):
        documents = application.load_documents()
        documents.append({
            "id": "unverified-policy",
            "title": "Department Records Appraisal Policy",
            "section": "Full document",
            "version": "Website copy",
            "status": "Unverified",
            "classification": "Public",
            "clearance_rank": 0,
            "owner": "Basic Education",
            "date": "2025-01-01",
            "repository": "Policy Library",
            "document_type": "Department policy/governance PDF",
            "source_url": "https://www.education.gov.za/files/policy.pdf",
            "page_url": "https://www.education.gov.za/policies",
            "text": "Records must undergo a records appraisal before destruction.",
        })
        application.save_documents(documents)
        response = self.client.post(
            "/api/chat",
            json={"question": "What does the department policy say about records appraisal before destruction?"},
        )
        result = response.get_json()
        self.assertTrue(any("could not be verified" in warning for warning in result["warnings"]))
        self.assertTrue(any(citation["status"] == "Unverified" for citation in result["citations"]))
        citation = next(item for item in result["citations"] if item["status"] == "Unverified")
        self.assertEqual(citation["source_url"], "https://www.education.gov.za/files/policy.pdf")
        self.assertEqual(citation["page_url"], "https://www.education.gov.za/policies")

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
            with patch.dict(os.environ, {
                "LOCAL_JSON_STORE": "1",
                "DATABASE_URL_POOLED": "postgresql://pooled.example/db",
            }, clear=True):
                self.assertIsNone(storage.database_url())
        finally:
            self.database_url_patch.start()

    def test_postgres_document_saves_use_cursor_batch_execution(self):
        connection = MagicMock()
        connection.__enter__.return_value = connection
        cursor = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        document = {
            "id": "sample",
            "title": "Sample policy",
            "classification": "Public",
            "status": "Current",
            "text": "Sample content",
        }
        with patch.object(storage, "_connect", return_value=connection):
            storage.save_documents([document])
        cursor.executemany.assert_called_once()

    def test_vercel_ingestion_requires_persistent_database(self):
        with patch.dict(os.environ, {"VERCEL": "1"}, clear=False), patch.object(storage, "database_url", return_value=None):
            response = self.client.post("/api/ingest-url", json={"url": "http://localhost"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("DATABASE_URL", response.get_json()["error"])

    def test_database_errors_return_clear_json_without_details(self):
        with patch.object(storage, "database_url", return_value="postgresql://db.invalid"), patch.object(
            storage,
            "load_documents",
            side_effect=psycopg.OperationalError("sensitive connection detail"),
        ):
            response = self.client.get("/api/documents")
        self.assertEqual(response.status_code, 503)
        self.assertIn("DATABASE_URL_POOLED", response.get_json()["error"])
        self.assertNotIn("sensitive connection detail", response.get_data(as_text=True))

    def test_public_assets_are_served_from_vercel_public_directory(self):
        script = self.client.get("/static/app.js")
        stylesheet = self.client.get("/static/styles.css")
        accessibility = self.client.get("/static/accessibility.css")
        try:
            self.assertEqual(script.status_code, 200)
            self.assertIn(b"function ask", script.data)
            self.assertEqual(stylesheet.status_code, 200)
            self.assertEqual(accessibility.status_code, 200)
        finally:
            script.close()
            stylesheet.close()
            accessibility.close()

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

    def test_groq_base_url_removes_sdk_managed_openai_path(self):
        passage = {
            "ref": "C1",
            "title": "Sample policy",
            "section": "Section 1",
            "version": "1.0",
            "status": "Current",
            "text": "Records must be reviewed annually.",
        }
        with patch.dict(os.environ, {
            "GROQ_API_KEY": "test-key",
            "GROQ_BASE_URL": "https://api.groq.com/openai/v1/",
        }), patch("groq.Groq") as groq_client:
            groq_client.return_value.chat.completions.create.return_value.choices = [
                MagicMock(message=MagicMock(content="Records must be reviewed annually. [C1]"))
            ]
            answer = integrations.groq_answer("What is required?", [], [passage])
        self.assertIn("[C1]", answer)
        groq_client.assert_called_once_with(api_key="test-key", base_url="https://api.groq.com")


if __name__ == "__main__":
    unittest.main()
