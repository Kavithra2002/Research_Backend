"""
testing.py
==========
Side-by-side viewer for the artefacts produced by ``Data_retrive.py``.

Each company folder under ``testing/<company>/`` is expected to contain:
    <company>_pages.json       (step-1 manifest)
    <company>_results.json     (OpenAI-extracted JSON)
    captures/<statement>/page_*.png  (rendered statement page images)

This script walks every such company and builds an HTML page that puts the
original captured PDF page(s) on the LEFT and the extracted financial table
(rendered as a real HTML table) on the RIGHT, so the two can be compared
visually one row at a time.

Output:
    testing/index.html                       <- list of all companies
    testing/<company>/<company>_view.html    <- detailed side-by-side view

Usage
-----
    # Build the HTML files
    python testing.py

    # Build + spin up a local web server so the images load reliably
    python testing.py --serve

    # Only rebuild a single company
    python testing.py --only company1

    # Use a different testing folder
    python testing.py --testing E:\\some\\where\\testing
"""

from __future__ import annotations

import argparse
import html
import http.server
import json
import socketserver
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Re-use every render helper from step-4 so the table formatting stays
# identical to the existing extracted-JSON viewer.
import step4_view_extracted_table as step4


# ─────────────────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR        = Path(__file__).resolve().parent
BACKEND_DIR       = SCRIPT_DIR.parent
DEFAULT_TESTING   = BACKEND_DIR / "testing"

STMT_ORDER  = step4.STMT_ORDER
STMT_LABELS = step4.STMT_LABELS
STMT_COLORS = step4.STMT_COLORS


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────

def discover_companies(testing_dir: Path) -> list[tuple[str, Path]]:
    """Return [(company_key, results_json_path), ...]."""
    if not testing_dir.exists():
        return []
    found: list[tuple[str, Path]] = []
    for child in sorted(testing_dir.iterdir()):
        if not child.is_dir():
            continue
        candidates = list(child.glob("*_results.json"))
        if not candidates:
            continue
        # Prefer <folder>_results.json if it exists, otherwise the first.
        named = child / f"{child.name}_results.json"
        chosen = named if named.exists() else candidates[0]
        found.append((child.name, chosen))
    return found


def find_capture_images(company_dir: Path, stmt: str) -> list[Path]:
    """Find captured page images for one statement.

    Supports both layouts:
        testing/<company>/captures/<stmt>/page_*.png  (new, from Data_retrive.py)
        testing/<company>/<stmt>/page_*.png           (fallback)
    """
    candidates = [
        company_dir / "captures" / stmt,
        company_dir / stmt,
    ]
    for folder in candidates:
        if folder.is_dir():
            imgs = sorted(folder.glob("page_*.png"))
            if imgs:
                return imgs
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Page builders
# ─────────────────────────────────────────────────────────────────────────────

EXTRA_CSS = """
/* testing.py-specific tweaks: highlight matching rows and stretch images */
header .meta a{color:var(--accent);text-decoration:none;}
header .meta a:hover{text-decoration:underline;}
.summary-bar{display:flex;gap:0.5rem;flex-wrap:wrap;margin:0.4rem 0 1rem;}
.summary-bar .pill{background:#161b27;border:1px solid #1f2535;border-radius:999px;
  padding:0.2rem 0.7rem;color:#cbd5e1;font-size:0.7rem;}
.summary-bar .pill.ok{border-color:#065f46;color:#34d399;}
.summary-bar .pill.warn{border-color:#92400e;color:#fbbf24;}
.summary-bar .pill.err{border-color:#7f1d1d;color:#f87171;}
.empty-stmt{color:var(--muted);font-style:italic;padding:1rem;text-align:center;}
.split > div{max-height:920px;}
"""


def build_company_view(company: str, company_dir: Path,
                       results: dict, view_html_path: Path) -> str:
    nav_links = "".join(
        f"<a href='#{k}'>{step4.esc(STMT_LABELS.get(k, k))}</a>"
        for k in STMT_ORDER if k in results
    )

    n_stmts = sum(1 for k in STMT_ORDER if k in results)
    n_imgs  = 0

    body = ""
    for key in STMT_ORDER:
        if key not in results:
            continue
        node   = results[key]
        title  = STMT_LABELS.get(key, key.replace("_", " ").title())
        color  = STMT_COLORS.get(key, "#374151")
        status = node.get("status", "")

        imgs = find_capture_images(company_dir, key)
        n_imgs += len(imgs)

        if imgs:
            img_html = "".join(
                f'<img src="{html.escape(step4._try_relative(view_html_path, ip))}" '
                f'alt="{step4.esc(ip.name)}"/>'
                f'<div class="pageno">{step4.esc(ip.name)}</div>'
                for ip in imgs
            )
        else:
            img_html = '<p class="muted">No captured page images found.</p>'

        try:
            rendered = step4.render_statement_body(key, node)
        except Exception as e:
            rendered = (f'<p class="err">Renderer crashed: {step4.esc(e)}</p>'
                        + step4.render_json_pre(node))

        body += f"""
        <section class="stmt" id="{key}">
          <div class="hd" style="background:{color};">
            <span>{step4.esc(title)}</span>
            <span class="badge">{step4.esc(status or '-')}</span>
          </div>
          <div class="split">
            <div>
              <h3>Original captured page(s) — {len(imgs)} image(s)</h3>
              {img_html}
            </div>
            <div>
              <h3>Extracted table</h3>
              <a class="toplink" href="#top">^ back to top</a>
              {rendered}
            </div>
          </div>
        </section>"""

    if not body:
        body = "<p class='empty-stmt'>No statements extracted for this company.</p>"

    summary_pills = (
        f"<span class='pill ok'>{n_stmts} statement(s)</span>"
        f"<span class='pill'>{n_imgs} captured image(s)</span>"
    )

    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'/>
<title>Testing view — {step4.esc(company)}</title>
<style>{step4.CSS}{EXTRA_CSS}</style></head><body>
<header id='top'>
  <h1>{step4.esc(company)} — Side-by-side comparison</h1>
  <div class='meta'>Generated: {datetime.now().isoformat(timespec='seconds')}
    &nbsp;|&nbsp; <a href='../index.html'>back to index</a>
    &nbsp;|&nbsp; folder: <code>{step4.esc(str(company_dir))}</code></div>
  <div class='summary-bar'>{summary_pills}</div>
</header>
<main>
  <div class='nav'>{nav_links}</div>
  {body}
</main>
</body></html>"""


def build_index(testing_dir: Path,
                rows_data: list[tuple[str, Path, dict, dict]]) -> str:
    rows = ""
    for company, view_path, results, meta in rows_data:
        rel    = step4._try_relative(testing_dir / "index.html", view_path)
        n_st   = sum(1 for k in STMT_ORDER if k in results)
        stmts  = ", ".join(k for k in STMT_ORDER if k in results)
        gen    = meta.get("generated_at", "")
        model  = meta.get("model", "")
        rows += (
            f"<tr>"
            f"<td><a href='{html.escape(rel)}'>{step4.esc(company)}</a></td>"
            f"<td class='num'>{n_st}</td>"
            f"<td class='lbl'>{step4.esc(stmts)}</td>"
            f"<td class='ref'>{step4.esc(model)}</td>"
            f"<td class='ref'>{step4.esc(gen)}</td>"
            f"</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='5' class='muted'>No companies found.</td></tr>"

    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'/>
<title>Testing — Side-by-side index</title>
<style>{step4.CSS}{EXTRA_CSS}</style></head><body>
<header>
  <h1>Testing — Side-by-side index</h1>
  <div class='meta'>Folder: {step4.esc(str(testing_dir))}
    &nbsp;|&nbsp; {len(rows_data)} compan{'y' if len(rows_data)==1 else 'ies'}
    &nbsp;|&nbsp; Generated {datetime.now().isoformat(timespec='seconds')}</div>
</header>
<main>
  <table class='fin'>
    <thead><tr>
      <th class='lbl'>Company</th>
      <th>#stmts</th>
      <th class='lbl'>Statements</th>
      <th class='lbl'>Model</th>
      <th class='lbl'>Generated</th>
    </tr></thead>
    <tbody>{rows}</tbody>
  </table>
</main>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────────────────────

def run(testing_dir: Path, only: str | None) -> Path:
    testing_dir.mkdir(parents=True, exist_ok=True)

    found = discover_companies(testing_dir)
    if only:
        found = [c for c in found if c[0] == only]
        if not found:
            print(f"\n  [error] no results found for company '{only}' in {testing_dir}")
            sys.exit(2)

    print(f"\n{'='*64}")
    print(f"  TESTING — build side-by-side viewer")
    print(f"  Folder    : {testing_dir}")
    print(f"  Companies : {len(found)}")
    print(f"{'='*64}\n")

    rows_data: list[tuple[str, Path, dict, dict]] = []

    for company, results_path in found:
        try:
            results = json.loads(results_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  [warn] {company}: cannot read results JSON ({e})")
            continue

        company_dir = results_path.parent
        view_path   = company_dir / f"{company}_view.html"

        try:
            html_str = build_company_view(
                company       = company,
                company_dir   = company_dir,
                results       = results,
                view_html_path = view_path,
            )
            view_path.write_text(html_str, encoding="utf-8")
        except Exception as e:
            print(f"  [warn] {company}: failed to build view ({e})")
            continue

        meta_path = company_dir / "extraction_meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        meta.setdefault("statements", sorted(results.keys()))

        rows_data.append((company, view_path, results, meta))
        print(f"  [ok]   {company:<24} -> {view_path.name}")

    index_path = testing_dir / "index.html"
    index_path.write_text(
        build_index(testing_dir, rows_data),
        encoding="utf-8",
    )
    print(f"\n  [save] Index page -> {index_path}\n")
    return index_path


def serve(serve_root: Path, port: int, open_path: str) -> None:
    serve_root = serve_root.resolve()
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(serve_root), **kw
    )
    with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{port}/{open_path}"
        print(f"  Serving {serve_root} on  http://127.0.0.1:{port}/")
        print(f"  Opening {url}\n  (Ctrl+C to stop)\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  Stopped.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Build the side-by-side viewer for everything under testing/."
    )
    ap.add_argument("--testing", type=Path, default=DEFAULT_TESTING,
                    help=f"Folder produced by Data_retrive.py  (default: {DEFAULT_TESTING})")
    ap.add_argument("--only",    default=None,
                    help="Only rebuild this single company folder name")
    ap.add_argument("--serve",   action="store_true",
                    help="Start a local web server after building and open the index")
    ap.add_argument("--port",    type=int, default=8001,
                    help="Port for --serve  (default: 8001)")
    args = ap.parse_args()

    testing_dir = args.testing.resolve()
    index_path  = run(testing_dir, args.only)

    if args.serve:
        try:
            rel_index = index_path.resolve().relative_to(testing_dir.resolve())
            url_path  = str(rel_index).replace("\\", "/")
        except Exception:
            url_path = "index.html"
        serve(testing_dir, args.port, url_path)
    else:
        print(f"  Open in browser : {index_path}")
        print(f"  Or run with --serve to start a local web server.\n")
