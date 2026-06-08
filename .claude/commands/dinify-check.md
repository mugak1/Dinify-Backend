# Dinify Backend Check

Run this after completing any backend task before opening a PR.

## 1. Run the mechanical checks — `./scripts/verify.sh`
`scripts/verify.sh` is the single committed source of truth for the runnable
checks. It runs the same commands as CI (`.github/workflows/ci.yml`), against
`dinify_backend.test_settings`:
- `django check`
- `makemigrations --check --dry-run`
- the full Django test suite

Run it from the repo root and confirm every step reports PASS:

    ./scripts/verify.sh

Notes:
- If `makemigrations --check` fails, you changed a model without a migration —
  generate it, then confirm the new migration file is committed to this branch
  (the check passes once the file exists on disk, so verify it is staged).
- Do not re-list these commands anywhere else; if they change, change
  `verify.sh`.

The remaining checks are semantic — `verify.sh` cannot perform them. Work
through each and report PASS or FAIL:

## 2. EDIT_INFORMATION Coverage
- Read `dinify_backend/configss/edit_information.py`
- Identify any model fields added or modified in this task
- Confirm each new editable field has been added to the appropriate
  EDIT_INFORMATION list
- If any are missing, add them now before proceeding

## 3. SMS Calls
- Search the files touched in this task for any calls to Yo Uganda
  SMS functions
- Confirm every SMS call is dispatched via `threading.Thread(daemon=True)`
- Flag any synchronous SMS calls as a blocker

## 4. MongoDB Dependencies
- Search files touched in this task for any new direct MongoDB calls
  outside of `archive_record` and `save_action_log`
- Confirm any new MongoDB calls have try/except wrappers
- Flag any hard dependencies as a blocker

## 5. Monetary Fields
- Search models touched in this task for any new monetary/financial fields
- Confirm all use `DecimalField`, not `FloatField`
- Flag any FloatField on a monetary value as a blocker

## 6. Endpoint Registration
- If a new endpoint file was created in `restaurants_app/endpoints/`,
  confirm it is registered in `restaurants_app/urls.py`
- Confirm it is placed ABOVE the catch-all `<str:config_detail>/` route

Report PASS or FAIL for each section. Fix any failures before opening the PR.
