"""Signed-document PDF generation (fpdf2, pure-Python — no system deps).

Renders a signed Letter of Representation: the agreement body + an electronic-signature
block (typed name, timestamp, IP, method) that records intent for ESIGN/UETA validity.
"""
from __future__ import annotations

from fpdf import FPDF


def _safe(s: str) -> str:
    # fpdf2 core fonts are latin-1 only; replace anything outside it so we never crash.
    return (s or "").encode("latin-1", "replace").decode("latin-1")


def lor_pdf(*, firm: str, client_name: str, lor_text: str, signer_name: str,
            signed_at: str, ip: str | None) -> bytes:
    """Return a signed-LOR PDF as bytes."""
    pdf = FPDF(format="Letter")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(20, 20, 20)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 12, _safe("Letter of Representation"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(110, 110, 110)
    pdf.cell(0, 6, _safe(firm), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    # Body — drop the blank signature placeholder line; render paragraphs.
    body = lor_text.split("Client signature:")[0].strip()
    pdf.set_font("Helvetica", "", 11)
    for para in [p.strip() for p in body.split("\n\n") if p.strip()]:
        if para.upper().startswith("LETTER OF REPRESENTATION"):
            continue  # already the title
        pdf.multi_cell(0, 6, _safe(para))
        pdf.ln(3)

    pdf.ln(6)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(20, pdf.get_y(), 195, pdf.get_y())
    pdf.ln(4)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _safe("Electronically signed"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 6, _safe(
        f"Signed by: {signer_name}\n"
        f"Date / time: {signed_at}\n"
        f"IP address: {ip or 'n/a'}\n"
        f"Method: typed-name electronic signature, executed under the U.S. ESIGN Act / UETA."
    ))
    return bytes(pdf.output())
