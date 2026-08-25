"""
Restaurant admin-plane endpoints — the collection (read + create), the detail read,
and the lifecycle transition.

``admin/v1/restaurants/`` is ONE RESOURCE with two methods and two very different
authority bars:

    GET  -> the directory. An authenticated session, not elevation-gated, not audited.
    POST -> creation. Authenticated + RECENTLY ELEVATED + CSRF, and audited exactly
            once per request.

They share a route because they are the same resource — listing a collection and
adding to it — and putting creation on an invented ``/restaurants/create/`` would
make the URL, rather than the method, carry the meaning. They do NOT share a
permission set: ``get_permissions`` resolves per method, so reading the portfolio
stays ordinary work while creating a tenant is a step-up decision. Every rule about
what a creation request IS lives in ``endpoints/restaurant_creation``, and every row
it writes is written by ``platform_admin_app.onboarding_creation``.

READS ARE NOT ELEVATION-GATED, AND THAT IS DELIBERATE. ``IsRecentlyElevated`` exists
for actions whose blast radius justifies re-proving a second factor mid-session —
stopping a tenant trading, minting a delegation. Reading the operator's own portfolio
is the ordinary work of the control plane: it is what the operator does on arriving,
and demanding a TOTP code to look at a list would train them to re-elevate reflexively,
which is precisely the habit step-up authentication depends on them NOT having. A
valid session is the right bar for a read.

ORDINARY GETS ARE NOT AUDITED. ``AdminAuditLog`` is the record of administrative
ACTION, not an access log; the "exactly one entry per unsafe request" convention on
``AdminAuditLog`` covers POST/PUT/PATCH/DELETE. Auditing directory views would bury
the transitions and delegations the log exists to make findable under a drift of page
loads — and would then feed itself, since ``last_activity_at`` reads that same log.

THE VIEWS ARE THIN. The read model lives in ``platform_admin_app.restaurant_reads``
and every lifecycle rule in ``restaurants_app.controllers.lifecycle``, for the same
reason the transition view delegates: a second caller should inherit behaviour rather
than re-implement it.

ELEVATION. Changing a tenant's operational state stops their diners ordering, their
kitchen working and their staff signing in, so the whole endpoint is step-up gated:
a valid session is not enough, the administrator must have cleared a second factor
within the elevation window. That covers the specification's "elevated confirmation"
requirement for offboarding and applies the same bar to suspension, which is just as
disruptive to a restaurant mid-service.

THE VIEW IS THIN. Every rule — the matrix, the reason requirement, the readiness and
receivables preconditions, the row lock and the audit entry — lives in
``restaurants_app.controllers.lifecycle``. This translates HTTP to that call and back,
so a second caller (a management command, Phase-1 automation) inherits identical
behaviour rather than a re-implementation.
"""
from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from platform_admin_app import onboarding_creation, restaurant_reads
from platform_admin_app.audit_actions import (
    ADMIN_RESTAURANT_CREATED,
    ADMIN_RESTAURANT_TRANSITION_DENIED,
)
from platform_admin_app.endpoints import restaurant_creation
from platform_admin_app.endpoints.reasoned_request import read_request_body
from platform_admin_app.models import RESULT_DENIED, RESULT_FAILURE, RESULT_SUCCESS
from platform_admin_app.permissions import IsRecentlyElevated
from platform_admin_app.views import AdminAPIView
from restaurants_app.controllers import lifecycle
from restaurants_app.models import Restaurant


def _not_found():
    """404 for a missing OR soft-deleted restaurant — the plane never distinguishes."""
    return Response({'status': 404, 'message': 'Restaurant not found.'}, status=404)


class AdminRestaurantCollectionView(AdminAPIView):
    """
    The restaurant collection: ``GET`` the directory, ``POST`` to create one.

    ``GET`` is filtered, paginated and answered in one query for the page. Query
    parameters: ``search``, ``status``, ``attention``, ``page``, ``page_size``. A
    malformed one is a 400 with field-keyed errors, never an empty 200 — an empty
    page means "nothing matches", and returning it for a typo would let the operator
    conclude something false about the portfolio.

    ``POST`` creates a NEW canonical restaurant together with its owner authority,
    its ``admin_created`` provenance and the owner's initial claim credential — see
    ``post`` and ``platform_admin_app.onboarding_creation``.
    """

    # NOT a class-level list: the two methods have different bars and
    # ``permission_classes`` cannot express that. See ``get_permissions``.
    permission_classes = [IsAuthenticated]

    # --- authority ---

    def get_permissions(self):
        """
        Per-method authority. ``GET`` needs a session; ``POST`` needs a fresh factor.

        Requiring elevation to LOOK at the directory would train an operator to
        re-elevate reflexively, which is precisely the habit step-up authentication
        depends on them not having. Requiring it to CREATE A TENANT is the same bar
        the lifecycle transition and the commercial writes already set, and creation
        is at least as consequential: it mints a restaurant, an owner identity and a
        credential in one request.

        CSRF is unchanged and is not reimplemented here — ``AdminSessionAuthentication``
        runs Django's double-submit check on every unsafe admin request, because DRF
        marks each ``APIView`` ``csrf_exempt`` and the middleware therefore never sees
        it.
        """
        if self.request.method == 'POST':
            return [IsAuthenticated(), IsRecentlyElevated()]
        return [IsAuthenticated()]

    def permission_denied(self, request, message=None, code=None):
        """
        Audit a refused CREATION before DRF turns it into a 403.

        DRF evaluates permissions in ``check_permissions``, before the handler runs,
        so a stale-elevation POST would otherwise 403 with no entry — breaking
        ``AdminAPIView``'s "exactly one entry per unsafe request, including denials".

        THREE CONDITIONS, all load-bearing. Only for ``POST``, because ``GET`` is a
        safe request the convention does not cover. Only when the request
        AUTHENTICATED — an anonymous call is a 401 about identity, and a CSRF failure
        is refused inside authentication itself; neither may manufacture an audit
        actor the plane cannot name. And the body is NEVER read here: a denial is
        recorded from the request's authority, not from a payload the endpoint has
        just refused to act on.
        """
        if request.method == 'POST' and getattr(
            request, 'successful_authenticator', None,
        ):
            self.audit(
                request,
                ADMIN_RESTAURANT_CREATED,
                result=RESULT_DENIED,
                resource_type='Restaurant',
                # No resource id and no restaurant id: nothing was created, and the
                # collection route names no target.
                reason=message or 'Recent re-authentication required.',
                error_code='elevation_required',
            )
        return super().permission_denied(request, message=message, code=code)

    # --- read ---

    def get(self, request):
        try:
            params = restaurant_reads.parse_directory_params(request.query_params)
        except restaurant_reads.QueryParamError as exc:
            return Response(
                {'status': 400, 'message': 'Invalid query parameters.',
                 'errors': exc.errors},
                status=400,
            )

        queryset = restaurant_reads.apply_directory_filters(
            restaurant_reads.directory_queryset(), params,
        )

        page, page_size = params['page'], params['page_size']
        count = queryset.count()
        # Ceiling division. `pages` is 1 for an empty result rather than 0, so the
        # portal always has a page to render and never divides by it.
        pages = max(1, -(-count // page_size))
        offset = (page - 1) * page_size
        # A page beyond the end returns an empty `results` with honest metadata
        # rather than a 404: the page number is well-formed, the portfolio simply
        # does not extend that far, and the client can see that from `pages`.
        rows = list(queryset[offset:offset + page_size])

        return Response(
            {
                'status': 200,
                'data': {
                    'results': [restaurant_reads.serialize_row(r) for r in rows],
                    'pagination': {
                        'page': page,
                        'page_size': page_size,
                        'count': count,
                        'pages': pages,
                    },
                },
            },
            status=200,
        )


    # --- create ---

    def _audit_failure(self, request, *, reason, error_code):
        """
        One failure row for a refused creation.

        NO ``resource_id`` AND NO ``restaurant_id``: the tenant was not created, so
        there is no id to name, and inventing one would put a row in the log that
        ``restaurant_reads``'s activity strip would then attribute to some restaurant.
        No ``before_state`` and no ``after_state`` either — nothing existed and
        nothing was written.
        """
        self.audit(
            request,
            ADMIN_RESTAURANT_CREATED,
            result=RESULT_FAILURE,
            resource_type='Restaurant',
            reason=reason,
            error_code=error_code,
        )

    def post(self, request):
        """
        Create one restaurant, its owner authority, its provenance and its credential.

        Body::

            {"restaurant": {"name": ..., "location": ..., "is_test": false},
             "owner": {"mode": "new", "first_name": ..., "last_name": ...,
                       "phone_number": ..., "email": ...},
             "reason": "..."}

        or, to attach an existing account, ``{"owner": {"mode": "existing",
        "user_id": "<uuid>"}}``.

        ━━ ONE OUTER TRANSACTION: DOMAIN + AUDIT ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        The service call and its ``AdminAuditLog`` entry share one
        ``transaction.atomic()`` here, and the service's own atomic block nests as a
        savepoint inside it. That composition is exactly why the audit write does not
        live in ``onboarding_creation``: if the audit insert fails, the whole creation
        — the user, the restaurant, the membership, the onboarding row and the
        invitation — rolls back with it, because an administrative action that cannot
        be attributed must not be allowed to stand. The same structure is what lets a
        REFUSED creation still be recorded, since the domain exception unwinds only
        its own savepoint. Identical to the Step-3D.2 commercial writes.

        ━━ EXACTLY ONE AUDIT ROW ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        One request is ONE administrative decision however many rows it writes, so
        there is one entry under ``admin.restaurant.created`` — on success, on an
        unreadable body, on a rejected payload, on a domain conflict, and (via
        ``permission_denied``) on a stale-elevation refusal. Not audited: an anonymous
        request and a CSRF failure, both refused inside authentication before any
        decision could exist.

        ━━ WHAT THIS ENDPOINT DOES NOT DO ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

        It writes no model field of its own, sends no SMS, email or notification,
        touches no legacy action log, generates no temporary password, and creates no
        commercial configuration, subscription terms, dining area, table, QR
        credential, menu row or order. The invitation is ISSUED, not delivered.
        """
        payload, unreadable = read_request_body(request)
        if unreadable is not None:
            status, error_code, detail = unreadable
            # No reason can be read from a body that would not parse, and one must
            # never be invented.
            self._audit_failure(request, reason='', error_code=error_code)
            return Response(
                {
                    'status': status,
                    'message': 'The restaurant could not be created.',
                    'errors': {'__all__': [detail]},
                },
                status=status,
            )

        serializer = restaurant_creation.CreateRestaurantRequestSerializer(
            data=payload,
        )
        if not serializer.is_valid():
            # `audit_reason()` records the reason ONLY if the reason field itself
            # validated, and then its NORMALIZED value — never the raw one the
            # endpoint has just refused.
            self._audit_failure(
                request,
                reason=serializer.audit_reason(),
                error_code=serializer.audit_error_code(),
            )
            return Response(
                {
                    'status': 400,
                    'message': 'The restaurant could not be created.',
                    'errors': serializer.errors,
                },
                status=400,
            )

        data = serializer.validated_data
        facts = data['restaurant']
        reason = data['reason']

        with transaction.atomic():
            try:
                result = onboarding_creation.create_admin_restaurant(
                    name=facts['name'],
                    location=facts['location'],
                    is_test=facts['is_test'],
                    owner=restaurant_creation.owner_spec(data['owner']),
                    actor=request.user,
                    reason=reason,
                )
            except onboarding_creation.RestaurantCreationError as exc:
                status = restaurant_creation.STATUS_BY_CODE.get(exc.code)
                if status is None:
                    # Deliberately re-raised: an unmapped domain code is an internal
                    # condition, and relabelling it a tidy client error would hide a
                    # bug behind a 400. It surfaces as a 500 and rolls back.
                    raise
                self._audit_failure(request, reason=reason, error_code=exc.code)
                return Response(
                    restaurant_creation.error_body(exc, status), status=status,
                )

            self.audit(
                request,
                ADMIN_RESTAURANT_CREATED,
                result=RESULT_SUCCESS,
                resource_type='Restaurant',
                resource_id=str(result.restaurant.id),
                restaurant_id=result.restaurant.id,
                reason=reason,
                # No `before_state`: the resource did not exist, and a row of nulls
                # would imply a prior state something could be compared against.
                after_state=restaurant_creation.after_state(result),
            )

            # THE CANONICAL PROJECTION, re-read from the database inside the same
            # transaction — the very same one `GET admin/v1/restaurants/<id>/`
            # returns, never a second write-path shape. So the onboarding state the
            # client sees (`tracked`, `admin_created`, `consistent`,
            # `not_established`, `pending`) is DERIVED by the existing evidence rules
            # from the rows just written, rather than asserted here.
            detail = restaurant_reads.serialize_detail(
                restaurant_reads.directory_queryset()
                .filter(id=result.restaurant.id)
                .get()
            )

        response = Response(
            restaurant_creation.success_body(result, detail), status=201,
        )
        # THE RESPONSE CARRIES THE ONLY COPY OF A CREDENTIAL. Stamped explicitly
        # rather than through `misc_app.controllers.http.no_store`, which also varies
        # on the diner capability headers — meaningless on this plane, and a Vary
        # header that describes a credential this response does not use would be a
        # small lie in a place that should be exact.
        response['Cache-Control'] = 'no-store, private'
        response['Pragma'] = 'no-cache'
        response['Expires'] = '0'
        return response


class AdminRestaurantDetailView(AdminAPIView):
    """``GET`` one restaurant: the workspace header and Overview tab."""

    permission_classes = [IsAuthenticated]

    def get(self, request, restaurant_id):
        restaurant = (
            restaurant_reads.directory_queryset()
            .filter(id=restaurant_id)
            .first()
        )
        if restaurant is None:
            return _not_found()

        return Response(
            {'status': 200, 'data': restaurant_reads.serialize_detail(restaurant)},
            status=200,
        )


def _serialize(restaurant):
    """The restaurant's lifecycle position, plus where it can go next."""
    return {
        'id': str(restaurant.id),
        'name': restaurant.name,
        'status': restaurant.status,
        'allowed_transitions': lifecycle.allowed_targets(restaurant.status),
    }


class AdminRestaurantTransitionView(AdminAPIView):
    """``POST`` a lifecycle transition. Body: ``to_state``, ``reason``."""

    permission_classes = [IsAuthenticated, IsRecentlyElevated]

    def permission_denied(self, request, message=None, code=None):
        """
        Audit a refused transition before DRF turns it into a 403.

        DRF rejects a failed permission inside ``check_permissions``, before the
        handler runs — so a stale-elevation attempt would otherwise 403 with no
        entry, breaking ``AdminAPIView``'s "exactly one entry per unsafe request,
        including denials". Only for a request that AUTHENTICATED: an anonymous call
        is a 401 about identity, not a lifecycle decision, and must not manufacture a
        denial row for a caller we cannot name.
        """
        if getattr(request, 'successful_authenticator', None):
            self.audit(
                request,
                ADMIN_RESTAURANT_TRANSITION_DENIED,
                result=RESULT_DENIED,
                resource_type='Restaurant',
                resource_id=str(self.kwargs.get('restaurant_id') or ''),
                reason=message or 'Recent re-authentication required.',
                error_code='elevation_required',
            )
        return super().permission_denied(request, message=message, code=code)

    def post(self, request, restaurant_id):
        # Soft-deleted rows are excluded: `deleted` is the technical soft-delete and
        # a deleted restaurant has no lifecycle left to move. 404 rather than 400 —
        # the admin plane has no reason to distinguish "gone" from "never existed".
        restaurant = Restaurant.objects.filter(
            id=restaurant_id, deleted=False,
        ).first()
        if restaurant is None:
            # No audit entry: nothing was denied and no resource was touched. The
            # request never reached a decision about a real tenant.
            return _not_found()

        try:
            updated = lifecycle.transition_restaurant(
                restaurant=restaurant,
                to_state=request.data.get('to_state'),
                reason=request.data.get('reason'),
                request=request,
            )
        except lifecycle.LifecycleTransitionError as exc:
            # The service already wrote the `transition_denied` entry — it owns that
            # record because it is the only layer that knows the real from-state.
            return Response(
                {'status': 400, 'message': 'This transition was refused.',
                 'errors': exc.errors, 'code': exc.code},
                status=400,
            )

        return Response(
            {'status': 200, 'message': 'Lifecycle state updated.',
             'data': _serialize(updated)},
            status=200,
        )
