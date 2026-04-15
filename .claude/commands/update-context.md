# Update Project Context

Run this at the start of a new working session or after several PRs have
been merged, to keep CLAUDE.md accurate as the project evolves.

## 1. Read the current CLAUDE.md
- Read `CLAUDE.md` at the project root
- Note the current implementation status section and any rules or notes

## 2. Audit the actual repo state
- Check `restaurants_app/endpoints/` for any new endpoint files
- Check `restaurants_app/migrations/` for the latest migration number
- Check `dinify_backend/configss/edit_information.py` for any new sections
  or fields added since the last update
- Check `orders_app/` for any new URL files or models
- Check `.github/workflows/` for any new or changed workflows
- Check `requirements.txt` for any new dependencies worth noting

## 3. Compare and identify drift
- Which phases or modules have been completed since the last update?
- Are there any new patterns, conventions, or gotchas that have emerged
  that are not yet documented in CLAUDE.md?
- Are there any items marked as outstanding or in-progress that are
  now resolved?
- Are there any rules or notes that are now outdated or incorrect?

## 4. Update CLAUDE.md
- Update the Implementation Status section to reflect current reality
- Add any new patterns or conventions that have emerged
- Remove or correct anything that is no longer accurate
- Do NOT remove any rules marked as CRITICAL unless they are genuinely
  no longer applicable
- Do NOT change the overall structure of the file

## 5. Report what changed
- List every change made to CLAUDE.md and why
- If nothing needed changing, say so explicitly

Commit the updated CLAUDE.md with message `docs: update project context`
and open a PR.
