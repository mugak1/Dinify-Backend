"""Standing source guards run by CI and scripts/verify.sh.

A package only so ``scripts/tests_guards.py`` is discoverable by ``unittest`` (and by
the Django test runner); every guard is still run as a plain script:
``python scripts/check_money_fields.py``.
"""
