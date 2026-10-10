# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` and unique `attendance_logs(emp_code, date)` give identity and one record per day. `attendance_logs(date desc, emp_code)` serves the list sort and the leaderboard's month range; `(emp_code, punch_in desc)` finds the record a punch-out closes; `employees(department, joined_on)` serves headcount. I rejected an index on `late_minutes`: the month's date range already narrows the rows, so it would only slow writes.

2. **Punch-in race.** Both requests may see no record and insert. The unique `(emp_code, date)` index lets exactly one succeed (201); the other raises `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$setWindowFields` with `$rank` on total late minutes, then `$match rank <= limit`. Limit applies to rank, not row count, so everyone tied at the cutoff is returned (1, 2, 2 with limit 2). Rows sort by minutes, then `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined on or before month end) and uses `$lookup` for logs, so someone with zero logs still reaches the department `$group` and is counted.

5. **100x data.** I would store `department` on each attendance log. The trend and leaderboard currently join every log to `employees` just to read the department. Cost: a department change must also update old logs.