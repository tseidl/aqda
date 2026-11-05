"""Read spreadsheets (.csv, .tsv, .xlsx) as a header row plus data rows of strings.

Used by the table import, which turns each row into a document: one column holds the
text to code, the others can become document variables.
"""

import csv
import io
import zipfile
from datetime import date, datetime, time

TABLE_EXTENSIONS = {".csv", ".tsv", ".xlsx"}
# The upload limit applies to the compressed .xlsx; the XML inside may be much larger.
MAX_XLSX_XML_BYTES = 200 * 1024 * 1024
# Rows used to suggest which column holds the text.
SUGGESTION_SAMPLE_ROWS = 200


def table_extension(filename: str) -> str | None:
    """Return the table extension of a filename (e.g. '.csv'), or None for other files."""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext if ext in TABLE_EXTENSIONS else None


def _cell_text(value) -> str:
    """Render a spreadsheet cell as text: integers without '.0', dates without midnight."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _clean_headers(raw: list[str]) -> list[str]:
    """Name empty header cells 'Column N' and make repeated names unique ('name (2)')."""
    headers: list[str] = []
    for i, name in enumerate(raw, start=1):
        base = name.strip() or f"Column {i}"
        candidate, n = base, 2
        while candidate in headers:
            candidate, n = f"{base} ({n})", n + 1
        headers.append(candidate)
    return headers


def _shape(rows: list[list[str]]) -> tuple[list[str], list[list[str]]]:
    """Split off the header row and give every row below it exactly one cell per column."""
    # Trailing empty columns (common in Excel exports) are dropped.
    width = max((len(r) for r in rows), default=0)
    while width and all(len(r) < width or not r[width - 1] for r in rows):
        width -= 1
    if not rows or width == 0:
        raise ValueError("The table is empty.")
    headers = _clean_headers((rows[0] + [""] * width)[:width])
    return headers, [(r + [""] * width)[:width] for r in rows[1:]]


def suggest_skip_rows(body: list[list[str]]) -> int:
    """Rows below the header that hold no responses: 2 for a Qualtrics export, else 0.

    Qualtrics puts the question wording in the second row and import IDs such as
    {"ImportId":"QID1"} in the third.
    """
    if len(body) >= 2 and any(cell.startswith('{"ImportId"') for cell in body[1]):
        return 2
    return 0


def _read_delimited(text: str, ext: str) -> list[list[str]]:
    """Parse CSV/TSV text; for .csv the delimiter (comma, semicolon, or tab) is detected."""
    delimiter = "\t"
    if ext == ".csv":
        try:
            delimiter = csv.Sniffer().sniff(text[:20000], delimiters=",;\t").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    return [[_cell_text(cell) for cell in row] for row in reader]


def xlsx_sheet_names(content: bytes) -> list[str]:
    """List the worksheet names of an .xlsx file."""
    return _open_workbook(content).sheetnames


def _open_workbook(content: bytes):
    """Open an .xlsx read-only, refusing archives that would expand beyond the limit."""
    from openpyxl import load_workbook

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_XLSX_XML_BYTES:
                raise ValueError("The spreadsheet is too large to import.")
    except zipfile.BadZipFile:
        raise ValueError("This is not a readable .xlsx file.")
    return load_workbook(io.BytesIO(content), read_only=True, data_only=True)


def _read_xlsx(content: bytes, sheet: str | None) -> list[list[str]]:
    """Read one worksheet (the first if none is named) as rows of cell text."""
    workbook = _open_workbook(content)
    try:
        if sheet is not None and sheet not in workbook.sheetnames:
            raise ValueError(f"The spreadsheet has no sheet named “{sheet}”.")
        ws = workbook[sheet] if sheet is not None else workbook.worksheets[0]
        rows = [[_cell_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
    finally:
        workbook.close()
    # Drop trailing empty cells per row so _shape can trim unused columns.
    for row in rows:
        while row and not row[-1]:
            row.pop()
    return rows


def read_table(
    filename: str, content: bytes, decode_text, sheet: str | None = None,
    skip_rows: int | None = 0,
) -> tuple[list[str], list[list[str]], int]:
    """Return (column names, data rows, rows skipped) of a .csv, .tsv, or .xlsx upload.

    ``skip_rows`` rows directly below the header are left out (None: as suggested by
    ``suggest_skip_rows``); blank rows are dropped. ``decode_text`` turns CSV bytes into text
    (the document import's encoding detection). Raises ValueError with a researcher-facing
    message when the table cannot be used.
    """
    ext = table_extension(filename)
    if ext is None:
        raise ValueError("Choose a .csv, .tsv, or .xlsx file.")
    rows = _read_xlsx(content, sheet) if ext == ".xlsx" else _read_delimited(decode_text(content), ext)
    headers, body = _shape(rows)
    if skip_rows is None:
        skip_rows = suggest_skip_rows(body)
    data = [r for r in body[max(skip_rows, 0):] if any(cell for cell in r)]
    if not data:
        if skip_rows:
            raise ValueError(f"No rows are left after skipping {skip_rows} below the header.")
        raise ValueError("The table has a header row but no data rows.")
    return headers, data, skip_rows


def suggest_text_column(headers: list[str], rows: list[list[str]]) -> str:
    """Suggest the column with the longest entries on average as the text to code."""
    sample = rows[:SUGGESTION_SAMPLE_ROWS]
    lengths = [sum(len(r[i]) for r in sample) for i in range(len(headers))]
    return headers[max(range(len(headers)), key=lambda i: lengths[i])]
