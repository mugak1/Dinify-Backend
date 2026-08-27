"""
Custom credential headers must survive a real browser CORS preflight (CORS-HEADER-00).

THE FAILURE MODE THIS CATCHES, AND WHY NOTHING ELSE DOES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Every credential on this backend that is deliberately accepted HEADER-ONLY — the diner
capability pair, the delegation pair, and the owner-claim token — is invisible to the
server unless the browser is told, at preflight, that it may send it. If a header is
missing from ``CORS_ALLOW_HEADERS`` the browser strips it and the request arrives
looking exactly like one that never carried a credential.

**No endpoint test can see this.** Django's test client, DRF's ``APIRequestFactory``
and ``curl`` all send whatever header they are handed; none of them performs a
preflight. So the feature's own suite stays entirely green while the feature is broken
for every real user. That asymmetry is the whole reason this module exists, and it is
why the assertion below is an OPTIONS request rather than
``assertIn('x-owner-claim-token', settings.CORS_ALLOW_HEADERS)`` — the latter restates
the setting rather than exercising it, and would pass against a middleware that had
been removed from the stack entirely.

WHY THE ORIGIN OVERRIDE IS LOAD-BEARING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

``dinify_backend/test_settings.py`` sets ``CORS_ORIGIN_ALLOW_ALL=True``. Left alone,
the origin half of every assertion here would pass for a reason that has nothing to do
with the allowlist. Each test therefore pins ``CORS_ORIGIN_ALLOW_ALL=False`` and names
ONE explicitly allowed origin, which is also the posture production runs in.

``CORS_ALLOW_HEADERS`` itself is deliberately NOT overridden: these tests read the real
production tuple, so removing an entry from it fails them.
"""
from django.conf import settings
from django.test import TestCase, override_settings

# An origin that exists only here. Naming a real one would make the test pass or fail
# for reasons belonging to deployment configuration rather than to this allowlist.
TEST_ORIGIN = 'https://claim.test.example'

CHALLENGE_PATH = '/api/v1/users/owner-claim/challenge/'

# Every credential this backend accepts header-only. Listed here rather than derived
# from the setting, so deleting one from `settings.py` fails a test instead of quietly
# shrinking what this module checks.
CUSTOM_CREDENTIAL_HEADERS = (
    'x-diner-session',
    'x-diner-credential',
    'x-delegation-session',
    'x-delegation-code',
    'x-owner-claim-token',
)


@override_settings(
    CORS_ORIGIN_ALLOW_ALL=False,
    CORS_ALLOWED_ORIGINS=[TEST_ORIGIN],
)
class OwnerClaimPreflightTests(TestCase):
    """
    The Step-2F.1 regression: a browser must be permitted to send the claim token.

    ``POST /api/v1/users/owner-claim/challenge/`` reads the raw credential ONLY from
    ``X-Owner-Claim-Token``. A custom request header plus a JSON content type makes
    the request non-simple, so the browser sends an OPTIONS preflight first and obeys
    its answer.
    """

    def preflight(self, request_headers, path=CHALLENGE_PATH, origin=TEST_ORIGIN):
        return self.client.options(
            path,
            HTTP_ORIGIN=origin,
            HTTP_ACCESS_CONTROL_REQUEST_METHOD='POST',
            HTTP_ACCESS_CONTROL_REQUEST_HEADERS=request_headers,
        )

    def allowed_headers(self, response):
        raw = response.get('Access-Control-Allow-Headers', '')
        return {value.strip().lower() for value in raw.split(',') if value.strip()}

    def test_the_claim_token_header_survives_preflight(self):
        """THE REGRESSION. Without the allowlist entry this is what a browser sees."""
        response = self.preflight('x-owner-claim-token, content-type')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(
            'x-owner-claim-token', self.allowed_headers(response),
            'the browser will strip X-Owner-Claim-Token, and the owner-claim '
            'challenge will fail in-browser while every endpoint test stays green',
        )

    def test_the_allowed_origin_is_returned(self):
        # The other half of a usable preflight: permitting the header is worthless if
        # the origin is refused, and vice versa.
        response = self.preflight('x-owner-claim-token, content-type')
        self.assertEqual(response['Access-Control-Allow-Origin'], TEST_ORIGIN)

    def test_the_content_type_header_survives_too(self):
        # The claim request posts JSON, so `content-type` is part of the same
        # preflight. It comes from `default_headers`; asserting it proves the
        # spread of that default was not lost when the custom entries were added.
        self.assertIn('content-type', self.allowed_headers(self.preflight(
            'x-owner-claim-token, content-type',
        )))

    def test_an_origin_outside_the_allowlist_is_not_granted(self):
        """
        PERMITTING A HEADER IS NOT PERMITTING AN ORIGIN.

        The fix adds one entry to ``CORS_ALLOW_HEADERS`` and touches
        ``CORS_ALLOWED_ORIGINS`` not at all. This is what makes that claim checkable
        rather than merely stated: an unlisted origin gets no
        ``Access-Control-Allow-Origin``, so the browser refuses the response however
        generous the header allowlist is.
        """
        response = self.preflight(
            'x-owner-claim-token, content-type',
            origin='https://not-allowed.example',
        )
        self.assertIsNone(response.get('Access-Control-Allow-Origin'))

    def test_every_header_only_credential_survives_preflight(self):
        """
        The same trap applies to all five, and they were added at different times.

        Kept in one place so the next header-only credential has an obvious home —
        and so removing an existing entry is a test failure rather than a silent
        in-browser outage nobody can reproduce locally.
        """
        allowed = self.allowed_headers(self.preflight(
            ', '.join(CUSTOM_CREDENTIAL_HEADERS) + ', content-type',
        ))
        for header in CUSTOM_CREDENTIAL_HEADERS:
            with self.subTest(header=header):
                self.assertIn(header, allowed)

    def test_the_allowlist_is_what_the_preflight_reports(self):
        # Ties the response back to the setting, so a future middleware change that
        # started echoing the REQUESTED headers instead of the CONFIGURED ones — which
        # would make every assertion above vacuous — is caught.
        allowed = self.allowed_headers(self.preflight('x-owner-claim-token'))
        self.assertEqual(
            allowed, {value.lower() for value in settings.CORS_ALLOW_HEADERS},
        )
