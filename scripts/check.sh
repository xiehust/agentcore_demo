#!/bin/sh
# Usage: ./scripts/check.sh

repo_root=$(CDPATH= cd "$(dirname "$0")/.." && pwd) || exit 1
status=0

for file in README.md CHANGELOG.md docs/FAQ.md; do
    if [ ! -f "$repo_root/$file" ]; then
        printf 'Missing: %s\n' "$file" >&2
        status=1
    fi
done

if [ "$status" -eq 0 ]; then
    printf 'all docs present\n'
fi

exit "$status"
