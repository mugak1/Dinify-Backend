import logging
from dinify_backend import settings
from django.core.mail import EmailMessage

from notifications_app.controllers.sms import send_sms

logger = logging.getLogger(__name__)


class Messenger():
    def __init__(self):
        self.from_email = '' + settings.EMAIL_HOST_USER

    def send_email(self, to: list, cc: list, subject: str, message: str) -> bool:
        # if to is not a list, make it a list
        if not isinstance(to, list):
            to = [to]
        email = EmailMessage(
            subject=subject,
            body=message,
            to=to,
            cc=cc,
            from_email=self.from_email
        )
        email.content_subtype = 'html'
        # fail_silently=False so an SMTP failure surfaces HERE (logged, bool
        # returned) instead of vanishing inside Django. Callers get the truth.
        try:
            email.send(fail_silently=False)
        except Exception as exc:
            logger.error("Email send failed (subject=%r, to=%s): %s", subject, to, exc)
            return False
        return True

    def send_sms(self, message: str, msisdn: str) -> bool:
        # Thin delegate — the single consolidated sender owns the gateway
        # contract (see notifications_app/controllers/sms.py).
        return send_sms(message=message, msisdn=msisdn)
