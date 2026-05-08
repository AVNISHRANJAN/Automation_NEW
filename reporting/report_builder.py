"""
reporting/report_builder.py — Generates the final HTML test report.

Produces a self-contained HTML file with:
  - Summary header (pages tested, actions, errors)
  - Per-page action table (colour-coded: green=pass, red=fail)
  - Inline screenshot thumbnails for errors
  - Dark mode, responsive design
"""

import logging
import html
from pathlib import Path
from datetime import datetime, timezone
from typing import List

import sys
import os
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import config

logger = logging.getLogger(__name__)


class ReportBuilder:
    """
    Builds a single self-contained HTML report from recorded test data.
    Call build() once after the crawl completes.
    """

    def __init__(self, run_id: str, target_url: str):
        self.run_id     = run_id
        self.target_url = target_url
        report_dir = config.OUTPUT_DIR / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = report_dir / f"{run_id}.html"

    def build(self, records: list, visited_pages: list, security_findings: list | None = None) -> str:
        """
        Generate the HTML report and write it to disk.
        Returns the file path as a string.
        """
        errors   = [r for r in records if not r.success]
        n_pass   = len(records) - len(errors)
        n_fail   = len(errors)
        now      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        security_findings = security_findings or []

        # ── Build rows ────────────────────────────────────────────────────────
        rows_html = ""
        for r in records:
            status_cls  = "pass" if r.success else "fail"
            status_icon = "✓" if r.success else "✗"
            screenshot  = ""
            if not r.success and r.screenshot_path:
                rel = html.escape(r.screenshot_path)
                screenshot = f'<br><a href="file://{rel}" target="_blank">📸 screenshot</a>'

            rows_html += f"""
            <tr class="{status_cls}">
                <td class="status-cell">{status_icon}</td>
                <td>{html.escape(r.url[:60])}</td>
                <td><code>{html.escape(r.element_type)}</code></td>
                <td>{html.escape(r.element_label[:60])}</td>
                <td>{html.escape(r.action[:80])}</td>
                <td>{html.escape(r.error_message[:120]) if r.error_message else "—"}{screenshot}</td>
            </tr>"""

        # ── Visited pages list ────────────────────────────────────────────────
        pages_html = "".join(
            f"<li><a href='{html.escape(p)}' target='_blank'>{html.escape(p)}</a></li>"
            for p in visited_pages
        )

        sec_rows = ""
        for f in security_findings:
            sev = html.escape(str(f.get("severity", "LOW")))
            conf = html.escape(str(f.get("confidence", "LOW")))
            sec_rows += f"""
            <tr>
                <td><code>{sev}</code></td>
                <td><code>{conf}</code></td>
                <td>{html.escape(str(f.get("route", ""))[:70])}</td>
                <td>{html.escape(str(f.get("title", ""))[:80])}</td>
                <td>{html.escape(str(f.get("evidence", ""))[:180])}</td>
                <td>{html.escape(str(f.get("reproduction_steps", ""))[:120])}</td>
                <td>{html.escape(str(f.get("remediation", ""))[:120])}</td>
            </tr>"""

        # ── Full HTML ─────────────────────────────────────────────────────────
        report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Web Auto Tester — {html.escape(self.run_id)}</title>
  <style>
    :root {{
      --bg:       #0f1117;
      --surface:  #1a1d27;
      --border:   #2e3148;
      --text:     #e2e8f0;
      --muted:    #94a3b8;
      --green:    #22c55e;
      --red:      #ef4444;
      --accent:   #6366f1;
      --code-bg:  #1e2235;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: 'Segoe UI', system-ui, sans-serif;
      font-size: 14px;
      line-height: 1.6;
      padding: 2rem;
    }}
    header {{
      margin-bottom: 2rem;
      border-bottom: 1px solid var(--border);
      padding-bottom: 1.5rem;
    }}
    header h1 {{
      font-size: 1.8rem;
      font-weight: 700;
      color: var(--accent);
      margin-bottom: 0.4rem;
    }}
    header .meta {{ color: var(--muted); font-size: 0.85rem; }}
    .summary {{
      display: flex;
      gap: 1.5rem;
      margin-bottom: 2rem;
      flex-wrap: wrap;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.2rem 1.8rem;
      min-width: 140px;
    }}
    .card .val {{
      font-size: 2rem;
      font-weight: 700;
      line-height: 1;
    }}
    .card .lbl {{ color: var(--muted); font-size: 0.8rem; margin-top: 4px; }}
    .card.green .val {{ color: var(--green); }}
    .card.red   .val {{ color: var(--red); }}
    .card.blue  .val {{ color: var(--accent); }}
    h2 {{
      font-size: 1.1rem;
      font-weight: 600;
      margin: 1.5rem 0 0.8rem;
      color: var(--text);
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--surface);
      border-radius: 12px;
      overflow: hidden;
      border: 1px solid var(--border);
    }}
    thead tr {{ background: #1f2337; }}
    th {{
      text-align: left;
      padding: 0.6rem 1rem;
      color: var(--muted);
      font-weight: 600;
      font-size: 0.8rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }}
    td {{
      padding: 0.55rem 1rem;
      border-top: 1px solid var(--border);
      vertical-align: top;
      word-break: break-word;
      max-width: 250px;
    }}
    tr.pass .status-cell {{ color: var(--green); font-weight: 700; }}
    tr.fail .status-cell {{ color: var(--red);   font-weight: 700; }}
    tr.fail {{ background: rgba(239,68,68,0.05); }}
    code {{
      background: var(--code-bg);
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.8rem;
    }}
    a {{ color: var(--accent); }}
    ul {{ padding-left: 1.2rem; color: var(--muted); font-size: 0.85rem; }}
    ul li {{ margin: 3px 0; }}
  </style>
</head>
<body>
  <header>
    <h1>🤖 Web Auto Tester Report</h1>
    <div class="meta">
      Run ID: <strong>{html.escape(self.run_id)}</strong> &nbsp;|&nbsp;
      Target: <strong><a href="{html.escape(self.target_url)}" target="_blank">{html.escape(self.target_url)}</a></strong> &nbsp;|&nbsp;
      Generated: {now}
    </div>
  </header>

  <div class="summary">
    <div class="card blue">
      <div class="val">{len(visited_pages)}</div>
      <div class="lbl">Pages Tested</div>
    </div>
    <div class="card blue">
      <div class="val">{len(records)}</div>
      <div class="lbl">Actions Logged</div>
    </div>
    <div class="card green">
      <div class="val">{n_pass}</div>
      <div class="lbl">Passed</div>
    </div>
    <div class="card red">
      <div class="val">{n_fail}</div>
      <div class="lbl">Failed</div>
    </div>
    <div class="card blue">
      <div class="val">{len(security_findings)}</div>
      <div class="lbl">Security Findings</div>
    </div>
  </div>

  <h2>📋 Action Log</h2>
  <table>
    <thead>
      <tr>
        <th style="width:40px"></th>
        <th>Page URL</th>
        <th>Type</th>
        <th>Element</th>
        <th>Action</th>
        <th>Error</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>

  <h2>🌐 Pages Visited ({len(visited_pages)})</h2>
  <ul>{pages_html}</ul>

  <h2>🛡️ Security Findings ({len(security_findings)})</h2>
  <table>
    <thead>
      <tr>
        <th>Severity</th>
        <th>Confidence</th>
        <th>Route</th>
        <th>Finding</th>
        <th>Evidence</th>
        <th>Repro</th>
        <th>Remediation</th>
      </tr>
    </thead>
    <tbody>
      {sec_rows if sec_rows else '<tr><td colspan=\"7\">No security findings detected.</td></tr>'}
    </tbody>
  </table>

</body>
</html>
"""
        try:
            self.report_path.write_text(report_html, encoding="utf-8")
            logger.info("Report written: %s", self.report_path)
        except Exception as exc:
            logger.error("Failed to write report: %s", exc)

        return str(self.report_path)
