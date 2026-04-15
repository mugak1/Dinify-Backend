# Dinify Backend Check

Run this after completing any backend task before opening a PR.

Work through each item below and report the result of each check:

## 1. EDIT_INFORMATION Coverage
- Read `dinify_backend/configss/edit_information.py`
- Identify any model fields added or modified in this task
- Confirm each new editable field has been added to the appropriate
  EDIT_INFORMATION list
- If any are missing, add them now before proceeding

## 2. Migrations
- Confirm that `makemigrations --check --dry-run` would pass
- If any model was changed, confirm a migration file was generated
  and is included in this branch
- If missing, generate it now before proceeding

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
