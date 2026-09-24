"""
The account an email address names, when the address may be typed in any capitals.

Registration (``self_register``) and admin onboarding (``onboarding_creation``) store an
email lower-cased, while a profile edit (``self_update_user_profile``) stores it exactly
as typed. So an address can reach a lookup in different capitals from the ones it was
stored in, and ``login`` and ``reset_password._resolve_user`` both resolve through here.
"""
from users_app.models import User


def get_user_by_email(value):
    """
    ``User.objects.get(email=value)``, falling back to the address lower-cased.

    It answers exactly as that ``get`` does, except in ONE case: when the address as
    typed names no account but its lower-cased form names exactly one, that account is
    returned instead of ``DoesNotExist`` being raised.

    - The address AS TYPED is asked first. An account stored with other capitals still
      resolves to itself when its owner types them, even beside an account holding the
      lower-cased address.
    - The fallback accepts a SINGLE account only. ``User.email`` has no unique
      constraint; for an address several accounts share, this raises the
      ``DoesNotExist`` the exact lookup raised before, never a new
      ``MultipleObjectsReturned`` that a caller handling only ``DoesNotExist`` would
      turn into a 500. A shared address typed exactly still raises
      ``MultipleObjectsReturned``, as it always has.
    """
    try:
        return User.objects.get(email=value)
    except User.DoesNotExist:
        lowered = value.lower()
        if lowered == value:
            raise
        matches = list(User.objects.filter(email=lowered)[:2])
        if len(matches) != 1:
            raise
        return matches[0]
