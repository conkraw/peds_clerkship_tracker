# Validation — version 1.3.0

## Executed automated checks

Command:

```sh
python -m unittest test_tracker test_online test_simple test_manual -v
```

**199 tests passed**: the 151 existing tests plus 48 manual-import tests. Full results are in `TEST_RESULTS.txt`.

The added tests cover no-token/no-network preparation; valid and missing reference structure; duplicate parent IDs; wrong rotation; longitudinal/unsupported reference forms; preserving existing repeat instances; appending after the existing maximum; preventing duplicate imports with an updated reference; source-conflict preservation; unavailable/ambiguous mapping; proper CSV header order, quoting, Unicode, and line-break round trips; explicit integer identities; no blank clearing; no duplicate target rows; optional dictionary validation and coded-identity matching; saved/current exclusion rules and conflicts; and conditional UI downloads after reference confirmation.

The UI-control tests use a small simulated Streamlit API. They exercise the manual option after an API-read failure, independent reminder generation, no automatic REDCap preparation during reminder generation, no network call on a manual download, reference changes invalidating old results, and optional use of an already-read reference. They are **not tests of Streamlit's actual browser renderer**.

## Supplied-file local regression

The latest source files and 0959 reference were processed with version 1.2 and version 1.3 locally; the original standalone scripts were not executed. No live endpoint was contacted.

- All **553 clinical assessment totals**, imputation values, manual-exclusion flags, and automatic-drop flags matched version 1.2.
- The bytes of all three reminder CSVs were identical between versions for the comparison run.
- The original July schedule combined with the August checklist/matches was correctly blocked for a manual import, not treated as zero completed work.
- For a separate regression only, a matching August 31 roster of **12 students** was derived from existing parent rows in the supplied reference. This produced a locally validated **95-row** manual-import plan (13 clinical-evaluation rows and 82 checklist rows), with no plan errors. CSV serialization round-tripped to the exact planned values. This was an offline preparation test, not an actual import.
- No generated student-data CSVs or source records from these tests are included in the package. Aggregate regression results are in `validation_metrics.json`.

## Not verified

Streamlit is not installed in this build environment. No actual Streamlit browser session, Community Cloud deployment, or browser download was exercised. The UI tests use simulated controls.

No live REDCap server was contacted. No records, definitions, or exclusion settings were changed. REDCap's actual manual import, project-specific field validation, API configuration, and user permissions remain untested. An optional Data Dictionary improves the app's checks; without it, field types and choice codes are not fully validated by the app.

An offline snapshot cannot establish whether data changed after export. Use a current full raw export, prevent intervening imports/edits, and inspect REDCap's real-time comparison table. Keep existing IDs and set **Overwrite data with blank values: NO**. If REDCap rejects fields or shows unexpected values, stop and review the mappings rather than bypassing validation.

No Power Automate flow was executed. The existing reminder CSV outputs were preserved, not the unseen production flow configuration independently verified.

The portfolio PDF does not expose every calculation expression. Existing REDCap final-grade and exclusion calculations have not been replaced or certified. Test a small approved import and inspect the resulting review form before production use.
