import logging
import smtplib
import socket
from dinify_backend import settings
from django.core.mail import EmailMessage

from notifications_app.controllers.sms import send_sms

logger = logging.getLogger(__name__)


def _email_error_category(exc):
    """A closed category for a send failure; never the exception's own text."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return 'smtp_auth'
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'smtp_recipients_refused'
    if isinstance(exc, smtplib.SMTPException):
        return 'smtp'
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return 'timeout'
    if isinstance(exc, OSError):
        return 'connection'
    return 'other'


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
        #
        # The log is SANITIZED AT THE SOURCE (D11 B1): counts and a closed error
        # category only. The subject (an OTP email's subject says what it is), the
        # recipients and the exception text (SMTP errors quote the refused address)
        # never reach it.
        try:
            email.send(fail_silently=False)
        except Exception as exc:
            logger.error(
                "Email send failed (recipients=%d, cc=%d, category=%s)",
                len(to), len(cc or []), _email_error_category(exc),
            )
            return False
        return True

    def send_sms(self, message: str, msisdn: str) -> bool:
        # Thin delegate — the single consolidated sender owns the gateway
        # contract (see notifications_app/controllers/sms.py).
        return send_sms(message=message, msisdn=msisdn)
