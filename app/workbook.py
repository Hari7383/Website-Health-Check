"""Reading the checklist from, and writing results back into, the hygiene workbook.

The sheet is laid out as a wide grid: columns A and B hold Category and
Checklist Item, and every check run adds a three-column block (Status,
Checked By, Notes) under a merged date cell in row 1.

Writes are additive. The tool only ever fills the three cells of one date
block per row, and takes a timestamped backup before saving over the original.
"""
import datetime
import shutil
from copy import copy
from pathlib import Path

import openpyxl
from openpyxl.utils import get_column_letter

from .config import BLOCK_WIDTH, DATE_ROW, FIRST_ITEM_ROW, HEADER_ROW


def _date_columns(ws) -> dict:
    """date -> starting column, for every dated block in row 1."""
    out = {}
    for col in range(3, ws.max_column + 1):
        value = ws.cell(DATE_ROW, col).value
        if isinstance(value, datetime.datetime):
            out[value.date()] = col
        elif isinstance(value, datetime.date):
            out[value] = col
    return out


def infer_check_id(item: str) -> str | None:
    """Map common checklist language to a safe existing browser check.

    The workbook text remains the source of truth. This mapping only selects
    an implementation; it never changes the checklist wording. Unknown text
    is deliberately left unmapped instead of being falsely marked Pass.
    """
    text = " ".join(item.lower().split())
    rules = [
        (("siem", "calculator"), "siem_calculator"),
        (("phone", "call"), "click_to_call"),
        (("click-to-call",), "click_to_call"),
        (("calendly",), "calendly"),
        (("schedule", "booking"), "calendly"),
        (("contact", "form"), "contact_form"),
        (("get in touch",), "get_in_touch_form"),
        (("red checklist",), "red_checklist"),
        (("cta",), "cta_buttons"),
        (("call-to-action",), "cta_buttons"),
        (("landing page",), "landing_pages"),
        (("page load",), "page_speed"),
        (("load speed",), "page_speed"),
        (("mobile", "responsive"), "mobile_responsive"),
        (("navigation",), "nav_menus"),
        (("menu",), "nav_menus"),
        (("cookie",), "cookie_banner"),
        (("privacy", "consent"), "cookie_banner"),
        (("spelling",), "spelling"),
        (("grammar",), "spelling"),
        (("images",), "images_media"),
        (("media", "load"), "images_media"),
    ]
    for keywords, check_id in rules:
        if all(k in text for k in keywords):
            return check_id
    return None


def read_checklist(path) -> list:
    """Read the uploaded workbook's actual checklist.

    The ``Checklist Item`` header is the source of truth: every non-empty
    cell below that header becomes one scenario to test.  Category is optional
    and is read from a neighbouring ``Category`` column when present.
    """
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active

    headers = {}
    for col in range(1, ws.max_column + 1):
        value = ws.cell(1, col).value or ws.cell(2, col).value
        if value is None:
            continue
        normalized = " ".join(str(value).strip().lower().split())
        if normalized:
            headers[normalized] = col

    item_col = headers.get("checklist item")
    if item_col is None:
        # Search all cells in the first 10 rows in case the workbook has a
        # title row above its real header.
        for row in range(1, min(ws.max_row, 10) + 1):
            for col in range(1, ws.max_column + 1):
                value = ws.cell(row, col).value
                if value is not None and " ".join(str(value).strip().lower().split()) == "checklist item":
                    item_col = col
                    header_row = row
                    break
            if item_col is not None:
                break
        else:
            header_row = None
    else:
        header_row = 1 if ws.cell(1, item_col).value is not None else 2

    if item_col is None:
        wb.close()
        raise ValueError('Uploaded workbook must contain a "Checklist Item" header.')

    category_col = headers.get("category")
    rows = []
    for row in range(header_row + 1, ws.max_row + 1):
        value = ws.cell(row, item_col).value
        if value is None or not str(value).strip():
            continue
        item = str(value).strip()
        category = (
            str(ws.cell(row, category_col).value).strip()
            if category_col and ws.cell(row, category_col).value is not None
            else "Website Health"
        )
        check_id = infer_check_id(item)
        mode = "manual" if check_id == "spelling" else ("auto" if check_id else "manual")
        rows.append({
            "row": row,
            "id": check_id,
            "category": category,
            "item": item,
            "mode": mode,
        })

    wb.close()
    if not rows:
        raise ValueError('The "Checklist Item" column contains no checklist items.')
    return rows


def describe(path) -> dict:
    """Summary of the uploaded workbook using its actual Checklist Item rows."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    cols = _date_columns(ws)
    dates = sorted(cols)

    checklist = read_checklist(path)
    item_rows = [r["row"] for r in checklist]
    populated = []
    for d in dates:
        col = cols[d]
        if any(ws.cell(r, col).value for r in item_rows):
            populated.append(d)

    info = {
        "sheet": ws.title,
        "items": len(checklist),
        "blocks": len(dates),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "last_completed": populated[-1] if populated else None,
        "completed_runs": len(populated),
    }
    wb.close()
    return info


def last_run_for(path, on_date):
    """Existing Status/Checked By/Notes for a date, or None if empty."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    col = _date_columns(ws).get(on_date)
    if col is None:
        wb.close()
        return None
    rows = read_checklist(path)
    out = {}
    for entry in rows:
        row = entry["row"]
        out[row] = {
            "status": ws.cell(row, col).value,
            "checked_by": ws.cell(row, col + 1).value,
            "notes": ws.cell(row, col + 2).value,
        }
    wb.close()
    return out if any(v["status"] for v in out.values()) else None


def _copy_block_style(ws, src_col, dst_col):
    """Clone fonts, borders, fills and widths from one 3-column block to another."""
    for offset in range(BLOCK_WIDTH):
        s_letter = get_column_letter(src_col + offset)
        d_letter = get_column_letter(dst_col + offset)
        if s_letter in ws.column_dimensions:
            ws.column_dimensions[d_letter].width = ws.column_dimensions[s_letter].width

        for row in range(DATE_ROW, ws.max_row + 1):
            src = ws.cell(row, src_col + offset)
            dst = ws.cell(row, dst_col + offset)
            if src.has_style:
                dst.font = copy(src.font)
                dst.border = copy(src.border)
                dst.fill = copy(src.fill)
                dst.alignment = copy(src.alignment)
                dst.number_format = src.number_format


def _repair_date_merges(ws):
    """Make every row-1 date cell span its three columns.

    openpyxl's insert_cols shifts most merged ranges but can drop the last
    one, which leaves a date header unmerged. Rebuilding row 1 from the date
    cells themselves keeps the sheet visually correct regardless.
    """
    wanted = {col: col + BLOCK_WIDTH - 1 for col in _date_columns(ws).values()}

    for rng in [r for r in ws.merged_cells.ranges if r.min_row == DATE_ROW]:
        if wanted.get(rng.min_col) != rng.max_col:
            ws.unmerge_cells(str(rng))

    existing = {r.min_col for r in ws.merged_cells.ranges if r.min_row == DATE_ROW}
    for start, end in wanted.items():
        if start not in existing:
            ws.merge_cells(start_row=DATE_ROW, start_column=start,
                           end_row=DATE_ROW, end_column=end)


def ensure_date_block(ws, on_date):
    """Return the starting column for `on_date`, inserting a block if needed."""
    cols = _date_columns(ws)
    if on_date in cols:
        return cols[on_date], False

    later = sorted(d for d in cols if d > on_date)
    earlier = sorted(d for d in cols if d < on_date)

    if later:
        insert_at = cols[later[0]]
        template = cols[earlier[-1]] if earlier else cols[later[0]]
    elif earlier:
        insert_at = cols[earlier[-1]] + BLOCK_WIDTH
        template = cols[earlier[-1]]
    else:
        insert_at, template = 3, None

    ws.insert_cols(insert_at, BLOCK_WIDTH)

    # insert_cols shifts cells but not the template index if it sat to the right
    if template is not None and template >= insert_at:
        template += BLOCK_WIDTH

    if template is not None:
        _copy_block_style(ws, template, insert_at)

    date_cell = ws.cell(DATE_ROW, insert_at)
    date_cell.value = datetime.datetime(on_date.year, on_date.month, on_date.day)
    date_cell.number_format = "d-mmm-yy"
    ws.merge_cells(start_row=DATE_ROW, start_column=insert_at,
                   end_row=DATE_ROW, end_column=insert_at + BLOCK_WIDTH - 1)

    for offset, label in enumerate(("Status ", "Checked By", "Notes")):
        ws.cell(HEADER_ROW, insert_at + offset).value = label

    _repair_date_merges(ws)
    return insert_at, True


def write_run(path, on_date, rows, checked_by, details=None, backup=True):
    """Write one run into the workbook.

    `rows` maps sheet row number -> {"status": str, "notes": str}. Only rows
    present in the mapping are written; everything else is left untouched.
    """
    path = Path(path)
    backup_path = None
    if backup:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = path.with_name(f"{path.stem}.backup-{stamp}{path.suffix}")
        shutil.copy2(path, backup_path)

    wb = openpyxl.load_workbook(path)
    ws = wb.active
    col, created = ensure_date_block(ws, on_date)

    written = 0
    for row, payload in rows.items():
        status = (payload.get("status") or "").strip()
        notes = (payload.get("notes") or "").strip()
        if not status:
            continue
        ws.cell(row, col).value = status
        ws.cell(row, col + 1).value = checked_by
        ws.cell(row, col + 2).value = notes or None
        written += 1

    # Keep the original checklist layout intact, but also append a detailed
    # audit log with one row per scenario. This is where timing/evidence lives.
    if details:
        sheet_name = "Health Check Details"
        if sheet_name in wb.sheetnames:
            dws = wb[sheet_name]
            if dws.cell(1, 16).value != "URL(s)":
                dws.cell(1, 16).value = "URL(s)"
        else:
            dws = wb.create_sheet(sheet_name)
            dws.append(["Run Date", "Checklist Row", "Category", "Checklist Item",
                        "Status", "Automated", "Scenario Time (sec)",
                        "Page Load Time (sec)", "Process Time (sec)", "Total Time (sec)",
                        "Action / Scenario", "Expected Result", "Actual Result",
                        "Evidence", "Error", "URL(s)"])
        for d in details:
            dws.append([
                d.get("run_date"), d.get("row"), d.get("category"), d.get("item"),
                d.get("status"), d.get("automated"), d.get("scenario_time_sec"),
                d.get("page_load_time_sec"), d.get("process_time_sec"),
                d.get("total_time_sec"), d.get("action"), d.get("expected"),
                d.get("actual"), d.get("evidence"), d.get("error"), d.get("urls")])
        dws.freeze_panes = "A2"
        dws.auto_filter.ref = dws.dimensions
        widths = [14, 14, 24, 60, 12, 12, 18, 20, 18, 18, 50, 50, 60, 100, 50, 100]
        for i, width in enumerate(widths, 1):
            dws.column_dimensions[get_column_letter(i)].width = width

    wb.save(path)
    wb.close()

    return {
        "column": col,
        "column_letter": get_column_letter(col),
        "created_block": created,
        "rows_written": written,
        "backup": str(backup_path) if backup_path else None,
    }
