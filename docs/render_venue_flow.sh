#!/usr/bin/env bash
# Regenerate the venue Filters & Enrichments diagrams (PNG + SVG) from Mermaid.
#
# Companion to docs/venue_filters_and_enrichments.md. Uses mermaid-cli via npx
# (needs Node; no global install). Run from anywhere:
#     docs/render_venue_flow.sh
#
# Wide charts are rendered with an explicit large --width so the labels stay
# sharp (mermaid-cli otherwise caps width at 800px and the text turns blurry).
set -euo pipefail
cd "$(dirname "$0")"

BG="#0f172a"
MMDC="@mermaid-js/mermaid-cli@latest"

render() {
  local name="$1"; shift
  npx -y "$MMDC" -i "diagrams/${name}.mmd" -o "${name}.png" -b "$BG" "$@"
  npx -y "$MMDC" -i "diagrams/${name}.mmd" -o "${name}.svg" -b "$BG"
  echo "rendered ${name} (.png + .svg)"
}

# Macro overview: whole-system shape in one screen.
render 0_overview  -w 3600 --scale 2
# The big everything-graph: very wide, meant to be zoomed + scrolled.
render 1_full_flow -w 7000 --scale 2

echo "done — output in docs/"
