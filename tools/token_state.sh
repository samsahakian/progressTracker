#!/usr/bin/env bash
# Keeps Concept2's refresh token between workflow runs, encrypted, on the c2-state branch.
# The key is C2_CLIENT_SECRET (a repo secret), so the file is useless without it.
#
#   tools/token_state.sh load        print the saved refresh token
#   tools/token_state.sh save FILE   encrypt FILE and replace what's on the branch
set -euo pipefail
BRANCH=c2-state
crypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -a -A -pass env:C2_CLIENT_SECRET "$@"; }

case "${1:-}" in
  load)
    if ! git fetch -q --depth 1 origin "$BRANCH" 2>/dev/null; then
      echo "No saved Concept2 token yet. Run the 'Connect Concept2' workflow first." >&2
      exit 1
    fi
    git show FETCH_HEAD:refresh_token.enc | crypt -d
    ;;
  save)
    blob=$(crypt -salt < "$2" | git hash-object -w --stdin)
    tree=$(printf '100644 blob %s\trefresh_token.enc\n' "$blob" | git mktree)
    commit=$(GIT_AUTHOR_NAME=github-actions GIT_AUTHOR_EMAIL=github-actions@users.noreply.github.com \
             GIT_COMMITTER_NAME=github-actions GIT_COMMITTER_EMAIL=github-actions@users.noreply.github.com \
             git commit-tree "$tree" -m "Update encrypted Concept2 token")
    git push -q -f origin "$commit:refs/heads/$BRANCH"
    ;;
  *) echo "usage: $0 load | save FILE" >&2; exit 2 ;;
esac
