#!/usr/bin/env bash
#
# leak-check.sh - refuse to ship content that does not belong in a public
# repository: credentials, contact details, absolute paths from a developer
# machine, and characters outside ASCII.
#
# Usage:
#   tools/leak-check.sh                       scan the files git would publish
#   tools/leak-check.sh --all                 scan every file in the tree
#   tools/leak-check.sh --commits <range>     scan commit metadata and messages
#   tools/leak-check.sh --patterns extra.txt  add project specific patterns
#   tools/leak-check.sh path/to/dir           scan somewhere else
#
# The optional pattern file holds one extended regular expression per line;
# blank lines and lines starting with # are ignored, matching is case
# insensitive. Use it for strings that are specific to your organisation, such
# as internal hostnames or unreleased product names, and keep the file outside
# the repository so it is not published along with the code.
#
# A pattern file may also carry exceptions, one per line:
#
#   allow <path glob> <extended regex>
#
# A finding is dropped when its file matches the glob and its text matches the
# regex. The term stays in force everywhere else, which is the point: a name
# that belongs in LICENSE must still be caught in a source file.
#
# The default scope is what git would publish: tracked files, plus files that
# are untracked and not ignored, because those are one 'git add' away from
# being published too. Build output, virtual environments and compiled
# bytecode are ignored by git but can still hold absolute paths from the
# machine that produced them, so --all scans the whole working tree, binaries
# included, before you package or archive a directory by hand.
#
# Commit metadata is published as surely as the files are, and it is not
# covered by either scope: --commits takes a revision range and reads the
# author, the committer address and the full message of every commit in it.
#
# Exit status: 0 when clean, 1 when something was found, 2 on usage error.

set -uo pipefail

usage() {
  sed -n '3,41p' "$0" | sed 's/^# \{0,1\}//; s/^#$//'
}

TARGET="."
PATTERNS_FILE="${SOCKPOST_LEAKCHECK_PATTERNS:-}"
SCAN_ALL=0
COMMIT_RANGE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --patterns)
      shift
      [ $# -gt 0 ] || { echo "error: --patterns needs a file" >&2; exit 2; }
      PATTERNS_FILE="$1"
      ;;
    --commits)
      shift
      [ $# -gt 0 ] || { echo "error: --commits needs a revision range" >&2; exit 2; }
      COMMIT_RANGE="$1"
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
# exceptions
# ---------------------------------------------------------------------------
# Three parallel arrays, one entry per exception: the path glob, the regex the
# offending text has to match, and which rules the exception covers ("ALL" or
# a comma separated list). Built in entries are explained where they are added.
ALLOW_GLOBS=()
ALLOW_RES=()
ALLOW_RULES=()

allow() {
  # allow <path glob> <regex> [rule[,rule...]]
  ALLOW_GLOBS[${#ALLOW_GLOBS[@]}]="$1"
  ALLOW_RES[${#ALLOW_RES[@]}]="$2"
  ALLOW_RULES[${#ALLOW_RULES[@]}]="${3:-ALL}"
}

# The author and the committer of a commit are public by construction: git
# copies them into every clone, so an address there is not a leak, it is how
# the commit is signed. Reporting it once per commit would bury the findings
# that matter. Only the contact rules stand down, and only on that one pseudo
# path: the deny list and every credential rule still apply to it.
allow 'commit:*:identity' '.' 'email,phone'

allowed() {
  # allowed <pseudo path> <rule> <text>
  local path="$1" rule="$2" text="$3" index=0 glob regex rules
  while [ "$index" -lt "${#ALLOW_GLOBS[@]}" ]; do
    glob="${ALLOW_GLOBS[$index]}"
    regex="${ALLOW_RES[$index]}"
    rules="${ALLOW_RULES[$index]}"
    index=$((index + 1))
    case "$path" in
      $glob) ;;
      *) continue ;;
    esac
    if [ "$rules" != "ALL" ]; then
      case ",$rules," in
        *",$rule,"*) ;;
        *) continue ;;
      esac
    fi
    printf '%s' "$text" | LC_ALL=C grep -qiE -e "$regex" && return 0
  done
  return 1
}

# ---------------------------------------------------------------------------
# file list
# ---------------------------------------------------------------------------
everything() {
  find . -type f -not -path './.git/*' | sed 's|^\./||'
}

SCOPE="tracked files"
FILES=""
UNTRACKED_COUNT=0
if [ -n "$COMMIT_RANGE" ]; then
  SCOPE="commits in $COMMIT_RANGE"
elif [ "$SCAN_ALL" -eq 1 ]; then
  SCOPE="every file in the working tree"
  FILES=$(everything)
else
  FILES=$(git ls-files 2>/dev/null)
  if [ -z "$FILES" ] && ! git rev-parse --git-dir >/dev/null 2>&1; then
    SCOPE="every file in the working tree (not a git repository)"
    FILES=$(everything)
  else
    # Untracked and not ignored: one 'git add' away from being published, so
    # they are inside the default scope rather than a footnote under it.
    UNTRACKED=$(git ls-files --others --exclude-standard 2>/dev/null)
    if [ -n "$UNTRACKED" ]; then
      UNTRACKED_COUNT=$(printf '%s\n' "$UNTRACKED" | grep -c .)
      SCOPE="tracked files and $UNTRACKED_COUNT untracked, unignored file(s)"
      FILES=$(printf '%s\n%s' "$FILES" "$UNTRACKED" | grep -v '^$')
    fi
  fi
fi

if [ -z "$FILES" ] && [ -z "$COMMIT_RANGE" ]; then
  echo "leak-check: no files to scan" >&2
  exit 2
fi

# Binary files are skipped in the default scope and read in --all, where the
# point is precisely to catch a path baked into compiled output.
BINARY_FLAG="-I"
[ "$SCAN_ALL" -eq 1 ] && BINARY_FLAG="-a"

FINDINGS=0

# How many colon separated fields make up the location at the head of a hit:
# one for a file path, three for the "commit:<sha>:<part>" pseudo path.
PATH_FIELDS=1
[ -n "$COMMIT_RANGE" ] && PATH_FIELDS=3

report() {
  # report <rule> <grep output>
  local rule="$1"
  local hits="$2"
  [ -n "$hits" ] || return 0
  local line path text
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    path=$(printf '%s' "$line" | cut -d: -f"1-$PATH_FIELDS")
    # The exception regex is matched against the offending text alone, without
    # the "file:line:" prefix, so an anchored pattern means what it looks like.
    text=$(printf '%s' "$line" | cut -d: -f"$((PATH_FIELDS + 2))-")
    if allowed "$path" "$rule" "$text"; then
      continue
    fi
    printf 'FINDING rule=%s %s\n' "$rule" "$line"
    FINDINGS=$((FINDINGS + 1))
  done <<EOF
$hits
EOF
}

scan() {
  # scan <rule> <extended regex> [extra grep flags] [binary flag override]
  local rule="$1"
  local pattern="$2"
  local extra="${3:-}"
  local binary="${4:-$BINARY_FLAG}"
  local hits
  if [ -n "$COMMIT_RANGE" ]; then
    hits=$(printf '%s\n' "$COMMIT_TEXT" \
      | LC_ALL=C grep -n -E $extra -e "$pattern" 2>/dev/null \
      | LC_ALL=C cut -c1-200 \
      | while IFS= read -r hit; do
          # The record number is meaningless to a reader; the pseudo path
          # already carries the commit and the part of it that matched.
          printf '%s\n' "${hit#*:}"
        done)
  else
    hits=$(printf '%s\n' "$FILES" | tr '\n' '\0' \
      | LC_ALL=C xargs -0 grep -n -H "$binary" -E $extra -e "$pattern" 2>/dev/null \
      | LC_ALL=C cut -c1-200)
  fi
  report "$rule" "$hits"
}

# ---------------------------------------------------------------------------
# commit text
# ---------------------------------------------------------------------------
# One line per record, prefixed with a pseudo path so a finding names the
# commit it came from: "commit:<short sha>:identity" for the author and the
# committer, "commit:<short sha>:message" for every line of the message.
COMMIT_TEXT=""
COMMIT_COUNT=0
if [ -n "$COMMIT_RANGE" ]; then
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "error: --commits needs a git repository" >&2
    exit 2
  fi
  if ! git rev-list "$COMMIT_RANGE" >/dev/null 2>&1; then
    echo "error: cannot resolve revision range $COMMIT_RANGE" >&2
    exit 2
  fi
  COMMIT_COUNT=$(git rev-list --count "$COMMIT_RANGE")
  COMMIT_TEXT=$(
    for sha in $(git rev-list "$COMMIT_RANGE"); do
      short=$(git rev-parse --short "$sha")
      git log -1 --format='%an%n%ae%n%cn%n%ce' "$sha" \
        | sed "s|^|commit:$short:identity:|"
      git log -1 --format='%B' "$sha" \
        | sed "s|^|commit:$short:message:|"
    done)
fi

# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

# The sources are written in ASCII. This catches emoji, smart quotes and text
# pasted in from an editor or a document with a different character set. It
# runs in every scope; in --all it keeps skipping binaries, where a byte above
# 127 is content rather than a mistake.
scan non-ascii '[^[:print:][:space:]]' '' '-I'

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

# Optional project specific patterns and their exceptions.
if [ -n "$PATTERNS_FILE" ]; then
  if [ ! -r "$PATTERNS_FILE" ]; then
    echo "error: cannot read patterns file $PATTERNS_FILE" >&2
    exit 2
  fi
  # Exceptions are collected first: a rule cannot be excused by a line the
  # scanner has not read yet.
  while IFS= read -r raw; do
    case "$raw" in
      allow\ *)
        rest="${raw#allow }"
        glob="${rest%% *}"
        regex="${rest#* }"
        if [ -z "$glob" ] || [ "$glob" = "$regex" ]; then
          echo "error: malformed allow line: $raw" >&2
          exit 2
        fi
        allow "$glob" "$regex"
        ;;
    esac
  done < "$PATTERNS_FILE"
  while IFS= read -r raw; do
    case "$raw" in
      ''|\#*|allow\ *) continue ;;
    esac
    scan project "$raw" "-i"
  done < "$PATTERNS_FILE"
fi

# ---------------------------------------------------------------------------
# verdict
# ---------------------------------------------------------------------------
if [ -n "$COMMIT_RANGE" ]; then
  COUNT="$COMMIT_COUNT"
  UNIT="commits"
else
  COUNT=$(printf '%s\n' "$FILES" | grep -c .)
  UNIT="files"
fi

if [ "$FINDINGS" -eq 0 ]; then
  echo "leak-check: clean (scope=$SCOPE $UNIT=$COUNT findings=0)"
  exit 0
fi
echo "leak-check: FAILED (scope=$SCOPE $UNIT=$COUNT findings=$FINDINGS)" >&2
exit 1
