# Validation report — version 2.0.0

Date: September 20, 2026. Scope: code and local fixture processing, not a live
REDCap or Streamlit Cloud deployment.

## Automated tests actually executed
`python -m unittest discover -v` completed with **118 passing tests**.
The package contains all four test files and the test output in TEST_RESULTS.txt.

Tests cover the retained CSV parsing, name/ID reconciliation, participation rules,
scoring, dropped-evaluation behavior, exclusions and reminders; the fresh-project
schema and matching dictionary; stable source-derived IDs; import field and
numeric/date validation; same-run identity conflicts; source changes and stale
outputs; and no-API user-interface call paths. The 15 interface tests use a fake
Streamlit object, not Streamlit AppTest or an actual browser.

## Supplied-file regression actually executed
The uploaded source schedule lists a July 6 rotation, while the checklist and
preceptor matches concern August 31. Running those original four files together
correctly withholds the REDCap import and affected reminder files. The original
source files were not changed.

For a consistent-cohort comparison, a TEST roster of 12 August 31 students was
constructed from the supplied preceptor associations. With the actual checklist,
matches and OASIS file, reminder date 2026-09-19, and reference date 2026-09-10:

- All three primary Power Automate CSVs byte-matched version 1.3.0.
- Clinical score summary rows matched version 1.3.0.
- The new export produced 193 validated records across 142 defined fields:
  12 summaries, 13 clinical evaluations, 2 H&Ps, 1 handoff, 82 checklist entries,
  and 83 assessment-match records.

Across the entire supplied OASIS source, 763 assessment forms were normalized.
All **553 clinical evaluation totals** matched the previous app and an independent
mean-of-observed-domains × 75 calculation from the raw question responses.

This regression roster was a test construction, not a claim that the originally
uploaded schedule matched the other files. No real-student output is packaged
with this release. The three original exclusion rules remain in the private code.

## Synthetic import
The fictional example creates 22 records. Its adjusted clinical
score is 300/375 after dropping the lowest of four scorable clinical evaluations.
The example CSV round-trips through the Python CSV reader and matches every field
in the supplied Data Dictionary. Repeat-import and update semantics were checked
in local record-map simulations, not through REDCap itself.

## Not tested / not done
Streamlit was not installed in this build environment; installing it failed
because the package server could not be reached. The actual Streamlit widgets,
cloud deployment, and your Power Automate flows were not exercised. No API calls,
mailings, GitHub changes or REDCap writes were made. REDCap has not accepted this
dictionary or import on a live server yet. Test the synthetic example in an empty
development project and review its repeated import before production use.

## Operational limitations
Use complete exports. Stable IDs preserve matching source identities, not arbitrary
renames or deleted rows. Missing records are not deleted on later CSV imports.
Import the newest batch last, preserve record IDs, and review the comparison table.
For this NEW generated-only schema, blank overwriting is YES so old generated values
can clear; manual fields belong on another instrument. This is not safe guidance
for reusing the old project's import schema. New exclusion changes need a JSON
backup/restore or the optional EXCLUSIONS_JSON Secret to survive a fresh session.
No NBME score, final clerkship grade or email-sent status is inferred.
