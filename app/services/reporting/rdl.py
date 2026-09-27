"""Builds an SSRS report definition (RDL 2016) for one query: a title above a table with one column per output column.

The look comes from a layout template (TEMPLATES); the query and its columns are the caller's.
Every string that reaches the XML goes through ElementTree (escaped), and a literal that would read as an RDL
expression (a leading "=") is written as a quoted string instead, so nothing typed into a heading or default value
is ever evaluated. The RDL carries no credentials: it reaches its data through a shared data source on the server
or a connection string that uses integrated security."""

import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from app.schemas.report import ReportColumn

_NS = "http://schemas.microsoft.com/sqlserver/reporting/2016/01/reportdefinition"
_RD = "http://schemas.microsoft.com/SQLServer/reporting/reportdesigner"
ET.register_namespace("", _NS)
ET.register_namespace("rd", _RD)

# Anything outside XML 1.0's character range makes the document unreadable to SSRS.
_XML_ILLEGAL = re.compile(r"[^\t\n\r\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")

DATA_SOURCE_NAME = "Source"
_DATA_SET_NAME = "Query"
_MARGIN_CM = 1.0


@dataclass(frozen=True)
class TabularTemplate:
    font: str = "Segoe UI"
    font_size: str = "9pt"
    title_size: str = "14pt"
    header_fill: str = "#1F4E79"
    header_color: str = "White"
    band_fill: str = "#F2F2F2"
    border_color: str = "#D9D9D9"
    column_width_cm: float = 3.5
    row_height_cm: float = 0.65
    title_height_cm: float = 1.0
    min_width_cm: float = 8.0
    footer_height_cm: float = 1.0
    footer_text_width_cm: float = 5.0


TEMPLATES = {"tabular": TabularTemplate()}


def _tag(name: str) -> str:
    prefix, _, local = name.rpartition(":")
    return f"{{{_RD}}}{local}" if prefix == "rd" else f"{{{_NS}}}{name}"


def _add(parent: ET.Element, name: str, text: str | None = None) -> ET.Element:
    element = ET.SubElement(parent, _tag(name))
    if text is not None:
        if _XML_ILLEGAL.search(text):
            raise ValueError(f"text contains a control character that a report definition cannot hold: {text[:40]!r}")
        element.text = text
    return element


def _literal(text: str) -> str:
    """`text` as an RDL value that is always read as text: one that starts with "=" would otherwise be an expression."""
    return '="' + text.replace('"', '""') + '"' if text.startswith("=") else text


def _identifier(name: str, taken: set[str]) -> str:
    """A name RDL accepts for a field or textbox (letters, digits, underscore; not starting with a digit), unique in `taken`."""
    base = re.sub(r"\W", "_", name) or "Column"
    if base[0].isdigit():
        base = f"_{base}"
    candidate, n = base, 2
    while candidate.lower() in taken:
        candidate, n = f"{base}_{n}", n + 1
    taken.add(candidate.lower())
    return candidate


def _cm(value: float) -> str:
    return f"{value:.2f}cm"


def _border(style: ET.Element, line: str, color: str | None = None) -> None:
    border = _add(style, "Border")
    if color:
        _add(border, "Color", color)
    _add(border, "Style", line)


def _textbox(
    parent: ET.Element,
    name: str,
    value: str,
    tpl: TabularTemplate,
    *,
    header: bool = False,
    framed: bool = True,
    fill: str | None = None,
    size: str | None = None,
    bold: bool = False,
    align: str = "Left",
    number_format: str | None = None,
    geometry: tuple[float, float, float, float] | None = None,
) -> None:
    box = _add(parent, "Textbox")
    box.set("Name", name)
    _add(box, "CanGrow", "true")
    _add(box, "KeepTogether", "true")
    paragraph = _add(_add(box, "Paragraphs"), "Paragraph")
    run = _add(_add(paragraph, "TextRuns"), "TextRun")
    _add(run, "Value", value)
    run_style = _add(run, "Style")
    _add(run_style, "FontFamily", tpl.font)
    _add(run_style, "FontSize", size or tpl.font_size)
    if header or bold:
        _add(run_style, "FontWeight", "Bold")
    if number_format:
        _add(run_style, "Format", number_format)
    if header:
        _add(run_style, "Color", tpl.header_color)
    _add(_add(paragraph, "Style"), "TextAlign", align)
    _add(box, "rd:DefaultName", name)
    if geometry:
        top, left, height, width = geometry
        _add(box, "Top", _cm(top))
        _add(box, "Left", _cm(left))
        _add(box, "Height", _cm(height))
        _add(box, "Width", _cm(width))
    style = _add(box, "Style")
    if framed:
        _border(style, "Solid", tpl.border_color)
    if fill:
        _add(style, "BackgroundColor", fill)
    for padding in ("PaddingLeft", "PaddingRight", "PaddingTop", "PaddingBottom"):
        _add(style, padding, "3pt")


def _data_source(root: ET.Element, data_source_path: str | None, connection_string: str | None) -> None:
    sources = _add(root, "DataSources")
    source = _add(sources, "DataSource")
    source.set("Name", DATA_SOURCE_NAME)
    if data_source_path:
        _add(source, "DataSourceReference", data_source_path)
    elif connection_string:
        properties = _add(source, "ConnectionProperties")
        _add(properties, "DataProvider", "SQL")
        _add(properties, "ConnectString", connection_string)
        _add(properties, "IntegratedSecurity", "true")
        _add(source, "rd:SecurityType", "Integrated")
    else:
        raise ValueError("no data source: set SSRS_DATA_SOURCE_PATH (a shared data source) or SSRS_CONNECTION_STRING")
    _add(source, "rd:DataSourceID", str(uuid.uuid4()))


def build_rdl(
    *,
    title: str,
    description: str | None = None,
    sql: str,
    columns: list[ReportColumn],
    template: str = "tabular",
    landscape: bool = True,
    data_source_path: str | None = None,
    connection_string: str | None = None,
) -> bytes:
    """The RDL for `sql`, as UTF-8 XML. `columns` are output columns of the SQL (their `name` is the alias). The
    caller has checked them against the SQL, that the SQL reads no @parameters, and that `connection_string` (used
    only when there is no `data_source_path`) holds no credentials."""
    tpl = TEMPLATES[template]
    if not columns:
        raise ValueError("a report needs at least one column")

    root = ET.Element(_tag("Report"))
    if description:
        _add(root, "Description", description)
    _add(root, "AutoRefresh", "0")
    _data_source(root, data_source_path, connection_string)

    # Fields and textboxes need identifier-safe names; the field's DataField is the column's real name.
    taken: set[str] = set()
    field_names = [_identifier(c.name, taken) for c in columns]

    data_sets = _add(root, "DataSets")
    data_set = _add(data_sets, "DataSet")
    data_set.set("Name", _DATA_SET_NAME)
    query = _add(data_set, "Query")
    _add(query, "DataSourceName", DATA_SOURCE_NAME)
    _add(query, "CommandText", sql)
    fields = _add(data_set, "Fields")
    for column, field_name in zip(columns, field_names, strict=True):
        field = _add(fields, "Field")
        field.set("Name", field_name)
        _add(field, "DataField", column.name)

    widths = [c.width_cm or tpl.column_width_cm for c in columns]
    width = sum(widths)
    section_width = max(width, tpl.min_width_cm)  # the title and footer need room even for a one-column table
    table_top = tpl.title_height_cm + 0.3
    row_count = 2  # header + details
    body_height = table_top + row_count * tpl.row_height_cm

    sections = _add(root, "ReportSections")
    section = _add(sections, "ReportSection")
    body = _add(section, "Body")
    items = _add(body, "ReportItems")

    _textbox(
        items, "Title", _literal(title), tpl, framed=False, size=tpl.title_size, bold=True,
        geometry=(0, 0, tpl.title_height_cm, section_width),
    )

    tablix = _add(items, "Tablix")
    tablix.set("Name", "Table")
    tablix_body = _add(tablix, "TablixBody")
    tablix_columns = _add(tablix_body, "TablixColumns")
    for column_width in widths:
        _add(_add(tablix_columns, "TablixColumn"), "Width", _cm(column_width))
    tablix_rows = _add(tablix_body, "TablixRows")

    header_row = _add(tablix_rows, "TablixRow")
    _add(header_row, "Height", _cm(tpl.row_height_cm))
    header_cells = _add(header_row, "TablixCells")
    for column, field_name in zip(columns, field_names, strict=True):
        contents = _add(_add(header_cells, "TablixCell"), "CellContents")
        _textbox(
            contents, f"H_{field_name}", _literal(column.header or column.name), tpl,
            header=True, fill=tpl.header_fill, align=column.align or "Left",
        )

    detail_row = _add(tablix_rows, "TablixRow")
    _add(detail_row, "Height", _cm(tpl.row_height_cm))
    detail_cells = _add(detail_row, "TablixCells")
    band = f'=IIf(RowNumber(Nothing) Mod 2 = 0, "{tpl.band_fill}", "White")'
    for column, field_name in zip(columns, field_names, strict=True):
        contents = _add(_add(detail_cells, "TablixCell"), "CellContents")
        # "General" left-aligns text and right-aligns numbers, which is right when the column's type is unknown.
        _textbox(
            contents, f"D_{field_name}", f"=Fields!{field_name}.Value", tpl,
            fill=band, align=column.align or "General", number_format=column.format,
        )

    column_members = _add(_add(tablix, "TablixColumnHierarchy"), "TablixMembers")
    for _ in columns:
        _add(column_members, "TablixMember")
    row_members = _add(_add(tablix, "TablixRowHierarchy"), "TablixMembers")
    header_member = _add(row_members, "TablixMember")
    _add(header_member, "KeepWithGroup", "After")
    _add(header_member, "RepeatOnNewPage", "true")
    _add(header_member, "KeepTogether", "true")
    _add(_add(row_members, "TablixMember"), "Group").set("Name", "Details")

    _add(tablix, "DataSetName", _DATA_SET_NAME)
    _add(tablix, "Top", _cm(table_top))
    _add(tablix, "Left", _cm(0))
    _add(tablix, "Height", _cm(row_count * tpl.row_height_cm))
    _add(tablix, "Width", _cm(width))
    _border(_add(tablix, "Style"), "None")

    _add(body, "Height", _cm(body_height))
    _border(_add(body, "Style"), "None")

    # The page is at least A4; a wide table makes it wider so columns are not split across pages.
    a4_long, a4_short = 29.7, 21.0
    page_width = max(a4_long if landscape else a4_short, section_width + 2 * _MARGIN_CM)
    page_height = a4_short if landscape else a4_long
    _add(section, "Width", _cm(section_width))
    page = _add(section, "Page")
    footer = _add(page, "PageFooter")
    _add(footer, "Height", _cm(tpl.footer_height_cm))
    _add(footer, "PrintOnFirstPage", "true")
    _add(footer, "PrintOnLastPage", "true")
    _textbox(
        _add(footer, "ReportItems"), "ExecutionTime", "=Globals!ExecutionTime", tpl, framed=False, align="Right",
        geometry=(0.3, page_width - 2 * _MARGIN_CM - tpl.footer_text_width_cm, tpl.footer_height_cm - 0.3, tpl.footer_text_width_cm),
    )
    _border(_add(footer, "Style"), "None")
    _add(page, "PageHeight", _cm(page_height))
    _add(page, "PageWidth", _cm(page_width))
    for margin in ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin"):
        _add(page, margin, _cm(_MARGIN_CM))
    _add(page, "ColumnSpacing", "0.13cm")
    _add(page, "Style")

    _add(root, "rd:ReportUnitType", "Cm")
    _add(root, "rd:ReportID", str(uuid.uuid4()))

    ET.indent(root)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
