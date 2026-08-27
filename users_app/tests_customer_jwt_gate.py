"""
The customer plane refuses a platform-staff JWT on every PRESENTED token.

THE WINDOW THIS CLOSES. ``login`` and ``GatedTokenRefreshView`` both refuse a
``platform_staff`` account, but both bind where a token is MINTED. Nothing read
``account_type`` when a token was presented, so an access token issued moments before
an account was promoted stayed valid for the rest of its lifetime — up to
``ACCESS_TOKEN_LIFETIME`` (30 minutes by default). Migration ``users_app/0013``
blacklists outstanding REFRESH tokens; access tokens are stateless and cannot be
revoked that way. The headline test below constructs the token BEFORE promotion and
asserts it is refused after, because a test that promotes first and then mints would
pass against the old code too and prove nothing.

WHY THE GATE IS WHERE IT IS. Two properties have to hold at once, and they pull in
opposite directions:

* ``misc_app.controllers.decode_auth_token`` instantiates an authenticator DIRECTLY,
  outside the DRF chain, on behalf of ~30 customer-plane endpoints. A gate wired only
  into ``DEFAULT_AUTHENTICATION_CLASSES`` would leave every one of them open — so the
  wiring assertions here cover BOTH sites, and the source scan keeps a third from
  appearing.
* On this plane ``request.user.account_type == 'platform_staff'`` is true if and only
  if the request is DELEGATED. A refusal keyed on ``request.user`` generally — a
  middleware, a permission class — would break delegated drill-in entirely. The gate
  therefore lives on the JWT resolution path specifically, and the delegated-path test
  in ``platform_admin_app.tests_delegated_session`` stays green alongside it.
"""
import ast
import os
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from rest_framework_simplejwt.tokens import RefreshToken

from dinify_backend.configss.string_definitions import (
    ACCOUNT_TYPE_PLATFORM_STAFF, ACCOUNT_TYPE_RESTAURANT_USER,
)
from users_app.authentication import CustomerJWTAuthentication
from users_app.models import User

REPO_ROOT = Path(__file__).resolve().parent.parent

CHOKEPOINT = 'users_app.authentication.CustomerJWTAuthentication'
STOCK = 'rest_framework_simplejwt.authentication.JWTAuthentication'

# The two modules entitled to name the stock authenticator: the chokepoint (which
# subclasses it) and this file (which has to spell it to look for it).
STOCK_EXEMPT = frozenset({
    'users_app/authentication.py',
    'users_app/tests_customer_jwt_gate.py',
})

# Directories outside the customer plane's Python surface.
PRUNE_DIRS = frozenset({
    'migrations', '__pycache__', 'node_modules', 'site-packages',
    'venv', 'env', 'staticfiles', 'media',
})


def _customer_plane_modules():
    """Yield (relative_path, source) for every scannable customer-plane module."""
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [
            d for d in dirnames if d not in PRUNE_DIRS and not d.startswith('.')
        ]
        for filename in sorted(filenames):
            if not filename.endswith('.py'):
                continue
            path = Path(dirpath) / filename
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in STOCK_EXEMPT:
                continue
            yield relative, path.read_text(encoding='utf-8', errors='replace')


def _names_stock_authenticator(source):
    """
    Whether ``source`` imports ``JWTAuthentication`` from SimpleJWT's auth module.

    An AST walk rather than a substring match, so the docstring of a module that
    merely DISCUSSES the stock class (this one, or a comment explaining the swap) is
    not a false positive. A file that does not parse yields nothing — a syntax error
    is ``django check``'s to report.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - defensive
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == 'rest_framework_simplejwt.authentication':
                if any(alias.name == 'JWTAuthentication' for alias in node.names):
                    return True
    return False


class CustomerJWTWiringTests(TestCase):
    """The chokepoint is actually installed at both entry points."""

    def test_default_authentication_classes_use_the_gated_subclass(self):
        classes = settings.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES']
        self.assertIn(CHOKEPOINT, classes)
        self.assertNotIn(STOCK, classes)

    def test_decode_auth_token_uses_the_gated_subclass(self):
        """
        The ~30-call-site path. This helper builds its own authenticator, so the
        settings wiring above does not reach it — it has to name the subclass itself.
        """
        from misc_app.controllers import decode_auth_token

        self.assertIs(decode_auth_token.CustomerJWTAuthentication,
                      CustomerJWTAuthentication)

    def test_no_customer_plane_module_imports_the_stock_authenticator(self):
        """
        A third direct instantiation would silently reopen the window.

        `decode_auth_token` is the one that already existed and was missed until this
        change; nothing stops a future endpoint from doing the same. Import the
        chokepoint instead — it IS a JWTAuthentication.
        """
        offenders = [
            relative for relative, source in _customer_plane_modules()
            if _names_stock_authenticator(source)
        ]
        self.assertEqual(
            offenders, [],
            'these modules import the ungated JWTAuthentication directly; import '
            f'{CHOKEPOINT} instead: {offenders}',
        )


class PlatformStaffJWTRefusedTests(TestCase):
    """A customer access token stops working the moment the account is promoted."""

    def setUp(self):
        self.user = User.objects.create_user(
            first_name='Pre', last_name='Promotion',
            email='pre.promotion@test.com',
            phone_number='256700000901', username='256700000901',
            country='Uganda', password='password',
        )

    def _access_token(self, user):
        return str(RefreshToken.for_user(user).access_token)

    def test_token_minted_before_promotion_is_refused_after_it(self):
        """
        The headline property, and the reason for the ordering.

        The token is built while the account is still a ``restaurant_user`` — exactly
        the window the old code left open — and only then is the account promoted.
        Minting after promotion would pass on the unfixed code and prove nothing.
        """
        self.assertEqual(self.user.account_type, ACCOUNT_TYPE_RESTAURANT_USER)
        token = self._access_token(self.user)

        # It authenticates while the account is still a customer. Asserted as
        # "not 401" rather than "200" because what is under test is the
        # AUTHENTICATION outcome, not this route's body: reaching method dispatch
        # at all is the proof. (Step 2F.3 gave the route a GET handler, so it now
        # answers 200 here where it used to answer 405 — the gate assertion is
        # deliberately indifferent to which, and must stay that way.)
        response = self.client.get(
            '/api/v1/users/user-profile/', HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertNotEqual(response.status_code, 401)

        User.objects.filter(pk=self.user.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )

        # Same token, same request, now refused — without waiting for expiry.
        response = self.client.get(
            '/api/v1/users/user-profile/', HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertEqual(response.status_code, 401)

    def test_a_restaurant_user_token_is_unaffected(self):
        """The gate subtracts only; an ordinary customer session is untouched."""
        token = self._access_token(self.user)
        response = self.client.get(
            '/api/v1/users/user-profile/', HTTP_AUTHORIZATION=f'Bearer {token}',
        )
        self.assertNotEqual(response.status_code, 401)

    def test_the_refusal_is_not_an_account_type_oracle(self):
        """
        A promoted account and a merely DEACTIVATED one answer identically.

        Both come out of ``get_user`` as ``user_inactive``, so a prober holding a
        stale token cannot learn that the account was promoted rather than disabled.
        """
        promoted = self._access_token(self.user)
        User.objects.filter(pk=self.user.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        promoted_response = self.client.get(
            '/api/v1/users/user-profile/', HTTP_AUTHORIZATION=f'Bearer {promoted}',
        )

        deactivated_user = User.objects.create_user(
            first_name='De', last_name='Activated',
            email='de.activated@test.com',
            phone_number='256700000902', username='256700000902',
            country='Uganda', password='password',
        )
        deactivated = self._access_token(deactivated_user)
        User.objects.filter(pk=deactivated_user.pk).update(is_active=False)
        deactivated_response = self.client.get(
            '/api/v1/users/user-profile/', HTTP_AUTHORIZATION=f'Bearer {deactivated}',
        )

        self.assertEqual(promoted_response.status_code, 401)
        self.assertEqual(deactivated_response.status_code, 401)
        self.assertEqual(
            promoted_response.json(), deactivated_response.json(),
            'the refusal distinguishes a promoted account from a deactivated one',
        )

    def test_the_decode_auth_token_path_refuses_too(self):
        """
        The ~30 endpoints that bypass the DRF chain are covered by the same gate.

        Asserted through the helper directly rather than through one arbitrary
        endpoint, because the helper is what every one of those call sites shares.
        """
        from rest_framework.test import APIRequestFactory

        from misc_app.controllers.decode_auth_token import decode_jwt_token

        token = self._access_token(self.user)
        factory = APIRequestFactory()

        request = factory.get('/', HTTP_AUTHORIZATION=f'Bearer {token}')
        self.assertEqual(
            decode_jwt_token(request)['id'], str(self.user.id),
            'a restaurant_user must still resolve through this path',
        )

        User.objects.filter(pk=self.user.pk).update(
            account_type=ACCOUNT_TYPE_PLATFORM_STAFF,
        )
        request = factory.get('/', HTTP_AUTHORIZATION=f'Bearer {token}')
        with self.assertRaises(Exception):
            decode_jwt_token(request)
