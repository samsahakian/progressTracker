#!/usr/bin/env bash
# Keeps rotating OAuth refresh tokens between workflow runs, encrypted, on the c2-state branch.
# The key is C2_CLIENT_SECRET (a repo secret), so the files are useless without it.
#
#   tools/token_state.sh load [NAME]        print the saved refresh token (NAME: c2 or strava; default c2)
#   tools/token_state.sh save FILE [NAME]   encrypt FILE and store it, keeping the other tokens
set -euo pipefail
BRANCH=c2-state
crypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -a -A -pass env:C2_CLIENT_SECRET "$@"; }
fname() { if [ "${1:-c2}" = c2 ]; then echo refresh_token.enc; else echo "$1_refresh_token.enc"; fi; }

case "${1:-}" in
  load)
    f=$(fname "${2:-c2}")
    if ! git fetch -q --depth 1 origin "$BRANCH" 2>/dev/null || ! git cat-file -e "FETCH_HEAD:$f" 2>/dev/null; then
      echo "No saved ${2:-c2} token yet. Run its Connect workflow first." >&2
      exit 1
    fi
    git show "FETCH_HEAD:$f" | crypt -d
    ;;
  save)
    f=$(fname "${3:-c2}")
    blob=$(crypt -salt < "$2" | git hash-object -w --stdin)
    others=""
    if git fetch -q --depth 1 origin "$BRANCH" 2>/dev/null; then
      others=$(git ls-tree FETCH_HEAD | awk -F'\t' -v f="$f" '$2 != f' || true)
    fi
    tree=$( { [ -n "$others" ] && printf '%s\n' "$others"; printf '100644 blob %s\t%s\n' "$blob" "$f"; } | git mktree)
    commit=$(GIT_AUTHOR_NAME=github-actions GIT_AUTHOR_EMAIL=github-actions@users.noreply.github.com \
             GIT_COMMITTER_NAME=github-actions GIT_COMMITTER_EMAIL=github-actions@users.noreply.github.com \
             git commit-tree "$tree" -m "Update encrypted tokens")
    git push -q -f origin "$commit:refs/heads/$BRANCH"
    ;;
  *) echo "usage: $0 load [NAME] | save FILE [NAME]" >&2; exit 2 ;;
esac
