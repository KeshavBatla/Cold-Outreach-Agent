"""
PDF generation module for formatted cover letters using FPDF and clean typography.
"""

import os
from pathlib import Path
from typing import Optional
from fpdf import FPDF
import config


def text_to_pdf(text: str, output_path: str, title: Optional[str] = None) -> str:
    """
    Render cover letter text into a clean, professional A4 PDF.
    Uses DejaVu Sans TTF fonts if present, with graceful fallback to standard Helvetica.
    """
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.add_page()
    pdf.set_margins(22, 22, 22)
    pdf.set_auto_page_break(auto=True, margin=20)

    # Resolve font files
    font_name = "Helvetica"
    font_reg = Path(config.FONT_REGULAR)
    font_bold = Path(config.FONT_BOLD)

    if font_reg.exists() and font_bold.exists():
        pdf.add_font("DejaVu", "", str(font_reg))
        pdf.add_font("DejaVu", "B", str(font_bold))
        font_name = "DejaVu"

    pdf.set_text_color(17, 17, 17)

    # Header / Title
    if title:
        pdf.set_font(font_name, style="B", size=15)
        pdf.multi_cell(w=0, h=7, text=title, align="L")
        pdf.ln(5)

    # Body Text
    pdf.set_font(font_name, size=10.5)
    paragraphs = text.split("\n\n")
    for para in paragraphs:
        formatted_para = " ".join([line.strip() for line in para.split("\n") if line.strip()])
        if formatted_para:
            pdf.multi_cell(w=0, h=5.2, text=formatted_para, align="L")
            pdf.ln(3.5)

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(output_file))
    return str(output_file)

