# Changes in 1.1.0 — September 19, 2026

- Prepared private online deployment instructions and headless configuration; original entry-point name retained.
- Embedded all three original student–preceptor exclusion rules; no separate private_exclusions.json is required.
- Added OASIS student/preceptor/form selectors, manual rule entry, reasons, and activate/deactivate controls.
- Added scoped matching by student and preceptor, optionally source form, rotation, or exact submitted timestamp.
- Kept submitted exclusions visible in audit and received for reminder suppression; manual rules precede automatic lowest-score dropping.
- Added REDCap-backed rule persistence in the new non-repeating notes field cst_exclusion_rules, with explicit Save, automatic session-start load, conflict checks, and read-back verification.
- Added a versioned JSON backup/merge option. Unsaved edits are explicitly labeled session-only.
- Changes invalidate previous scores and upload plans. Successful rule saves discard the stale live grading snapshot.
- Added a required shared-password gate unless an administrator explicitly delegates authentication to a trusted authenticated host. This is not enterprise identity, viewer authorization, or compliance certification.
- Preserved the rest of the original reminder, clinical scoring, raw import, and REDCap preview workflow.
- Added 52 synthetic rule/persistence regression tests; all 110 tests pass.
