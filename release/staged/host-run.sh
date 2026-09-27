#!/bin/bash
# STAGED — NOT ACTIVE. D08 B3: the script release/staged/deploy-backend.yml sends through SSM.
#
# The shebang is load-bearing: AWS-RunShellScript otherwise runs /bin/sh (dash). SSM runs this
# as ROOT. It is the ONLY privileged step, and it runs nothing from the candidate: the trusted
# verifier (release/ + dependency_audit/ at the WORKFLOW's own revision) does everything, from
# a root-owned directory this script creates, with the base interpreter, isolated from the
# environment and from user site-packages.
#
# Placeholders are substituted by the workflow AFTER the values were derived from bytes it
# holds (sha256 of each transferred file) or validated (the operation id). Nothing else in
# this file is expanded at workflow time: it is read with --rawfile, never interpolated.
#
# OUTPUT ORDER IS LOAD-BEARING: SSM keeps only the FIRST 24,000 characters. The CLI's full
# report goes to a root-only file; its attestations (the B3-* lines) are printed first.
set -euo pipefail
umask 077

OPERATION="__OPERATION__"
BUCKET="__BUCKET__"
PREFIX="__PREFIX__"
declare -A WANT=(
  [candidate.zip]="__CANDIDATE_SHA256__"
  [preflight.zip]="__PREFLIGHT_SHA256__"
  [admission.json]="__ADMISSION_SHA256__"
  [trusted.tar]="__TRUSTED_SHA256__"
)

# Host facts this script needs BEFORE the profile can be read. Each is created by the reviewed
# cutover (release/CUTOVER.md) and checked here, never created or repaired here.
BASE=/opt/dinify-backend-release          # root:root 0711; incoming/ 0700, trusted/ 0711 (see below)
PY=/usr/bin/python3.12                    # the base interpreter the profile names (checked again there)
AWS=/usr/local/bin/aws                    # AWS CLI v2, hand-installed (see CLAUDE.md)

fail() { echo "B3-OUTCOME: refused"; echo "B3-HOST: $*"; exit 1; }

printf '%s' "$OPERATION" | grep -Eq '^[a-z0-9][a-z0-9-]{2,79}$' || fail "operation id malformed"
[ "$(id -u)" = "0" ] || fail "not root"
# incoming/ holds the transferred files and stays private. BASE and trusted/ are 0711 —
# traversable, not listable, not writable — because the preparation, migration and runtime
# identities import the trusted verifier and run from inside it; 0700 there fails every one of
# them with EACCES before anything is decided (the CLI refuses that by name too).
for entry in "$BASE:711" "$BASE/incoming:700" "$BASE/trusted:711"; do
  d="${entry%:*}"; mode="${entry##*:}"
  [ -d "$d" ] && [ ! -L "$d" ] || fail "$d is not a directory"
  [ "$(stat -c '%u %a' "$d")" = "0 $mode" ] || fail "$d must be root-owned mode $mode"
done
[ -x "$PY" ] && [ -x "$AWS" ] || fail "the base interpreter or the AWS CLI is missing"

IN="$BASE/incoming/$OPERATION"
T="$BASE/trusted/$OPERATION"
[ ! -e "$IN" ] && [ ! -e "$T" ] || fail "operation $OPERATION was already used on this host"
mkdir -m 0700 "$IN" "$T"

for f in "${!WANT[@]}"; do
  "$AWS" s3 cp "s3://${BUCKET}/${PREFIX}/${f}" "$IN/$f" --only-show-errors >/dev/null 2>&1 || fail "could not fetch $f"
  have="$(sha256sum "$IN/$f" | cut -d' ' -f1)"
  [ "$have" = "${WANT[$f]}" ] || fail "$f is not the file the workflow admitted"
done

# The trusted verifier: a git archive of two directories. Refuse anything else in it, then
# make it root-owned and unwritable; the CLI checks that again before it trusts it.
tar -tf "$IN/trusted.tar" | grep -Evq '^(release|dependency_audit)(/|$)' && fail "trusted.tar holds more than release/ and dependency_audit/"
tar -xf "$IN/trusted.tar" -C "$T" --no-same-owner --no-same-permissions --no-overwrite-dir
chown -R root:root "$T"
find "$T" -type d -exec chmod 0755 {} +
find "$T" -type f -exec chmod 0644 {} +
chmod 0755 "$T"
PROFILE="$T/release/profiles/uat-backend.json"

set +e
( cd "$T" && env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8 "$PY" -B -E -s -m release host deploy \
    --profile "$PROFILE" --operation "$OPERATION" --admission "$IN/admission.json" \
    --candidate-zip "$IN/candidate.zip" --preflight-zip "$IN/preflight.zip" --trusted "$T" ) > "$IN/report.out" 2> "$IN/report.err"
rc=$?
set -e
grep '^B3-' "$IN/report.out" || true
echo "B3-EXIT: $rc"
echo "---- problems (the full report stays in $IN on the host) ----"
grep -F '✗' "$IN/report.err" | head -40 || true
# The transferred zips are no longer needed; the report, the admission and the trusted
# verifier stay for the record. Nothing under the release root is touched here.
rm -f "$IN/candidate.zip" "$IN/preflight.zip" "$IN/trusted.tar"
exit "$rc"
