"""
the serializers for the restaurant app
"""
import json
import logging
import uuid
from datetime import datetime

logger = logging.getLogger(__name__)

from rest_framework.serializers import ModelSerializer, SerializerMethodField
from orders_app.models import Order
from rest_framework import serializers
from restaurants_app.models import (
    Restaurant, RestaurantEmployee, MenuSection, MenuItem, Table,
    SectionGroup, DiningArea, UpsellConfig, UpsellItem,
    Reservation, WaitlistEntry, RestaurantTag
)
from misc_app.serializers.fields import JSONStringCompatField, JSONStringCompatListField
from restaurants_app.controllers.tables import get_table_availability
from restaurants_app.controllers.tenant_scope import assert_fks_belong_to_restaurant
from restaurants_app.controllers.menu_relationships import (
    validate_menu_item_relationships,
)
from dinify_backend.tenancy.relations import SameTenant, GlobalRelation

# Two-tenant behavioural test classes that PROVE the SameTenant runtime
# enforcement declared below (the tenancy meta-test asserts each verified_by
# resolves to an importable object). Menu section/group tenancy is already
# proven by PR3's suite; the tables-domain (table/dining-area/reservation/
# waitlist) and upsell relations are proven by tests_write_surface_tenancy.
_MENU_FK_EVIDENCE = 'restaurants_app.tests.MenuFkTenantBoundaryTests'
_TABLE_EVIDENCE = 'restaurants_app.tests_write_surface_tenancy.TableDiningAreaTenantTests'
_RESERVATION_EVIDENCE = 'restaurants_app.tests_write_surface_tenancy.ReservationFkTenantTests'
_WAITLIST_EVIDENCE = 'restaurants_app.tests_write_surface_tenancy.WaitlistFkTenantTests'
_SECTION_GROUP_EVIDENCE = 'restaurants_app.tests_write_surface_tenancy.SectionGroupSectionTenantTests'


class SerializerGetRestaurantDetail(ModelSerializer):
    class Meta:
        """
        the meta class for the serializers
        """
        model = Restaurant
        fields = '__all__'


class SerializerPutRestaurant(ModelSerializer):
    """
    serializer for adding and editing restaurant details

    Explicit write contract (TENANT-ISO-PR5): only the identity / settings / tax
    fields in EDIT_INFORMATION['restaurants'] are client-writable. `owner` and
    every audit / lifecycle / approval / billing-lifecycle field are server-owned
    and read_only — `owner` is set at creation through the trusted save() channel
    (create_restaurant), and the non-admin `status`/`flat_fee` post-gate strip
    stays in restaurant_setup (PR#211).
    """
    class Meta:
        model = Restaurant
        fields = (
            # client-writable (mirrors EDIT_INFORMATION['restaurants'])
            'name', 'location', 'logo', 'cover_photo', 'status',
            'require_order_prepayments', 'expose_order_ratings',
            'allow_deliveries', 'allow_pickups', 'preferred_subscription_method',
            'order_surcharge_percentage', 'order_surcharge_min_amount',
            'order_surcharge_cap_amount', 'flat_fee', 'branding_configuration',
            'preset_tags', 'country', 'contact_phone', 'contact_email',
            'landmark', 'tagline', 'cuisine_types', 'socials',
            'accepting_orders', 'opening_hours', 'vat_registered', 'vat_rate',
            'tin', 'receipt_footer',
            # server-owned (output-only)
            'id', 'owner', 'menu_item_sort_mode',
            'first_time_menu_approval', 'first_time_menu_approval_decision',
            'subscription_validity', 'subscription_expiry_date',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'owner', 'menu_item_sort_mode',
            'first_time_menu_approval', 'first_time_menu_approval_decision',
            'subscription_validity', 'subscription_expiry_date',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )


class SerializerPublicGetRestaurant(ModelSerializer):
    """
    serializer for getting public restaurant details
    """
    owner = SerializerMethodField()

    class Meta:
        model = Restaurant
        fields = (
            "id", "name", "location",
            "logo", "cover_photo", "status", "owner",
            "preset_tags"
        )

    def get_owner(self, restaurant):
        return {
            'id': str(restaurant.owner.pk),
            'first_name': restaurant.owner.first_name,
            'last_name': restaurant.owner.last_name,
            'email': restaurant.owner.email,
            'phone': restaurant.owner.phone_number
        }


class SerializerMiscPublicRestaurant(ModelSerializer):
    """
    public restaurant listing for the AllowAny misc-public endpoint.

    Deliberately excludes the owner block: a public listing exposes the
    restaurant, never the owner's personal PII (name/email/phone).
    """
    class Meta:
        model = Restaurant
        fields = (
            "id", "name", "location",
            "logo", "cover_photo", "status",
            "preset_tags",
        )


class SerializerEmployeeGetRestaurant(ModelSerializer):
    """
    serializer for getting restaurant details for the employee
    """
    class Meta:
        model = RestaurantEmployee
        fields = ("id", "name", "location")


class SerializerPutRestaurantEmployee(ModelSerializer):
    """
    serializer for adding and editing restaurant employees

    `restaurant` is server-derived (read_only, set via save() by create_employee)
    so an employee can never be reassigned to another tenant; `user` is a
    platform-global identity picked at creation (GlobalRelation), the endpoint
    gating who may add members. Only roles / active are ordinarily editable.
    """
    class Meta:
        model = RestaurantEmployee
        fields = (
            'id', 'user', 'restaurant', 'roles', 'active',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'restaurant', 'created_by', 'deleted', 'deleted_by',
            'time_deleted', 'deletion_reason', 'time_created', 'time_last_updated',
        )
        tenant_relations = {
            'user': GlobalRelation(
                reason='Users are platform-global identities; the restaurant is '
                       'server-derived (read_only, set via save()) and the endpoint '
                       'gates who may add a member.'
            ),
        }


class SerializerGetRestaurantEmployee(ModelSerializer):
    """
    serializer for getting restaurant employees
    """
    name = SerializerMethodField()
    user = SerializerMethodField()

    class Meta:
        model = RestaurantEmployee
        fields = (
            "id", "time_created", "time_last_updated",
            "name", "roles", "active",
            "user"
        )

    def get_name(self, employee):
        """
        returns the name of the employee
        """
        return f"{employee.user.first_name} {employee.user.last_name}"

    def get_user(self, employee):
        """
        returns the user details of the employee
        """
        return {
            'id': str(employee.user.pk),
            'first_name': employee.user.first_name,
            'last_name': employee.user.last_name,
            'email': employee.user.email,
            'phone_number': employee.user.phone_number,
        }


class SerializerPutMenuSection(ModelSerializer):
    """
    serializer for adding menu section

    `restaurant` and the publication flags (approved/enabled) are server-owned
    (read_only) — the parent restaurant is set via save() from the authorized
    resource, and the auto-publication defaults are set server-side on create.
    """
    schedules = JSONStringCompatField(required=False)

    class Meta:
        model = MenuSection
        fields = (
            'id', 'name', 'description', 'section_banner_image', 'available',
            'listing_position', 'availability', 'schedules',
            'restaurant', 'approved', 'enabled',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'restaurant', 'approved', 'enabled',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )


class SerializerPublicGetMenuSection(ModelSerializer):
    """
    serializer for getting the menu section
    """
    item_count = SerializerMethodField()
    has_groups = SerializerMethodField()
    groups = SerializerMethodField()

    class Meta:
        model = MenuSection
        fields = (
            'id', 'name', 'description', 'section_banner_image',
            'available', 'availability', 'schedules',
            'item_count', 'has_groups', 'groups',
            'listing_position'
        )

    def get_item_count(self, menu_section):
        return MenuItem.objects.filter(
            section=menu_section,
            # section_group__deleted=False,
            # section_group__available=True,
            deleted=False
        ).count()

    def get_has_groups(self, menu_section):
        return SectionGroup.objects.filter(
            section=menu_section,
            deleted=False
        ).count() > 0

    def get_groups(self, menu_section):
        groups = SectionGroup.objects.filter(
            section=menu_section,
            deleted=False,
            # available=True
        )
        return [
            {
                'id': str(group.pk),
                'name': group.name,
                'available': group.available
            } for group in groups
        ]


class SerializerPutSectionGroup(ModelSerializer):
    class Meta:
        model = SectionGroup
        fields = (
            'id', 'name', 'description', 'available', 'section',
            'approved', 'enabled',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'approved', 'enabled',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        # `section` is client-picked on create (which section owns the group) and
        # must be same-tenant; the validate() below also pins it on update. Not in
        # EI_SECTION_GROUP, so Secretary strips it from ordinary updates.
        tenant_relations = {
            'section': SameTenant('restaurant_id', verified_by=_SECTION_GROUP_EVIDENCE),
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Defense-in-depth: SectionGroup.section is the tenancy path
        # (section__restaurant). UNREACHABLE via the endpoint today — `section`
        # is not in EI_SECTION_GROUP, so Secretary strips it before this
        # serializer runs (see test_section_group_section_is_not_editable). This
        # guard is here so a cross-tenant group move is already blocked if
        # `section` ever becomes editable. On create the endpoint gate
        # (_resolve_sectiongroups('create')) authorizes the supplied section, so
        # this is instance-only.
        if self.instance is not None:
            incoming_section = attrs.get('section')
            if incoming_section is not None:
                current_restaurant_id = MenuSection.objects.values_list(
                    'restaurant_id', flat=True
                ).get(id=self.instance.section_id)
                if incoming_section.restaurant_id != current_restaurant_id:
                    raise serializers.ValidationError({
                        'section': "Cannot move a section group to another restaurant's section."
                    })
        return attrs


class SerializerPublicGetSectionGroup(ModelSerializer):
    item_count = SerializerMethodField()

    class Meta:
        model = SectionGroup
        fields = ('id', 'name', 'item_count',)

    def get_item_count(self, group):
        return MenuItem.objects.filter(
            section_group=group,
            section_group__deleted=False,
            section_group__available=True,
            deleted=False,
        ).count()


class SerializerPutMenuItem(ModelSerializer):
    """
    serializer for adding menu Item
    """
    options = JSONStringCompatField(required=False)
    allergens = JSONStringCompatField(required=False)
    discount_details = JSONStringCompatField(required=False)
    # Typed list of extra-item UUIDs. Accepts a JSON array or a multipart
    # stringified array; each member is coerced to a uuid.UUID by the child, then
    # validate() canonicalises to lowercase strings, rejects duplicates, and proves
    # tenancy/is_extra. Omitted -> unchanged; [] -> cleared (see menu_relationships).
    extras_applicable = JSONStringCompatListField(
        child=serializers.UUIDField(),
        required=False,
        allow_empty=True,
    )
    tag_ids = JSONStringCompatListField(
        child=serializers.UUIDField(),
        write_only=True,
        required=False,
        allow_empty=True,
    )

    class Meta:
        model = MenuItem
        fields = (
            'id', 'section', 'section_group', 'image', 'name', 'description',
            'calories', 'allergens', 'tags', 'tag_ids',
            'primary_price', 'discounted_price', 'running_discount',
            'consider_discount_object', 'discount_description', 'discount_details',
            'available', 'in_stock', 'is_extra', 'is_special', 'is_featured',
            'is_popular', 'is_new', 'options', 'has_extras', 'extras_applicable',
            'age_restricted', 'extras_min_selections', 'extras_max_selections',
            'listing_position',
            # server-owned (output-only)
            'approved', 'enabled',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'tags', 'approved', 'enabled',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        # section / section_group are client-writable (an operator moves an item
        # between sections/groups) and MUST be same-tenant — enforced at runtime
        # by validate_menu_item_relationships (PR3), proven by _MENU_FK_EVIDENCE.
        tenant_relations = {
            'section': SameTenant('restaurant_id', verified_by=_MENU_FK_EVIDENCE),
            'section_group': SameTenant(
                'section__restaurant_id', verified_by=_MENU_FK_EVIDENCE
            ),
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)

        # Defense-in-depth contract parity with the admin discounts form (PR #480):
        # reject a clearly-inverted date window. Placed before the tag_ids
        # early-return so a discount-only PUT is still validated. end == start is
        # a valid one-day window (strict '<'). Only fires on a well-formed window —
        # unparseable dates are left to is_discount_active() (which treats them as
        # no bound). A fully-past window is NOT rejected (the form only warns).
        discount_details = attrs.get('discount_details')
        if isinstance(discount_details, dict):
            start_raw = discount_details.get('start_date') or ''
            end_raw = discount_details.get('end_date') or ''
            if start_raw and end_raw:
                try:
                    start_date = datetime.strptime(start_raw, '%Y-%m-%d').date()
                    end_date = datetime.strptime(end_raw, '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    start_date = end_date = None
                if start_date and end_date and end_date < start_date:
                    raise serializers.ValidationError(
                        {'discount_details': 'End date must be on or after the start date.'})

        # Persistent menu relationship integrity — the single write-time authority
        # (restaurants_app/controllers/menu_relationships.py): section-move tenancy +
        # section/group cohesion on the EFFECTIVE state (omitted vs explicit-null
        # aware), extras canonicalisation + tenancy/is_extra/self/duplicate, the
        # has_extras / selection-limit effective-state rules, and the referenced-extra
        # lifecycle guard. Runs on CREATE and UPDATE, ABOVE the tag_ids early-return
        # (which is bypassable by omitting tag_ids), and inside Secretary's
        # transaction so the row locks it takes are held through save().
        attrs = validate_menu_item_relationships(instance=self.instance, attrs=attrs)

        tag_ids = attrs.get('tag_ids')
        if tag_ids is None:
            return attrs

        # Resolve the restaurant from the section (in attrs for create,
        # in self.instance for update). All supplied tag IDs must belong
        # to that restaurant — cross-tenant references are rejected.
        section = attrs.get('section')
        if section is None and self.instance is not None:
            section = self.instance.section
        if section is None:
            raise serializers.ValidationError({
                'tag_ids': 'Cannot apply tags without a section.'
            })

        section_id = getattr(section, 'id', section)
        try:
            restaurant_id = MenuSection.objects.values(
                'restaurant_id'
            ).get(id=section_id)['restaurant_id']
        except MenuSection.DoesNotExist:
            raise serializers.ValidationError({
                'tag_ids': 'Section does not exist.'
            })

        unique_ids = list({str(tid) for tid in tag_ids})
        if unique_ids:
            valid_count = RestaurantTag.objects.filter(
                restaurant_id=restaurant_id,
                id__in=unique_ids,
                deleted=False,
            ).count()
            if valid_count != len(unique_ids):
                raise serializers.ValidationError({
                    'tag_ids': (
                        'One or more tag IDs do not belong to this '
                        'restaurant or do not exist.'
                    )
                })
        return attrs

    def create(self, validated_data):
        tag_ids = validated_data.pop('tag_ids', None)
        instance = super().create(validated_data)
        if tag_ids is not None:
            instance.sync_tag_links(tag_ids)
        return instance

    def update(self, instance, validated_data):
        tag_ids = validated_data.pop('tag_ids', None)
        instance = super().update(instance, validated_data)
        if tag_ids is not None:
            instance.sync_tag_links(tag_ids)
        return instance


class SerializerRestaurantTag(ModelSerializer):
    """Serializer for the restaurant-scoped tag catalog.

    `restaurant` is server-derived (read_only): the endpoint resolves + gates the
    restaurant and sets it via save() on create; it can never be reassigned.
    """

    class Meta:
        model = RestaurantTag
        fields = (
            'id', 'restaurant', 'name', 'category',
            'icon', 'colour', 'filterable',
            'display_order', 'is_system_preset',
        )
        read_only_fields = ('id', 'restaurant', 'is_system_preset',)


class SerializerPublicGetMenuItem(ModelSerializer):
    """
    serializer for getting the menu Item
    """
    has_options = SerializerMethodField()
    group = SerializerMethodField()
    extras = SerializerMethodField()
    discount_percentage = SerializerMethodField()
    is_discount_active = SerializerMethodField()
    current_price = SerializerMethodField()
    tags = SerializerMethodField()

    class Meta:
        model = MenuItem
        fields = (
            'id', 'name', 'description', 'calories', 'primary_price',
            'discounted_price', 'running_discount', 'image',
            'available', 'in_stock', 'allergens', 'tags', 'discount_details',
            'has_options', 'options', 'section', 'group', 'extras', 'is_extra',
            'discount_percentage', 'is_discount_active', 'current_price',
            'has_extras', 'is_special',
            'is_featured', 'is_popular', 'is_new',
            'age_restricted', 'extras_min_selections', 'extras_max_selections'
        )

    def get_tags(self, menu_item):
        # RestaurantTag.Meta.ordering is ['display_order', 'name'] (display_order
        # is a non-null default-0 IntegerField), so .all() yields the same order
        # as an explicit order_by while staying prefetch-friendly on the menu path.
        tags = menu_item.tags.all()
        return [
            {
                'id': str(tag.id),
                'name': tag.name,
                'category': tag.category,
                'icon': tag.icon,
                'colour': tag.colour,
            }
            for tag in tags
        ]

    def get_has_options(self, menu_item):
        options = menu_item.options
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except (ValueError, TypeError):
                return False
        if not isinstance(options, dict):
            return False
        return bool(options.get('hasModifiers'))

    def get_group(self, menu_item):
        if menu_item.section_group is None:
            return None
        policy = self.context.get('menu_policy')
        if policy is not None:
            # Public path: suppress a group the diner cannot currently see, so an
            # item never leaks the id/name of a group the public group list hid
            # (approved/enabled/available/deleted + its section's schedule).
            from restaurants_app.controllers.menu_publication import (
                group_operationally_visible,
            )
            if not group_operationally_visible(menu_item.section_group, policy['now']):
                return None
        elif menu_item.section_group.deleted:
            # No policy context (operator management / unit tests): unchanged.
            return None
        return {
            'id': str(menu_item.section_group.pk),
            'name': menu_item.section_group.name
        }

    def get_extras(self, menu_item):
        policy = self.context.get('menu_policy')
        if policy is not None:
            # Public path: extras come ONLY from the request-scoped safe map
            # (same restaurant, is_extra, structurally published), filtered by
            # THIS parent's normalized allowlist — configured order preserved,
            # deduped, self-reference dropped. No global/cross-tenant query.
            from restaurants_app.controllers.menu_publication import (
                normalize_extras_applicable,
            )
            extras_map = policy.get('extras_map') or {}
            parent_id = str(menu_item.id)
            extras = []
            for extra_id in normalize_extras_applicable(menu_item.extras_applicable):
                if extra_id == parent_id:
                    continue
                extra = extras_map.get(extra_id)
                if extra is None:
                    continue
                extras.append({
                    'id': extra.id,
                    'name': extra.name,
                    'primary_price': extra.primary_price,
                    'discount_details': extra.discount_details,
                })
            return extras

        # No policy context (operator management / unit tests): expose the parent's
        # VALID configured extras so the operator editor can reopen and edit an item
        # whose extra is not yet published WITHOUT the selection silently vanishing.
        # Same restaurant + is_extra + not deleted — but deliberately NOT filtered on
        # approved/enabled/available/in_stock (publication is the diner policy branch
        # above, PR #233). ONE bounded batch query replaces the old per-id global
        # lookup (which was also un-scoped by tenant); configured order preserved,
        # self and foreign/corrupt references dropped.
        from restaurants_app.controllers.menu_publication import (
            normalize_extras_applicable,
        )
        canonical = normalize_extras_applicable(menu_item.extras_applicable)
        if not canonical:
            return []
        parent_id = str(menu_item.id)
        # Scope to the parent's own restaurant via a subquery keyed off its already
        # loaded section_id, so this stays ONE query even on the list path (no
        # per-parent section fetch, no per-extra lookup).
        parent_restaurant = MenuSection.objects.filter(
            pk=menu_item.section_id
        ).values('restaurant_id')
        rows = {
            str(record['id']): record
            for record in MenuItem.objects.filter(
                pk__in=canonical,
                section__restaurant_id__in=parent_restaurant,
                is_extra=True,
                deleted=False,
            ).values('id', 'name', 'primary_price', 'discount_details')
        }
        return [rows[eid] for eid in canonical if eid != parent_id and eid in rows]

    def get_discount_percentage(self, menu_item):
        # Returns the discount magnitude as a non-negative percentage.
        # Source-of-truth precedence: discount_details.discount_percentage,
        # then discount_details.discount_amount, then derive from discounted_price.
        from decimal import Decimal
        if not menu_item.is_discount_active():
            return 0
        primary = Decimal(str(menu_item.primary_price or 0))
        if primary == 0:
            return 0

        details = menu_item.discount_details or {}
        if isinstance(details, dict):
            pct = Decimal(str(details.get('discount_percentage', 0) or 0))
            amt = Decimal(str(details.get('discount_amount', 0) or 0))
            if pct > 0:
                return float(round(pct, 2))
            if amt > 0:
                return float(round((amt / primary) * Decimal('100'), 2))

        if menu_item.discounted_price is not None:
            diff = primary - Decimal(str(menu_item.discounted_price))
            if diff <= 0:
                return 0
            return float(round((diff / primary) * Decimal('100'), 2))
        return 0

    def get_is_discount_active(self, menu_item):
        return menu_item.is_discount_active()

    def get_current_price(self, menu_item):
        # Effective BASE price (no modifiers): discounted when the discount is
        # active, else primary_price. Serialized as a string to match the
        # Decimal money convention of primary_price/discounted_price here.
        from decimal import Decimal
        return str(menu_item.effective_base_price().quantize(Decimal('0.01')))


class SerializerPutTable(ModelSerializer):
    """
    serializer for adding a table

    qr_version / qr_regenerated_at are the QR-generation counter + rotation
    timestamp — server-owned by the regenerate-qr endpoint and read_only here, so
    an ordinary table create/update can never rotate a credential. `restaurant` is
    client-supplied + endpoint-gated on create and pinned on update (validate);
    `dining_area` must be same-tenant. Audit/lifecycle fields are read_only.
    """
    class Meta:
        model = Table
        fields = (
            'id', 'restaurant', 'dining_area', 'number', 'str_number',
            'prepayment_required', 'room_name', 'smoking_zone', 'outdoor_seating',
            'reserved', 'enabled', 'display_name', 'min_capacity', 'max_capacity',
            'shape', 'status', 'tags', 'has_qr', 'qr_mode',
            'floor_x', 'floor_y', 'floor_width', 'floor_height', 'is_active',
            # server-owned (output-only)
            'qr_version', 'qr_regenerated_at',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'qr_version', 'qr_regenerated_at',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        tenant_relations = {
            'restaurant': SameTenant('id', verified_by=_TABLE_EVIDENCE),
            'dining_area': SameTenant('restaurant_id', verified_by=_TABLE_EVIDENCE),
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Bind the nested `dining_area` FK to the restaurant the caller was gated
        # against. The parent `restaurant` pin here is DEFENSE-IN-DEPTH — the
        # tables PUT flows through Secretary, which strips `restaurant` (not an
        # EDIT_INFORMATION['table'] key) before this serializer runs (see the
        # tripwire test). It exists so a cross-tenant table move is already
        # blocked if `restaurant` ever becomes editable, mirroring the #219
        # section-group guard. Reject only a DIFFERING value — an EQUAL one is a
        # no-op that must still pass.
        if self.instance is not None:
            restaurant_id = self.instance.restaurant_id
            incoming = attrs.get('restaurant')
            if incoming is not None and incoming.id != restaurant_id:
                raise serializers.ValidationError(
                    {'restaurant': 'Cannot move this record to another restaurant.'})
        else:
            incoming = attrs.get('restaurant')
            restaurant_id = incoming.id if incoming is not None else None
        if restaurant_id is not None:
            assert_fks_belong_to_restaurant(restaurant_id, attrs, ('dining_area',))
        return attrs


class SerializerPublicGetTable(ModelSerializer):
    """
    serializer for getting tables
    """
    dining_area = SerializerMethodField()

    class Meta:
        model = Table
        fields = '__all__'
    
    def get_dining_area(self, table):
        if table.dining_area is None:
            return None
        return {
            'name': table.dining_area.name,
            'available': table.dining_area.available,
            'smoking_zone': table.dining_area.smoking_zone,
            'outdoor_seating': table.dining_area.outdoor_seating,
            'is_indoor': table.dining_area.is_indoor,
            'accessible': table.dining_area.accessible,
            'default_server_section': table.dining_area.default_server_section,
            'is_active': table.dining_area.is_active,
        }


class SerializerPublicGetTableDetails(ModelSerializer):
    """
    serializer for getting details of a single table
    """
    current_order = SerializerMethodField()
    restaurant = SerializerMethodField()
    dining_area = SerializerMethodField()
    available = SerializerMethodField()

    class Meta:
        model = Table
        # Diner scan payload (PR 7A). Internal ops fields the diner never needs —
        # floor-plan geometry, is_active/enabled, and QR ops metadata
        # (has_qr/qr_regenerated_at) — are deliberately NOT exposed. The raw table
        # `id` is now inert (scanning needs the credential, downstream needs the
        # session), and `current_order.order_id` is likewise inert (order-details
        # is session-scoped) but kept so the diner can resume.
        fields = (
            'id', 'number', 'room_name', 'prepayment_required',
            'available', 'current_order', 'restaurant', 'reserved',
            'dining_area',
            'display_name', 'min_capacity', 'max_capacity', 'shape',
            'status', 'tags', 'qr_mode',
        )

    def get_dining_area(self, table):
        if table.dining_area is None:
            return None
        return {
            'name': table.dining_area.name,
            'available': table.dining_area.available,
            'smoking_zone': table.dining_area.smoking_zone,
            'outdoor_seating': table.dining_area.outdoor_seating,
            'is_indoor': table.dining_area.is_indoor,
            'accessible': table.dining_area.accessible,
            'default_server_section': table.dining_area.default_server_section,
            'is_active': table.dining_area.is_active,
        }

    def get_current_order(self, table):
        # Single source of truth for table occupancy: the same fulfilment-axis
        # gate the kitchen board and the order-create path use (not deleted, not
        # cancelled, fulfilment_status != 'served'). The old
        # order_status/payment_status check wrongly flagged a served-but-unpaid
        # order as ongoing — diner payment is unwired, so it stays 'pending' —
        # which blocked the diner while the kitchen board (keyed off the
        # fulfilment axis) showed nothing. Local import avoids a
        # serializers <-> controllers import cycle.
        from orders_app.controllers.con_orders import ConOrder
        result = ConOrder.any_present_ongoing_order(table)
        return {
            'ongoing': result.get('present', False),
            'order_id': result.get('order_id'),
        }

    def get_restaurant(self, table):
        restaurant = table.restaurant
        logo = restaurant.logo
        cover_photo = restaurant.cover_photo

        if logo is not None:
            if len(str(logo)) < 1:
                logo = None
            else:
                logo = str(logo)
        if cover_photo is not None:
            if len(str(cover_photo)) < 1:
                cover_photo = None
            else:
                cover_photo = str(cover_photo)
        return {
            'id': str(restaurant.pk),
            'name': restaurant.name,
            'logo': logo,
            'cover_photo': cover_photo,
            'branding_configuration': restaurant.branding_configuration,
            'socials': restaurant.socials,
            'menu_approval_status': restaurant.first_time_menu_approval_decision,
            'preset_tags': SerializerRestaurantTag(
                RestaurantTag.objects.filter(
                    restaurant=restaurant, deleted=False,
                ).order_by('display_order', 'name'),
                many=True,
            ).data
        }

    def get_available(self, table):
        # Reuse the already-loaded instance — avoids a redundant Table re-fetch
        # inside get_table_availability.
        return get_table_availability(table=table)


class SerializerGetFullMenu(ModelSerializer):
    item_count = SerializerMethodField()
    groups = SerializerMethodField()
    items = SerializerMethodField()
    is_currently_active = SerializerMethodField()

    class Meta:
        model = MenuSection
        fields = (
            'id', 'name', 'section_banner_image', 'available',
            'availability', 'schedules', 'is_currently_active',
            'item_count', 'groups', 'items'
        )

    def _menu_policy(self):
        # The trusted internal publication context set ONLY by the public
        # show-menu path. Its presence switches this serializer (and the nested
        # item serializer) into strict diner mode at a single captured time; its
        # absence (operator management, direct unit tests) preserves the prior
        # behaviour exactly.
        return self.context.get('menu_policy')

    def _visible_items(self, section):
        # Public path only. The section's items filtered by the canonical READ
        # policy at the captured time, computed ONCE per section (get_item_count
        # reuses it — no duplicate query). Items are fetched with their group
        # (select_related) and tags (prefetch); the parent section is cached onto
        # each item and its group so the policy predicate never re-queries a
        # section/group row. Structural + available is filtered in the query;
        # item_visible_in_menu adds the group-visibility + schedule check.
        cache = getattr(self, '_visible_items_cache', None)
        if cache is None:
            cache = self._visible_items_cache = {}
        if section.pk in cache:
            return cache[section.pk]
        from restaurants_app.controllers.menu_publication import (
            item_visible_in_menu,
        )
        now = self._menu_policy()['now']
        visible = []
        for item in (
            MenuItem.objects
            .filter(
                section=section, approved=True, enabled=True,
                deleted=False, available=True,
            )
            .select_related('section_group')
            .prefetch_related('tags')
        ):
            item.section = section
            if item.section_group is not None:
                item.section_group.section = section
            if item_visible_in_menu(item, now):
                visible.append(item)
        cache[section.pk] = visible
        return visible

    def get_is_currently_active(self, section):
        from restaurants_app.controllers.utils.schedule_utils import (
            is_section_currently_active
        )
        policy = self._menu_policy()
        now = policy['now'] if policy is not None else None
        return is_section_currently_active(section, now=now)

    def get_groups(self, section):
        policy = self._menu_policy()
        if policy is None:
            # No policy context (operator management / unit tests): unchanged.
            filters = {
                'section': section,
                'approved': True,
                'enabled': True,
                'deleted': False,
            }
            groups = SectionGroup.objects.filter(**filters)
            return [
                {'id': str(group.pk), 'name': str(group.name)} for group in groups
            ]
        # Public path: only groups the diner can currently see (structural +
        # available; the parent section is already visible). Cache the section onto
        # each group so the operational predicate does not re-query it.
        from restaurants_app.controllers.menu_publication import (
            group_operationally_visible,
        )
        now = policy['now']
        result = []
        for group in SectionGroup.objects.filter(section=section):
            group.section = section
            if group_operationally_visible(group, now):
                result.append({'id': str(group.pk), 'name': str(group.name)})
        return result

    def get_items(self, section):
        policy = self._menu_policy()
        if policy is None:
            # No policy context (operator management / unit tests): unchanged
            # null-safe anti-join dropping only soft-deleted groups.
            filters = {
                'section': section,
                'approved': True,
                'enabled': True,
                'deleted': False,
                'available': True
            }
            items = MenuItem.objects.filter(**filters).exclude(
                section_group__deleted=True
            )
            return SerializerPublicGetMenuItem(items, many=True).data
        # Public path: serialize the policy-filtered items; forward the context so
        # the nested item serializer's get_group/get_extras stay strict and use the
        # request-scoped safe extras map.
        return SerializerPublicGetMenuItem(
            self._visible_items(section), many=True, context=self.context,
        ).data

    def get_item_count(self, section):
        policy = self._menu_policy()
        if policy is None:
            # No policy context (operator management / unit tests): unchanged.
            filters = {
                'section': section,
                'approved': True,
                'enabled': True,
                'deleted': False,
                'available': True
            }
            return MenuItem.objects.filter(**filters).exclude(
                section_group__deleted=True
            ).count()
        # Public path: derive from the already-filtered collection (no re-query).
        return len(self._visible_items(section))


class SerializerPutDiningArea(ModelSerializer):
    """
    `restaurant` is server-derived (read_only): set at creation by
    create_dining_area from the authorized resource; never client-reassignable
    (this serializer only backs the UPDATE path via Secretary).
    """
    class Meta:
        model = DiningArea
        fields = (
            'id', 'name', 'description', 'available', 'smoking_zone',
            'outdoor_seating', 'is_indoor', 'accessible',
            'default_server_section', 'is_active', 'restaurant',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'restaurant', 'created_by', 'deleted', 'deleted_by',
            'time_deleted', 'deletion_reason', 'time_created', 'time_last_updated',
        )


class SerializerGetDiningArea(ModelSerializer):
    no_tables = SerializerMethodField()
    tables = SerializerMethodField()

    class Meta:
        model = DiningArea
        fields = (
            'id', 'name', 'description',
            'smoking_zone', 'outdoor_seating',
            'is_indoor', 'accessible', 'default_server_section', 'is_active',
            'no_tables', 'tables', 'available'
        )

    def get_no_tables(self, dining_area):
        return Table.objects.filter(
            dining_area=dining_area, deleted=False
        ).count()

    def get_tables(self, dining_area):
        tables = Table.objects.filter(dining_area=dining_area, deleted=False)
        return [
            {
                'id': str(table.pk),
                'number': table.number,
                'available': get_table_availability(table_id=str(table.pk)),
                'reserved': table.reserved,
                'enabled': table.enabled,
                'display_name': table.display_name,
                'min_capacity': table.min_capacity,
                'max_capacity': table.max_capacity,
                'shape': table.shape,
                'status': table.status,
                'tags': table.tags,
                'has_qr': table.has_qr,
                'qr_mode': table.qr_mode,
                'floor_x': table.floor_x,
                'floor_y': table.floor_y,
                'is_active': table.is_active,
            } for table in tables
        ]


class UpsellItemSerializer(ModelSerializer):
    """Serializer for individual upsell items — includes basic menu item info.

    item_price is the ORIGINAL price (primary_price). When item_running_discount
    is true, item_discounted_price holds the effective price; the diner UI shows
    the discounted price with the original struck through and adds the discounted
    price to the basket. Mirrors the discount projection in MenuItemSerializer.
    """
    item_id = serializers.UUIDField(source='menu_item.id', read_only=True)
    item_name = serializers.CharField(source='menu_item.name', read_only=True)
    item_price = serializers.DecimalField(
        source='menu_item.primary_price', max_digits=50, decimal_places=2, read_only=True
    )
    item_discounted_price = serializers.DecimalField(
        source='menu_item.discounted_price', max_digits=50, decimal_places=2,
        read_only=True, allow_null=True
    )
    item_running_discount = serializers.BooleanField(
        source='menu_item.running_discount', read_only=True
    )
    item_discount_percentage = SerializerMethodField()
    item_image = serializers.ImageField(source='menu_item.image', read_only=True)
    item_available = serializers.BooleanField(source='menu_item.available', read_only=True)
    item_in_stock = serializers.BooleanField(source='menu_item.in_stock', read_only=True)

    class Meta:
        model = UpsellItem
        fields = [
            'id', 'menu_item', 'item_id', 'item_name', 'item_price',
            'item_discounted_price', 'item_running_discount', 'item_discount_percentage',
            'item_image', 'item_available', 'item_in_stock', 'listing_position'
        ]
        # This serializer is a READ-ONLY projection — upsell items are created /
        # reordered directly by the endpoint (_add_items validates menu_item against
        # config.restaurant, then get_or_create). `menu_item` is therefore
        # server-derived here (read_only), so it is not a client write surface.
        read_only_fields = ('id', 'menu_item',)

    def get_item_discount_percentage(self, upsell_item):
        # Same precedence as MenuItemSerializer.get_discount_percentage:
        # discount_details.discount_percentage, then discount_amount, then derive
        # from discounted_price. Returns a non-negative percentage (0 when no discount).
        from decimal import Decimal
        menu_item = upsell_item.menu_item
        if not menu_item or not menu_item.running_discount:
            return 0
        primary = Decimal(str(menu_item.primary_price or 0))
        if primary == 0:
            return 0
        details = menu_item.discount_details or {}
        if isinstance(details, dict):
            pct = Decimal(str(details.get('discount_percentage', 0) or 0))
            amt = Decimal(str(details.get('discount_amount', 0) or 0))
            if pct > 0:
                return float(round(pct, 2))
            if amt > 0:
                return float(round((amt / primary) * Decimal('100'), 2))
        if menu_item.discounted_price is not None:
            diff = primary - Decimal(str(menu_item.discounted_price))
            if diff <= 0:
                return 0
            return float(round((diff / primary) * Decimal('100'), 2))
        return 0


class UpsellConfigSerializer(ModelSerializer):
    """Full upsell config with nested items.

    On the anonymous diner path the caller passes context={'public_only': True},
    which prunes carousel entries whose referenced menu item is no longer
    published (unapproved / disabled / soft-deleted) — a soft delete leaves the
    UpsellItem row intact (FK cascade only fires on hard delete), so without this
    an unpublished item would re-enter the public payload. The operator-facing
    endpoint passes no context and still sees every configured item.
    """
    items = SerializerMethodField()

    class Meta:
        model = UpsellConfig
        fields = [
            'id', 'enabled', 'title', 'max_items_to_show',
            'hide_if_in_basket', 'hide_out_of_stock', 'items'
        ]

    def get_items(self, config):
        # Manager order (listing_position) is preserved by using the related
        # manager directly. availability/stock stay passthrough data (the diner
        # UI applies hide_out_of_stock).
        upsell_items = config.upsell_items.all()
        if self.context.get('public_only'):
            policy = self.context.get('menu_policy')
            if policy is not None:
                # Public menu path: an upsell inherits the SAME publication policy
                # as a top-level diner item — same restaurant, structurally
                # published, under a currently-visible section AND group (schedule
                # + availability) at the captured time. So an item hidden from the
                # main menu by its section/group/schedule cannot re-enter via
                # upsell. The item's OWN available/in_stock are intentionally NOT
                # gated here (they remain passthrough for hide_out_of_stock),
                # mirroring item_orderable.
                from restaurants_app.controllers.menu_publication import (
                    item_orderable,
                )
                now = policy['now']
                restaurant_id = str(policy['restaurant_id'])
                upsell_items = [
                    ui for ui in upsell_items.select_related(
                        'menu_item__section', 'menu_item__section_group',
                    )
                    if ui.menu_item is not None
                    and str(ui.menu_item.section.restaurant_id) == restaurant_id
                    and item_orderable(ui.menu_item, now)
                ]
            else:
                # public_only without a menu_policy (e.g. a direct-serializer unit
                # test): keep the item-level publication filter (unchanged).
                upsell_items = upsell_items.filter(
                    menu_item__approved=True,
                    menu_item__enabled=True,
                    menu_item__deleted=False,
                )
        return UpsellItemSerializer(
            upsell_items, many=True, context=self.context
        ).data


class UpsellConfigUpdateSerializer(ModelSerializer):
    """For updating config settings (without items)."""
    class Meta:
        model = UpsellConfig
        fields = ['enabled', 'title', 'max_items_to_show', 'hide_if_in_basket', 'hide_out_of_stock']


class SerializerPutReservation(ModelSerializer):
    class Meta:
        model = Reservation
        fields = (
            'id', 'restaurant', 'table', 'server',
            'guest_name', 'guest_phone', 'guest_email',
            'date_time', 'party_size', 'status',
            'area_preference', 'notes', 'tags', 'seated_at',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        # restaurant is client-supplied + endpoint-gated on create, pinned on
        # update (validate below); table/server must be same-tenant.
        tenant_relations = {
            'restaurant': SameTenant('id', verified_by=_RESERVATION_EVIDENCE),
            'table': SameTenant('restaurant_id', verified_by=_RESERVATION_EVIDENCE),
            'server': SameTenant('restaurant_id', verified_by=_RESERVATION_EVIDENCE),
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Bind the nested FKs (table, server) to the restaurant the caller was
        # gated against. On update the parent `restaurant` is PINNED to the row's
        # own restaurant (LOAD-BEARING — closes the cross-tenant row-move): a
        # DIFFERING value is rejected, an EQUAL one (the frontend re-sends the
        # row's own restaurant on PUT) is a no-op that must still pass.
        if self.instance is not None:
            restaurant_id = self.instance.restaurant_id
            incoming = attrs.get('restaurant')
            if incoming is not None and incoming.id != restaurant_id:
                raise serializers.ValidationError(
                    {'restaurant': 'Cannot move this record to another restaurant.'})
        else:
            incoming = attrs.get('restaurant')
            restaurant_id = incoming.id if incoming is not None else None
        if restaurant_id is not None:
            assert_fks_belong_to_restaurant(restaurant_id, attrs, ('table', 'server'))
        return attrs


class SerializerGetReservation(ModelSerializer):
    table_info = SerializerMethodField()

    class Meta:
        model = Reservation
        fields = (
            'id', 'restaurant', 'table', 'table_info',
            'guest_name', 'guest_phone', 'guest_email',
            'date_time', 'party_size', 'status',
            'area_preference', 'notes', 'tags',
            'seated_at', 'server',
            'time_created', 'time_last_updated',
        )

    def get_table_info(self, reservation):
        if reservation.table is None:
            return None
        return {
            'id': str(reservation.table.pk),
            'number': reservation.table.number,
            'display_name': reservation.table.display_name,
        }


class SerializerPutWaitlistEntry(ModelSerializer):
    class Meta:
        model = WaitlistEntry
        fields = (
            'id', 'restaurant', 'seated_table',
            'guest_name', 'guest_phone', 'party_size',
            'quoted_wait_min', 'quoted_wait_max', 'tags', 'notes',
            'status', 'seated_at', 'added_at',
            'created_by', 'deleted', 'deleted_by', 'time_deleted',
            'deletion_reason', 'time_created', 'time_last_updated',
        )
        read_only_fields = (
            'id', 'added_at', 'created_by', 'deleted', 'deleted_by',
            'time_deleted', 'deletion_reason', 'time_created', 'time_last_updated',
        )
        # restaurant is client-supplied + endpoint-gated on create, pinned on
        # update (validate below); seated_table must be same-tenant.
        tenant_relations = {
            'restaurant': SameTenant('id', verified_by=_WAITLIST_EVIDENCE),
            'seated_table': SameTenant('restaurant_id', verified_by=_WAITLIST_EVIDENCE),
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        # Bind the nested `seated_table` FK to the restaurant the caller was gated
        # against. On update the parent `restaurant` is PINNED to the row's own
        # restaurant (LOAD-BEARING — closes the cross-tenant row-move): a DIFFERING
        # value is rejected, an EQUAL one (the frontend re-sends the row's own
        # restaurant on PUT) is a no-op that must still pass.
        if self.instance is not None:
            restaurant_id = self.instance.restaurant_id
            incoming = attrs.get('restaurant')
            if incoming is not None and incoming.id != restaurant_id:
                raise serializers.ValidationError(
                    {'restaurant': 'Cannot move this record to another restaurant.'})
        else:
            incoming = attrs.get('restaurant')
            restaurant_id = incoming.id if incoming is not None else None
        if restaurant_id is not None:
            assert_fks_belong_to_restaurant(restaurant_id, attrs, ('seated_table',))
        return attrs


class SerializerGetWaitlistEntry(ModelSerializer):
    seated_table_info = SerializerMethodField()

    class Meta:
        model = WaitlistEntry
        fields = (
            'id', 'restaurant', 'guest_name', 'guest_phone',
            'party_size', 'quoted_wait_min', 'quoted_wait_max',
            'added_at', 'tags', 'notes', 'status',
            'seated_table', 'seated_table_info', 'seated_at',
            'time_created', 'time_last_updated',
        )

    def get_seated_table_info(self, entry):
        if entry.seated_table is None:
            return None
        return {
            'id': str(entry.seated_table.pk),
            'number': entry.seated_table.number,
            'display_name': entry.seated_table.display_name,
        }
