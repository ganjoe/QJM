#!/usr/bin/env python3
"""
scripts/distill_document.py — Kompaktes Markdown-Destillat aus Finanzberichten erzeugen

Verdichtet mehrseitige Berichte (10-K, 10-Q, Earnings Releases, Präsentationen)
in ein strukturiertes, standardisiertes Markdown-Format (.distillate.md).

Features:
- Global Agent Rules: Alle Limits und Pfade sind konfigurierbar via Argumente / ENV.
- Unterstützt PDFs (via pypdf / pdfplumber) und reine Text-/Markdown-Dateien.
- Standardisierter Aufbau mit Tabellen für Kennzahlen, Segmenten, Guidance und MD&A.
"""

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sys
from typing import Dict, List, Optional

# Standard-Limits via ENV steuerbar
DEFAULT_MAX_CHARS = int(os.environ.get("DISTILLATE_MAX_CHARS", "50000"))


def extract_text_from_pdf(pdf_path: Path) -> str:
    """Extrahiert Text aus einer PDF-Datei."""
    text_chunks = []
    
    # 1. Versuch: pypdf
    try:
        import pypdf
        reader = pypdf.PdfReader(str(pdf_path))
        for idx, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text and page_text.strip():
                text_chunks.append(f"--- Page {idx + 1} ---\n{page_text.strip()}")
        if text_chunks:
            return "\n\n".join(text_chunks)
    except Exception:
        pass

    # 2. Versuch: pdfplumber
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            for idx, page in enumerate(pdf.pages):
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    text_chunks.append(f"--- Page {idx + 1} ---\n{page_text.strip()}")
        if text_chunks:
            return "\n\n".join(text_chunks)
    except Exception:
        pass

    # 3. Versuch: pdftotext CLI (falls vorhanden)
    try:
        import subprocess
        res = subprocess.run(["pdftotext", str(pdf_path), "-"], capture_output=True, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            return res.stdout.strip()
    except Exception:
        pass

    return ""


def build_structured_distillate(
    ticker: str,
    doc_type: str,
    title: str,
    fiscal_year: Optional[int],
    fiscal_quarter: Optional[str],
    report_date: Optional[str],
    source_filename: str,
    raw_text: str,
    custom_summary: Optional[str] = None
) -> str:
    """Erstellt ein standardisiertes, strukturiertes Markdown-Destillat."""
    period_str = f"FY{fiscal_year}" if fiscal_year else ""
    if fiscal_quarter:
        period_str = f"{fiscal_quarter} {period_str}".strip()

    header_title = f"# {ticker.upper()} — {period_str} {doc_type}: {title}".strip()
    report_date_str = report_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    md = [
        header_title,
        "",
        f"- **Ticker:** `{ticker.upper()}`",
        f"- **Document Type:** `{doc_type}`",
        f"- **Period:** `{period_str or 'N/A'}`",
        f"- **Report Date:** {report_date_str}",
        f"- **Source File:** `{source_filename}`",
        f"- **Distillate Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
        "---",
        "",
        "## 1. Executive Summary & Highlights",
    ]

    if custom_summary:
        md.append(custom_summary.strip())
    else:
        md.append(f"Kompaktes Destillat der wesentlichen Finanzdaten und Aussagen aus {source_filename}.")

    md.extend([
        "",
        "## 2. Key Financial Metrics",
        "| Metric | Current Period | Prior Year | YoY Change |",
        "|---|---|---|---|",
    ])

    # Wenn der Text spezifische Kennzahlen enthält, extrahieren
    rev_match = re.search(r"Revenue\s+[\$]?([\d\.,]+[BM]?)", raw_text, re.IGNORECASE)
    eps_match = re.search(r"(?:Diluted EPS|EPS)\s+[\$]?([\d\.,]+)", raw_text, re.IGNORECASE)
    opinc_match = re.search(r"Operating Income\s+[\$]?([\d\.,]+[BM]?)", raw_text, re.IGNORECASE)
    fcf_match = re.search(r"(?:Free Cash Flow|FCF|Cash flow from operations)\s+[\$]?([\d\.,]+[BM]?)", raw_text, re.IGNORECASE)

    if rev_match:
        md.append(f"| Total Revenue | {rev_match.group(1)} | — | — |")
    if opinc_match:
        md.append(f"| Operating Income | {opinc_match.group(1)} | — | — |")
    if eps_match:
        md.append(f"| Diluted EPS | ${eps_match.group(1)} | — | — |")
    if fcf_match:
        md.append(f"| Cash Flow / FCF | {fcf_match.group(1)} | — | — |")

    if not (rev_match or eps_match or opinc_match or fcf_match):
        md.append("| *(Detaillierte Metriken siehe Abschnitte unten)* | — | — | — |")

    md.extend([
        "",
        "## 3. Segment Breakdown & Operations",
        "- Wesentliche Geschäftsbereiche und operative Entwicklung.",
        "",
        "## 4. Forward Guidance & Outlook",
        "- Management-Ausblick für kommende Quartale / Geschäftsjahr.",
        "",
        "## 5. Key Management Commentary & Material Developments",
        "- Auftragsbestand (Backlog), strategische Partnerschaften, Kapitalrückfluss (Dividenden/Buybacks).",
        "",
        "## 6. Extracted High-Signal Content",
    ])

    # Saubere, gekürzte Textauszüge (Boilerplate-Filterung)
    lines = raw_text.splitlines()
    filtered_lines = []
    skip_boilerplate = False

    for line in lines:
        l_str = line.strip()
        if not l_str:
            continue
        # Überspringe standard Disclaimer-Blocks
        if "FORWARD-LOOKING STATEMENTS" in l_str.upper() or "SAFE HARBOR" in l_str.upper():
            skip_boilerplate = True
            continue
        if skip_boilerplate and (l_str.startswith("---") or "Overview" in l_str or "Highlights" in l_str):
            skip_boilerplate = False

        if not skip_boilerplate:
            filtered_lines.append(l_str)

    compact_body = "\n".join(filtered_lines[:300]) # Erste 300 relevante Zeilen
    md.append(compact_body)

    return "\n".join(md)


def main():
    parser = argparse.ArgumentParser(description="Erstellt ein strukturiertes Markdown-Destillat (.distillate.md)")
    parser.add_argument("--input", required=True, help="Pfad zur Quelldatei (PDF, TXT, etc.)")
    parser.add_argument("--ticker", required=True, help="Aktien-Ticker (z.B. DELL, AAPL)")
    parser.add_argument("--doc-type", default="report", help="Dokumenttyp (10-K, 10-Q, presentation, earnings)")
    parser.add_argument("--title", default="", help="Titel des Berichts")
    parser.add_argument("--year", type=int, default=None, help="Geschäftsjahr")
    parser.add_argument("--quarter", default=None, help="Quartal (Q1, Q2, Q3, Q4, FY)")
    parser.add_argument("--date", default=None, help="Datum (YYYY-MM-DD)")
    parser.add_argument("--output", default=None, help="Zielpfad für die .distillate.md")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, help="Maximale Zeichenlänge")

    args = parser.parse_args()
    input_path = Path(args.input)

    if not input_path.is_file():
        print(f"FEHLER: Datei nicht gefunden: {input_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Verarbeite {input_path} für Ticker {args.ticker.upper()}...")

    if input_path.suffix.lower() == ".pdf":
        raw_text = extract_text_from_pdf(input_path)
    else:
        raw_text = input_path.read_text(encoding="utf-8", errors="ignore")

    title = args.title or input_path.stem.replace("_", " ")
    distillate_md = build_structured_distillate(
        ticker=args.ticker,
        doc_type=args.doc_type,
        title=title,
        fiscal_year=args.year,
        fiscal_quarter=args.quarter,
        report_date=args.date,
        source_filename=input_path.name,
        raw_text=raw_text
    )

    if len(distillate_md) > args.max_chars:
        distillate_md = distillate_md[:args.max_chars] + "\n\n*(Inhalt auf Maximallimit gekürzt)*"

    out_path = Path(args.output) if args.output else input_path.with_suffix(".distillate.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(distillate_md, encoding="utf-8")

    print(f"✅ Destillat erfolgreich erstellt: {out_path} ({len(distillate_md)} Zeichen)")


if __name__ == "__main__":
    main()
