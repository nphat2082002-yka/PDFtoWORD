"""Layer 6 OpenXML DOCX synthesis from `ADRTPageDocument` structures.

Modeled on ABBYY FineReader 16 Stage 5 (`Export.Common.dll`, `export.wp.dll`):
- Calibrates Word `section` page dimensions, orientation, and margins (`w:sectPr`).
- Routes top/bottom 6% running headers & footers to `section.header` / `section.footer`.
- Emits real `<w:p>` paragraphs with `w:jc`, `w:ind`, `w:spacing`, `w:sz`, `w:b`, and
  `Times New Roman` (`w:rFonts`) in precomposed Unicode NFC.
- Renders side-by-side 2-column blocks (`ParallelColumnsBlock`) as borderless
  invisible `<w:tbl>` (`w:val="nil"`) with independent cell alignment.
- Renders structured data tables (`ReconstructedTable`) with `<w:tblBorders>` (`single`),
  proportional column widths, and merged cells (`<w:gridSpan>`, `<w:vMerge>`).
- Embeds non-text figures (`FigureBlock`) inline with accurate point dimensions.
"""

from __future__ import annotations

import io
from typing import Iterable

from docx import Document
from docx.enum.section import WD_ORIENT, WD_SECTION
from docx.enum.table import WD_ALIGN_VERTICAL, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import nsmap, pfxmap, qn
from docx.shared import Pt, RGBColor

from .layout_engine import (
    ADRTPageDocument,
    FigureBlock,
    FloatingTextBox,
    PageGeometry,
    ParallelColumnsBlock,
    ReconstructedTable,
    SynthesizedParagraph,
)
from .vietnamese import normalize_vietnamese_nfc

if "v" not in nsmap:
    nsmap["v"] = "urn:schemas-microsoft-com:vml"
    pfxmap["urn:schemas-microsoft-com:vml"] = "v"

_DEFAULT_VIET_FONT = "Times New Roman"

_ALIGN_MAP = {
    "LEFT": WD_ALIGN_PARAGRAPH.LEFT,
    "CENTER": WD_ALIGN_PARAGRAPH.CENTER,
    "RIGHT": WD_ALIGN_PARAGRAPH.RIGHT,
    "JUSTIFY": WD_ALIGN_PARAGRAPH.JUSTIFY,
}


def _set_run_font(
    run,
    font_name: str,
    font_size_pt: float,
    is_bold: bool,
    is_heading: bool = False,
    is_italic: bool = False,
) -> None:
    """Configure ASCII + hAnsi + ComplexScript font, size (`w:sz`/`w:szCs`), bold, and italic."""
    run.font.name = font_name
    run.font.size = Pt(font_size_pt)
    run.bold = is_bold
    run.italic = is_italic
    if is_heading:
        run.font.color.rgb = RGBColor(0, 0, 0)

    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, r_fonts)
    r_fonts.set(qn("w:ascii"), font_name)
    r_fonts.set(qn("w:hAnsi"), font_name)
    r_fonts.set(qn("w:cs"), font_name)

    half_pts = str(max(2, int(round(font_size_pt * 2.0))))
    for tag in ("w:sz", "w:szCs"):
        sz_el = r_pr.find(qn(tag))
        if sz_el is None:
            sz_el = OxmlElement(tag)
            r_pr.append(sz_el)
        sz_el.set(qn("w:val"), half_pts)


def _apply_tbl_borders(table, val: str, sz: str = "4", color: str = "333333") -> None:
    """Apply explicit `<w:tblBorders>` (`single` or `none`) and replace existing border XML."""
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblBorders"))
    if existing is not None:
        tbl_pr.remove(existing)

    is_hidden = val in ("nil", "none")
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{side}")
        if is_hidden:
            el.set(qn("w:val"), "none")
            el.set(qn("w:sz"), "0")
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "auto")
        else:
            el.set(qn("w:val"), val)
            el.set(qn("w:sz"), sz)
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), color)
        borders.append(el)
    tbl_pr.append(borders)


def _apply_cell_borders(cell, val: str, sz: str = "4", color: str = "333333") -> None:
    """Apply explicit `<w:tcBorders>` on an individual table cell."""
    tc_pr = cell._tc.get_or_add_tcPr()
    existing = tc_pr.find(qn("w:tcBorders"))
    if existing is not None:
        tc_pr.remove(existing)

    is_hidden = val in ("nil", "none")
    borders = OxmlElement("w:tcBorders")
    for side in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{side}")
        if is_hidden:
            el.set(qn("w:val"), "none")
            el.set(qn("w:sz"), "0")
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), "auto")
        else:
            el.set(qn("w:val"), val)
            el.set(qn("w:sz"), sz)
            el.set(qn("w:space"), "0")
            el.set(qn("w:color"), color)
        borders.append(el)
    tc_pr.append(borders)


def _set_table_cell_margins(table, top: int = 40, bottom: int = 40, left: int = 80, right: int = 80) -> None:
    """Set `<w:tblCellMar>` in twips for clean spacing inside tables."""
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblCellMar"))
    if existing is not None:
        tbl_pr.remove(existing)
    mar = OxmlElement("w:tblCellMar")
    for side, val in (("top", top), ("left", left), ("bottom", bottom), ("right", right)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        mar.append(el)
    tbl_pr.append(mar)


def _set_table_width_pt(table, width_pt: float) -> None:
    tbl_pr = table._tbl.tblPr
    existing = tbl_pr.find(qn("w:tblW"))
    if existing is not None:
        tbl_pr.remove(existing)
    tbl_w = OxmlElement("w:tblW")
    tbl_w.set(qn("w:w"), str(max(100, int(round(width_pt * 20)))))
    tbl_w.set(qn("w:type"), "dxa")
    tbl_pr.append(tbl_w)


class DocxStructureSynthesizer:
    """Synthesizes clean OpenXML `.docx` documents from `ADRTPageDocument` sequences."""

    def __init__(self, default_font: str = _DEFAULT_VIET_FONT) -> None:
        self.default_font = default_font

    def configure_section(self, section, geometry: PageGeometry) -> None:
        """Apply calibrated page size, orientation, and safe margins to a Word section."""
        section.orientation = WD_ORIENT.LANDSCAPE if geometry.is_landscape else WD_ORIENT.PORTRAIT
        section.page_width = Pt(geometry.width_pt)
        section.page_height = Pt(geometry.height_pt)
        section.top_margin = Pt(geometry.top_margin_pt)
        section.bottom_margin = Pt(geometry.bottom_margin_pt)
        section.left_margin = Pt(geometry.left_margin_pt)
        section.right_margin = Pt(geometry.right_margin_pt)
        section.header_distance = Pt(max(12.0, geometry.top_margin_pt * 0.45))
        section.footer_distance = Pt(max(12.0, geometry.bottom_margin_pt * 0.45))

    def render_paragraph_into_container(
        self,
        container,
        para: SynthesizedParagraph,
        doc: Document,
        reuse_first_empty: bool = False,
        allow_heading_style: bool = True,
    ):
        """Render a `SynthesizedParagraph` into a document, table cell, header, or footer."""
        text = normalize_vietnamese_nfc(para.text, collapse_spaces=True).strip()
        if not text:
            return None

        if reuse_first_empty and container.paragraphs and not container.paragraphs[0].text.strip():
            p = container.paragraphs[0]
        else:
            p = container.add_paragraph()

        if allow_heading_style and para.is_heading and para.heading_level in (1, 2):
            p.style = doc.styles[f"Heading {para.heading_level}"]

        pf = p.paragraph_format
        pf.alignment = _ALIGN_MAP.get(para.alignment, WD_ALIGN_PARAGRAPH.LEFT)
        pf.line_spacing = para.line_spacing
        pf.space_before = Pt(max(0.0, para.space_before_pt))
        pf.space_after = Pt(max(0.0, para.space_after_pt))
        if para.left_indent_pt > 0.0:
            pf.left_indent = Pt(para.left_indent_pt)
        right_indent_pt = getattr(para, "right_indent_pt", 0.0)
        if right_indent_pt > 0.0:
            pf.right_indent = Pt(right_indent_pt)
        if para.first_line_indent_pt > 0.0:
            pf.first_line_indent = Pt(para.first_line_indent_pt)

        if getattr(para, "has_bottom_border", False):
            p_pr = p._p.get_or_add_pPr()
            old_p_bdr = p_pr.find(qn("w:pBdr"))
            if old_p_bdr is not None:
                p_pr.remove(old_p_bdr)
            p_bdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "000000")
            p_bdr.append(bottom)
            p_pr.append(p_bdr)

        is_full_bold = bool(para.is_bold or para.is_heading)
        is_italic = bool(getattr(para, "is_italic", False))
        bold_prefix_len = int(getattr(para, "bold_prefix_len", 0) or 0)
        if not is_full_bold and 0 < bold_prefix_len < len(text):
            prefix_run = p.add_run(text[:bold_prefix_len])
            _set_run_font(
                prefix_run,
                font_name=self.default_font,
                font_size_pt=para.font_size_pt,
                is_bold=True,
                is_heading=para.is_heading,
                is_italic=is_italic,
            )
            rest_run = p.add_run(text[bold_prefix_len:])
            _set_run_font(
                rest_run,
                font_name=self.default_font,
                font_size_pt=para.font_size_pt,
                is_bold=False,
                is_heading=para.is_heading,
                is_italic=is_italic,
            )
        else:
            run = p.add_run(text)
            _set_run_font(
                run,
                font_name=self.default_font,
                font_size_pt=para.font_size_pt,
                is_bold=is_full_bold,
                is_heading=para.is_heading,
                is_italic=is_italic,
            )
        return p

    def render_parallel_columns_block(
        self,
        doc: Document,
        block: ParallelColumnsBlock,
        geometry: PageGeometry,
    ) -> None:
        """Render a 2-column parallel block as a borderless layout table (`w:val="nil"`)."""
        table = doc.add_table(rows=1, cols=2)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        table.autofit = False

        _apply_tbl_borders(table, val="nil")
        _set_table_cell_margins(table, top=20, bottom=20, left=40, right=40)

        avail_w = max(200.0, geometry.content_width_pt)
        raw_total = max(1.0, block.left_width_pt + block.right_width_pt)
        left_w = max(72.0, avail_w * (block.left_width_pt / raw_total))
        right_w = max(72.0, avail_w - left_w)
        _set_table_width_pt(table, left_w + right_w)

        row = table.rows[0]
        left_cell, right_cell = row.cells[0], row.cells[1]
        left_cell.width = Pt(left_w)
        right_cell.width = Pt(right_w)
        left_cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP
        right_cell.vertical_alignment = WD_ALIGN_VERTICAL.TOP
        _apply_cell_borders(left_cell, val="nil")
        _apply_cell_borders(right_cell, val="nil")

        for idx, para in enumerate(block.left_paragraphs):
            self.render_paragraph_into_container(
                left_cell,
                para,
                doc=doc,
                reuse_first_empty=(idx == 0),
                allow_heading_style=False,
            )

        right_fig = getattr(block, "right_figure", None)
        after_idx = getattr(block, "right_figure_after_idx", -1)
        fig_inserted = False

        def _insert_right_figure(reuse_empty: bool) -> None:
            if right_fig is None or not right_fig.image_png_bytes:
                return
            if reuse_empty and right_cell.paragraphs and not right_cell.paragraphs[0].text.strip():
                fp = right_cell.paragraphs[0]
            else:
                fp = right_cell.add_paragraph()
            fp.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
            fp.paragraph_format.space_before = Pt(2)
            fp.paragraph_format.space_after = Pt(2)
            target_w = min(right_fig.width_pt, right_w * 0.85, 135.0)
            fp.add_run().add_picture(io.BytesIO(right_fig.image_png_bytes), width=Pt(target_w))

        if right_fig is not None and after_idx < 0 and not block.right_paragraphs:
            _insert_right_figure(reuse_empty=True)
            fig_inserted = True

        for idx, para in enumerate(block.right_paragraphs):
            self.render_paragraph_into_container(
                right_cell,
                para,
                doc=doc,
                reuse_first_empty=(idx == 0 and not fig_inserted),
                allow_heading_style=False,
            )
            if right_fig is not None and not fig_inserted and idx == after_idx:
                _insert_right_figure(reuse_empty=False)
                fig_inserted = True

        if right_fig is not None and not fig_inserted:
            _insert_right_figure(reuse_empty=(len(block.right_paragraphs) == 0))

    def render_reconstructed_table(
        self,
        doc: Document,
        table_data: ReconstructedTable,
        geometry: PageGeometry,
    ) -> None:
        """Render a bordered data table with proportional column widths and merged cells."""
        if table_data.rows_count <= 0 or table_data.cols_count <= 0 or not table_data.cells:
            return

        word_table = doc.add_table(rows=table_data.rows_count, cols=table_data.cols_count)
        word_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        word_table.autofit = False

        border_val = "single" if table_data.has_borders else "nil"
        _apply_tbl_borders(word_table, val=border_val, sz="4", color="333333")
        _set_table_cell_margins(word_table, top=40, bottom=40, left=80, right=80)

        # Normalize column widths to fit page content width cleanly
        avail_w = max(180.0, geometry.content_width_pt)
        raw_total_w = max(1.0, sum(table_data.col_widths_pt))
        scale_w = min(1.0, avail_w / raw_total_w) if raw_total_w > avail_w else 1.0
        scaled_col_widths = [max(24.0, w * scale_w) for w in table_data.col_widths_pt]
        _set_table_width_pt(word_table, sum(scaled_col_widths))

        # Apply base borders and widths across grid cells first
        for r in range(table_data.rows_count):
            for c in range(table_data.cols_count):
                wc = word_table.cell(r, c)
                wc.width = Pt(scaled_col_widths[c])
                _apply_cell_borders(wc, val=border_val, sz="4", color="333333")

        # Merge cells and populate anchor content
        for cell_box in table_data.cells:
            r0 = cell_box.row
            c0 = cell_box.col
            r1 = min(table_data.rows_count - 1, r0 + max(1, cell_box.row_span) - 1)
            c1 = min(table_data.cols_count - 1, c0 + max(1, cell_box.col_span) - 1)

            anchor_cell = word_table.cell(r0, c0)
            if r1 > r0 or c1 > c0:
                anchor_cell.merge(word_table.cell(r1, c1))

            merged_w = sum(scaled_col_widths[c] for c in range(c0, c1 + 1))
            anchor_cell.width = Pt(merged_w)
            anchor_cell.vertical_alignment = WD_ALIGN_VERTICAL.CENTER
            _apply_cell_borders(anchor_cell, val=border_val, sz="4", color="333333")

            if cell_box.paragraphs:
                for p_idx, para in enumerate(cell_box.paragraphs):
                    self.render_paragraph_into_container(
                        anchor_cell,
                        para,
                        doc=doc,
                        reuse_first_empty=(p_idx == 0),
                        allow_heading_style=False,
                    )
            elif cell_box.text.strip():
                fallback_para = SynthesizedParagraph(
                    bbox=cell_box.bbox,
                    text=cell_box.text,
                    alignment="LEFT",
                    font_size_pt=10.5,
                    is_bold=(r0 == 0),
                    space_before_pt=0.0,
                    space_after_pt=2.0,
                )
                self.render_paragraph_into_container(
                    anchor_cell,
                    fallback_para,
                    doc=doc,
                    reuse_first_empty=True,
                    allow_heading_style=False,
                )

    def render_figure_block(self, doc: Document, fig: FigureBlock, geometry: PageGeometry) -> None:
        """Insert a non-text image block at its document flow position."""
        if not fig.image_png_bytes:
            return
        max_w = max(72.0, geometry.content_width_pt)
        target_w = min(fig.width_pt, max_w)
        p = doc.add_paragraph()
        p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(4)
        run = p.add_run()
        run.add_picture(io.BytesIO(fig.image_png_bytes), width=Pt(target_w))

    def render_floating_textbox(
        self, doc: Document, fbox: FloatingTextBox, geometry: PageGeometry
    ) -> None:
        """Render a floating stamp/tag as an independent VML `<w:pict><v:shape>` TextBox."""
        text = normalize_vietnamese_nfc(fbox.text, collapse_spaces=True).strip()
        if not text:
            return

        x0, y0, x1, y1 = fbox.bbox
        raw_w = max(24.0, x1 - x0)
        raw_h = max(14.0, y1 - y0)
        box_w = max(raw_w + 14.0, 48.0)
        box_h = max(raw_h + 8.0, fbox.font_size_pt * 1.6 + 6.0)
        left_pt = max(0.0, min(x0 - 4.0, geometry.width_pt - box_w - 6.0))
        top_pt = max(6.0, y0 - 2.0)

        anchor_p = doc.add_paragraph()
        anchor_pf = anchor_p.paragraph_format
        anchor_pf.space_before = Pt(0)
        anchor_pf.space_after = Pt(0)
        anchor_pf.line_spacing = Pt(1)
        ap_pr = anchor_p._p.get_or_add_pPr()
        ap_rpr = OxmlElement("w:rPr")
        for tag in ("w:sz", "w:szCs"):
            sz_el = OxmlElement(tag)
            sz_el.set(qn("w:val"), "2")
            ap_rpr.append(sz_el)
        ap_pr.append(ap_rpr)

        anchor_r = anchor_p.add_run()
        anchor_r.font.size = Pt(1)

        pict = OxmlElement("w:pict")
        shape = OxmlElement("v:shape")
        shape.set("id", f"FloatingBox_{id(fbox)}")
        shape.set("type", "#_x0000_t202")
        shape.set(
            "style",
            (
                f"position:absolute;margin-left:{left_pt:.2f}pt;margin-top:{top_pt:.2f}pt;"
                f"width:{box_w:.2f}pt;height:{box_h:.2f}pt;z-index:251658240;"
                "mso-position-horizontal-relative:page;mso-position-vertical-relative:page;"
                "v-text-anchor:middle"
            ),
        )
        shape.set("stroked", "t" if fbox.has_border else "f")
        shape.set("strokeweight", "1pt" if fbox.has_border else "0pt")
        shape.set("strokecolor", "#000000")
        shape.set("filled", "f")

        textbox = OxmlElement("v:textbox")
        textbox.set("inset", "2pt,2pt,2pt,2pt")

        txbx = OxmlElement("w:txbxContent")
        tp = OxmlElement("w:p")
        tp_pr = OxmlElement("w:pPr")
        spacing = OxmlElement("w:spacing")
        spacing.set(qn("w:before"), "0")
        spacing.set(qn("w:after"), "0")
        spacing.set(qn("w:line"), "240")
        spacing.set(qn("w:lineRule"), "auto")
        tp_pr.append(spacing)

        jc_map = {"LEFT": "left", "CENTER": "center", "RIGHT": "right", "JUSTIFY": "both"}
        jc = OxmlElement("w:jc")
        jc.set(qn("w:val"), jc_map.get(fbox.alignment.upper(), "center"))
        tp_pr.append(jc)
        tp.append(tp_pr)

        tr = OxmlElement("w:r")
        tr_pr = OxmlElement("w:rPr")
        r_fonts = OxmlElement("w:rFonts")
        r_fonts.set(qn("w:ascii"), self.default_font)
        r_fonts.set(qn("w:hAnsi"), self.default_font)
        r_fonts.set(qn("w:cs"), self.default_font)
        tr_pr.append(r_fonts)
        if fbox.is_bold:
            tr_pr.append(OxmlElement("w:b"))
        if fbox.is_italic:
            tr_pr.append(OxmlElement("w:i"))
        color = OxmlElement("w:color")
        color.set(qn("w:val"), "000000")
        tr_pr.append(color)
        half_pts = str(max(2, int(round(fbox.font_size_pt * 2.0))))
        for tag in ("w:sz", "w:szCs"):
            sz_el = OxmlElement(tag)
            sz_el.set(qn("w:val"), half_pts)
            tr_pr.append(sz_el)
        tr.append(tr_pr)

        wt = OxmlElement("w:t")
        wt.text = text
        tr.append(wt)
        tp.append(tr)
        txbx.append(tp)
        textbox.append(txbx)
        shape.append(textbox)
        pict.append(shape)
        anchor_r._element.append(pict)

    def append_page(
        self,
        doc: Document,
        page_doc: ADRTPageDocument,
        is_first_page: bool = True,
    ) -> None:
        """Append a single `ADRTPageDocument` into an open `python-docx` Document."""
        if is_first_page:
            section = doc.sections[0]
            self.configure_section(section, page_doc.geometry)
        else:
            prev_sec = doc.sections[-1]
            same_size = (
                abs(prev_sec.page_width.pt - page_doc.geometry.width_pt) <= 4.0
                and abs(prev_sec.page_height.pt - page_doc.geometry.height_pt) <= 4.0
            )
            if same_size:
                doc.add_page_break()
                section = prev_sec
            else:
                section = doc.add_section(WD_SECTION.NEW_PAGE)
                self.configure_section(section, page_doc.geometry)

        # Render running header & footer into the section (first time per section)
        if page_doc.header_paragraphs and not any(p.text.strip() for p in section.header.paragraphs):
            section.header.is_linked_to_previous = False
            for idx, hp in enumerate(page_doc.header_paragraphs):
                hp.space_before_pt = 0.0
                hp.space_after_pt = 0.0
                self.render_paragraph_into_container(
                    section.header,
                    hp,
                    doc=doc,
                    reuse_first_empty=(idx == 0),
                    allow_heading_style=False,
                )

        if page_doc.footer_paragraphs and not any(p.text.strip() for p in section.footer.paragraphs):
            section.footer.is_linked_to_previous = False
            for idx, fp in enumerate(page_doc.footer_paragraphs):
                fp.space_before_pt = 0.0
                fp.space_after_pt = 0.0
                self.render_paragraph_into_container(
                    section.footer,
                    fp,
                    doc=doc,
                    reuse_first_empty=(idx == 0),
                    allow_heading_style=False,
                )

        # Render body elements in top-to-bottom order
        for el in page_doc.body_elements:
            if isinstance(el, ParallelColumnsBlock):
                self.render_parallel_columns_block(doc, el, page_doc.geometry)
            elif isinstance(el, ReconstructedTable):
                self.render_reconstructed_table(doc, el, page_doc.geometry)
            elif isinstance(el, FigureBlock):
                self.render_figure_block(doc, el, page_doc.geometry)
            elif isinstance(el, FloatingTextBox):
                self.render_floating_textbox(doc, el, page_doc.geometry)
            elif isinstance(el, SynthesizedParagraph):
                self.render_paragraph_into_container(doc, el, doc=doc, allow_heading_style=True)

    def synthesize(self, pages: Iterable[ADRTPageDocument], output_docx: str | None = None) -> Document:
        """Build a complete `Document` from `ADRTPageDocument` pages and optionally save it."""
        doc = Document()
        for idx, page_doc in enumerate(pages):
            self.append_page(doc, page_doc, is_first_page=(idx == 0))
        if output_docx is not None:
            doc.save(output_docx)
        return doc
