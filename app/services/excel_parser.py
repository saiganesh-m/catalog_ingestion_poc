from io import BytesIO
from typing import List, Dict, Any, Union, Optional
from dataclasses import dataclass
from pathlib import Path
import openpyxl


@dataclass
class RawRow:
    """Represents a single raw row extracted from Excel with complete coordinate provenance."""
    row_index: int                      # 1-indexed original Excel row number (e.g. Row 2)
    sheet_name: str                     # Worksheet name
    data: Dict[str, Any]                # {header_name: raw_cell_value}
    cell_coordinates: Dict[str, str]    # {header_name: "A2", "B2", ...}


@dataclass
class WorkbookExtraction:
    """Represents the complete extracted contents and metadata of an Excel workbook."""
    filename: str
    sheet_name: str
    headers: List[str]
    rows: List[RawRow]
    column_samples: Dict[str, List[str]]  # Sample values per column passed to JEV for decision-making


class ExcelParser:
    """
    Programmatic Excel workbook reader using openpyxl.
    Retains cell coordinates, 1-indexed row numbers, and sample values for JEV analysis.
    """

    def parse(
        self,
        source: Union[str, Path, bytes, BytesIO],
        filename: str = "unknown.xlsx",
        sheet_name: Optional[str] = None,
    ) -> WorkbookExtraction:
        """
        Reads an Excel workbook and returns structured rows with provenance metadata.
        
        :param source: File path or raw bytes (for file uploads via FastAPI).
        :param filename: Original filename for provenance tracking.
        :param sheet_name: Specific sheet name to read, or None for the active sheet.
        """
        if isinstance(source, (bytes, BytesIO)):
            file_stream = BytesIO(source) if isinstance(source, bytes) else source
            wb = openpyxl.load_workbook(file_stream, data_only=True)
        else:
            path = Path(source)
            if not path.exists():
                raise FileNotFoundError(f"Excel file not found at: {path}")
            wb = openpyxl.load_workbook(path, data_only=True)
            filename = path.name

        if sheet_name and sheet_name not in wb.sheetnames:
            raise ValueError(f"Sheet '{sheet_name}' not found. Available sheets: {wb.sheetnames}")
        ws = wb[sheet_name] if sheet_name else wb.active
        active_sheet_name = ws.title

        # Step 1: Extract header row (Row 1)
        headers: List[str] = []
        header_col_indices: Dict[int, str] = {}  # col_index -> header_name

        for col_idx in range(1, ws.max_column + 1):
            cell = ws.cell(row=1, column=col_idx)
            val = cell.value
            if val is not None and str(val).strip():
                clean_header = str(val).strip()
                headers.append(clean_header)
                header_col_indices[col_idx] = clean_header

        if not headers:
            raise ValueError(f"No valid headers found in sheet '{active_sheet_name}'")

        # Step 2: Read data rows (starting at Row 2)
        rows: List[RawRow] = []
        column_samples: Dict[str, List[str]] = {h: [] for h in headers}

        for row_idx in range(2, ws.max_row + 1):
            row_data: Dict[str, Any] = {}
            coords: Dict[str, str] = {}
            is_empty_row = True

            for col_idx, header_name in header_col_indices.items():
                cell = ws.cell(row=row_idx, column=col_idx)
                raw_val = cell.value
                coords[header_name] = cell.coordinate

                if raw_val is not None and str(raw_val).strip() != "":
                    is_empty_row = False
                    clean_str_val = str(raw_val).strip()
                    row_data[header_name] = clean_str_val
                    # Collect up to 4 non-empty samples for JEV decision-making
                    if len(column_samples[header_name]) < 4 and clean_str_val not in column_samples[header_name]:
                        column_samples[header_name].append(clean_str_val)
                else:
                    row_data[header_name] = None

            # Skip entirely blank rows
            if not is_empty_row:
                rows.append(
                    RawRow(
                        row_index=row_idx,
                        sheet_name=active_sheet_name,
                        data=row_data,
                        cell_coordinates=coords,
                    )
                )

        wb.close()

        return WorkbookExtraction(
            filename=filename,
            sheet_name=active_sheet_name,
            headers=headers,
            rows=rows,
            column_samples=column_samples,
        )

