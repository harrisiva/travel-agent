#!/usr/bin/env bash
# Package each skill directory into dist/<name>.skill for upload to claude.ai.
#
# A .skill file is just a zip of the skill directory, so this is only a
# tidy-and-compress step — the directories at the repo root are the source of
# truth, and nothing here rewrites them.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root"

mkdir -p dist
rm -f dist/*.skill

for skill in */SKILL.md; do
    name="$(dirname "$skill")"
    zip -qr "dist/$name.skill" "$name" \
        -x "*__pycache__*" "*.pyc" "*.DS_Store" "*.cineplex_key"
    printf '  %-22s %s\n' "$name.skill" "$(du -h "dist/$name.skill" | cut -f1)"
done

echo "built $(ls dist/*.skill | wc -l | tr -d ' ') skill(s) into dist/"
