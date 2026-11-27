import os
import unittest
from unittest.mock import patch

from auth import demo_auth_enabled, entra_configuration, principal_from_claims, role_from_claims


class EntraIdentityTests(unittest.TestCase):
    def test_role_comes_from_validated_roles_or_configured_groups(self):
        with patch.dict(os.environ, {"ENTRA_GROUP_ROLE_MAP": '{"group-manager":"Manager"}'}):
            self.assertEqual(role_from_claims({"roles": ["Officer"]}), "Officer")
            self.assertEqual(role_from_claims({"groups": ["group-manager"]}), "Manager")
            self.assertEqual(role_from_claims({"roles": ["Unrecognized"]}), "Citizen")

    def test_principal_requires_a_stable_subject(self):
        self.assertEqual(
            principal_from_claims({"oid": "user-1", "name": "Officer", "roles": ["Officer"]}),
            {"id": "user-1", "name": "Officer", "role": "Officer"},
        )
        with self.assertRaises(ValueError):
            principal_from_claims({"name": "No subject"})

    def test_entra_configuration_requires_redirect_uri_and_server_secret(self):
        values = {
            "ENTRA_TENANT_ID": "tenant-id",
            "ENTRA_CLIENT_ID": "client-id",
            "ENTRA_CLIENT_SECRET": "client-secret",
            "ENTRA_REDIRECT_URI": "https://assistant.example.gov/auth/callback",
            "FLASK_SECRET_KEY": "session-secret",
        }
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(entra_configuration()["redirect_uri"], values["ENTRA_REDIRECT_URI"])
        with patch.dict(os.environ, {key: value for key, value in values.items() if key != "FLASK_SECRET_KEY"}, clear=True):
            self.assertIsNone(entra_configuration())

    def test_demo_auth_is_never_enabled_on_vercel(self):
        with patch.dict(os.environ, {"DEMO_AUTH_ENABLED": "1"}, clear=True):
            self.assertTrue(demo_auth_enabled())
        with patch.dict(os.environ, {"DEMO_AUTH_ENABLED": "1", "VERCEL": "1"}, clear=True):
            self.assertFalse(demo_auth_enabled())


if __name__ == "__main__":
    unittest.main()
