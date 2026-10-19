#!/usr/bin/env bash
# Clone the corpus of corpus/repos.tsv into $CORPUS_DIR, blobless and sparse so
# only the reviewed paths land on disk.
#
#   corpus/fetch.sh [name ...]      # all repos, or just the named ones
#
# Already-present repos at the pinned ref are left alone, so this is cheap to
# run on every container start. Writes $CORPUS_DIR/<name>.sha for the runner to
# record in the result metadata.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CORPUS_DIR="${CORPUS_DIR:-/corpus}"
mkdir -p "$CORPUS_DIR"
want=("$@")

wanted() {
  [ "${#want[@]}" -eq 0 ] && return 0
  local n
  for n in "${want[@]}"; do [ "$n" = "$1" ] && return 0; done
  return 1
}

while IFS=$'\t' read -r name url ref paths; do
  case "${name:-}" in ''|'#'*) continue ;; esac
  wanted "$name" || continue
  dir="$CORPUS_DIR/$name"

  if [ -f "$dir/.sha" ] && [ "$(cat "$dir/.sha")" = "$ref" ]; then
    echo "corpus: $name already at ${ref:0:12}"
    continue
  fi

  echo "corpus: fetching $name ${ref:0:12} ($paths)"
  rm -rf "$dir"
  mkdir -p "$dir"
  git -C "$dir" init -q
  git -C "$dir" remote add origin "$url"
  git -C "$dir" config extensions.partialClone origin
  git -C "$dir" sparse-checkout init --cone
  # shellcheck disable=SC2086
  git -C "$dir" sparse-checkout set $paths

  # GitHub serves arbitrary reachable SHAs; fall back to the default branch.
  if git -C "$dir" fetch -q --depth 1 --filter=blob:none origin "$ref" 2>/dev/null; then
    git -C "$dir" checkout -q FETCH_HEAD
  else
    echo "corpus: $name has no such ref on the server, taking HEAD instead" >&2
    git -C "$dir" fetch -q --depth 1 --filter=blob:none origin HEAD
    git -C "$dir" checkout -q FETCH_HEAD
  fi

  git -C "$dir" rev-parse HEAD > "$dir/.sha"
  echo "corpus: $name at $(cat "$dir/.sha")"
done < "$HERE/repos.tsv"
