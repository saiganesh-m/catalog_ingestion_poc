"""
Generates the two sample supplier workbooks in data/:

  sample_catalog.xlsx             13 rows covering all 8 edge cases from docs/original_brief.txt
  sample_catalog_supplier_b.xlsx   a second supplier with different headers and harder formats:
                                   competing price columns (Price vs Line Total), an ambiguous
                                   'Weight' column, suffix currency symbols, CA$, ¥, tonnes, inches,
                                   comma-thousands units, a currency conflict and a price that
                                   differs from an already-approved SKU (RB-1003)

Run from the project root:  python -m scripts.generate_sample_catalog
"""
from pathlib import Path
from typing import List, Optional
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from app.core.config import settings

SAMPLE_HEADERS = [
    "Part Number",           # Maps to 'sku'
    "Product Description",   # Maps to 'name'
    "Unit Cost",             # Ambiguous: Maps to 'unit_price' (vs Extended Amount)
    "Extended Amount",       # Competing column: Should be ignored by JEV
    "Rated Load",            # Maps to 'payload_kg'
    "Working Reach",         # Maps to 'reach_m'
    "Currency",              # ISO currency
    "Warranty Duration",     # Irrelevant column: Should be ignored by JEV
]

SAMPLE_ROWS = [
    # 1. Clean valid standard item (Row 2 in Excel)
    ["RB-1001", "Articulated Robot Arm 6-Axis", "$1,250.00", "$6,250.00", "10 kg", "1500 mm", "USD", "24 Months"],
    # 2. Alternative number formatting (European comma decimal) (Row 3)
    ["RB-1002", "SCARA High-Speed Assembly Arm", "€ 2.450,50", "€ 4.901,00", "5 kg", "800 mm", "EUR", "12 Months"],
    # 3. Sub-units: grams and meters (Row 4)
    ["RB-1003", "Micro-Delta Precision Sorter", "£ 850.00", "£ 1,700.00", "500 g", "1.2 m", "GBP", "36 Months"],
    # 4. Imperial units: lbs and inches (Row 5)
    ["RB-1004", "Heavy Industrial Palletizer", "$8,900.00", "$8,900.00", "50 lbs", "78 in", "USD", "5 Years"],
    # 5. Indian Rupee format (INR / ₹) with lakh comma separator (Row 6)
    ["RB-1005", "Pick-and-Place Cartesian Sorter", "₹ 1,85,000.00", "₹ 1,85,000.00", "8 kg", "1100 mm", "INR", "18 Months"],
    # 6. Missing required value: Blank SKU (Row 7) -> REJECTED
    [None, "Gantry Cartesian Linear Unit", "$3,100.00", "$3,100.00", "20 kg", "2000 mm", "USD", "12 Months"],
    # 7. Missing required value: Blank Name (Row 8) -> REJECTED
    ["RB-1006", "", "$1,500.00", "$1,500.00", "15 kg", "1100 mm", "USD", "12 Months"],
    # 8. Invalid numeric value: 'N/A' in price (Row 9) -> REJECTED
    ["RB-1007", "Cobot Collaborative Welder", "N/A", "TBD", "12 kg", "1300 mm", "USD", "24 Months"],
    # 9. Invalid numeric value: Negative price (Row 10) -> REJECTED
    ["RB-1008", "Laser Inspection Module", "-$450.00", "-$450.00", "2 kg", "500 mm", "USD", "6 Months"],
    # 10. Ambiguous currency symbol: Bare '$' without currency column (Row 11) -> NEEDS_REVIEW
    ["RB-1009", "Cleanroom Pick and Place Robot", "$ 720.00", "$ 720.00", "3 kg", "650 mm", None, "12 Months"],
    # 11. Competing price columns: Unit Cost vs Extended Amount (Row 12) -> JEV chooses Unit Cost -> READY
    ["RB-1010", "Modular Servo Actuator", "$180.00", "$1,800.00", "1.5 kg", "300 mm", "USD", "12 Months"],
    # 12. Duplicate Product: Same SKU as Row 2 (Row 13) -> Duplicate in catalog -> NEEDS_REVIEW
    ["RB-1001", "Articulated Robot Arm 6-Axis (Duplicate)", "$1,250.00", "$1,250.00", "10 kg", "1500 mm", "USD", "24 Months"],
    # 13. Valid item with missing optional fields (payload & reach NULL) (Row 14) -> READY
    ["RB-1012", "Controller Cabinet Master Unit", "$4,200.00", "$4,200.00", None, None, "USD", "36 Months"],
]

SUPPLIER_B_HEADERS = [
    "Product Code",          # sku (header synonym)
    "Item Label",            # name (header synonym)
    "Price",                 # unit_price, competing with Line Total
    "Line Total",            # a total, not a unit price
    "Max Load",              # payload_kg
    "Arm Length",            # reach_m
    "Curr.",                 # currency (abbreviated)
    "Weight",                # the robot's own weight: NOT payload
    "Warehouse Location",    # irrelevant
]

SUPPLIER_B_ROWS = [
    # Row 2: ISO code inside the price, comma-thousands units -> READY (1500 kg, 3.2 m)
    ["SB-2001", "Heavy-Duty Palletizing Robot", "USD 48,500.00", "USD 97,000.00", "1,500 kg", "3,200 mm", "USD", "2,850 kg", "W1-A"],
    # Row 3: CA$ with no currency column value -> READY (CAD)
    ["SB-2002", "Compact SCARA Arm", "CA$ 7,250.00", "CA$ 14,500.00", "6 kg", "600 mm", None, "28 kg", "W1-B"],
    # Row 4: European price with suffix symbol, tonnes, inches -> READY (1250.00 EUR, 500 kg, 1.219 m)
    ["SB-2003", "Gantry Loader", "1.250,00 €", "2.500,00 €", "0,5 t", '48"', "EUR", "410 kg", "W2-A"],
    # Row 5: price in euro but currency column says USD, unreadable payload -> NEEDS_REVIEW
    ["SB-2004", "Vision Inspection Cell", "€ 3,900.00", "€ 3,900.00", "N/A", "1.1 m", "USD", "95 kg", "W2-B"],
    # Row 6: '¥' could be JPY or CNY -> NEEDS_REVIEW
    ["SB-2005", "Pallet Conveyor Robot", "¥ 1,200,000", "¥ 2,400,000", "25 kg", "1800 mm", None, "150 kg", "W3-A"],
    # Row 7: price is not a number -> REJECTED
    ["SB-2006", "Bin Picking Arm", "approx. 1250", "approx. 2500", "10 kg", "900 mm", "USD", "40 kg", "W3-B"],
    # Row 8: negative payload -> NEEDS_REVIEW
    ["SB-2007", "Delta Picker", "£ 5,600.00", "£ 5,600.00", "-3 kg", "800 mm", "GBP", "60 kg", "W4-A"],
    # Row 9: accounting-style negative price -> REJECTED
    ["SB-2008", "Linear Axis Module", "(450.00)", "(450.00)", "5 kg", "2 m", "GBP", "12 kg", "W4-B"],
    # Row 10: same SKU as the main sample with a new price -> NEEDS_REVIEW once RB-1003 is approved
    ["RB-1003", "Micro-Delta Precision Sorter", "£ 900.00", "£ 1,800.00", "500 g", "1.2 m", "GBP", "18 kg", "W5-A"],
]


def write_workbook(output_path: Path, sheet_title: str, headers: List[str], rows: List[List[Optional[str]]]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title
    ws.append(headers)
    for row in rows:
        ws.append(row)

    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_fill = PatternFill(start_color="1F497D", end_color="1F497D", fill_type="solid")
    thin = Side(style="thin", color="D3D3D3")
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in ws.iter_rows(min_row=2, max_row=len(rows) + 1, max_col=len(headers)):
        for cell in row:
            cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
            cell.alignment = Alignment(horizontal="left", vertical="center")
    for index, header in enumerate(headers, start=1):
        longest = max([len(header)] + [len(str(row[index - 1] or "")) for row in rows])
        ws.column_dimensions[get_column_letter(index)].width = min(longest + 4, 42)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"Generated {output_path} ({len(rows)} rows)")


def generate_sample_catalogs() -> None:
    write_workbook(settings.DATA_DIR / "sample_catalog.xlsx", "Supplier_Robotics_Catalog", SAMPLE_HEADERS, SAMPLE_ROWS)
    write_workbook(settings.DATA_DIR / "sample_catalog_supplier_b.xlsx", "Supplier_B_Price_List", SUPPLIER_B_HEADERS, SUPPLIER_B_ROWS)


if __name__ == "__main__":
    generate_sample_catalogs()
