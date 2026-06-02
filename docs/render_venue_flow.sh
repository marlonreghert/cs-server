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

# Wide left-to-right charts: force a large canvas so glyphs are crisp.
render 1_cs_server_ingest     -w 5000 --scale 2
render 2_cs_server_enrichment -w 5000 --scale 2
# Tall top-down pipeline: width is not the constraint, push DPI instead.
render 3_vibes_bot_pipeline   -w 1600 --scale 3

echo "done — output in docs/"
