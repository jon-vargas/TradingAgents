"""
PDF Report Generator - Professional-grade financial research reports.

Generates clean, digestible PDF reports from trading analysis results.
"""

import logging
import os
import re
import json
import html
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from pathlib import Path

logger = logging.getLogger("tradingagents.reporting.pdf_generator")

# Try to import optional dependencies
try:
    from jinja2 import Environment, FileSystemLoader, BaseLoader
    JINJA2_AVAILABLE = True
except ImportError:
    JINJA2_AVAILABLE = False

# WeasyPrint is optional - don't import at module level due to system library requirements
WEASYPRINT_AVAILABLE = False

def _check_weasyprint():
    """Lazily check if WeasyPrint is available."""
    global WEASYPRINT_AVAILABLE
    try:
        from weasyprint import HTML
        WEASYPRINT_AVAILABLE = True
        return True
    except (ImportError, OSError):
        WEASYPRINT_AVAILABLE = False
        return False


def clean_internal_language(text: str) -> str:
    """Remove internal analyst terminology from text for client-facing reports."""
    if not text:
        return ""
    
    # Replace internal analyst names with professional terms
    replacements = [
        (r'\bRisky Analyst\b', 'Aggressive Strategy'),
        (r'\bSafe Analyst\b', 'Conservative Strategy'),
        (r'\bNeutral Analyst\b', 'Balanced Strategy'),
        (r'\bBull Analyst\b', 'Bullish Perspective'),
        (r'\bBear Analyst\b', 'Bearish Perspective'),
        (r'\brisky analyst\b', 'aggressive strategy'),
        (r'\bsafe analyst\b', 'conservative strategy'),
        (r'\bneutral analyst\b', 'balanced strategy'),
        (r'\bbull analyst\b', 'bullish perspective'),
        (r'\bbear analyst\b', 'bearish perspective'),
        # Remove conversational phrases
        (r"Hey there!?\s*", ""),
        (r"Let's dive into", "Analysis of"),
        (r"Let's get into it\.?\s*", ""),
        (r"Alright,?\s*", ""),
        (r"We've got two sides here,?\s*", ""),
        (r"I'd like to engage with", "Considering"),
        (r"Thank you for the overview!?\s*", ""),
        (r"I appreciate your enthusiasm[^.]*\.\s*", ""),
    ]
    
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    
    return text


def markdown_to_html(text: str) -> str:
    """Convert markdown-style text to HTML with proper formatting."""
    if not text:
        return ""
    
    # Clean internal language first
    text = clean_internal_language(text)
    
    # Escape HTML entities first (but preserve markdown syntax)
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    
    # Convert markdown headers (handle #### before ### before ## before #)
    text = re.sub(r'^#{4,}\s*(.+)$', r'<h4>\1</h4>', text, flags=re.MULTILINE)
    text = re.sub(r'^###\s*(.+)$', r'<h4>\1</h4>', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s*(.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s*(.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    
    # Convert bold text (handle **text** and __text__)
    text = re.sub(r'\*\*([^*]+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__([^_]+?)__', r'<strong>\1</strong>', text)
    
    # Convert italic text (handle *text* and _text_ but not inside words)
    text = re.sub(r'(?<!\w)\*([^*\n]+?)\*(?!\w)', r'<em>\1</em>', text)
    text = re.sub(r'(?<!\w)_([^_\n]+?)_(?!\w)', r'<em>\1</em>', text)
    
    # Convert markdown tables to HTML tables
    text = convert_markdown_tables(text)
    
    # Process lines for lists
    lines = text.split('\n')
    in_ul = False
    in_ol = False
    result_lines = []
    
    for line in lines:
        stripped = line.strip()
        
        # Skip empty lines but close lists
        if not stripped:
            if in_ul:
                result_lines.append('</ul>')
                in_ul = False
            if in_ol:
                result_lines.append('</ol>')
                in_ol = False
            continue
        
        # Check for bullet points (-, •, *)
        bullet_match = re.match(r'^[-•*]\s+(.+)$', stripped)
        if bullet_match:
            if in_ol:
                result_lines.append('</ol>')
                in_ol = False
            if not in_ul:
                result_lines.append('<ul class="report-list">')
                in_ul = True
            result_lines.append(f'<li>{bullet_match.group(1)}</li>')
            continue
        
        # Check for numbered lists (1., 2., etc.)
        num_match = re.match(r'^(\d+)\.\s+(.+)$', stripped)
        if num_match:
            if in_ul:
                result_lines.append('</ul>')
                in_ul = False
            if not in_ol:
                result_lines.append('<ol class="report-list">')
                in_ol = True
            result_lines.append(f'<li>{num_match.group(2)}</li>')
            continue
        
        # Close any open lists for non-list content
        if in_ul:
            result_lines.append('</ul>')
            in_ul = False
        if in_ol:
            result_lines.append('</ol>')
            in_ol = False
        
        # Handle headers and other HTML (don't wrap in <p>)
        if stripped.startswith('<h') or stripped.startswith('<table') or stripped.startswith('<tr') or stripped.startswith('<div'):
            result_lines.append(stripped)
        elif stripped.startswith('</') or stripped.startswith('<ul') or stripped.startswith('<ol') or stripped.startswith('<li'):
            result_lines.append(stripped)
        else:
            # Wrap regular text in paragraphs
            result_lines.append(f'<p>{stripped}</p>')
    
    # Close any remaining open lists
    if in_ul:
        result_lines.append('</ul>')
    if in_ol:
        result_lines.append('</ol>')
    
    # Join and clean up
    result = '\n'.join(result_lines)
    result = re.sub(r'<p>\s*</p>', '', result)
    result = re.sub(r'\n{3,}', '\n\n', result)
    
    return result


def convert_markdown_tables(text: str) -> str:
    """Convert markdown tables to HTML tables."""
    lines = text.split('\n')
    result_lines = []
    in_table = False
    table_lines = []
    
    for line in lines:
        # Check if line looks like a table row (contains |)
        if '|' in line and line.strip().startswith('|'):
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(line)
        else:
            # If we were in a table, convert it
            if in_table:
                result_lines.append(convert_table_lines(table_lines))
                in_table = False
                table_lines = []
            result_lines.append(line)
    
    # Handle table at end of text
    if in_table and table_lines:
        result_lines.append(convert_table_lines(table_lines))
    
    return '\n'.join(result_lines)


def convert_table_lines(lines: list) -> str:
    """Convert a list of markdown table lines to HTML."""
    if not lines:
        return ""
    
    html = ['<table class="report-table" style="width:100%; border-collapse:collapse; margin:16px 0; font-size:9pt;">']
    
    for i, line in enumerate(lines):
        # Skip separator lines (|---|---|)
        if re.match(r'^\|[\s\-:]+\|$', line.strip()):
            continue
        
        # Parse cells
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        
        if i == 0:
            # Header row
            html.append('<thead><tr style="background:#f7fafc;">')
            for cell in cells:
                html.append(f'<th style="padding:10px 12px; border:1px solid #e2e8f0; text-align:left; font-weight:600;">{cell}</th>')
            html.append('</tr></thead><tbody>')
        else:
            # Data row
            html.append('<tr>')
            for cell in cells:
                html.append(f'<td style="padding:8px 12px; border:1px solid #e2e8f0;">{cell}</td>')
            html.append('</tr>')
    
    html.append('</tbody></table>')
    return '\n'.join(html)


def extract_key_points(text: str, max_points: int = 5) -> list:
    """Extract key bullet points from text."""
    if not text:
        return []
    
    points = []
    
    # Look for bullet points
    bullet_pattern = r'^[-•*]\s*(.+)$'
    matches = re.findall(bullet_pattern, text, re.MULTILINE)
    points.extend(matches[:max_points])
    
    # If no bullets found, try to extract sentences
    if not points:
        sentences = re.split(r'[.!?]+', text)
        for s in sentences[:max_points]:
            s = s.strip()
            if len(s) > 20 and len(s) < 200:
                points.append(s)
    
    return points[:max_points]


def extract_metrics_from_text(text: str) -> Dict[str, str]:
    """Extract numerical metrics from report text."""
    metrics = {}
    
    if not text:
        return metrics
    
    # Common patterns to look for
    patterns = {
        'Revenue Growth': r'revenue\s+growth[:\s]+([+-]?\d+\.?\d*%?)',
        'P/E Ratio': r'P/?E\s+(?:ratio)?[:\s]+(\d+\.?\d*)',
        'RSI': r'RSI[:\s]+(\d+\.?\d*)',
        'SMA': r'(?:50|200)\s*(?:day\s+)?SMA[:\s]+\$?(\d+\.?\d*)',
        'EPS': r'EPS[:\s]+\$?(\d+\.?\d*)',
        'Market Cap': r'market\s+cap[:\s]+\$?([\d.]+[BMT]?)',
    }
    
    text_lower = text.lower()
    for name, pattern in patterns.items():
        match = re.search(pattern, text_lower, re.IGNORECASE)
        if match:
            metrics[name] = match.group(1)
    
    return metrics


# HTML Template for the report - Professional Financial Research Style
REPORT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>{{ ticker }} Research Report - {{ analysis_date }}</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Source+Serif+4:wght@400;600;700&family=Inter:wght@400;500;600;700&display=swap');
        
        :root {
            --primary: #1a365d;
            --primary-light: #2c5282;
            --accent: #3182ce;
            --success: #276749;
            --success-bg: #f0fff4;
            --danger: #c53030;
            --danger-bg: #fff5f5;
            --warning: #b7791f;
            --warning-bg: #fffaf0;
            --text: #2d3748;
            --text-muted: #718096;
            --border: #e2e8f0;
            --bg-light: #f7fafc;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            font-size: 10pt;
            line-height: 1.6;
            color: var(--text);
            background: #fff;
        }
        
        .page {
            max-width: 850px;
            margin: 0 auto;
            padding: 40px 50px;
        }
        
        /* Header */
        .header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            padding-bottom: 24px;
            border-bottom: 3px solid var(--primary);
            margin-bottom: 32px;
        }
        
        .brand {
            display: flex;
            align-items: center;
            gap: 20px;
        }
        
        .ticker-badge {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
            color: white;
            padding: 16px 28px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(26, 54, 93, 0.3);
        }
        
        .ticker-symbol {
            font-size: 36pt;
            font-weight: 700;
            letter-spacing: 3px;
            font-family: 'Source Serif 4', Georgia, serif;
        }
        
        .report-type {
            font-size: 9pt;
            text-transform: uppercase;
            letter-spacing: 2px;
            opacity: 0.9;
            margin-top: 4px;
        }
        
        .meta {
            text-align: right;
            font-size: 9pt;
            color: var(--text-muted);
        }
        
        .meta-row {
            margin-bottom: 4px;
        }
        
        .meta-label {
            color: var(--text-muted);
        }
        
        .meta-value {
            font-weight: 600;
            color: var(--text);
        }
        
        /* Executive Summary */
        .executive-summary {
            display: flex;
            gap: 24px;
            padding: 24px;
            border-radius: 12px;
            margin-bottom: 32px;
        }
        
        .executive-summary.buy {
            background: linear-gradient(135deg, var(--success-bg) 0%, #c6f6d5 100%);
            border: 2px solid var(--success);
        }
        
        .executive-summary.sell {
            background: linear-gradient(135deg, var(--danger-bg) 0%, #fed7d7 100%);
            border: 2px solid var(--danger);
        }
        
        .executive-summary.hold {
            background: linear-gradient(135deg, var(--warning-bg) 0%, #feebc8 100%);
            border: 2px solid var(--warning);
        }
        
        .decision-chip {
            font-family: 'Source Serif 4', Georgia, serif;
            font-size: 18pt;
            font-weight: 700;
            padding: 12px 20px;
            border-radius: 8px;
            color: white;
            text-align: center;
            min-width: 140px;
            max-width: 210px;
            line-height: 1.2;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        }
        
        .decision-chip.buy { background: var(--success); }
        .decision-chip.sell { background: var(--danger); }
        .decision-chip.hold { background: var(--warning); }
        
        .summary-content {
            flex: 1;
        }
        
        .summary-title {
            font-size: 11pt;
            font-weight: 700;
            color: var(--primary);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        
        .summary-text {
            font-size: 11pt;
            line-height: 1.7;
            color: var(--text);
        }
        
        /* Metrics Dashboard */
        .metrics-dashboard {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 16px;
            margin-bottom: 32px;
        }
        
        .metric-card {
            background: var(--bg-light);
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 20px 16px;
            text-align: center;
        }
        
        .metric-value {
            font-family: 'Source Serif 4', Georgia, serif;
            font-size: 24pt;
            font-weight: 700;
            color: var(--primary);
            line-height: 1.2;
        }
        
        .metric-label {
            font-size: 8pt;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-top: 8px;
        }
        
        /* Sections */
        .section {
            margin-bottom: 28px;
            page-break-inside: avoid;
        }
        
        .section-header {
            display: flex;
            align-items: center;
            gap: 12px;
            padding-bottom: 12px;
            border-bottom: 2px solid var(--border);
            margin-bottom: 16px;
        }
        
        .section-icon {
            font-size: 20pt;
        }
        
        .section-title {
            font-family: 'Source Serif 4', Georgia, serif;
            font-size: 16pt;
            font-weight: 700;
            color: var(--primary);
            margin: 0;
        }
        
        .section-content {
            font-size: 10pt;
            line-height: 1.7;
            color: var(--text);
        }
        
        .section-content h2, 
        .section-content h3, 
        .section-content h4 {
            font-family: 'Source Serif 4', Georgia, serif;
            color: var(--primary);
            margin-top: 16px;
            margin-bottom: 8px;
        }
        
        .section-content h2 { font-size: 14pt; }
        .section-content h3 { font-size: 12pt; }
        .section-content h4 { font-size: 11pt; }
        
        .section-content p {
            margin-bottom: 12px;
        }
        
        .section-content strong {
            color: var(--primary);
        }
        
        .section-content ul.report-list {
            margin: 12px 0;
            padding-left: 20px;
        }
        
        .section-content ul.report-list li {
            margin-bottom: 8px;
            line-height: 1.5;
        }
        
        /* Key Points Box */
        .key-points {
            background: var(--bg-light);
            border-left: 4px solid var(--accent);
            padding: 16px 20px;
            margin: 16px 0;
            border-radius: 0 8px 8px 0;
        }
        
        .key-points-title {
            font-weight: 700;
            font-size: 10pt;
            color: var(--accent);
            margin-bottom: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .key-points ul {
            margin: 0;
            padding-left: 20px;
        }
        
        .key-points li {
            margin-bottom: 6px;
            font-size: 10pt;
        }
        
        /* Debate Grid */
        .debate-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin: 16px 0;
        }
        
        .debate-card {
            border-radius: 10px;
            padding: 20px;
        }
        
        .debate-card.bull {
            background: linear-gradient(135deg, #f0fff4 0%, #c6f6d5 100%);
            border: 1px solid #9ae6b4;
        }
        
        .debate-card.bear {
            background: linear-gradient(135deg, #fff5f5 0%, #fed7d7 100%);
            border: 1px solid #feb2b2;
        }
        
        .debate-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 14px;
        }
        
        .debate-icon {
            font-size: 24pt;
        }
        
        .debate-title {
            font-family: 'Source Serif 4', Georgia, serif;
            font-size: 14pt;
            font-weight: 700;
        }
        
        .debate-card.bull .debate-title { color: var(--success); }
        .debate-card.bear .debate-title { color: var(--danger); }
        
        .debate-content {
            font-size: 10pt;
            line-height: 1.6;
        }
        
        .debate-content ul {
            margin: 0;
            padding-left: 18px;
        }
        
        .debate-content li {
            margin-bottom: 6px;
        }
        
        /* Risk Assessment */
        .risk-matrix {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
            margin: 16px 0;
        }
        
        .risk-card {
            border-radius: 10px;
            padding: 18px;
            font-size: 9pt;
        }
        
        .risk-card.aggressive {
            background: linear-gradient(135deg, #fffaf0 0%, #feebc8 100%);
            border: 1px solid #f6ad55;
        }
        
        .risk-card.neutral {
            background: linear-gradient(135deg, #f0fff4 0%, #c6f6d5 100%);
            border: 1px solid #68d391;
        }
        
        .risk-card.conservative {
            background: linear-gradient(135deg, #ebf8ff 0%, #bee3f8 100%);
            border: 1px solid #63b3ed;
        }
        
        .risk-header {
            font-weight: 700;
            font-size: 10pt;
            margin-bottom: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .risk-card.aggressive .risk-header { color: #c05621; }
        .risk-card.neutral .risk-header { color: #276749; }
        .risk-card.conservative .risk-header { color: #2b6cb0; }
        
        /* Trading Plan */
        .trading-plan-box {
            background: var(--bg-light);
            border: 2px solid var(--border);
            border-radius: 12px;
            padding: 24px;
            margin: 16px 0;
        }
        
        .plan-grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
        }
        
        .plan-item {
            text-align: center;
        }
        
        .plan-label {
            font-size: 8pt;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 6px;
        }
        
        .plan-value {
            font-family: 'Source Serif 4', Georgia, serif;
            font-size: 16pt;
            font-weight: 700;
            color: var(--primary);
        }
        
        .plan-value.entry { color: var(--accent); }
        .plan-value.stop { color: var(--danger); }
        .plan-value.target { color: var(--success); }
        
        /* Footer */
        .footer {
            margin-top: 40px;
            padding-top: 24px;
            border-top: 2px solid var(--border);
            text-align: center;
        }
        
        .footer-main {
            font-size: 9pt;
            color: var(--text-muted);
            margin-bottom: 12px;
        }
        
        .disclaimer {
            font-size: 7pt;
            color: var(--text-muted);
            font-style: italic;
            max-width: 600px;
            margin: 0 auto;
            line-height: 1.5;
        }

        .decision-scorecard { margin: 0 0 28px 0; }
        .score-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 12px;
            margin-bottom: 14px;
        }
        .score-box {
            border-radius: 10px;
            padding: 14px 16px;
            border: 1px solid var(--border);
            background: #f8fafc;
        }
        .score-box.pass { border-color: var(--success); background: var(--success-bg); }
        .score-box.fail { border-color: var(--danger); background: var(--danger-bg); }
        .score-box.watch { border-color: var(--warning); background: var(--warning-bg); }
        .score-box-kicker { font-size: 8pt; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); }
        .score-box-status { font-size: 14pt; font-weight: 700; margin: 4px 0; }
        .score-box-why { font-size: 9pt; color: var(--text); }
        .score-note, .lockup-note { font-size: 9pt; margin: 8px 0; color: var(--text); }
        .gate-table { width: 100%; border-collapse: collapse; font-size: 9pt; margin: 12px 0 8px; }
        .gate-table caption { text-align: left; font-weight: 700; margin-bottom: 6px; }
        .gate-table th, .gate-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); text-align: left; }
        .gate-table tr.ok td:last-child { color: var(--success); font-weight: 700; }
        .gate-table tr.bad td:last-child { color: var(--danger); font-weight: 700; }
        .satellite-appendix { margin-top: 16px; padding: 12px 14px; border: 1px dashed var(--border); border-radius: 8px; background: #f8fafc; }
        .satellite-appendix h3 { margin: 0 0 6px; font-size: 11pt; }
        .satellite-appendix pre { white-space: pre-wrap; font-size: 8.5pt; margin: 8px 0 0; }
        
        /* Earnings Quality */
        .eq-grade {
            margin-bottom: 14px;
        }

        .grade-badge {
            display: inline-block;
            font-family: 'Source Serif 4', Georgia, serif;
            font-size: 20pt;
            font-weight: 700;
            padding: 8px 24px;
            border-radius: 8px;
            color: white;
        }

        .grade-badge.grade-a { background: var(--success); }
        .grade-badge.grade-b { background: #38a169; }
        .grade-badge.grade-c { background: var(--warning); }
        .grade-badge.grade-d { background: #dd6b20; }
        .grade-badge.grade-f { background: var(--danger); }

        .eq-metrics {
            width: 100%;
            border-collapse: collapse;
            margin-top: 12px;
            font-size: 10pt;
        }

        .eq-metrics td {
            padding: 10px 14px;
            border: 1px solid var(--border);
        }

        .eq-metrics tr:nth-child(even) {
            background: var(--bg-light);
        }

        .eq-metrics td:first-child {
            font-weight: 600;
            color: var(--primary);
            width: 55%;
        }

        /* Valuation / DCF */
        .dcf-result {
            margin-bottom: 20px;
        }

        .dcf-summary {
            display: flex;
            align-items: center;
            gap: 16px;
            margin: 12px 0;
        }

        .fair-value {
            font-family: 'Source Serif 4', Georgia, serif;
            font-size: 28pt;
            font-weight: 700;
            color: var(--primary);
        }

        .margin {
            font-size: 11pt;
            font-weight: 600;
            padding: 6px 14px;
            border-radius: 6px;
        }

        .margin.positive {
            color: var(--success);
            background: var(--success-bg);
        }

        .margin.negative {
            color: var(--danger);
            background: var(--danger-bg);
        }

        .dcf-assumptions {
            font-size: 9pt;
            color: var(--text-muted);
            margin-top: 8px;
        }

        .scenario-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 12px;
            font-size: 10pt;
        }

        .scenario-table th,
        .scenario-table td {
            padding: 10px 14px;
            border: 1px solid var(--border);
            text-align: center;
        }

        .scenario-table thead tr {
            background: var(--bg-light);
        }

        .scenario-table th {
            font-weight: 600;
            color: var(--primary);
        }

        .scenario-table tr.bull td:first-child { color: var(--success); font-weight: 600; }
        .scenario-table tr.base td:first-child { color: var(--warning); font-weight: 600; }
        .scenario-table tr.bear td:first-child { color: var(--danger); font-weight: 600; }

        /* Catalyst Pipeline */
        .catalyst-content {
            font-size: 10pt;
            line-height: 1.7;
            color: var(--text);
        }

        .catalyst-content p {
            margin-bottom: 10px;
        }

        /* Print Styles */
        @media print {
            body { font-size: 9pt; }
            .page { padding: 20px; max-width: 100%; }
            .section { page-break-inside: avoid; }
        }
    </style>
</head>
<body>
    <div class="page">
        <!-- Header -->
        <div class="header">
            <div class="brand">
                <div class="ticker-badge">
                    <div class="ticker-symbol">{{ ticker }}</div>
                    <div class="report-type">Research Report</div>
                </div>
            </div>
            <div class="meta">
                <div class="meta-row">
                    <span class="meta-label">Analysis Date:</span>
                    <span class="meta-value">{{ analysis_date }}</span>
                </div>
                <div class="meta-row">
                    <span class="meta-label">Generated:</span>
                    <span class="meta-value">{{ generated_at }}</span>
                </div>
                <div class="meta-row">
                    <span class="meta-label">Model:</span>
                    <span class="meta-value">{{ model_info }}</span>
                </div>
                {% if run_settings_html %}
                <div class="meta-row">
                    <span class="meta-label">Run:</span>
                    <span class="meta-value">{{ run_settings_html }}</span>
                </div>
                {% endif %}
            </div>
        </div>
        
        <!-- Executive Summary -->
        <div class="executive-summary {{ decision_class }}">
            <div class="decision-chip {{ decision_class }}">{{ decision_label }}</div>
            <div class="summary-content">
                <div class="summary-title">Executive Summary</div>
                <div class="summary-text">{{ executive_summary }}</div>
            </div>
        </div>
        {{ decision_scorecard_html }}
        {{ screening_summary_block }}
        
        <!-- Metrics Dashboard -->
        <div class="metrics-dashboard">
            <div class="metric-card">
                <div class="metric-value">{{ confidence }}%</div>
                <div class="metric-label">Confidence</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{{ data_quality_score }}%</div>
                <div class="metric-label">Data Quality</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{{ debate_rounds }}</div>
                <div class="metric-label">Debate Rounds</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{{ duration }}</div>
                <div class="metric-label">Analysis Time</div>
            </div>
            <div class="metric-card">
                <div class="metric-value">{{ data_sources }}</div>
                <div class="metric-label">Data Sources</div>
            </div>
        </div>

        {% if provenance_summary %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">🧾</span>
                <h2 class="section-title">Data Provenance</h2>
            </div>
            <div class="section-content">
                {{ provenance_summary_html | safe }}
                {% if provenance_details %}
                {{ provenance_details_html | safe }}
                {% endif %}
            </div>
        </div>
        {% endif %}

        {% if qa_warnings %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">⚠️</span>
                <h2 class="section-title">QA Warnings</h2>
            </div>
            <div class="section-content">
                {{ qa_warnings_html | safe }}
            </div>
        </div>
        {% endif %}

        {% if section_attribution %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">📌</span>
                <h2 class="section-title">Section Attribution</h2>
            </div>
            <div class="section-content">
                {{ section_attribution_html | safe }}
            </div>
        </div>
        {% endif %}
        
        <!-- Technical Analysis -->
        {% if market_report %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">📈</span>
                <h2 class="section-title">Technical Analysis</h2>
            </div>
            <div class="section-content">
                {{ market_report_html | safe }}
            </div>
        </div>
        {% endif %}
        
        <!-- Fundamental Analysis -->
        {% if fundamentals_report %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">💰</span>
                <h2 class="section-title">Fundamental Analysis</h2>
            </div>
            <div class="section-content">
                {{ fundamentals_report_html | safe }}
            </div>
        </div>
        {% endif %}
        
        <!-- News & Sentiment -->
        {% if news_report or sentiment_report %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">📰</span>
                <h2 class="section-title">News & Sentiment</h2>
            </div>
            <div class="section-content">
                {% if news_report %}
                {{ news_report_html | safe }}
                {% endif %}
                {% if sentiment_report %}
                <div style="margin-top: 20px;">
                    <h4>Social Sentiment</h4>
                    {{ sentiment_report_html | safe }}
                </div>
                {% endif %}
                {% if latest_headlines_summary %}
                <div style="margin-top: 20px;">
                    {{ latest_headlines_html | safe }}
                </div>
                {% endif %}
            </div>
        </div>
        {% endif %}

        {%- if sec_filings_summary or earnings_transcript_summary %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">📄</span>
                <h2 class="section-title">Filings &amp; Earnings Highlights</h2>
            </div>
            <div class="section-content">
                {% if sec_filings_summary %}
                {{ sec_filings_summary_html | safe }}
                {% endif %}
                {% if earnings_transcript_summary %}
                <div style="margin-top: 16px;">
                    {{ earnings_transcript_summary_html | safe }}
                </div>
                {% endif %}
            </div>
        </div>
        {%- endif %}

        {%- if kpi_deltas_summary or guidance_shifts_summary %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">📊</span>
                <h2 class="section-title">KPI &amp; Guidance Changes</h2>
            </div>
            <div class="section-content">
                {% if kpi_deltas_summary %}
                {{ kpi_deltas_html | safe }}
                {% endif %}
                {% if guidance_shifts_summary %}
                <div style="margin-top: 16px;">
                    {{ guidance_shifts_html | safe }}
                </div>
                {% endif %}
            </div>
        </div>
        {%- endif %}
        
        <!-- Investment Debate -->
        {% if bull_summary or bear_summary %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">⚖️</span>
                <h2 class="section-title">Investment Debate</h2>
            </div>
            <div class="debate-grid">
                <div class="debate-card bull">
                    <div class="debate-header">
                        <span class="debate-icon">🐂</span>
                        <span class="debate-title">Bull Case</span>
                    </div>
                    <div class="debate-content">
                        {{ bull_summary_html | safe }}
                    </div>
                </div>
                <div class="debate-card bear">
                    <div class="debate-header">
                        <span class="debate-icon">🐻</span>
                        <span class="debate-title">Bear Case</span>
                    </div>
                    <div class="debate-content">
                        {{ bear_summary_html | safe }}
                    </div>
                </div>
            </div>
        </div>
        {% endif %}
        
        <!-- Risk Assessment -->
        {% if risk_aggressive or risk_conservative or risk_neutral %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">🛡️</span>
                <h2 class="section-title">Risk Assessment</h2>
            </div>
            <div class="risk-matrix">
                {% if risk_aggressive %}
                <div class="risk-card aggressive">
                    <div class="risk-header">🔥 Aggressive</div>
                    <div class="risk-content">{{ risk_aggressive }}</div>
                </div>
                {% endif %}
                {% if risk_neutral %}
                <div class="risk-card neutral">
                    <div class="risk-header">⚖️ Balanced</div>
                    <div class="risk-content">{{ risk_neutral }}</div>
                </div>
                {% endif %}
                {% if risk_conservative %}
                <div class="risk-card conservative">
                    <div class="risk-header">🛡️ Conservative</div>
                    <div class="risk-content">{{ risk_conservative }}</div>
                </div>
                {% endif %}
            </div>
        </div>
        {% elif risk_assessment %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">🛡️</span>
                <h2 class="section-title">Risk Assessment</h2>
            </div>
            <div class="section-content">
                {{ risk_assessment_html | safe }}
            </div>
        </div>
        {% endif %}

        <!-- Earnings Quality Assessment -->
        {% if earnings_quality %}
        <div class="section earnings-quality">
            <div class="section-header">
                <span class="section-icon">📋</span>
                <h2 class="section-title">Earnings Quality Assessment</h2>
            </div>
            <div class="eq-grade">
                <span class="grade-badge grade-{{ earnings_quality_grade_lower }}">{{ earnings_quality_grade }}</span>
            </div>
            <table class="eq-metrics">
                <tr><td>Accruals Ratio</td><td>{{ earnings_quality_accruals }}</td></tr>
                <tr><td>Cash Conversion</td><td>{{ earnings_quality_cash_conversion }}</td></tr>
                <tr><td>Positive Operating Cash Flow</td><td>{{ earnings_quality_pos_ocf }}</td></tr>
                <tr><td>Positive Net Income</td><td>{{ earnings_quality_pos_ni }}</td></tr>
            </table>
        </div>
        {% endif %}

        <!-- Valuation Analysis -->
        {% if intrinsic_value or scenario_analysis %}
        <div class="section valuation">
            <div class="section-header">
                <span class="section-icon">💎</span>
                <h2 class="section-title">Valuation Analysis</h2>
            </div>
            <div class="section-content">
                {% if intrinsic_value %}
                <div class="dcf-result">
                    <h3>DCF Intrinsic Value</h3>
                    <div class="dcf-summary">
                        <span class="fair-value">${{ dcf_fair_value }}</span>
                        <span class="margin {{ dcf_margin_class }}">{{ dcf_margin_pct }}% {{ dcf_margin_label }}</span>
                    </div>
                    <div class="dcf-assumptions">
                        <p>FCF: ${{ dcf_fcf }} | Growth: {{ dcf_growth_rate }}% | WACC: {{ dcf_wacc }}% | Beta: {{ dcf_beta }}</p>
                    </div>
                    {% if dcf_implied_growth_rate and dcf_implied_growth_rate != 'N/A' %}
                    <div class="dcf-reverse">
                        <p><strong>Reverse DCF (implied by market price):</strong>
                           Growth priced in: {{ dcf_implied_growth_rate }}
                           ({{ dcf_implied_vs_consensus }} vs consensus)</p>
                    </div>
                    {% endif %}
                    {% if dcf_sensitivity_grid %}
                    <div class="dcf-sensitivity">
                        <h4>DCF Sensitivity Grid (fair value per share)</h4>
                        <table class="sensitivity-table">
                            <thead><tr><th>WACC / TG</th>{% for cell_key, cell_val in dcf_sensitivity_grid.items() %}<th>{{ cell_key }}</th>{% endfor %}</tr></thead>
                            <tbody>
                                <tr>{% for cell_key, cell_val in dcf_sensitivity_grid.items() %}<td>${{ cell_val }}</td>{% endfor %}</tr>
                            </tbody>
                        </table>
                    </div>
                    {% endif %}
                </div>
                {% endif %}
                {% if scenario_analysis %}
                <div class="scenarios">
                    <h3>Scenario Analysis (PE x EPS)</h3>
                    <table class="scenario-table">
                        <thead><tr><th>Scenario</th><th>Price</th><th>EPS</th><th>P/E</th><th>Upside</th></tr></thead>
                        <tbody>
                            <tr class="bull"><td>Bull</td><td>${{ scenario_bull_price }}</td><td>${{ scenario_bull_eps }}</td><td>{{ scenario_bull_pe }}x</td><td>{{ scenario_bull_upside }}%</td></tr>
                            <tr class="base"><td>Base</td><td>${{ scenario_base_price }}</td><td>${{ scenario_base_eps }}</td><td>{{ scenario_base_pe }}x</td><td>{{ scenario_base_upside }}%</td></tr>
                            <tr class="bear"><td>Bear</td><td>${{ scenario_bear_price }}</td><td>${{ scenario_bear_eps }}</td><td>{{ scenario_bear_pe }}x</td><td>{{ scenario_bear_upside }}%</td></tr>
                        </tbody>
                    </table>
                    {% if scenario_blended_fair_value and scenario_blended_fair_value != 'N/A' %}
                    <p class="blended-fv"><strong>Blended Fair Value:</strong> ${{ scenario_blended_fair_value }} ({{ scenario_blended_upside_pct }}% vs current price)</p>
                    {% endif %}
                </div>
                {% endif %}
                {% if peer_comps and peer_comps.multiples %}
                <div class="peer-comps">
                    <h3>Peer Comparison ({{ peer_comps.sector }})</h3>
                    <table class="peer-table">
                        <thead><tr><th>Multiple</th><th>{{ peer_comps.ticker }}</th><th>Sector Median</th><th>Percentile</th></tr></thead>
                        <tbody>
                            {% for metric_key, m in peer_comps.multiples.items() %}
                            {% if m.target_value is not none %}
                            <tr>
                                <td>{{ m.label }}</td>
                                <td>{{ m.target_value }}x</td>
                                <td>{{ m.peer_median }}x</td>
                                <td>{{ m.percentile_rank }}th</td>
                            </tr>
                            {% endif %}
                            {% endfor %}
                        </tbody>
                    </table>
                </div>
                {% endif %}
            </div>
        </div>
        {% endif %}

        <!-- Upcoming Catalysts -->
        {% if catalyst_pipeline %}
        <div class="section catalysts">
            <div class="section-header">
                <span class="section-icon">🚀</span>
                <h2 class="section-title">Upcoming Catalysts</h2>
            </div>
            <div class="catalyst-content">
                {{ catalyst_pipeline_html | safe }}
            </div>
        </div>
        {% endif %}
        
        <!-- Trading Plan -->
        {% if entry_price != 'N/A' or stop_loss != 'N/A' or target_1 != 'N/A' %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">🎯</span>
                <h2 class="section-title">Trading Plan</h2>
            </div>
            <div class="trading-plan-box">
                <div class="plan-grid">
                    <div class="plan-item">
                        <div class="plan-label">{{ plan_entry_label }}</div>
                        <div class="plan-value entry">${{ entry_price }}</div>
                    </div>
                    <div class="plan-item">
                        <div class="plan-label">{{ plan_stop_label }}</div>
                        <div class="plan-value stop">${{ stop_loss }}</div>
                    </div>
                    <div class="plan-item">
                        <div class="plan-label">{{ plan_target_label }}</div>
                        <div class="plan-value target">${{ target_1 }}</div>
                    </div>
                    <div class="plan-item">
                        <div class="plan-label">Target 2</div>
                        <div class="plan-value target">${{ target_2 }}</div>
                    </div>
                </div>
            </div>
        </div>
        {% elif trading_plan %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">🎯</span>
                <h2 class="section-title">Trading Plan</h2>
            </div>
            <div class="section-content">
                {{ trading_plan_html | safe }}
            </div>
        </div>
        {% endif %}
        
        <!-- Final Decision -->
        {% if final_decision %}
        <div class="section">
            <div class="section-header">
                <span class="section-icon">✅</span>
                <h2 class="section-title">Final Decision & Rationale</h2>
            </div>
            <div class="section-content">
                {{ final_decision_html | safe }}
            </div>
        </div>
        {% endif %}
        
        <!-- Footer -->
        <div class="footer">
            <div class="footer-main">
                <strong>TradingAgents</strong> | {{ ticker }} Research Report | {{ analysis_date }}
            </div>
            <div class="disclaimer">
                This report is generated by AI for research purposes only and does not constitute financial advice. 
                Past performance does not guarantee future results. Always conduct your own due diligence before making investment decisions.
            </div>
        </div>
    </div>
</body>
</html>
"""


def truncate_text(text: str, max_length: int = 500, preserve_structure: bool = True) -> str:
    """Truncate text to max length, preserving structure and ending at natural breaks.
    
    Args:
        text: Text to truncate
        max_length: Maximum character length
        preserve_structure: If True, try to end at section/paragraph boundaries
        
    Returns:
        Truncated text ending at a natural break point
    """
    if not text or len(text) <= max_length:
        return text or ""
    
    truncated = text[:max_length]
    
    if preserve_structure:
        # Priority order for break points (best to worst)
        break_patterns = [
            r'\n\n(?=#{1,4}\s)',  # Before a new header
            r'\n\n(?=\d+\.)',     # Before a numbered section
            r'\n\n(?=\*\*)',      # Before bold text (often a subsection)
            r'\n\n',              # Double newline (paragraph break)
            r'\.\s+(?=[A-Z])',    # Period followed by capital (sentence end)
            r'\.\n',              # Period at end of line
            r'\n',                # Single newline
        ]
        
        for pattern in break_patterns:
            matches = list(re.finditer(pattern, truncated))
            if matches:
                # Find the last match that's at least 60% into the text
                for match in reversed(matches):
                    if match.start() > max_length * 0.6:
                        return truncated[:match.start()].rstrip()
    
    # Fallback: try to end at a sentence
    last_period = truncated.rfind('.')
    if last_period > max_length * 0.7:
        return truncated[:last_period + 1]
    
    # Last resort: just cut and add ellipsis
    return truncated.rstrip() + "..."


# Limits are calibrated against the empirical maximum section lengths observed across
# Deep-mode Terra debates routinely land ~8–21K chars; thresholds fire a warning
# only when an LLM response is genuinely cut off — not to impose a display cap.
# Do not lower these values.
SECTION_LIMITS = {
    "technical": 10000,     # Technical analysis — full OHLCV, indicators, support/resistance
    "fundamental": 10000,   # Financials — income stmt, balance sheet, cash flow detail
    "news": 12000,          # News — full article summaries + sentiment breakdown
    "sentiment": 12000,     # Sentiment — social media + analyst sentiment detail
    "bull": 24000,          # Bull case — multi-factor arguments from LLM debate
    "bear": 24000,          # Bear case — multi-factor arguments from LLM debate
    "risk": 22000,          # Risk assessment — three debator positions in full
    "trading": 5000,        # Trading plan — entry/exit levels + rationale
    "decision": 9000,       # Final decision — quant + qualitative synthesis
    "executive": 800,       # Executive summary — intentionally brief
    "default": 6000,
}

# Raw state fields used for oversized-section heuristics (confidence calibration).
RAW_SECTION_LIMIT_KEYS = {
    "market_report": "technical",
    "fundamentals_report": "fundamental",
    "news_report": "news",
    "sentiment_report": "sentiment",
    "trader_investment_plan": "trading",
    "final_trade_decision": "decision",
}


def smart_truncate_section(text: str, section_type: str) -> str:
    """Intelligently truncate a section based on its type and importance."""
    max_length = SECTION_LIMITS.get(section_type, SECTION_LIMITS["default"])
    return truncate_text(text, max_length, preserve_structure=True)


def _has_citation(text: str) -> bool:
    from tradingagents.reporting.context_qc import has_citation

    return has_citation(text)


def get_decision_class(decision: str) -> str:
    """Get CSS class for decision type."""
    decision_upper = decision.upper() if decision else ""
    if "BUY" in decision_upper:
        return "buy"
    elif "SELL" in decision_upper:
        return "sell"
    else:
        return "hold"


def extract_decision_word(decision: str) -> str:
    """Extract main decision word."""
    decision_upper = decision.upper() if decision else ""
    if "BUY" in decision_upper:
        return "BUY"
    elif "SELL" in decision_upper:
        return "SELL"
    else:
        return "HOLD"


def display_decision_from_state(state: dict, decision: str) -> str:
    """Report chip uses ``final_rating`` when the run wrote a canonical label."""
    final_rating = str((state or {}).get("final_rating") or "").strip().upper()
    if final_rating in {"BUY", "SELL", "HOLD", "REVIEW"}:
        return final_rating
    return extract_decision_word(decision)


def extract_bull_bear_summaries(state: Dict[str, Any]) -> tuple:
    """Extract bull and bear case summaries from state."""
    bull_summary = ""
    bear_summary = ""
    
    # Try investment_debate_state - the actual debate content
    debate_state = state.get("investment_debate_state", {})
    if isinstance(debate_state, dict):
        # Get the full bull and bear histories
        bull_history = debate_state.get("bull_history", "")
        bear_history = debate_state.get("bear_history", "")
        
        if bull_history:
            bull_summary = bull_history
        if bear_history:
            bear_summary = bear_history
    
    # Fallback to looking in investment_plan
    if not bull_summary or not bear_summary:
        investment_plan = state.get("investment_plan", "")
        if investment_plan:
            # Try to extract bull/bear sections
            bull_match = re.search(r'(?:bull|bullish|positive)[:\s]+(.+?)(?:bear|bearish|negative|$)', 
                                   investment_plan, re.IGNORECASE | re.DOTALL)
            if bull_match and not bull_summary:
                bull_summary = bull_match.group(1).strip()
            
            bear_match = re.search(r'(?:bear|bearish|negative)[:\s]+(.+?)(?:conclusion|decision|$)', 
                                   investment_plan, re.IGNORECASE | re.DOTALL)
            if bear_match and not bear_summary:
                bear_summary = bear_match.group(1).strip()
    
    return bull_summary, bear_summary


def _extract_price_from_stock_data_payload(stock_data: str, analysis_date: str) -> Optional[float]:
    """Extract close price on (or most recent before) analysis_date from stock CSV payload."""
    if not stock_data:
        return None

    try:
        target_date = datetime.strptime(str(analysis_date)[:10], "%Y-%m-%d")
    except Exception:
        target_date = None

    in_csv = False
    best_date = None
    best_close = None
    for raw_line in stock_data.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not in_csv:
            if line.startswith("Date,") and "Close" in line:
                in_csv = True
            continue

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            row_date = datetime.strptime(parts[0], "%Y-%m-%d")
            row_close = float(parts[4])
        except Exception:
            continue

        if target_date is not None:
            if row_date <= target_date and (best_date is None or row_date > best_date):
                best_date = row_date
                best_close = row_close
        else:
            if best_date is None or row_date > best_date:
                best_date = row_date
                best_close = row_close

    return best_close


def _resolve_price_from_state(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
) -> Optional[float]:
    """
    Resolve as-of price deterministically from the same stock-data tool path used in analysis.
    Falls back to regex extraction only if structured path fails.
    """
    provenance = state.get("data_provenance") or []
    args = None

    for event in reversed(provenance if isinstance(provenance, list) else []):
        if (
            isinstance(event, dict)
            and event.get("method") == "get_stock_data"
            and event.get("status") == "success"
        ):
            ev_args = event.get("args") or []
            if len(ev_args) >= 3 and str(ev_args[0]).upper() == ticker.upper():
                args = [str(ev_args[0]), str(ev_args[1]), str(ev_args[2])]
                break

    # Always anchor through the requested analysis_date to avoid stale tool-call end dates.
    try:
        analysis_dt = datetime.strptime(str(analysis_date)[:10], "%Y-%m-%d")
    except Exception:
        return None

    if args is None:
        start_dt = analysis_dt.replace(year=max(1970, analysis_dt.year - 2))
        args = [ticker.upper(), start_dt.strftime("%Y-%m-%d"), analysis_dt.strftime("%Y-%m-%d")]
    else:
        args[2] = analysis_dt.strftime("%Y-%m-%d")

    try:
        from tradingagents.dataflows.interface import route_to_vendor

        stock_data = route_to_vendor("get_stock_data", args[0], args[1], args[2])
        return _extract_price_from_stock_data_payload(str(stock_data), analysis_date)
    except Exception as exc:
        logger.debug("Structured as-of price resolution failed for %s: %s", ticker, exc)
        return None


def create_executive_summary(state: Dict[str, Any], decision: str, ticker: str, analysis_date: str) -> str:
    """Create a clean, professional 2-3 sentence executive summary.
    
    Synthesizes key points from the analysis into a concise summary.
    Returns plain text (no markdown) suitable for the executive summary box.
    """
    investment_plan = strip_signal_json(state.get("investment_plan", ""))
    fundamentals_report = strip_signal_json(state.get("fundamentals_report", ""))
    market_report = strip_signal_json(state.get("market_report", ""))
    
    # Extract key data points for the summary
    decision_word = decision.upper().strip()
    if "BUY" in decision_word:
        action = "BUY"
    elif "SELL" in decision_word:
        action = "SELL"
    else:
        action = "HOLD"
    
    # Extract current price from market or fundamentals
    current_price_value = _resolve_price_from_state(state, ticker, analysis_date)
    if current_price_value is None:
        current_price_value = extract_current_price(market_report, fundamentals_report)
    current_price = f"${current_price_value:.2f}" if current_price_value else ""
    
    # Extract key metrics
    ps_match = re.search(r'Price\s+to\s+Sales\s+Ratio[:\s]*([\d,]+\.?\d*)', fundamentals_report, re.IGNORECASE)
    ps_ratio = ps_match.group(1).replace(',', '') if ps_match else ""
    
    # Extract bull's key point (first parenthetical from bull section)
    bull_key = ""
    if "Bull" in investment_plan:
        bull_section = investment_plan.split("Bear")[0] if "Bear" in investment_plan else investment_plan
        # Look for numbered points like (1), (2), etc.
        points = re.findall(r'\([\d]\)\s*([^,\(]+)', bull_section)
        if points:
            bull_key = points[0].strip()
    
    # Extract bear's key point
    bear_key = ""
    if "Bear" in investment_plan:
        bear_section = investment_plan.split("Bear")[1] if "Bear" in investment_plan else ""
        points = re.findall(r'\([\d]\)\s*([^,\(]+)', bear_section)
        if points:
            bear_key = points[0].strip()
    
    from tradingagents.reporting.position_action import (
        format_decision_label,
        parse_position_action_from_state,
    )

    sizing = parse_position_action_from_state(state)
    label = format_decision_label(action, sizing)
    sizing_u = str(sizing or "").upper()

    # Build the summary based on decision type
    if action == "SELL":
        parts = [f"Recommendation: {label} {ticker}."]
        if sizing_u == "REDUCE":
            parts.append("Trim existing exposure; do not initiate a new long.")
        elif sizing_u == "AVOID":
            parts.append("Do not initiate a new long.")
        if bear_key:
            parts.append(f"Key concern: {bear_key}.")
        elif sizing_u not in {"REDUCE", "AVOID"}:
            if ps_ratio:
                parts.append(f"Valuation stretched with Price/Sales ratio of {ps_ratio}.")
            else:
                parts.append("Analysis indicates elevated risk profile with uncertain near-term catalysts.")

        if current_price:
            parts.append(f"Current price {current_price}.")

        return " ".join(parts)

    elif action == "BUY":
        parts = [f"Recommendation: {label} {ticker}."]
        if sizing_u == "AVOID":
            parts.append("Do not initiate a new long.")
        elif bull_key:
            parts.append(f"Key catalyst: {bull_key}.")
        else:
            parts.append("Technical and fundamental analysis support upside potential.")

        if current_price:
            parts.append(f"Entry point at {current_price}.")

        return " ".join(parts)

    else:  # HOLD
        parts = [f"Recommendation: {label} {ticker}."]
        if sizing_u == "AVOID":
            from tradingagents.reporting.decision_scorecard import compute_decision_scorecard

            card = compute_decision_scorecard(
                state,
                "HOLD",
                ticker,
                risk_profile=str(state.get("risk_profile") or "growth"),
            )
            gates = card.get("gates") or []
            failed_limits = any(g.get("pass") is False for g in gates)
            lockup_ratio = (card.get("lockup") or {}).get("ratio")
            if failed_limits:
                parts.append("Do not initiate — risk metrics exceed the profile limits.")
            elif lockup_ratio is not None and float(lockup_ratio) >= 1:
                parts.append("Do not initiate — a live supply event exceeds average daily volume.")
            else:
                parts.append("Do not initiate a new long.")
        else:
            parts.append("Mixed signals warrant maintaining current position.")

        if bull_key and bear_key:
            parts.append("Upside catalysts balanced against near-term risks.")

        return " ".join(parts)


def extract_current_price(market_report: str, fundamentals_report: str) -> Optional[float]:
    """Extract current price from market or fundamentals reports."""
    if not market_report and not fundamentals_report:
        return None

    # These patterns are ordered by specificity (most specific first)
    price_patterns = [
        r'closing\s+price\s+(?:of\s+)?(\d+\.?\d*)',
        r'price\s+ranged?\s+(?:around|near|at)\s+(\d+\.?\d*)',
        r'(?:current|stock|share)\s*price[:\s]*\$?(\d+\.?\d*)',
        r'trading\s+(?:at|around|near)\s+\$?(\d+\.?\d*)',
        r'\$(\d+\.\d{2})(?:\s|,|\.|\)|$)',
    ]

    for pattern in price_patterns:
        match = re.search(pattern, market_report or "", re.IGNORECASE)
        if match:
            try:
                price = float(match.group(1))
                if price > 1.0:
                    return price
            except ValueError:
                continue

    fundamentals_patterns = [
        r'(?:current|stock)\s+price[:\s]*\$?(\d+\.?\d*)',
        r'trading\s+(?:at|around)\s+\$?(\d+\.?\d*)',
    ]
    for pattern in fundamentals_patterns:
        match = re.search(pattern, fundamentals_report or "", re.IGNORECASE)
        if match:
            try:
                price = float(match.group(1))
                if 1.0 < price < 10000:
                    return price
            except ValueError:
                continue

    return None


def _eq_positive_flag(eq_data: Dict[str, Any], kind: str) -> bool:
    """Accept both compute keys and legacy display keys for OCF / NI flags."""
    if kind == "ocf":
        keys = ("has_positive_ocf", "positive_operating_cash_flow")
    else:
        keys = ("has_positive_net_income", "positive_net_income")
    for key in keys:
        if key in eq_data:
            return bool(eq_data.get(key))
    return False


def _plan_uses_short_geometry(decision: str = "", position_action: str = "") -> bool:
    """Short-style levels only for an initiated short, not SELL/REDUCE of a long."""
    action = str(position_action or "").strip().upper()
    dec = str(decision or "").strip().upper()
    return dec == "SHORT" or action == "SHORT"


def extract_trading_metrics(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
    decision: str = "",
    position_action: str = "",
) -> Dict[str, str]:
    """Extract trading plan metrics from market data and analysis.
    
    Calculates entry, stop loss, and target prices based on:
    - Current stock price from market report
    - ATR for volatility-based stops
    - Technical levels mentioned in analysis

    Short geometry (stop above, target below) is only for an initiated short.
    SELL / REDUCE / HOLD on a long uses protective long levels so the plan box
    is not inverted.
    """
    market_report = state.get("market_report") or ""
    fundamentals_report = state.get("fundamentals_report") or ""
    trader_decision = state.get("trader_investment_decision") or state.get("trader_investment_plan") or ""
    investment_plan = state.get("investment_plan") or ""
    is_short = _plan_uses_short_geometry(decision, position_action)
    
    metrics = {}
    
    # First, try to extract explicitly mentioned prices from trader decision
    text = trader_decision or investment_plan or ""
    explicit_patterns = {
        'entry': r'entry[:\s]+\$?([\d.]+)',
        'stop_loss': r'stop[- ]?loss[:\s]+\$?([\d.]+)',
        'target': r'(?:target|price target)[:\s]+\$?([\d.]+)',
        'target_1': r'target\s*1[:\s]+\$?([\d.]+)',
        'target_2': r'target\s*2[:\s]+\$?([\d.]+)',
    }
    
    for name, pattern in explicit_patterns.items():
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            metrics[name] = match.group(1)
    
    # If no explicit prices, calculate from market data
    if not metrics.get('entry'):
        current_price = _resolve_price_from_state(state, ticker, analysis_date)
        if current_price is None:
            current_price = extract_current_price(market_report, fundamentals_report)
        if current_price:
            metrics['entry'] = f"{current_price:.2f}"
            
            # Try to find ATR for stop loss calculation
            atr_match = re.search(r'ATR[:\s]+\$?([\d.]+)', market_report, re.IGNORECASE)
            if atr_match:
                try:
                    atr = float(atr_match.group(1))
                    if is_short:
                        metrics['stop_loss'] = f"{current_price + (2 * atr):.2f}"
                        metrics['target_1'] = f"{current_price - (3 * atr):.2f}"
                    else:
                        metrics['stop_loss'] = f"{current_price - (2 * atr):.2f}"
                        metrics['target_1'] = f"{current_price + (3 * atr):.2f}"
                except ValueError:
                    pass
            
            # Fallback: use percentage-based stops if no ATR
            if not metrics.get('stop_loss'):
                if is_short:
                    metrics['stop_loss'] = f"{current_price * 1.08:.2f}"
                    metrics['target_1'] = f"{current_price * 0.85:.2f}"
                else:
                    metrics['stop_loss'] = f"{current_price * 0.92:.2f}"
                    metrics['target_1'] = f"{current_price * 1.15:.2f}"

    action = str(position_action or "").strip().upper()
    if is_short:
        metrics["plan_kind"] = "short"
    elif action in {"REDUCE", "TRIM"} or str(decision or "").strip().upper() == "SELL":
        metrics["plan_kind"] = "reduce"
    else:
        metrics["plan_kind"] = "long"
    return metrics


def _extract_latest_news_window_from_provenance(
    state: Dict[str, Any],
    ticker: str,
) -> Optional[tuple]:
    """Extract latest successful get_news date window for the ticker."""
    provenance = state.get("data_provenance", []) or []
    for event in reversed(provenance if isinstance(provenance, list) else []):
        if not isinstance(event, dict):
            continue
        if event.get("method") != "get_news" or event.get("status") != "success":
            continue
        args = event.get("args") or []
        if len(args) < 3:
            continue
        if str(args[0]).upper() != ticker.upper():
            continue
        return str(args[1]), str(args[2])
    return None


def _parse_latest_headlines(news_payload: str, max_items: int = 6) -> List[Dict[str, str]]:
    """Parse headline/date/source/link tuples from formatted get_news payload."""
    if not news_payload:
        return []

    entries: List[Dict[str, str]] = []
    blocks = re.split(r"\n---\n", news_payload)
    for block in blocks:
        if len(entries) >= max_items:
            break

        title_match = re.search(r"^###\s+(.+)$", block, flags=re.MULTILINE)
        meta_match = re.search(
            r"\*\*Date:\*\*\s*([^|]+)\|\s*\*\*Source:\*\*\s*(.+)$",
            block,
            flags=re.MULTILINE,
        )
        link_match = re.search(r"\[Read more\]\(([^)]+)\)", block)
        if not title_match or not meta_match:
            continue

        entries.append(
            {
                "title": title_match.group(1).strip(),
                "date": meta_match.group(1).strip(),
                "source": meta_match.group(2).strip(),
                "url": link_match.group(1).strip() if link_match else "",
            }
        )

    return entries


def build_latest_headlines_appendix(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
    max_items: int = 6,
) -> str:
    """
    Build a bounded deterministic latest-headlines appendix for the report.
    """
    window = _extract_latest_news_window_from_provenance(state, ticker)
    if window:
        start_date, end_date = window
    else:
        try:
            end_dt = datetime.strptime(analysis_date[:10], "%Y-%m-%d")
            start_date = (end_dt - timedelta(days=7)).strftime("%Y-%m-%d")
            end_date = end_dt.strftime("%Y-%m-%d")
        except Exception:
            return ""

    try:
        from tradingagents.dataflows.interface import route_to_vendor

        payload = route_to_vendor("get_news", ticker.upper(), start_date, end_date)
    except Exception as exc:
        logger.debug("Latest headlines appendix unavailable for %s: %s", ticker, exc)
        return ""

    items = _parse_latest_headlines(str(payload), max_items=max(1, int(max_items)))
    if not items:
        return ""

    lines = [f"#### Latest Headlines Appendix ({start_date} to {end_date})"]
    for item in items:
        title = item.get("title", "").strip()
        source = item.get("source", "").strip()
        date_text = item.get("date", "").strip()
        url = item.get("url", "").strip()

        if url:
            lines.append(f"- **{date_text}** | *{source}* | [{title}]({url})")
        else:
            lines.append(f"- **{date_text}** | *{source}* | {title}")

    return "\n".join(lines)


def _extract_decision_json_from_state(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract DECISION_JSON from the final trade decision text."""
    text = state.get("final_trade_decision", "")
    if not text:
        return None
    marker = "DECISION_JSON:"
    idx = text.find(marker)
    if idx == -1:
        return None
    after = text[idx + len(marker):]
    brace_start = after.find("{")
    if brace_start == -1:
        return None
    depth = 0
    for i in range(brace_start, len(after)):
        ch = after[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(after[brace_start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def extract_confidence_score(state: Dict[str, Any], decision: str) -> int:
    """Extract or calculate a confidence score based on analysis consensus.

    Integrates DECISION_JSON conviction, SIGNAL_JSON agent agreement,
    and composite signal magnitude alongside existing heuristics.
    """
    base_score = 55

    # Factor 1: Agreement between debate judge and risk manager
    investment_plan = (state.get("investment_plan") or "").upper()
    risk_state = state.get("risk_debate_state", {})

    investment_decision = ""
    if "BUY" in investment_plan:
        investment_decision = "BUY"
    elif "SELL" in investment_plan:
        investment_decision = "SELL"
    else:
        investment_decision = "HOLD"

    if investment_decision == decision.upper():
        base_score += 10

    # Factor 2: Risk analyst agreement
    if isinstance(risk_state, dict):
        aggressive = (risk_state.get("risky_history") or "").upper()
        conservative = (risk_state.get("safe_history") or "").upper()
        neutral = (risk_state.get("neutral_history") or "").upper()

        agreement_count = 0
        for analyst_text in [aggressive, conservative, neutral]:
            if decision.upper() in analyst_text[-500:]:
                agreement_count += 1

        base_score += agreement_count * 5

    # Factor 3: Data completeness
    data_fields = ["market_report", "fundamentals_report", "news_report", "sentiment_report"]
    complete_data = sum(1 for field in data_fields if state.get(field))
    base_score += complete_data * 2

    # Factor 4: Debate depth
    debate_state = state.get("investment_debate_state", {})
    if isinstance(debate_state, dict):
        history = debate_state.get("history", "")
        rounds = len(re.findall(r'(?:Bull|Bear)\s*Analyst:', history, re.IGNORECASE)) // 2
        base_score += min(rounds * 3, 9)

    # Factor 5: DECISION_JSON conviction
    decision_json = _extract_decision_json_from_state(state)
    if decision_json:
        conviction = str(decision_json.get("conviction", "")).lower()
        if conviction == "high":
            base_score += 10
        elif conviction == "low":
            base_score -= 10

    # Factor 6: SIGNAL_JSON agent agreement
    try:
        from tradingagents.reporting.attribution import _extract_signal_json
        from tradingagents.graph.signal_aggregator import _stance_to_score

        agent_fields = {
            "market_report": "Market",
            "fundamentals_report": "Fundamentals",
            "news_report": "News",
            "sentiment_report": "Sentiment",
            "investment_plan": "Research",
            "trader_investment_plan": "Trading Plan",
        }
        stances = []
        for field in agent_fields:
            text = state.get(field, "")
            if text:
                sig = _extract_signal_json(text)
                if sig:
                    stances.append(_stance_to_score(sig.get("stance", "")))

        if stances:
            decision_direction = 1.0 if decision.upper() == "BUY" else (-1.0 if decision.upper() == "SELL" else 0.0)
            agreeing = sum(1 for s in stances if (s > 0 and decision_direction > 0) or (s < 0 and decision_direction < 0) or (s == 0 and decision_direction == 0))
            if agreeing >= 5:
                base_score += 12
            elif agreeing >= 3:
                base_score += 5
        else:
            analyst_reports = sum(
                1 for field in ("market_report", "fundamentals_report", "news_report", "sentiment_report")
                if state.get(field)
            )
            if analyst_reports:
                base_score -= min(16, analyst_reports * 4)

    except Exception:
        pass

    if not _extract_decision_json_from_state(state):
        base_score -= 8

    # Factor 7: Composite signal magnitude and cross-agent disagreement
    try:
        from tradingagents.graph.signal_aggregator import compute_signal_summary
        ticker = state.get("company_of_interest", "")
        if ticker:
            sig_result = compute_signal_summary(state, ticker)
            composite = abs(sig_result.get("composite", 0))
            if decision.upper() == "HOLD":
                if composite > 0.35:
                    base_score -= 6
                elif composite > 0.25:
                    base_score -= 3
            elif composite > 0.5:
                base_score += 8
            elif composite > 0.25:
                base_score += 4
            elif composite < 0.1:
                base_score -= 5

            disagreement = sig_result.get("disagreement")
            if isinstance(disagreement, (int, float)):
                if disagreement >= 0.6:
                    base_score -= 10
                elif disagreement >= 0.45:
                    base_score -= 5
    except Exception:
        pass

    # Factor 8: Oversized sections likely truncated in report output
    oversized = _count_oversized_sections(state)
    if oversized:
        base_score -= min(12, oversized * 2)

    # Factor 9: Contextual conflicts (street consensus, valuation, risk nuance)
    try:
        from tradingagents.reporting.context_qc import compute_context_warnings

        context_warnings = compute_context_warnings(state, decision, ticker=state.get("company_of_interest"))
        if "street_consensus_conflict" in context_warnings:
            base_score -= 6
        if "valuation_conflict" in context_warnings:
            base_score -= 5
        if "risk_nuance_conflict" in context_warnings:
            base_score -= 4
        if "citations_thin" in context_warnings:
            base_score -= 3
        if "citations_missing" in context_warnings:
            base_score -= 4
        if "sell_guardrail_weak" in context_warnings:
            from tradingagents.default_config import DEFAULT_CONFIG

            sell_cfg = DEFAULT_CONFIG.get("decision_guardrails", {}).get("sell", {})
            base_score -= int(sell_cfg.get("confidence_haircut", 8))
    except Exception:
        pass

    # Factor 10: Research/trader BUY vs risk HOLD/AVOID
    trader_plan = (state.get("trader_investment_plan") or "").upper()
    trader_decision = "BUY" if "BUY" in trader_plan else ("SELL" if "SELL" in trader_plan else "HOLD")
    if investment_decision == "BUY" and decision.upper() == "HOLD":
        base_score -= 8
    if trader_decision == "BUY" and decision.upper() == "HOLD":
        base_score -= 6
    if investment_decision == "SELL" and decision.upper() == "HOLD":
        base_score -= 5
    if trader_decision == "SELL" and decision.upper() == "HOLD":
        base_score -= 4
    # Research/trader HOLD overridden to a directional call (LUV: HOLD tape, SELL/REDUCE gates).
    if investment_plan and investment_decision == "HOLD" and decision.upper() in {"SELL", "BUY"}:
        base_score -= 8
    if trader_plan and trader_decision == "HOLD" and decision.upper() in {"SELL", "BUY"}:
        base_score -= 6
    dj = decision_json or {}
    if str(dj.get("position_action") or "").upper() == "AVOID":
        base_score -= 4

    # Factor 11: Index stress overlay (BUY-only; read graph-owned macro snapshot)
    macro = state.get("macro_snapshot") or {}
    if isinstance(macro, dict) and decision.upper() == "BUY":
        regime_haircut = 0
        if macro.get("index_stress") == "stressed":
            regime_haircut += 5
        index_trend = macro.get("index_trend") or macro.get("market_regime")
        if index_trend == "bear":
            regime_haircut += 2
        base_score -= min(regime_haircut, 7)

    # Factor 12: Provenance defects (bad tool args, failed calls)
    try:
        from tradingagents.agents.utils.technical_indicators_tools import _KNOWN_INDICATORS

        for entry in state.get("data_provenance") or []:
            if not isinstance(entry, dict):
                continue
            args = entry.get("args") or []
            if entry.get("method") == "get_indicators" and args:
                if str(args[0]).lower() in _KNOWN_INDICATORS:
                    base_score -= 5
                    break
            status = str(entry.get("status") or "").lower()
            if status in {"error", "failed", "failure"}:
                base_score -= 4
                break
    except Exception:
        pass

    return max(0, min(100, base_score))


def calibrate_confidence_score(raw_score: int, data_quality: Optional[int] = None, quality_weight: float = 0.3) -> int:
    """Calibrate confidence score using backtest outcomes if available.

    Guards against empty or insufficient backtest data to prevent
    confidence collapse.
    """
    try:
        from tradingagents.backtesting import compute_calibration, calibrate_score
    except Exception as e:
        logger.debug("Backtesting calibration unavailable, using raw score: %s", e)
        return raw_score

    calibration = compute_calibration()
    meta = calibration.get("meta", {})
    total_samples = meta.get("total_samples", 0)
    valid_bins = meta.get("valid_bins", 0)

    if total_samples < 20 or valid_bins == 0:
        logger.debug(
            "Insufficient backtest data (samples=%d, valid_bins=%d), skipping calibration",
            total_samples, valid_bins,
        )
        if data_quality is not None:
            weight = max(0.0, min(1.0, float(quality_weight)))
            adjusted = (raw_score * (1.0 - weight)) + (data_quality * weight)
            return int(round(max(0.0, min(100.0, adjusted))))
        return raw_score

    adjusted = calibrate_score(float(raw_score), calibration)
    if data_quality is not None:
        weight = max(0.0, min(1.0, float(quality_weight)))
        adjusted = (adjusted * (1.0 - weight)) + (data_quality * weight)
    return int(round(max(0.0, min(100.0, adjusted))))


from tradingagents.reporting.attribution import (
    _extract_signal_json,
    compute_section_attribution,
    format_section_attribution,
    strip_signal_json,
)


def normalize_risk_sentence(text: str, max_len: int = 220) -> str:
    """Normalize risk sentences to avoid mid-sentence truncation."""
    if not text:
        return ""

    cleaned = re.sub(r'\s+', ' ', text.replace("...", ".")).strip()
    if not cleaned:
        return ""

    sentences = re.split(r'(?<=[.!?])\s+', cleaned)
    if sentences:
        for sent in sentences:
            if len(sent) >= 40 and len(sent) <= max_len:
                return sent.strip()
        first = sentences[0].strip()
        if not re.search(r'[.!?]$', first):
            first = f"{first}."
        return first

    if not re.search(r'[.!?]$', cleaned):
        cleaned = f"{cleaned}."
    return cleaned


PROVENANCE_SECTIONS = {
    "Market Data": {"get_stock_data", "get_indicators"},
    "Fundamentals": {"get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"},
    "News & Sentiment": {"get_news", "get_global_news", "get_insider_sentiment", "get_insider_transactions"},
    "Deep Research": {
        "get_deep_research",
        "get_earnings_deep_dive",
        "get_sec_deep_dive",
        "get_earnings_transcript_snapshot",
        "get_sec_filings_snapshot",
    },
}


_INDICATOR_ARG_NAMES = {
    "close_50_sma", "close_200_sma", "close_10_ema", "macd", "macds", "macdh",
    "rsi", "boll", "boll_ub", "boll_lb", "atr", "vwma", "mfi", "kdjk", "kdjd",
    "dx", "wr", "close_10_roc",
}


def _successful_provenance_methods(entries: Optional[List[Dict[str, Any]]]) -> set:
    """Return tool methods that completed successfully (or via cache)."""
    methods = set()
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        method = entry.get("method")
        if not method:
            continue
        status = str(entry.get("status") or "").lower()
        if status in {"success", "ok"} or entry.get("cache_hit"):
            methods.add(method)
    return methods


def compute_data_quality_score(
    entries: Optional[List[Dict[str, Any]]],
    *,
    analysis_mode: Optional[str] = None,
    has_transcript_snapshot: bool = False,
    is_commodity_etf: bool = False,
) -> int:
    """Compute a simple data quality score from provenance coverage."""
    if not entries:
        return 0

    methods = {entry.get("method") for entry in entries if isinstance(entry, dict) and entry.get("method")}
    successful_methods = _successful_provenance_methods(entries)
    vendors = {entry.get("vendor") for entry in entries if isinstance(entry, dict) and entry.get("vendor")}

    coverage = sum(1 for section_methods in PROVENANCE_SECTIONS.values() if methods & section_methods)
    section_score = (coverage / max(1, len(PROVENANCE_SECTIONS))) * 70.0
    vendor_score = (min(len(vendors), 4) / 4) * 30.0
    score = section_score + vendor_score

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").lower()
        if status in {"error", "failed", "failure"}:
            score -= 8
        args = entry.get("args") or []
        if entry.get("method") == "get_indicators" and args:
            if str(args[0]).lower() in _INDICATOR_ARG_NAMES:
                score -= 12
    deep_methods = successful_methods & PROVENANCE_SECTIONS["Deep Research"]
    if deep_methods and not (successful_methods & {"get_sec_filings_snapshot", "get_sec_deep_dive"}):
        score -= 8
    mode = str(analysis_mode or "").lower().strip()
    if mode == "deep" and not is_commodity_etf and not has_transcript_snapshot:
        if "get_earnings_transcript_snapshot" not in successful_methods:
            score -= 12
    return int(round(max(0.0, min(100.0, score))))


def _extract_snapshot_payload(state: Dict[str, Any], field: str, marker_prefix: str) -> str:
    raw = state.get(field, "")
    if raw:
        return raw
    for message in reversed(state.get("messages", []) or []):
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            continue
        if marker_prefix in content:
            return content.split(marker_prefix, 1)[1].strip()
    return ""


def _parse_snapshot_json(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("JSON:"):
        text = text[len("JSON:"):].strip()
    if text.startswith("RAW:"):
        text = text[len("RAW:"):].strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


def format_sec_filings_snapshot(raw: str) -> str:
    payload = _parse_snapshot_json(raw)
    if not payload:
        return ""

    lines = ["### SEC Filings Snapshot"]
    filings = payload.get("filings", []) or []
    if filings:
        for filing in filings[:4]:
            form = filing.get("form") or "Filing"
            filing_date = filing.get("filing_date") or "Unknown date"
            period_end = filing.get("period_end") or "Unknown period"
            lines.append(f"- **{form}** filed {filing_date} (period end {period_end})")
            highlights = filing.get("highlights") or []
            if highlights:
                lines.append(f"  - Highlights: {', '.join(str(h) for h in highlights[:3])}")
            risks = filing.get("risk_factors") or []
            if risks:
                lines.append(f"  - Risk factors: {', '.join(str(r) for r in risks[:3])}")
            events = filing.get("material_events") or []
            if events:
                lines.append(f"  - Material events: {', '.join(str(e) for e in events[:3])}")
    else:
        lines.append("- No filings returned.")

    insider = payload.get("insider_activity") or []
    if insider:
        lines.append("#### Insider Activity")
        for entry in insider[:4]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name") or "Insider"
            role = entry.get("role") or ""
            tx = entry.get("transaction_type") or "transaction"
            date = entry.get("date") or "Unknown date"
            lines.append(f"- {name} {role} {tx} on {date}")

    return "\n".join(lines)


def format_earnings_transcript_snapshot(raw: str) -> str:
    payload = _parse_snapshot_json(raw)
    if not payload:
        return ""

    lines = ["### Earnings Transcript Snapshot"]
    period = payload.get("period")
    call_date = payload.get("call_date")
    if period or call_date:
        lines.append(f"- **{period or 'Latest'}** call on {call_date or 'Unknown date'}")

    guidance = payload.get("guidance") or []
    if guidance:
        lines.append("#### Guidance")
        for item in guidance[:4]:
            metric = item.get("metric") or "metric"
            value = item.get("range") or item.get("value") or ""
            timeframe = item.get("timeframe") or ""
            context = item.get("context") or ""
            summary = f"{metric}: {value}".strip(": ")
            if timeframe:
                summary = f"{summary} ({timeframe})"
            if context:
                summary = f"{summary} — {context}"
            lines.append(f"- {summary}")

    kpis = payload.get("kpis") or []
    if kpis:
        lines.append("#### Key KPIs")
        for item in kpis[:4]:
            name = item.get("name") or "KPI"
            value = item.get("value") or ""
            period = item.get("period") or ""
            context = item.get("context") or ""
            summary = f"{name}: {value}".strip(": ")
            if period:
                summary = f"{summary} ({period})"
            if context:
                summary = f"{summary} — {context}"
            lines.append(f"- {summary}")

    quotes = payload.get("key_quotes") or []
    if quotes:
        lines.append("#### Notable Quotes")
        for quote in quotes[:3]:
            speaker = quote.get("speaker") or "Speaker"
            text = quote.get("quote") or ""
            topic = quote.get("topic") or ""
            summary = f'"{text}"'
            if topic:
                summary = f'{summary} ({topic})'
            lines.append(f"- {speaker}: {summary}")

    return "\n".join(lines)


def format_kpi_deltas(deltas: List[Dict[str, Any]]) -> str:
    """Format KPI deltas as Markdown for report display."""
    if not deltas:
        return ""

    lines = ["### KPI Deltas (vs Previous)"]
    for d in deltas:
        name = d.get("kpi_name") or "KPI"
        curr = d.get("current_value") or "-"
        prev = d.get("previous_value")
        if prev:
            lines.append(f"- **{name}**: {curr} (was {prev})")
        else:
            lines.append(f"- **{name}**: {curr} (no prior)")
    return "\n".join(lines)


def format_guidance_shifts(shifts: List[Dict[str, Any]]) -> str:
    """Format guidance shifts as Markdown for report display."""
    if not shifts:
        return ""

    lines = ["### Guidance Shifts (vs Previous)"]
    for s in shifts:
        metric = s.get("metric") or "Metric"
        curr = s.get("current_range") or "-"
        prev = s.get("previous_range")
        if prev:
            lines.append(f"- **{metric}**: {curr} (was {prev})")
        else:
            lines.append(f"- **{metric}**: {curr} (no prior)")
    return "\n".join(lines)


def format_provenance_summary(entries: Optional[List[Dict[str, Any]]]) -> str:
    """Create a short provenance summary from dataflow events."""
    if not entries:
        return ""

    # Filter to valid dict entries only
    entries = [e for e in entries if isinstance(e, dict)]
    total = len(entries)
    cache_hits = sum(1 for entry in entries if entry.get("cache_hit"))
    vendor_counts: Dict[str, int] = {}
    method_counts: Dict[str, int] = {}

    for entry in entries:
        vendor = entry.get("vendor", "unknown")
        method = entry.get("method", "unknown")
        vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
        method_counts[method] = method_counts.get(method, 0) + 1

    top_vendors = ", ".join(
        f"{vendor} ({count})" for vendor, count in sorted(vendor_counts.items(), key=lambda item: item[1], reverse=True)
    )
    top_methods = ", ".join(
        f"{method} ({count})" for method, count in sorted(method_counts.items(), key=lambda item: item[1], reverse=True)[:6]
    )

    summary_lines = [
        "### Data Provenance",
        f"- Total vendor calls: {total} (cache hits: {cache_hits})",
    ]
    if top_vendors:
        summary_lines.append(f"- Vendors: {top_vendors}")
    if top_methods:
        summary_lines.append(f"- Methods: {top_methods}")

    return "\n".join(summary_lines)


def format_provenance_by_section(entries: Optional[List[Dict[str, Any]]]) -> str:
    """Create section-level provenance details."""
    if not entries:
        return ""

    # Filter to valid dict entries only
    entries = [e for e in entries if isinstance(e, dict)]
    section_lines: List[str] = []
    for section_name, methods in PROVENANCE_SECTIONS.items():
        section_events = [entry for entry in entries if entry.get("method") in methods]
        if not section_events:
            continue

        vendor_counts: Dict[str, int] = {}
        cache_hits = sum(1 for entry in section_events if entry.get("cache_hit"))
        method_counts: Dict[str, int] = {}

        for entry in section_events:
            vendor = entry.get("vendor", "unknown")
            method = entry.get("method", "unknown")
            vendor_counts[vendor] = vendor_counts.get(vendor, 0) + 1
            method_counts[method] = method_counts.get(method, 0) + 1

        vendors = ", ".join(
            f"{vendor} ({count})" for vendor, count in sorted(vendor_counts.items(), key=lambda item: item[1], reverse=True)
        )
        methods_list = ", ".join(
            f"{method} ({count})" for method, count in sorted(method_counts.items(), key=lambda item: item[1], reverse=True)
        )

        section_lines.append(f"#### {section_name}")
        section_lines.append(f"- Vendors: {vendors}")
        section_lines.append(f"- Methods: {methods_list}")
        section_lines.append(f"- Cache hits: {cache_hits}")

    return "\n".join(section_lines)

def _build_risk_assessment_text(state: Dict[str, Any]) -> str:
    """Combine risk-debate histories into a single assessment block."""
    risk_state = state.get("risk_debate_state", {})
    if not isinstance(risk_state, dict):
        return ""

    parts: List[str] = []
    if risk_state.get("risky_history"):
        parts.append(f"AGGRESSIVE: {risk_state['risky_history']}")
    if risk_state.get("safe_history"):
        parts.append(f"CONSERVATIVE: {risk_state['safe_history']}")
    if risk_state.get("neutral_history"):
        parts.append(f"NEUTRAL: {risk_state['neutral_history']}")
    return "\n\n".join(parts)


def _count_oversized_sections(state: Dict[str, Any]) -> int:
    """Count raw analysis sections that exceed QC length thresholds."""
    count = 0
    for field, limit_key in RAW_SECTION_LIMIT_KEYS.items():
        text = state.get(field) or ""
        limit = SECTION_LIMITS.get(limit_key, SECTION_LIMITS["default"])
        if text and len(text) > limit:
            count += 1

    bull_summary, bear_summary = extract_bull_bear_summaries(state)
    for text, limit_key in ((bull_summary, "bull"), (bear_summary, "bear")):
        limit = SECTION_LIMITS.get(limit_key, SECTION_LIMITS["default"])
        if text and len(text) > limit:
            count += 1

    risk_text = _build_risk_assessment_text(state)
    risk_limit = SECTION_LIMITS.get("risk", SECTION_LIMITS["default"])
    if risk_text and len(risk_text) > risk_limit:
        count += 1
    return count


def extract_risk_positions(state: Dict[str, Any]) -> Dict[str, str]:
    """Extract concise risk position summaries from the risk debate.
    
    Returns short, actionable summaries (max 250 chars) for each risk perspective.
    """
    risk_state = state.get("risk_debate_state", {})
    
    positions = {
        'aggressive': '',
        'conservative': '',
        'neutral': ''
    }
    
    def extract_key_stance(text: str, perspective: str) -> str:
        """Extract a complete, meaningful sentence expressing the analyst's stance."""
        if not text:
            return ""
        
        # Clean the text
        text = clean_internal_language(text)
        
        # Remove analyst prefix
        text = re.sub(r'^\s*(?:Aggressive|Conservative|Balanced|Risky|Safe|Neutral)\s+(?:Strategy|Analyst)[:\s]*', '', text, flags=re.IGNORECASE)
        text = text.strip()
        
        # Split into sentences
        sentences = re.split(r'(?<=[.!?])\s+', text)
        
        # Define keywords for each perspective
        if perspective == 'aggressive':
            keywords = ['upside', 'opportunity', 'potential', 'catalyst', 'growth', 'breakout', 'momentum', 'capitalize', 'extraordinary']
        elif perspective == 'conservative':
            keywords = ['protect', 'preserve', 'prudent', 'caution', 'risk', 'downside', 'volatility', 'valuation', 'stretched', 'dilution']
        else:  # neutral
            keywords = ['balance', 'moderate', 'gradual', 'disciplined', 'measured', 'both', 'reduce exposure', 'maintain']
        
        # Find best matching sentence
        best_sentence = ""
        best_score = 0
        
        for sent in sentences[1:8] or sentences:
            sent = sent.strip()
            if len(sent) < 40:  # Skip short sentences
                continue
            
            # Score by keyword matches
            score = sum(1 for kw in keywords if kw.lower() in sent.lower())
            
            # Prefer sentences that start with a capital and contain action verbs
            if re.match(r'^[A-Z]', sent):
                score += 1
            if re.search(r'\b(should|would|could|must|recommend|advise|suggests?)\b', sent, re.IGNORECASE):
                score += 2
            
            if score > best_score:
                best_score = score
                best_sentence = sent
        
        # If we found a good sentence, use it
        if best_sentence:
            return normalize_risk_sentence(best_sentence)
        
        # Fallback: use second sentence if it's meaningful
        if len(sentences) > 1 and len(sentences[1]) > 50:
            return normalize_risk_sentence(sentences[1].strip())
        
        # Absolute fallback
        clean_text = re.sub(r'\s+', ' ', text).strip()
        return normalize_risk_sentence(clean_text)
    
    if isinstance(risk_state, dict):
        aggressive = risk_state.get("risky_history", "")
        conservative = risk_state.get("safe_history", "")
        neutral = risk_state.get("neutral_history", "")
        
        # For aggressive: focus on upside/opportunity
        agg_stance = extract_key_stance(aggressive, 'aggressive')
        if agg_stance and not agg_stance[0].isupper():
            agg_stance = agg_stance[0].upper() + agg_stance[1:] if len(agg_stance) > 1 else agg_stance.upper()
        positions['aggressive'] = agg_stance
        
        # For conservative: focus on protection/caution
        cons_stance = extract_key_stance(conservative, 'conservative')
        if cons_stance and not cons_stance[0].isupper():
            cons_stance = cons_stance[0].upper() + cons_stance[1:] if len(cons_stance) > 1 else cons_stance.upper()
        positions['conservative'] = cons_stance
        
        # For neutral: focus on balanced approach
        neut_stance = extract_key_stance(neutral, 'neutral')
        if neut_stance and not neut_stance[0].isupper():
            neut_stance = neut_stance[0].upper() + neut_stance[1:] if len(neut_stance) > 1 else neut_stance.upper()
        positions['neutral'] = neut_stance
    
    return positions


@dataclass
class ReportPayload:
    ticker: str
    analysis_date: str
    generated_at: str
    model_info: str
    decision: str
    decision_class: str
    decision_label: str
    executive_summary: str
    confidence: int
    data_quality_score: int
    debate_rounds: int
    duration: str
    data_sources: int
    market_report: str
    fundamentals_report: str
    news_report: str
    sentiment_report: str
    latest_headlines_summary: str
    bull_summary: str
    bear_summary: str
    risk_assessment: str
    trading_plan: str
    final_decision: str
    market_report_html: str
    fundamentals_report_html: str
    news_report_html: str
    sentiment_report_html: str
    latest_headlines_html: str
    bull_summary_html: str
    bear_summary_html: str
    risk_assessment_html: str
    trading_plan_html: str
    final_decision_html: str
    provenance_summary: str
    provenance_summary_html: str
    provenance_details: str
    provenance_details_html: str
    section_attribution: str
    section_attribution_html: str
    sec_filings_summary: str
    sec_filings_summary_html: str
    earnings_transcript_summary: str
    earnings_transcript_summary_html: str
    entry_price: str
    stop_loss: str
    target_1: str
    target_2: str
    risk_aggressive: str
    risk_conservative: str
    risk_neutral: str
    qa_warnings: List[str]
    qa_warning_groups: Dict[str, List[str]]
    qa_warnings_html: str
    kpi_deltas_summary: str
    kpi_deltas_html: str
    guidance_shifts_summary: str
    guidance_shifts_html: str
    # Earnings Quality
    earnings_quality: Optional[Dict[str, Any]]
    earnings_quality_grade: str
    earnings_quality_grade_lower: str
    earnings_quality_accruals: str
    earnings_quality_cash_conversion: str
    earnings_quality_pos_ocf: str
    earnings_quality_pos_ni: str
    # Valuation / DCF
    intrinsic_value: Optional[Dict[str, Any]]
    dcf_fair_value: str
    dcf_margin_pct: str
    dcf_margin_class: str
    dcf_margin_label: str
    dcf_fcf: str
    dcf_growth_rate: str
    dcf_wacc: str
    dcf_beta: str
    # Scenario Analysis
    scenario_analysis: Optional[Dict[str, Any]]
    scenario_bull_price: str
    scenario_bull_eps: str
    scenario_bull_pe: str
    scenario_bull_upside: str
    scenario_base_price: str
    scenario_base_eps: str
    scenario_base_pe: str
    scenario_base_upside: str
    scenario_bear_price: str
    scenario_bear_eps: str
    scenario_bear_pe: str
    scenario_bear_upside: str
    # Blended fair value + extended valuation (Plan B Phase 1)
    scenario_blended_fair_value: str
    scenario_blended_upside_pct: str
    dcf_implied_growth_rate: str
    dcf_implied_vs_consensus: str
    dcf_sensitivity_grid: Optional[Dict[str, float]]
    peer_comps: Optional[Dict[str, Any]]
    # Screening handoff (Phase 6C)
    screening_summary_block: str
    decision_scorecard_html: str
    # Catalyst Pipeline
    catalyst_pipeline: str
    catalyst_pipeline_html: str
    plan_kind: str = "long"
    plan_entry_label: str = "Entry Price"
    plan_stop_label: str = "Stop Loss"
    plan_target_label: str = "Target 1"
    run_settings_html: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def render_run_settings_html(settings: Optional[Dict[str, Any]]) -> str:
    """Compact run-settings line for the report header (#752)."""
    if not settings or not isinstance(settings, dict):
        return ""
    parts: List[str] = []
    analysts = settings.get("analysts") or []
    if analysts:
        parts.append("Analysts: " + ", ".join(str(a) for a in analysts))
    mode = settings.get("analysis_mode")
    if mode:
        parts.append(f"Mode: {mode}")
    risk = settings.get("risk_profile")
    if risk:
        parts.append(f"Risk: {risk}")
    debate = settings.get("max_debate_rounds")
    if debate is not None:
        parts.append(f"Debate rounds: {debate}")
    layout = settings.get("analyst_layout")
    if layout:
        parts.append(f"Layout: {layout}")
    vendor_bits: List[str] = []
    for label, mapping in (
        ("Data", settings.get("data_vendors") or {}),
        ("Tools", settings.get("tool_vendors") or {}),
    ):
        if isinstance(mapping, dict) and mapping:
            vendor_bits.append(
                f"{label}: " + ", ".join(f"{k}={v}" for k, v in sorted(mapping.items()))
            )
    if vendor_bits:
        parts.append("; ".join(vendor_bits))
    if not parts:
        return ""
    return html.escape(" | ".join(parts))


def _normalize_metric_value(value: str, field_name: str, warnings: List[str]) -> str:
    if not value or str(value).strip().upper() == "N/A":
        return "N/A"
    try:
        cleaned = str(value).replace("$", "").strip()
        match = re.search(r'(\d+(?:\.\d+)?)', cleaned)
        if not match:
            warnings.append(f"{field_name}_invalid")
            return "N/A"
        number = float(match.group(1))
        if number <= 0 or number > 100000:
            warnings.append(f"{field_name}_out_of_range")
            return "N/A"
        return f"{number:.2f}"
    except ValueError:
        warnings.append(f"{field_name}_invalid")
        return "N/A"


def apply_report_qc(
    payload: ReportPayload,
    state: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Validate and normalize report payload fields in-place."""
    warnings: List[str] = []

    payload.decision = payload.decision.upper().strip()
    payload.executive_summary = re.sub(r'[*_`#]+', '', payload.executive_summary or '').strip()
    if not payload.executive_summary:
        payload.executive_summary = f"Recommendation: {payload.decision} {payload.ticker}."
        warnings.append("executive_summary_missing")

    payload.entry_price = _normalize_metric_value(payload.entry_price, "entry_price", warnings)
    payload.stop_loss = _normalize_metric_value(payload.stop_loss, "stop_loss", warnings)
    payload.target_1 = _normalize_metric_value(payload.target_1, "target_1", warnings)
    payload.target_2 = _normalize_metric_value(payload.target_2, "target_2", warnings)

    for key in ("risk_aggressive", "risk_conservative", "risk_neutral"):
        value = getattr(payload, key, "") or ""
        value = normalize_risk_sentence(value)
        if not value:
            value = "No clear stance available."
            warnings.append(f"{key}_missing")
        setattr(payload, key, value)

    section_limits = {
        "market_report": "technical",
        "fundamentals_report": "fundamental",
        "news_report": "news",
        "latest_headlines_summary": "news",
        "sentiment_report": "sentiment",
        "bull_summary": "bull",
        "bear_summary": "bear",
        "risk_assessment": "risk",
        "trading_plan": "trading",
        "final_decision": "decision",
        "executive_summary": "executive",
    }
    for field, limit_key in section_limits.items():
        value = getattr(payload, field, "") or ""
        limit = SECTION_LIMITS.get(limit_key, SECTION_LIMITS["default"])
        if len(value) > limit:
            warnings.append(f"{field}_truncated")

    section_fields = (
        "market_report",
        "fundamentals_report",
        "news_report",
        "sentiment_report",
        "bull_summary",
        "bear_summary",
        "risk_assessment",
        "trading_plan",
        "final_decision",
    )
    for field in section_fields:
        value = getattr(payload, field, "") or ""
        if not value.strip():
            warnings.append(f"{field}_missing")

    citation_sections = ("market_report", "fundamentals_report", "news_report", "sentiment_report")
    if payload.data_sources > 0 and not any(
        _has_citation(getattr(payload, field, "")) for field in citation_sections
    ):
        if "citations_missing" not in warnings:
            warnings.append("citations_missing")

    decision_upper = (payload.decision or "").upper().strip()
    try:
        from tradingagents.graph.signal_processing import extract_explicit_decision
        final_decision_word = extract_explicit_decision(payload.final_decision or "")
    except Exception:
        final_decision_word = None
    if (
        final_decision_word
        and decision_upper in {"BUY", "SELL", "HOLD"}
        and {decision_upper, final_decision_word} == {"BUY", "SELL"}
    ):
        warnings.append("decision_conflict")

    analyst_state_fields = (
        "market_report",
        "fundamentals_report",
        "news_report",
        "sentiment_report",
    )
    if state:
        if any(
            (state.get(field) or "").strip() and not _extract_signal_json(state.get(field) or "")
            for field in analyst_state_fields
        ):
            warnings.append("signal_json_missing")
        final_raw = state.get("final_trade_decision") or ""
        if final_raw.strip() and "DECISION_JSON:" not in final_raw:
            warnings.append("decision_json_missing")
    else:
        if any(
            (getattr(payload, field, "") or "").strip()
            and "SIGNAL_JSON:" not in (getattr(payload, field, "") or "")
            for field in analyst_state_fields
        ):
            warnings.append("signal_json_missing")
        if (payload.final_decision or "").strip() and "DECISION_JSON:" not in (payload.final_decision or ""):
            warnings.append("decision_json_missing")

    if payload.data_sources <= 0:
        warnings.append("data_sources_missing")
    if payload.data_quality_score <= 0:
        warnings.append("data_quality_low")
    if not payload.provenance_summary:
        warnings.append("provenance_missing")

    if state:
        from tradingagents.reporting.context_qc import compute_context_warnings

        warnings.extend(compute_context_warnings(state, payload.decision, ticker=payload.ticker))
        methods = _successful_provenance_methods(state.get("data_provenance") or [])
        deep_ran = bool(methods & {"get_deep_research", "get_earnings_deep_dive"})
        sec_ran = bool(methods & {"get_sec_filings_snapshot", "get_sec_deep_dive"})
        if deep_ran and not sec_ran and "deep_research_incomplete" not in warnings:
            warnings.append("deep_research_incomplete")

    # Preserve insertion order, drop duplicates from overlapping QC paths.
    deduped: List[str] = []
    seen = set()
    for warning in warnings:
        if warning in seen:
            continue
        seen.add(warning)
        deduped.append(warning)
    return deduped


def build_report_payload(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
    decision: str,
    config: Dict[str, Any],
    duration_seconds: float = 0.0,
) -> ReportPayload:
    """Build a structured payload for report rendering."""
    if not state or not isinstance(state, dict):
        state = {}
    market_report = strip_signal_json(state.get("market_report") or "")
    fundamentals_report = strip_signal_json(state.get("fundamentals_report") or "")
    news_report = strip_signal_json(state.get("news_report") or "")
    sentiment_report = strip_signal_json(state.get("sentiment_report") or "")
    investment_plan = strip_signal_json(state.get("investment_plan") or "")
    trader_plan = strip_signal_json(state.get("trader_investment_plan") or "")
    risk_assessment_text = _build_risk_assessment_text(state)
    final_decision_text = state.get("final_trade_decision") or ""

    bull_summary, bear_summary = extract_bull_bear_summaries(state)
    executive_summary = create_executive_summary(state, decision, ticker, analysis_date)
    from tradingagents.reporting.position_action import parse_position_action_from_state as _parse_pa

    trading_metrics = extract_trading_metrics(
        state,
        ticker,
        analysis_date,
        decision=decision,
        position_action=_parse_pa(state) or "",
    )
    latest_headlines_max = max(3, int(config.get("report_latest_headlines_max", 6)))
    latest_headlines_summary = build_latest_headlines_appendix(
        state,
        ticker,
        analysis_date,
        max_items=latest_headlines_max,
    )
    risk_positions = extract_risk_positions(state)
    sec_filings_raw = _extract_snapshot_payload(state, "sec_filings_snapshot", "SEC_FILINGS_SNAPSHOT_")
    earnings_transcript_raw = _extract_snapshot_payload(
        state,
        "earnings_transcript_snapshot",
        "EARNINGS_TRANSCRIPT_SNAPSHOT_",
    )
    sec_filings_summary = format_sec_filings_snapshot(sec_filings_raw)
    earnings_transcript_summary = format_earnings_transcript_snapshot(earnings_transcript_raw)

    if isinstance(duration_seconds, (int, float)) and duration_seconds > 0:
        minutes = int(duration_seconds // 60)
        seconds = int(duration_seconds % 60)
        duration = f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s"
    else:
        duration = "N/A"

    data_sources = sum([
        1 if market_report else 0,
        1 if fundamentals_report else 0,
        1 if news_report else 0,
        1 if sentiment_report else 0,
    ])

    market_report_html = markdown_to_html(smart_truncate_section(market_report, 'technical'))
    fundamentals_report_html = markdown_to_html(smart_truncate_section(fundamentals_report, 'fundamental'))
    news_report_html = markdown_to_html(smart_truncate_section(news_report, 'news'))
    sentiment_report_html = markdown_to_html(smart_truncate_section(sentiment_report, 'sentiment'))
    latest_headlines_html = (
        markdown_to_html(smart_truncate_section(latest_headlines_summary, 'news'))
        if latest_headlines_summary
        else ""
    )
    bull_summary_html = markdown_to_html(smart_truncate_section(bull_summary, 'bull'))
    bear_summary_html = markdown_to_html(smart_truncate_section(bear_summary, 'bear'))
    risk_assessment_html = markdown_to_html(smart_truncate_section(risk_assessment_text, 'risk'))
    trading_plan_html = markdown_to_html(smart_truncate_section(trader_plan, 'trading'))
    final_decision_html = markdown_to_html(smart_truncate_section(final_decision_text, 'decision'))

    provenance_entries = state.get("data_provenance", [])
    provenance_summary = format_provenance_summary(provenance_entries)
    provenance_details = format_provenance_by_section(provenance_entries)
    data_quality_score = compute_data_quality_score(provenance_entries)
    try:
        from tradingagents.reporting.context_qc import compute_context_warnings as _ctx_warn

        ctx_warns = _ctx_warn(state, decision, ticker=ticker)
        if "citations_thin" in ctx_warns or "citations_missing" in ctx_warns:
            data_quality_score = max(0, data_quality_score - 8)
    except Exception:
        pass
    provenance_summary_html = markdown_to_html(provenance_summary) if provenance_summary else ""
    provenance_details_html = markdown_to_html(provenance_details) if provenance_details else ""
    sec_filings_summary_html = markdown_to_html(sec_filings_summary) if sec_filings_summary else ""
    earnings_transcript_summary_html = (
        markdown_to_html(earnings_transcript_summary) if earnings_transcript_summary else ""
    )

    section_attribution = compute_section_attribution(state, decision)
    section_attribution_text = format_section_attribution(section_attribution)
    section_attribution_html = markdown_to_html(section_attribution_text) if section_attribution_text else ""

    confidence_score = extract_confidence_score(state, decision)
    config_calibration = config.get("confidence_calibration", {})
    if config_calibration.get("enabled", True):
        quality_weight = config_calibration.get("quality_weight", 0.3)
        confidence_score = calibrate_confidence_score(
            confidence_score,
            data_quality=data_quality_score,
            quality_weight=quality_weight,
        )

    # --- Earnings Quality ---
    eq_data = state.get("earnings_quality")
    if isinstance(eq_data, dict) and eq_data:
        eq_grade = str(eq_data.get("grade", "N/A")).upper()
        eq_grade_lower = eq_grade[:1].lower() if eq_grade else "c"
        eq_accruals = str(eq_data.get("accruals_ratio", "N/A"))
        eq_cash_conversion = str(eq_data.get("cash_conversion", "N/A"))
        eq_pos_ocf = "Yes" if _eq_positive_flag(eq_data, "ocf") else "No"
        eq_pos_ni = "Yes" if _eq_positive_flag(eq_data, "ni") else "No"
    else:
        eq_data = None
        eq_grade = ""
        eq_grade_lower = ""
        eq_accruals = ""
        eq_cash_conversion = ""
        eq_pos_ocf = ""
        eq_pos_ni = ""

    # --- Intrinsic Value / DCF ---
    iv_data = state.get("intrinsic_value")
    if isinstance(iv_data, dict) and iv_data:
        dcf_fair_value = str(iv_data.get("fair_value", "N/A"))
        margin_pct = iv_data.get("margin_of_safety_pct", 0)
        try:
            margin_pct_f = float(margin_pct)
        except (TypeError, ValueError):
            margin_pct_f = 0.0
        dcf_margin_pct = f"{margin_pct_f:.1f}"
        dcf_margin_class = "positive" if margin_pct_f >= 0 else "negative"
        dcf_margin_label = "undervalued" if margin_pct_f >= 0 else "overvalued"
        assumptions = iv_data.get("assumptions", {}) or {}
        dcf_fcf = str(assumptions.get("fcf", iv_data.get("fcf", "N/A")))
        dcf_growth_rate = str(assumptions.get("growth_rate", iv_data.get("growth_rate", "N/A")))
        dcf_wacc = str(assumptions.get("wacc", iv_data.get("wacc", "N/A")))
        dcf_beta = str(assumptions.get("beta", iv_data.get("beta", "N/A")))
    else:
        iv_data = None
        dcf_fair_value = ""
        dcf_margin_pct = ""
        dcf_margin_class = ""
        dcf_margin_label = ""
        dcf_fcf = ""
        dcf_growth_rate = ""
        dcf_wacc = ""
        dcf_beta = ""

    # --- Scenario Analysis ---
    sa_data = state.get("scenario_analysis")
    if isinstance(sa_data, dict) and sa_data:
        scenarios = sa_data.get("scenarios", sa_data)

        def _sc(scenario_dict: Any, key: str, default: str = "N/A") -> str:
            if isinstance(scenario_dict, dict):
                return str(scenario_dict.get(key, default))
            return default

        bull_sc = scenarios.get("bull", {}) or {}
        base_sc = scenarios.get("base", {}) or {}
        bear_sc = scenarios.get("bear", {}) or {}
        sa_blended_fv = str(sa_data.get("blended_fair_value", "N/A")) if sa_data.get("blended_fair_value") else "N/A"
        sa_blended_up = str(sa_data.get("blended_upside_pct", "N/A")) if sa_data.get("blended_upside_pct") is not None else "N/A"
    else:
        sa_data = None
        bull_sc = {}
        base_sc = {}
        bear_sc = {}
        _sc = lambda d, k, default="N/A": default  # noqa: E731
        sa_blended_fv = "N/A"
        sa_blended_up = "N/A"

    # --- Extended DCF fields (reverse DCF + sensitivity grid) ---
    if isinstance(iv_data, dict):
        dcf_implied_growth = f"{iv_data['implied_growth_rate'] * 100:.1f}%" if iv_data.get("implied_growth_rate") is not None else "N/A"
        dcf_implied_vs_cons_raw = iv_data.get("implied_vs_consensus")
        dcf_implied_vs_cons = f"{dcf_implied_vs_cons_raw * 100:+.1f}pp" if dcf_implied_vs_cons_raw is not None else "N/A"
        dcf_sens_grid = iv_data.get("dcf_sensitivity_grid") or None
    else:
        dcf_implied_growth = "N/A"
        dcf_implied_vs_cons = "N/A"
        dcf_sens_grid = None

    # --- Peer comps ---
    peer_comps_data = state.get("peer_comps")
    if not isinstance(peer_comps_data, dict) or not peer_comps_data.get("multiples"):
        peer_comps_data = None

    # --- Catalyst Pipeline ---
    catalyst_raw = state.get("catalyst_pipeline", "")
    if isinstance(catalyst_raw, str) and catalyst_raw.strip():
        catalyst_pipeline = catalyst_raw.strip()
        catalyst_html = markdown_to_html(catalyst_pipeline)
        if not catalyst_html.strip():
            catalyst_html = catalyst_pipeline.replace("\n", "<br>")
    else:
        catalyst_pipeline = ""
        catalyst_html = ""

    from tradingagents.reporting.decision_scorecard import (
        compute_decision_scorecard,
        render_decision_scorecard_html,
    )
    from tradingagents.reporting.position_action import (
        format_decision_label,
        parse_position_action_from_state,
    )

    resolved_decision = extract_decision_word(decision)
    display_decision = display_decision_from_state(state, decision)
    scorecard = compute_decision_scorecard(
        state,
        resolved_decision,
        ticker,
        risk_profile=str(state.get("risk_profile") or config.get("risk_profile") or "growth"),
    )
    decision_label = scorecard.get("decision_label") or format_decision_label(
        resolved_decision, parse_position_action_from_state(state)
    )
    decision_scorecard_html = render_decision_scorecard_html(scorecard)

    payload = ReportPayload(
        ticker=ticker,
        analysis_date=analysis_date,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        model_info=f"{config.get('llm_provider', 'openai')} / {config.get('deep_think_llm', 'gpt-5.6-terra')}",
        decision=display_decision,
        decision_class=get_decision_class(display_decision),
        decision_label=decision_label,
        executive_summary=executive_summary,
        confidence=confidence_score,
        data_quality_score=data_quality_score,
        debate_rounds=config.get("max_debate_rounds", 1),
        duration=duration,
        data_sources=data_sources,
        market_report=market_report,
        fundamentals_report=fundamentals_report,
        news_report=news_report,
        sentiment_report=sentiment_report,
        latest_headlines_summary=latest_headlines_summary,
        bull_summary=bull_summary,
        bear_summary=bear_summary,
        risk_assessment=risk_assessment_text,
        trading_plan=trader_plan,
        final_decision=final_decision_text,
        market_report_html=market_report_html,
        fundamentals_report_html=fundamentals_report_html,
        news_report_html=news_report_html,
        sentiment_report_html=sentiment_report_html,
        latest_headlines_html=latest_headlines_html,
        bull_summary_html=bull_summary_html,
        bear_summary_html=bear_summary_html,
        risk_assessment_html=risk_assessment_html,
        trading_plan_html=trading_plan_html,
        final_decision_html=final_decision_html,
        provenance_summary=provenance_summary,
        provenance_summary_html=provenance_summary_html,
        provenance_details=provenance_details,
        provenance_details_html=provenance_details_html,
        section_attribution=section_attribution_text,
        section_attribution_html=section_attribution_html,
        sec_filings_summary=sec_filings_summary,
        sec_filings_summary_html=sec_filings_summary_html,
        earnings_transcript_summary=earnings_transcript_summary,
        earnings_transcript_summary_html=earnings_transcript_summary_html,
        entry_price=trading_metrics.get('entry', 'N/A'),
        stop_loss=trading_metrics.get('stop_loss', 'N/A'),
        target_1=trading_metrics.get('target_1', trading_metrics.get('target', 'N/A')),
        target_2=trading_metrics.get('target_2', 'N/A'),
        plan_kind=str(trading_metrics.get("plan_kind") or "long"),
        plan_entry_label="Mark" if trading_metrics.get("plan_kind") == "reduce" else "Entry Price",
        plan_stop_label="Invalidation" if trading_metrics.get("plan_kind") == "reduce" else "Stop Loss",
        plan_target_label="Repair" if trading_metrics.get("plan_kind") == "reduce" else "Target 1",
        risk_aggressive=risk_positions.get('aggressive', ''),
        risk_conservative=risk_positions.get('conservative', ''),
        risk_neutral=risk_positions.get('neutral', ''),
        qa_warnings=[],
        qa_warning_groups={"integrity": [], "context": [], "all": []},
        qa_warnings_html="",
        kpi_deltas_summary="",
        kpi_deltas_html="",
        guidance_shifts_summary="",
        guidance_shifts_html="",
        # Earnings Quality
        earnings_quality=eq_data,
        earnings_quality_grade=eq_grade,
        earnings_quality_grade_lower=eq_grade_lower,
        earnings_quality_accruals=eq_accruals,
        earnings_quality_cash_conversion=eq_cash_conversion,
        earnings_quality_pos_ocf=eq_pos_ocf,
        earnings_quality_pos_ni=eq_pos_ni,
        # Valuation / DCF
        intrinsic_value=iv_data,
        dcf_fair_value=dcf_fair_value,
        dcf_margin_pct=dcf_margin_pct,
        dcf_margin_class=dcf_margin_class,
        dcf_margin_label=dcf_margin_label,
        dcf_fcf=dcf_fcf,
        dcf_growth_rate=dcf_growth_rate,
        dcf_wacc=dcf_wacc,
        dcf_beta=dcf_beta,
        # Scenario Analysis
        scenario_analysis=sa_data,
        scenario_bull_price=_sc(bull_sc, "price"),
        scenario_bull_eps=_sc(bull_sc, "eps"),
        scenario_bull_pe=_sc(bull_sc, "pe"),
        scenario_bull_upside=_sc(bull_sc, "upside_pct", _sc(bull_sc, "upside")),
        scenario_base_price=_sc(base_sc, "price"),
        scenario_base_eps=_sc(base_sc, "eps"),
        scenario_base_pe=_sc(base_sc, "pe"),
        scenario_base_upside=_sc(base_sc, "upside_pct", _sc(base_sc, "upside")),
        scenario_bear_price=_sc(bear_sc, "price"),
        scenario_bear_eps=_sc(bear_sc, "eps"),
        scenario_bear_pe=_sc(bear_sc, "pe"),
        scenario_bear_upside=_sc(bear_sc, "upside_pct", _sc(bear_sc, "upside")),
        # Blended fair value + extended valuation
        scenario_blended_fair_value=sa_blended_fv,
        scenario_blended_upside_pct=sa_blended_up,
        dcf_implied_growth_rate=dcf_implied_growth,
        dcf_implied_vs_consensus=dcf_implied_vs_cons,
        dcf_sensitivity_grid=dcf_sens_grid,
        peer_comps=peer_comps_data,
        screening_summary_block=(
            f'<div id="screening-summary" class="card-section">'
            f'<h3>Screening Summary</h3>'
            f'<pre style="white-space:pre-wrap;font-size:0.9em;">'
            f'{html.escape(str(state.get("screening_summary_text") or ""))}'
            f'</pre></div>'
            if state.get("screening_summary_text")
            else ""
        ),
        decision_scorecard_html=decision_scorecard_html,
        # Catalyst Pipeline
        catalyst_pipeline=catalyst_pipeline,
        catalyst_pipeline_html=catalyst_html,
        run_settings_html=render_run_settings_html(state.get("run_settings")),
    )

    return payload


def generate_html_report(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
    decision: str,
    config: Dict[str, Any],
    duration_seconds: float = 0.0,
    output_path: Optional[str] = None,
) -> str:
    """Generate an HTML report from analysis results.
    
    Args:
        state: Final state from ta.propagate()
        ticker: Stock ticker
        analysis_date: Date of analysis
        decision: Final decision string
        config: Configuration used
        duration_seconds: Time taken for analysis
        output_path: Optional path to save HTML file
        
    Returns:
        HTML string or path to saved file
    """
    if not JINJA2_AVAILABLE:
        raise ImportError("Jinja2 is required for report generation. Install with: pip install jinja2")
    
    from jinja2 import Template
    
    payload = build_report_payload(
        state=state,
        ticker=ticker,
        analysis_date=analysis_date,
        decision=decision,
        config=config,
        duration_seconds=duration_seconds,
    )
    warnings = apply_report_qc(payload, state=state)
    if warnings:
        logger.warning("Report QA warnings: %s", ", ".join(warnings))
    if isinstance(state, dict):
        state["report_warnings"] = warnings
    from tradingagents.reporting.context_qc import classify_warnings, format_warning_groups_html

    payload.qa_warnings = warnings
    payload.qa_warning_groups = classify_warnings(warnings)
    payload.qa_warnings_html = (
        markdown_to_html(format_warning_groups_html(warnings))
        if warnings
        else ""
    )

    template_data = payload.to_dict()
    
    # Render template
    template = Template(REPORT_TEMPLATE)
    html_content = template.render(**template_data)
    
    # Save to file if output path provided
    if output_path:
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        return output_path
    
    return html_content


def generate_pdf_report(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
    decision: str,
    config: Dict[str, Any],
    duration_seconds: float = 0.0,
    output_path: Optional[str] = None,
) -> str:
    """Generate a PDF report from analysis results.
    
    Args:
        state: Final state from ta.propagate()
        ticker: Stock ticker
        analysis_date: Date of analysis
        decision: Final decision string
        config: Configuration used
        duration_seconds: Time taken for analysis
        output_path: Optional path to save PDF file
        
    Returns:
        Path to saved PDF file (or HTML fallback)
    """
    # Check if WeasyPrint is available
    if not _check_weasyprint():
        logger.warning("PDF generation requires WeasyPrint + system libraries (pango). "
                       "Install with: brew install pango && pip install weasyprint. "
                       "Generating HTML report instead (opens in browser).")
        
        # Fall back to HTML
        if output_path:
            html_path = output_path.replace('.pdf', '.html')
        else:
            html_path = f"{ticker}_{analysis_date}_report.html"
        return generate_html_report(
            state, ticker, analysis_date, decision, config, duration_seconds, html_path
        )
    
    # Import WeasyPrint now that we know it works
    from weasyprint import HTML
    
    # Generate HTML first
    html_content = generate_html_report(
        state, ticker, analysis_date, decision, config, duration_seconds
    )
    
    # Determine output path
    if not output_path:
        output_path = f"{ticker}_{analysis_date}_report.pdf"
    
    # Convert to PDF
    HTML(string=html_content).write_pdf(output_path)
    
    return output_path


def generate_report(
    state: Dict[str, Any],
    ticker: str,
    analysis_date: str,
    decision: str,
    config: Dict[str, Any],
    duration_seconds: float = 0.0,
    output_dir: Optional[str] = None,
    format: str = "both"
) -> Dict[str, str]:
    """Generate reports in specified format(s).
    
    Args:
        state: Final state from ta.propagate()
        ticker: Stock ticker
        analysis_date: Date of analysis
        decision: Final decision string
        config: Configuration used
        duration_seconds: Time taken for analysis
        output_dir: Directory to save reports (default: ./reports)
        format: "pdf", "html", or "both"
        
    Returns:
        Dictionary with paths to generated files
    """
    output_dir = output_dir or "./reports"
    reports_subdir = os.path.join(output_dir, "reports")
    os.makedirs(reports_subdir, exist_ok=True)

    analysis_mode = (
        config.get("analysis_mode")
        or state.get("analysis_mode")
        or "standard"
    )
    from tradingagents.reporting.report_paths import (
        write_report_html_path,
        write_report_pdf_path,
    )

    results = {}

    if format in ("html", "both"):
        html_path = write_report_html_path(output_dir, ticker, analysis_date, analysis_mode)
        generate_html_report(
            state, ticker, analysis_date, decision, config, duration_seconds, str(html_path)
        )
        results["html"] = str(html_path)
        logger.info("HTML report saved: %s", html_path)

    if format in ("pdf", "both"):
        pdf_path = write_report_pdf_path(output_dir, ticker, analysis_date, analysis_mode)
        try:
            actual_path = generate_pdf_report(
                state, ticker, analysis_date, decision, config, duration_seconds, str(pdf_path)
            )
            results["pdf"] = actual_path
            logger.info("PDF report saved: %s", actual_path)
        except Exception:
            logger.warning(
                "PDF generation failed for %s; HTML report is still available",
                ticker,
                exc_info=True,
            )

    return results
