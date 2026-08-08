#!/usr/bin/env bash
#
# leak-check.sh - refuse to ship content that does not belong in a public
# repository: credentials, contact details, absolute paths from a developer
# machine, and characters outside ASCII.
#
# Usage:
#   tools/leak-check.sh                       scan the files git would publish
#   tools/leak-check.sh --all                 scan every file in the tree
#   tools/leak-check.sh --patterns extra.txt  add project specific patterns
#   tools/leak-check.sh path/to/dir           scan somewhere else
#
# The optional pattern file holds one extended regular expression per line;
# blank lines and lines starting with # are ignored, matching is case
# insensitive. Use it for strings that are specific to your organisation, such
# as internal hostnames or unreleased product names, and keep the file outside
# the repository so it is not published along with the code.
#
# By default only tracked files are scanned, because those are the ones git
# will publish. Build output, virtual environments and compiled bytecode can
# still hold absolute paths from the machine that produced them, so --all
# scans the whole working tree, binaries included, before you package or
# archive a directory by hand.
#
# Exit status: 0 when clean, 1 when something was found, 2 on usage error.

set -uo pipefail

usage() {
  sed -n '3,25p' "$0" | sed 's/^# \{0,1\}//; s/^#$//'
}

TARGET="."
PATTERNS_FILE="${SOCKPOST_LEAKCHECK_PATTERNS:-}"
SCAN_ALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --patterns)
      shift
      [ $# -gt 0 ] || { echo "error: --patterns needs a file" >&2; exit 2; }
      PATTERNS_FILE="$1"
      ;;
    --all)
      SCAN_ALL=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown option $1" >&2
      exit 2
      ;;
    *)
      TARGET="$1"
      ;;
  esac
  shift
done

cd "$TARGET" || exit 2

# ---------------------------------------------------------------------------
# file list
# ---------------------------------------------------------------------------
everything() {
  find . -type f -not -path './.git/*' | sed 's|^\./||'
}

SCOPE="tracked files"
FILES=""
if [ "$SCAN_ALL" -eq 1 ]; then
  SCOPE="every file in the working tree"
  FILES=$(everything)
else
  FILES=$(git ls-files 2>/dev/null)
  if [ -z "$FILES" ]; then
    SCOPE="every file in the working tree (not a git repository)"
    FILES=$(everything)
  fi
fi

if [ -z "$FILES" ]; then
  echo "leak-check: no files to scan" >&2
  exit 2
fi

# Binary files are skipped in the default scope and read in --all, where the
# point is precisely to catch a path baked into compiled output.
BINARY_FLAG="-I"
[ "$SCAN_ALL" -eq 1 ] && BINARY_FLAG="-a"

FINDINGS=0

report() {
  # report <rule> <grep output>
  local rule="$1"
  local hits="$2"
  [ -n "$hits" ] || return 0
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    printf 'FINDING rule=%s %s\n' "$rule" "$line"
    FINDINGS=$((FINDINGS + 1))
  done <<EOF
$hits
EOF
}

scan() {
  # scan <rule> <extended regex> [extra grep flags]
  local rule="$1"
  local pattern="$2"
  local extra="${3:-}"
  local hits
  hits=$(printf '%s\n' "$FILES" | tr '\n' '\0' \
    | LC_ALL=C xargs -0 grep -n -H "$BINARY_FLAG" -E $extra -e "$pattern" 2>/dev/null \
    | LC_ALL=C cut -c1-200)
  report "$rule" "$hits"
}

# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

# The sources are written in ASCII. This catches emoji, smart quotes and text
# pasted in from an editor or a document with a different character set.
[ "$SCAN_ALL" -eq 1 ] || scan non-ascii '[^[:print:][:space:]]'

# Absolute paths from a developer machine, which carry an account name.
scan home-path '/(Users|home)/[A-Za-z0-9._-]+'

# Contact details.
scan email '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'
scan phone '\+[0-9]{2,3}[ -]?\(?[0-9]{2,3}\)?[ -]?[0-9]{4,5}[ -]?[0-9]{4}'

# Credentials.
scan private-key 'BEGIN [A-Z ]*PRIVATE KEY'
scan aws-key 'AKIA[0-9A-Z]{16}'
scan github-token 'gh[pousr]_[A-Za-z0-9]{20,}'
scan slack-token 'xox[abprs]-[A-Za-z0-9-]{10,}'
scan bearer '[Aa]uthorization:[ ]*[Bb]earer[ ]+[A-Za-z0-9._-]{8,}'
scan secret-assignment '(api[_-]?key|secret|token|passwd|password)[ ]*[:=][ ]*["'"'"'][^"'"'"']{8,}'

# Optional project specific patterns.
if [ -n "$PATTERNS_FILE" ]; then
  if [ ! -r "$PATTERNS_FILE" ]; then
    echo "error: cannot read patterns file $PATTERNS_FILE" >&2
    exit 2
  fi
  while IFS= read -r raw; do
    case "$raw" in
      ''|\#*) continue ;;
    esac
    scan project "$raw" "-i"
  done < "$PATTERNS_FILE"
fi

# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------
COUNT=$(printf '%s\n' "$FILES" | grep -c .)

# Be explicit about what was not looked at: a clean result is only as wide as
# its scope, and untracked build output is the usual place a stray path hides.
UNTRACKED=0
if [ "$SCAN_ALL" -eq 0 ] && git rev-parse --git-dir >/dev/null 2>&1; then
  UNTRACKED=$(everything | grep -c . )
  UNTRACKED=$((UNTRACKED - COUNT))
  [ "$UNTRACKED" -lt 0 ] && UNTRACKED=0
fi

if [ "$FINDINGS" -eq 0 ]; then
  echo "leak-check: clean (scope=$SCOPE files=$COUNT findings=0)"
  if [ "$UNTRACKED" -gt 0 ]; then
    echo "leak-check: note - $UNTRACKED file(s) in the working tree were not" \
         "scanned because they are untracked; run with --all before creating" \
         "an archive by hand"
  fi
  exit 0
fi
echo "leak-check: FAILED (scope=$SCOPE files=$COUNT findings=$FINDINGS)" >&2
exit 1
