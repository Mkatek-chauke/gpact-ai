import json
import os
from urllib.parse import urlparse

from authlib.integrations.flask_client import OAuth


ROLE_ORDER = ("Citizen", "Officer", "Manager", "Administrator")


def entra_configuration():
    tenant_id = os.getenv("ENTRA_TENANT_ID", "").strip()
    client_id = os.getenv("ENTRA_CLIENT_ID", "").strip()
    client_secret = os.getenv("ENTRA_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("ENTRA_REDIRECT_URI", "").strip()
    session_secret = os.getenv("FLASK_SECRET_KEY", "").strip()
    if not all((tenant_id, client_id, client_secret, redirect_uri, session_secret)):
        return None
    if "{" in tenant_id or "}" in tenant_id:
        return None
    parsed_redirect = urlparse(redirect_uri)
    if not parsed_redirect.hostname or parsed_redirect.username or parsed_redirect.password:
        return None
    if parsed_redirect.scheme != "https" and parsed_redirect.hostname not in {"localhost", "127.0.0.1"}:
        return None
    return {
        "tenant_id": tenant_id,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "session_secret": session_secret,
    }


def demo_auth_enabled():
    return (
        os.getenv("DEMO_AUTH_ENABLED", "").strip().lower() in {"1", "true", "yes"}
        and not os.getenv("VERCEL")
    )


def register_entra_oauth(app):
    config = entra_configuration()
    if not config:
        return None
    oauth = OAuth(app)
    issuer = f'https://login.microsoftonline.com/{config["tenant_id"]}/v2.0'
    oauth.register(
        name="entra",
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid profile email",
            "code_challenge_method": "S256",
        },
    )
    return oauth


def role_from_claims(claims):
    roles = claims.get("roles", [])
    groups = claims.get("groups", [])
    if isinstance(roles, str):
        roles = [roles]
    if isinstance(groups, str):
        groups = [groups]
    try:
        group_map = json.loads(os.getenv("ENTRA_GROUP_ROLE_MAP", "{}"))
    except json.JSONDecodeError:
        group_map = {}
    candidates = {role for role in roles if role in ROLE_ORDER}
    candidates.update(
        group_map[group]
        for group in groups
        if isinstance(group_map, dict) and group in group_map and group_map[group] in ROLE_ORDER
    )
    for role in reversed(ROLE_ORDER):
        if role in candidates:
            return role
    return "Citizen"


def principal_from_claims(claims):
    subject = claims.get("oid") or claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("The identity provider token has no subject.")
    return {
        "id": subject,
        "name": str(claims.get("name") or claims.get("preferred_username") or "Government user")[:160],
        "role": role_from_claims(claims),
    }
