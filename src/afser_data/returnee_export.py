"""Narrow, anonymous projection and XLSX export for returned students."""

import hashlib
import hmac
import json
import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape


HEADERS = (
    "Anonymer Schlüssel",
    "Austauschjahr",
    "AFS-Seminar 1",
    "AFS-Seminar 2",
    "Alle Camps absolviert",
)
NOTE = (
    "Ein Seminar gilt als absolviert, wenn das entsprechende AFS-Seminarfeld "
    "in AFSer ausgefüllt ist. Nur mit diesem AFSer-Zugang erreichbare Returnees."
)
NO_VALUES = frozenset({"0", "false", "no", "nein", "n", "none", "null"})


def _year(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1900 <= value <= 2100:
        return value
    if isinstance(value, str) and re.fullmatch(r"20\d{2}", value.strip()):
        return int(value.strip())
    return None


def _recorded(value):
    """The confirmed source fields are populated strings, not booleans."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and text.casefold() not in NO_VALUES
    return False


def project_returnees(db, snapshot, salt):
    """Read only status, program year, and the two explicitly approved fields."""
    records = []
    seen = set()
    for source_id, raw in db.execute(
        "SELECT source_id,payload FROM raw_records "
        "WHERE snapshot=? AND entity_type='getAllStudents' ORDER BY source_id",
        (snapshot,),
    ):
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("Status__c") != "Returned":
            continue
        opaque_id = hmac.new(
            salt,
            ("returnees:" + str(source_id)).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:20]
        if opaque_id in seen:
            continue
        seen.add(opaque_id)
        seminar1 = _recorded(payload.get("AFS_Seminar_1__c"))
        seminar2 = _recorded(payload.get("AFS_Seminar_2__c"))
        records.append(
            {
                "id": opaque_id,
                "year": _year(payload.get("Program_Year__c")),
                "seminar1Completed": seminar1,
                "seminar2Completed": seminar2,
                "allCampsCompleted": seminar1 and seminar2,
            }
        )
    return sorted(records, key=lambda row: (row["year"] is None, -(row["year"] or 0), row["id"]))


def _cell(reference, value, style=None):
    style_attr = f' s="{style}"' if style is not None else ""
    if value is None:
        return f'<c r="{reference}"{style_attr}/>'
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attr}><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"{style_attr}><v>{value}</v></c>'
    text = escape(str(value), {'"': "&quot;"})
    return f'<c r="{reference}" t="inlineStr"{style_attr}><is><t>{text}</t></is></c>'


def write_returnees_xlsx(path: Path, records, updated_at):
    """Write a dependency-free Open XML workbook with a filterable Excel table."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    last_row = 5 + len(records)
    rows = [
        '<row r="1">' + _cell("A1", "AFS Returnees", 1) + "</row>",
        '<row r="2">' + _cell("A2", "Datenstand: " + str(updated_at), 2) + "</row>",
        '<row r="3">' + _cell("A3", NOTE, 2) + "</row>",
        '<row r="5">'
        + "".join(_cell(f"{col}5", value, 3) for col, value in zip("ABCDE", HEADERS))
        + "</row>",
    ]
    for index, item in enumerate(records, start=6):
        values = (
            item["id"],
            item["year"],
            "Absolviert" if item["seminar1Completed"] else "Nicht eingetragen",
            "Absolviert" if item["seminar2Completed"] else "Nicht eingetragen",
            "Ja" if item["allCampsCompleted"] else "Nein",
        )
        rows.append(
            f'<row r="{index}">'
            + "".join(_cell(f"{col}{index}", value) for col, value in zip("ABCDE", values))
            + "</row>"
        )

    sheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<dimension ref="A1:E{last_row}"/>'
        '<sheetViews><sheetView workbookViewId="0" showGridLines="0">'
        '<pane ySplit="5" topLeftCell="A6" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="18"/>'
        '<cols><col min="1" max="1" width="24" customWidth="1"/>'
        '<col min="2" max="2" width="16" customWidth="1"/>'
        '<col min="3" max="4" width="24" customWidth="1"/>'
        '<col min="5" max="5" width="26" customWidth="1"/></cols>'
        '<sheetData>' + "".join(rows) + "</sheetData>"
        f'<tableParts count="1"><tablePart r:id="rId1"/></tableParts></worksheet>'
    )
    table_columns = "".join(
        '<tableColumn id="{}" name="{}"/>'.format(index, escape(name, {'"': "&quot;"}))
        for index, name in enumerate(HEADERS, 1)
    )
    table = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'id="1" name="ReturneesTable" displayName="ReturneesTable" ref="A5:E{last_row}" '
        'totalsRowShown="0"><autoFilter ref="A5:E' + str(last_row) + '"/>'
        f'<tableColumns count="5">{table_columns}</tableColumns>'
        '<tableStyleInfo name="TableStyleMedium2" showFirstColumn="0" showLastColumn="0" '
        'showRowStripes="1" showColumnStripes="0"/></table>'
    )
    parts = {
        "[Content_Types].xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
            '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/tables/table1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
            '</Relationships>'
        ),
        "docProps/core.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>AFS</dc:creator>'
            '<cp:lastModifiedBy>AFS</cp:lastModifiedBy></cp:coreProperties>'
        ),
        "docProps/app.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
            'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
            '<Application>AFS Returnee Export</Application></Properties>'
        ),
        "xl/workbook.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Returnees" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>'
        ),
        "xl/styles.xml": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="2"><font><sz val="10"/><name val="Arial"/></font>'
            '<font><b/><sz val="14"/><name val="Arial"/><color rgb="FF102B46"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill>'
            '<fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
            '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="center"/></xf>'
            '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>'
            '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
        ),
        "xl/worksheets/sheet1.xml": sheet,
        "xl/worksheets/_rels/sheet1.xml.rels": (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table1.xml"/>'
            '</Relationships>'
        ),
        "xl/tables/table1.xml": table,
    }
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as workbook:
        for name, content in parts.items():
            workbook.writestr(name, content)
    return path
