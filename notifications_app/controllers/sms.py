"""
The ONE Yo Uganda SMS sender.

Everything SMS goes through here so the gateway contract is enforced in exactly
one place:

- The gateway reports outcomes INSIDE HTTP 200 bodies, urlencoded.
  ``ybs_autocreate_status=OK`` is the ONLY success signal — HTTP 200 is NOT
  success. Per-destination states arrive in ``ybs_autocreate_message`` as
  ``<msisdn>:<STATE>`` (verified live 2026-07-20).
- Parameters travel via ``requests`` ``params`` (never f-string interpolation),
  so an ``&`` or ``#`` inside a message cannot corrupt the request.
- One retry on ``requests.RequestException`` only — a transport blip may
  deserve a second attempt; a non-2xx or a parsed gateway failure is a
  definitive answer and is never retried.
- Diagnostics are SANITIZED AT THE SOURCE (D11 B1). A log line carries a fixed
  event category plus bounded, allowlisted metadata only: the numeric HTTP
  status, the attempt number, a count of reported destinations, a closed
  gateway-status category and a closed transport-error category. It never
  carries the destination, the message (an OTP), the gateway URL or query (the
  account password travels as a query parameter, and ``requests`` exception
  text quotes the full URL), the response body, an arbitrary provider string or
  the raw exception. Masking or truncating would not be enough: the body and the
  exception text are not phone-shaped.
- ``capture`` is a SEPARATE, deliberate contract: the operator command
  ``send_test_sms`` asks for the raw exchange and prints it to its own stdout.
  Nothing about it changed.
"""
import logging
import time
from urllib.parse import parse_qs

import requests
from decouple import config

logger = logging.getLogger(__name__)

YO_SMS_URL = 'http://smgw1.yo.co.ug:9100/sendsms'
DEFAULT_TIMEOUT = 10  # seconds
RETRY_BACKOFF_SECONDS = 0.5

# The gateway status words a log line may repeat. Anything else the provider says
# is reduced to a category, never echoed.
_KNOWN_GATEWAY_STATUSES = frozenset({'OK', 'ERROR'})


def _numeric_status(value):
    """The HTTP status as a plain int, or -1 when it is not one."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return -1


def _gateway_status_category(parsed):
    values = parsed.get('ybs_autocreate_status')
    if not values:
        return 'missing'
    if len(values) == 1 and values[0] in _KNOWN_GATEWAY_STATUSES:
        return values[0]
    return 'unrecognised'


def _transport_error_category(exc):
    # Timeout first: ConnectTimeout is both a Timeout and a ConnectionError.
    if isinstance(exc, requests.Timeout):
        return 'timeout'
    if isinstance(exc, requests.ConnectionError):
        return 'connection'
    return 'other'


def send_sms(
    message: str,
    msisdn: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    bypass_env_gate: bool = False,
    capture: dict = None,
) -> bool:
    """Send one SMS through the Yo gateway. Returns the TRUTH.

    True  = the gateway accepted the message (``ybs_autocreate_status=OK``),
            or sending is not applicable in this environment (ENV gate below).
    False = transport failure after one retry, non-2xx, or a parsed gateway
            failure.

    ``bypass_env_gate`` exists ONLY for the ``send_test_sms`` management
    command, which verifies gateway credentials BEFORE an ENV change and so
    cannot be gated by the flag it exists to test. ``capture``, when a dict,
    receives the raw exchange (``status_code``/``body``) for that command's
    human-readable report — production callers never pass it.
    """
    env = config('ENV', default='dev')
    if not bypass_env_gate and env not in ['prod', 'test']:
        # "True" here means "not applicable in this environment" — dev logins
        # depend on this short-circuit staying a success.
        logger.info("SMS skipped: ENV=%s", env)
        return True

    params = {
        'ybsacctno': config('YO_SMS_ACCOUNT_NO'),
        'password': config('YO_SMS_PASSWORD'),
        'origin': 'Dinify',
        'sms_content': message,
        'destinations': msisdn,
        'nostore': 0,
    }

    response = None
    for attempt in (1, 2):  # exactly one retry, transport errors only
        try:
            response = requests.get(YO_SMS_URL, params=params, timeout=timeout)
            break
        except requests.RequestException as exc:
            logger.error(
                "SMS transport error (attempt %d of 2, category=%s)",
                attempt, _transport_error_category(exc),
            )
            if attempt == 1:
                time.sleep(RETRY_BACKOFF_SECONDS)
    if response is None:
        return False

    body = response.text or ''
    if capture is not None:
        capture['status_code'] = response.status_code
        capture['body'] = body
    parsed = parse_qs(body)
    destination_states = parsed.get('ybs_autocreate_message', [])

    if 200 <= response.status_code < 300 and parsed.get('ybs_autocreate_status') == ['OK']:
        logger.info(
            "SMS accepted by gateway (destination states reported=%d)",
            len(destination_states),
        )
        return True

    logger.error(
        "SMS gateway rejected send (HTTP %d, gateway_status=%s)",
        _numeric_status(response.status_code),
        _gateway_status_category(parsed),
    )
    return False
