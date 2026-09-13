#!/usr/bin/env bash
# Render the documentation Markdown to PDF.
#
#   ./scripts/build_docs_pdf.sh            # all documents
#   ./scripts/build_docs_pdf.sh INTERVIEW_PREP
#
# Pipeline: pandoc (Markdown -> standalone HTML) then Chrome headless
# (HTML -> PDF). Chrome is used rather than a LaTeX engine because it needs no
# TeX distribution, renders the same CSS the docs were written against, and is
# already on any machine that runs the frontend.
#
# Output lands in docs/pdf/ and is committed, so the PDFs can be handed to
# someone without a toolchain. They are generated artefacts: regenerate them
# whenever the Markdown changes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOCS="$ROOT/docs"
OUT="$DOCS/pdf"
CSS="$DOCS/pdf.css"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[[ -x "$CHROME" ]] || CHROME="$(command -v google-chrome || command -v chromium || command -v chromium-browser || true)"

command -v pandoc >/dev/null || { echo "pandoc is required (brew install pandoc)" >&2; exit 1; }

# The flag that inlines the stylesheet was renamed in pandoc 3. Support both,
# so this works on whatever the machine has.
if pandoc --help 2>&1 | grep -q -- --embed-resources; then
  INLINE_FLAG=--embed-resources
else
  INLINE_FLAG=--self-contained
fi
[[ -n "$CHROME" && -x "$CHROME" ]] || { echo "Google Chrome or Chromium is required" >&2; exit 1; }

mkdir -p "$OUT"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# document-basename -> "Output Name|toc?"
render() {
  local src="$1" out="$2" toc="$3" footer="$4"
  local md="$DOCS/$src.md"
  [[ -f "$md" ]] || { echo "missing: $md" >&2; return 1; }

  local html="$TMP/$src.html"
  local toc_args=("--metadata=pagetitle:$out")
  [[ "$toc" == "toc" ]] && toc_args+=(--toc --toc-depth=2)

  # `raw_tex-` keeps pandoc from swallowing the \newpage markers; they are
  # rewritten below into a CSS page break, which is what Chrome understands.
  pandoc "$md" \
    --from=markdown+pipe_tables+fenced_code_blocks+yaml_metadata_block \
    --to=html5 --standalone "$INLINE_FLAG" \
    --css="$CSS" \
    --metadata=lang:en \
    "${toc_args[@]}" \
    --output="$html"

  # \newpage in the source becomes an explicit page break.
  python3 - "$html" <<'PY'
import re, sys
path = sys.argv[1]
html = open(path, encoding="utf-8").read()
html = re.sub(r"<p>\s*\\newpage\s*</p>",
              '<div style="break-before:page;page-break-before:always"></div>',
              html)
html = html.replace("\\newpage", "")
open(path, "w", encoding="utf-8").write(html)
PY

  "$CHROME" \
    --headless --disable-gpu --no-sandbox --no-pdf-header-footer \
    --print-to-pdf-no-header \
    --virtual-time-budget=10000 \
    --print-to-pdf="$OUT/$out.pdf" \
    "file://$html" 2>/dev/null

  # Chrome cannot produce a custom footer from the command line, so page
  # numbers are merged on afterwards from an overlay with the same geometry.
  ( cd "$ROOT/backend" && uv run python "$ROOT/scripts/stamp_page_numbers.py" \
      "$OUT/$out.pdf" "$footer" >/dev/null )

  local pages
  pages="$(python3 -c "
import re,sys
data=open(sys.argv[1],'rb').read()
print(max(len(re.findall(rb'/Type\s*/Page[^s]', data)), 1))
" "$OUT/$out.pdf")"
  printf '  %-46s %s pages  %s\n' "$out.pdf" "$pages" \
    "$(du -h "$OUT/$out.pdf" | cut -f1 | tr -d ' ')"
}

TARGET="${1:-all}"

echo "Rendering documentation to $OUT"
if [[ "$TARGET" == "all" || "$TARGET" == "INTERVIEW_PREP" ]]; then
  render INTERVIEW_PREP "Clinical-Memory-AI-Interview-Prep" notoc \
    "Clinical Memory AI · Interview Preparation"
fi
if [[ "$TARGET" == "all" || "$TARGET" == "CLINICAL_MEMORY_AI_PROJECT_GUIDE" ]]; then
  # No --toc: the guide already carries its own Contents list, which is what
  # renders on GitHub. Generating a second one produced two tables of contents.
  render CLINICAL_MEMORY_AI_PROJECT_GUIDE "Clinical-Memory-AI-Project-Guide" notoc \
    "Clinical Memory AI · Project Guide"
fi
if [[ "$TARGET" == "all" || "$TARGET" == "MODEL_CARD" ]]; then
  render MODEL_CARD "Clinical-Memory-AI-Model-Card" notoc \
    "Clinical Memory AI · Model Card"
fi
echo "Done."
