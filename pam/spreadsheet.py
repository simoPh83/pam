"""Leads spreadsheet (XLSX): one row per approved application.

Rows are keyed by uid. New approvals are appended; state changes on existing
rows update the row in place (the file is small enough to rewrite each run).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

COLUMNS = [
    ("uid", "UID", 22),
    ("reference", "Reference", 14),
    ("authority", "Authority", 12),
    ("area_matched", "Area", 10),
    ("address", "Address", 40),
    ("postcode", "Postcode", 10),
    ("description", "Description", 60),
    ("app_type", "Type", 12),
    ("app_size", "Size", 8),
    ("app_state", "State", 11),
    ("start_date", "Applied", 11),
    ("decided_date", "Decided", 11),
    ("permission_expires", "Perm. expires", 12),
    ("agent_name", "Agent / practice", 24),
    ("applicant_name", "Applicant", 20),
    ("distance_km", "km from home", 9),
    ("state_changed", "State changed", 12),
    ("council_url", "Council record", 30),
    ("docs_url", "Council docs", 30),
    ("planit_url", "PlanIt page", 30),
]


class LeadsSheet:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self.wb = load_workbook(path)
            self.ws = self.wb.active
        else:
            self.wb = Workbook()
            self.ws = self.wb.active
            self.ws.title = "Leads"
            self.ws.append([label for _, label, _ in COLUMNS])
            for cell in self.ws[1]:
                cell.font = Font(bold=True)
            self.ws.freeze_panes = "A2"
        self._index = self._build_index()

    def _build_index(self) -> dict[str, int]:
        index = {}
        for row_num in range(2, self.ws.max_row + 1):
            uid = self.ws.cell(row=row_num, column=1).value
            if uid:
                index[str(uid)] = row_num
        return index

    def upsert(self, row: dict, *, state_changed: bool) -> str:
        """Insert or update a lead row. Returns 'added' or 'updated'."""
        values = []
        for key, _, _ in COLUMNS:
            if key == "state_changed":
                values.append(date.today().isoformat() if state_changed else None)
                continue
            value = row.get(key)
            if isinstance(value, date):
                value = value.isoformat()
            values.append(value)

        uid = row["uid"]
        if uid in self._index:
            row_num = self._index[uid]
            existing_changed = self.ws.cell(row=row_num, column=17).value
            for col, value in enumerate(values, start=1):
                if key_is_preserved_on_update(COLUMNS[col - 1][0]) and existing_changed:
                    continue
                self.ws.cell(row=row_num, column=col, value=value)
            action = "updated"
        else:
            self.ws.append(values)
            self._index[uid] = self.ws.max_row
            action = "added"

        # hyperlinks
        row_num = self._index[uid]
        for key, col in (("council_url", 18), ("docs_url", 19), ("planit_url", 20)):
            if row.get(key):
                cell = self.ws.cell(row=row_num, column=col)
                cell.hyperlink = row[key]
                cell.style = "Hyperlink"
        return action

    def save(self) -> None:
        for i, (_, _, width) in enumerate(COLUMNS, start=1):
            self.ws.column_dimensions[get_column_letter(i)].width = width
        self.wb.save(self.path)


def key_is_preserved_on_update(key: str) -> bool:
    # don't wipe an earlier "state changed" date unless it changed again today
    return key == "state_changed"
