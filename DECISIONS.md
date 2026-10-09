# DECISIONS.md

1. **Indexes.** I use a unique `employees.emp_code` index and a unique compound index on `attendance_logs(emp_code, date)` to enforce identity and one attendance row per employee/day. Department and join-date indexes support department headcount and trends. Date/employee, employee/punch-in, and date/status indexes support the list, punch-out, and monthly analytics access patterns. I avoided indexing every derived metric because it would add write cost without helping the main filters.

2. **Punch-in race.** Both requests may initially observe no record, but the database's unique compound index allows only one insert. The winning request gets `201`; the other gets `DuplicateKeyError`, translated to `409`.

3. **Ties.** I compute MongoDB competition rank on total late minutes, then sort by minutes and employee code. The cutoff applies to rank, not row count, so all employees tied at a rank within `limit` are returned.

4. **Headcount.** Department summary starts from eligible employees and looks up that employee's logs. Employees with no matching logs still flow to the department grouping and count toward headcount.

5. **100x data.** I would measure the real query plans and latency first, then consider pre-aggregated daily summaries and a retention/archive strategy while keeping raw records for audit.
