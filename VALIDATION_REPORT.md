# Validation report — online edition

Version 1.1.0 · September 19, 2026

## Completed checks

**110 synthetic automated tests passed**: the 58 original regression tests and 52 additional online-exclusion tests. `TEST_RESULTS.txt` contains the complete test names and output. All Python files passed syntax parsing; the configuration and Secrets template passed TOML parsing.

The original suite covers CSV normalization, roster resolution, rotation coverage, missing activities, five-domain scoring, unscored forms, imputation, manual exclusions before dropping the lowest evaluation, independent assessment-type reminder matching, duplicate suppression, REDCap repeat-instance matching, conservative update planning, and mocked upload orchestration.

The new suite covers automatic loading of the three original pair-specific rules; name/email and optional source-form/rotation/timestamp restrictions; inactive rules restoring scoring; exclusion followed by automatic dropping; received/excluded assessments suppressing reminders; validated backups; stable rule identities; picker de-duplication; missing or invalid configuration fields; duplicate/longitudinal/repeating records; sparse saved-rule updates; per-student conflict checks; no automatic creation of students; verified persistence across simulated store sessions; unrelated-student changes being preserved; and no automatic retry after a timeout or failed read-back.

**The persistence tests use a fake, in-memory REDCap API.** They verify payloads and error handling but do not establish that a real token has the required permissions or that the institutional server is reachable.

## Check against the supplied CSV samples

The updated processing pipeline was run on the four supplied source CSVs and the full 0959 REDCap export without modifying them. It retained the 11 scheduled students and processed 126 submitted assessments in the selected rotation. The three built-in rules loaded successfully.

As expected, the July 6 schedule and August 31 checklist/matching cohorts do not align. The checklist and student solicitation status were **Coverage not confirmed**; zero preceptor reminder rows were generated. Those source files are illustrative inputs, not a matched set for sending reminders. No source student CSVs or resulting individual-level reports are included in this package.

## Not tested or performed

- Streamlit could not be installed in this execution environment. A direct PyPI installation attempt also found no available package versions. Therefore real browser rendering, widget interactions, Streamlit session lifecycle behavior, and cloud deployment have **not** been tested here. No private application URL was created.
- No live REDCap record or metadata request, live import, or live exclusion save was performed. Existing records, instrument settings, permissions, and institutional network access were not changed.
- No email was sent. Generated reminders are not proof of delivery.
- No new claim of parity with REDCap's final clerkship-grade/NBME formulas is made. This update preserves the app's clinical-evaluation calculation, not unseen server formulas.
- The Dockerfile is supplied for administrator review; an image was not built or deployed.

## Production checks

Use approved private hosting and test with synthetic inputs before student data. Confirm that the deployment restricts viewers, has a private repository, and uses private Secrets. Add the internal non-repeating `cst_exclusion_rules` notes field in an approved test project, verify save/reload/deactivate behavior, then review a small intended live update before production use.

Rule-saving conflict checks are optimistic, not a REDCap-side atomic transaction. Avoid simultaneous editing of one student's exclusions. A timeout can occur after a server write; read back and reconcile before retrying.

## Confidential package content

There are no source student CSVs or actual API credentials in the package. The Python source **does** embed the three requested historical exclusions with student IDs/names and preceptor names. The source repository and any image built from it must remain private. `.gitignore` does not remove embedded identifiers.
