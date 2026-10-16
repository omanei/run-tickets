# Export the monthly report as CSV

Users can already see the monthly report in the app. They want to download it as a CSV
file to open it in a spreadsheet.

- `report export --format csv --month 2026-09` writes the report to stdout as CSV.
- Columns: date, category, amount. Amounts use a dot as the decimal separator.
- An unknown month is an error with a non-zero exit code.
