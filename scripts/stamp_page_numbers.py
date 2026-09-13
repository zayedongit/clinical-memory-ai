#!/usr/bin/env python3
"""Stamp "<title> · Page N of M" onto the footer of a rendered PDF.

    python3 scripts/stamp_page_numbers.py in.pdf "Interview Preparation"

Chrome's headless PDF printer offers only two footer options from the command
line: none, or a built-in one that prints the source `file:///...` URL and a
timestamp on every page. Neither is right for a document you hand to someone.
The DevTools protocol allows a custom footer template, but driving it needs a
WebSocket client, which is a lot of machinery for a footer.

So: render the content once, render a second PDF that is nothing but correctly
positioned footers, and merge the two page by page. The overlay is produced by
the same Chrome and the same page geometry, so the footers land in the right
place by construction.

Edits the file in place.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfReader, PdfWriter

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
    "chromium-browser",
)

# Geometry, and why it deliberately differs from docs/pdf.css.
#
# The content is rendered with a 26mm bottom margin, so its text stops 26mm
# above the page edge. The overlay uses a *smaller* bottom margin, so its
# content box extends further down — into that gap. The footer sits at the
# bottom of the overlay box and therefore lands in the content's margin,
# below the text rather than on top of it.
#
#   content text ends at   297 - 26 = 271mm from the page top
#   overlay box ends at    297 - 14 = 283mm
#   footer therefore sits between 271mm and 283mm: clear of the text.
#
# PAGE_HEIGHT must stay under the overlay's 263mm box or the footer spills
# onto an extra page — which the caller checks for and refuses.
PAGE_CSS = "size: A4; margin: 20mm 18mm 14mm 18mm;"
PAGE_HEIGHT = "260mm"

OVERLAY_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><style>
  @page {{ {page_css} }}
  html, body {{ margin: 0; padding: 0; }}
  .page {{
    /* One overlay page per content page. The final page must not emit a
       trailing break, or the overlay ends up one page longer than the
       content and the merge silently misaligns — which the caller checks for.
       The height is deliberately a little under the 259mm content box (A4
       height less the vertical margins): the footer is positioned inside the
       box, so anything taller overflows into an extra page. */
    height: {page_height};
    position: relative;
    break-after: page;
    page-break-after: always;
  }}
  .page:last-child {{ break-after: auto; page-break-after: auto; }}
  .footer {{
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 8pt;
    color: #7a7a7a;
    border-top: 0.5px solid #d8d8d8;
    padding-top: 2mm;
    display: flex;
    justify-content: space-between;
  }}
</style></head><body>{pages}</body></html>
"""


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        path = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if path:
            return path
    raise SystemExit("Google Chrome or Chromium is required")


def build_overlay(count: int, title: str, out: Path) -> Path:
    pages = "".join(
        f'<div class="page"><div class="footer">'
        f"<span>{title}</span><span>Page {i} of {count}</span>"
        f"</div></div>"
        for i in range(1, count + 1)
    )
    html = out.with_suffix(".html")
    html.write_text(OVERLAY_TEMPLATE.format(page_css=PAGE_CSS, page_height=PAGE_HEIGHT, pages=pages), encoding="utf-8")

    subprocess.run(
        [find_chrome(), "--headless", "--disable-gpu", "--no-sandbox",
         "--no-pdf-header-footer", f"--print-to-pdf={out}", html.as_uri()],
        check=True, capture_output=True,
    )
    return out


def stamp(pdf_path: Path, title: str) -> int:
    reader = PdfReader(str(pdf_path))
    count = len(reader.pages)

    with tempfile.TemporaryDirectory() as tmp:
        overlay_pdf = build_overlay(count, title, Path(tmp) / "overlay.pdf")
        overlay = PdfReader(str(overlay_pdf))
        if len(overlay.pages) != count:
            raise SystemExit(
                f"overlay has {len(overlay.pages)} pages but the document has {count}; "
                "the page geometry in PAGE_CSS has drifted from docs/pdf.css"
            )

        writer = PdfWriter()
        for content_page, footer_page in zip(reader.pages, overlay.pages, strict=True):
            content_page.merge_page(footer_page)
            writer.add_page(content_page)

        # Write to a sibling first: a crash mid-write must not destroy the input.
        staged = pdf_path.with_suffix(".stamped.pdf")
        with staged.open("wb") as fh:
            writer.write(fh)
        staged.replace(pdf_path)

    return count


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    pages = stamp(Path(sys.argv[1]), sys.argv[2])
    print(f"stamped {pages} pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
