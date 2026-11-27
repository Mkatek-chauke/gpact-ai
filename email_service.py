import html
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


RESEND_EMAILS_URL = "https://api.resend.com/emails"


class EmailDeliveryError(RuntimeError):
    pass


def resend_is_configured():
    return bool(
        os.getenv("RESEND_API_KEY", "").strip()
        and os.getenv("RESEND_FROM_EMAIL", "").strip()
    )


def send_password_reset_email(to_email, reset_url):
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    from_email = os.getenv("RESEND_FROM_EMAIL", "").strip()
    if not api_key or not from_email:
        raise EmailDeliveryError("Resend email delivery is not configured.")

    safe_url = html.escape(reset_url, quote=True)
    payload = json.dumps({
        "from": from_email,
        "to": [to_email],
        "subject": "Reset your GPACT AI password",
        "html": (
            "<p>We received a request to reset your GPACT AI password.</p>"
            f'<p><a href="{safe_url}">Reset your password</a></p>'
            "<p>This link expires in 30 minutes and can only be used once. "
            "If you did not request this, you can ignore this email.</p>"
        ),
        "text": (
            "We received a request to reset your GPACT AI password.\n"
            f"Reset your password: {reset_url}\n"
            "This link expires in 30 minutes and can only be used once. "
            "If you did not request this, you can ignore this email."
        ),
    }).encode("utf-8")
    request = Request(
        RESEND_EMAILS_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=10) as response:
            if not 200 <= response.status < 300:
                raise EmailDeliveryError(
                    f"Resend email delivery failed with HTTP {response.status}."
                )
    except HTTPError as exc:
        raise EmailDeliveryError(
            f"Resend email delivery failed with HTTP {exc.code}."
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise EmailDeliveryError(
            "Resend email delivery failed because the service could not be reached."
        ) from exc
