#!/usr/bin/env bash
# Validate the data, then publish it to the live site (GitHub Pages) with one command.
set -euo pipefail
cd "$(dirname "$0")"
python3 pipeline/measure.py --check
git add docs
if git diff --cached --quiet; then echo "Nothing new to publish."; exit 0; fi
git commit -m "${1:-Update measurements}"
git push
echo "Pushed. The site updates in about a minute."
