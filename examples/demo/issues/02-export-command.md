# 02: The export command writes CSV to stdout

**What to build:** `report export --format csv --month YYYY-MM` prints the month's report as
CSV.

**Blocked by:** Report rows can be written as CSV

**Status:** ready-for-agent

- [ ] The command prints the CSV of the requested month
- [ ] An unknown month exits with a non-zero code and a clear message
