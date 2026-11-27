import json
import os
import unittest
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

import email_service


class ResendEmailTests(unittest.TestCase):
    def test_password_reset_email_uses_configured_resend_sender_and_payload(self):
        response = MagicMock()
        response.status = 200
        with patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "test-key",
                "RESEND_FROM_EMAIL": "GPACT <reset@example.test>",
            },
            clear=False,
        ), patch.object(email_service, "urlopen") as send:
            send.return_value.__enter__.return_value = response
            email_service.send_password_reset_email(
                "citizen@example.test",
                "https://gpact.example/auth/account?token=one-time-token",
            )

        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, email_service.RESEND_EMAILS_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(payload["from"], "GPACT <reset@example.test>")
        self.assertEqual(payload["to"], ["citizen@example.test"])
        self.assertIn("one-time-token", payload["text"])
        self.assertIn("one-time-token", payload["html"])

    def test_resend_http_error_is_reported_without_response_body(self):
        with patch.dict(
            os.environ,
            {
                "RESEND_API_KEY": "test-key",
                "RESEND_FROM_EMAIL": "GPACT <reset@example.test>",
            },
            clear=False,
        ), patch.object(
            email_service,
            "urlopen",
            side_effect=HTTPError(
                email_service.RESEND_EMAILS_URL,
                403,
                "Forbidden",
                {},
                None,
            ),
        ):
            with self.assertRaisesRegex(email_service.EmailDeliveryError, "HTTP 403"):
                email_service.send_password_reset_email(
                    "citizen@example.test",
                    "https://gpact.example/auth/account?token=one-time-token",
                )
