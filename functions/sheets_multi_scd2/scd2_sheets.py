"""Google Sheets access: discover the tabs in a file, then read one.

Thin on purpose - everything worth testing lives in the pure modules. Google
clients are imported lazily so the rest of the package can be imported, and
tested, without the API libraries installed.

Reminder that costs more debugging time than anything else here: reading a
sheet is a Drive permission, not a GCP IAM role. The service account's IAM can
be perfect and the call still returns 403 until somebody opens the spreadsheet
and shares it with that address.
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

SCOPES = ("https://www.googleapis.com/auth/spreadsheets.readonly",)

#: Read a bounded number of columns rather than a whole unbounded row. Sheets
#: happily reports thousands of empty trailing columns otherwise.
MAX_COLUMN = "ZZ"


def build_service(credentials=None):
    """Build a Sheets API client using ADC unless credentials are supplied."""
    from googleapiclient.discovery import build  # type: ignore

    if credentials is None:
        import google.auth  # type: ignore

        credentials, _ = google.auth.default(scopes=list(SCOPES))
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def list_tabs(service, spreadsheet_id: str) -> List[str]:
    """Return every tab title in the file, in the order they appear.

    This is what makes "one file, many sheets" work without a code change: the
    set of tabs is read at runtime rather than compiled in.
    """
    meta = (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties.title")
        .execute()
    )
    return [s["properties"]["title"] for s in meta.get("sheets", [])]


def quote_tab(title: str) -> str:
    """Quote a tab title for use in an A1 range.

    Tab names routinely contain spaces, and Vietnamese sheets default to
    `Trang tinh1`. Single quotes inside a name are escaped by doubling.
    """
    return "'%s'" % title.replace("'", "''")


def read_tab(
    service, spreadsheet_id: str, title: str, header_row: int = 1
) -> Tuple[List[str], List[List[Any]]]:
    """Read one tab and split it into a header row and data rows.

    Returns `([], [])` for an empty tab rather than raising - an empty tab is
    a normal state (a new month's sheet created but not filled in yet), not a
    failure of the run.
    """
    a1 = "%s!A%d:%s" % (quote_tab(title), header_row, MAX_COLUMN)
    response = (
        service.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=a1,
            majorDimension="ROWS",
            valueRenderOption="UNFORMATTED_VALUE",
            dateTimeRenderOption="FORMATTED_STRING",
        )
        .execute()
    )
    values = response.get("values", [])
    if not values:
        return [], []
    header = [str(cell) for cell in values[0]]
    return header, values[1:]
