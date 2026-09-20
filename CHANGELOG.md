# Version 1.3.0 — September 20, 2026

- Added **Download REDCap import file — no API required**, separate from the three unchanged reminder downloads and the API-upload workflow.
- Added a current-full-export upload, explicit freshness/completeness confirmation, no-network preparation, preview, and **Download REDCap import CSV** (`redcap_import.csv`).
- Reuses existing record/instance reconciliation, source conflict preservation, manual exclusions, and automatic lowest-score drop. No blank-clearing or auto-numbered identities are emitted.
- Added reference-structure validation, coded-identity checks, optional Data Dictionary validation, saved-versus-session exclusion-rule reconciliation, and stale-result invalidation.
- CSV uses proper UTF-8 quoting for clinical narratives and explicit existing/newly assigned integer repeat instances. It is not the Power Automate-cleaned format and is not a Data Dictionary.
- Added downloadable import instructions and director-only change details. No download is labeled as an import receipt.
- Corrected checklist identity matching with metadata so coded item values are compared against their decoded labels rather than treated as different encounters.
- All 199 automated tests passed. All 553 supplied clinical assessment totals and the three reminder CSV byte outputs were unchanged from version 1.2 in the local regression. No live REDCap import, cloud deployment, or real browser test was performed.

# Version 1.2.0 — September 20, 2026

- Routine screen reduced to four CSV uploads, Create reminder files, and three download buttons/one reminder-only ZIP.
- Removed routine API URL/token inputs, Data Dictionary upload, optional-field-definition downloads, repeated exclusion-review confirmations, and mandatory full-project-export handling.
- Added automatic, session-local REDCap reference/metadata and saved-rule reads using existing Secrets settings. Reads occur when requested, never as imports.
- Kept reminder generation independent of REDCap availability and sync preparation. Valid raw inputs can generate reminders without a reference export.
- Preserved the combined CSV schemas from version 1.1; added an optional legacy-layout ZIP for older separate clinical/H&P flows, verified against the supplied header examples.
- Moved exclusion editing, processing overrides, connection diagnostics, scores, and audit downloads into director tools.
- Continued explicit preview/approval for REDCap imports; no tracking instrument required. Calculated/unrelated fields and conflicting nonblank source values are preserved.
- Added disabled-download and plain-language warnings for wrong-cohort inputs, missing identities/addresses, and stale exports.
- Preserved all earlier scoring and reminder outputs in the supplied-data regression comparison, including all 553 clinical assessment totals.
- Fixed `submitted_ce` preparation so incomplete checklists do not get an "all encounters submitted" date. Full portfolio-formula reconciliation remains outside this change.

Full validation and limitations are in `VALIDATION_REPORT.md`.
