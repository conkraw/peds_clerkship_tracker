# Pediatric Clerkship Reminders — version 1.3, with manual REDCap import

## Update the deployed app

Replace the existing `peds_clerkship_tracker.py` in the deployed GitHub branch with this package's file, keeping the same filename and folder. Dependencies are unchanged. Keep your existing Streamlit Secrets. The full package also includes tests and the updated guides; these are not required to launch the app.

Keep the repository private: the Python source retains the three student-specific exclusions requested by the owner. Do not commit student source CSVs, generated reminders, tokens, or actual secrets files.

## Routine use

Upload the rotation schedule, updated checklist, preceptor matches, and OASIS ME evaluation CSV. Click **Create reminder files**. Download the three individual CSVs or the single reminder ZIP. Do not change director settings for an ordinary run.

The default downloads are:

| Filename | Contents |
| --- | --- |
| `student_checklist_review.csv` | Students with missing, incomplete, or insufficient-participation encounter logs. |
| `feedback_reminders_power_automate.csv` | One row per student needing additional clinical-assessment, observed H&P, or handoff solicitations/submissions. |
| `preceptor_eval_reminders.csv` | One combined row per preceptor–student pair, listing outstanding assessment types. Any received matching assessment suppresses that assessment reminder. Duplicate matches do not create extra obligations. |

The ZIP contains only these mailing files, not grades or raw assessment narratives. Files with zero rows contain no sendable reminders; review any missing-email or matching warnings before concluding everyone is complete. The app creates files. It does not send email or claim that an email has been delivered.

## REDCap is separate

When the director has configured `REDCAP_API_URL` and `REDCAP_API_TOKEN` in Streamlit Secrets, clicking Create reads the current REDCap reference and field definitions automatically. No separate full-export or Data Dictionary upload is needed. Reading never imports records.

Without a configured connection, the four source files still generate reminders when student IDs/emails can be resolved. A student with no entries in any source cannot be identified from a name alone; use the optional reference export or have the director enable automatic lookup. Do not store the 0959 database export in the repository.

To update REDCap after reviewing the reminders, open **Update REDCap — optional**, click **Review REDCap update**, approve the proposed changes, and click **Confirm update to REDCap**. New tracking fields are not required. Existing raw-data instruments are reused. A failed REDCap connection or sync preview does not invalidate otherwise valid reminder files. If saved exclusions cannot be verified, the API-based score exports and updates are withheld, not the reminder matching. The manual path can independently validate saved rules from a current full export.

## Manual REDCap import — no API required

After creating reminders, open **Download REDCap import file — no API required**. Upload a **current full REDCap export (CSV)** from the destination project, confirm that it includes all records and repeating instances and that no data has changed since export, and click **Prepare REDCap import file**. Review the result and select **Download REDCap import CSV**. The generated filename is `redcap_import.csv`.

The full export is needed only for this optional import, not for ordinary reminder downloads. It supplies the current student record IDs and existing repeating-instance numbers. Use raw variable names and raw coded values, not a labeled or filtered report. The older 0959 file illustrates the structure; get a fresh export for actual use. The review-form PDF is not a substitute for the data export.

If a current reference was already uploaded or read from REDCap during reminder generation, it can be reused without an extra API call. An uploaded reference takes priority. A Data Dictionary is optional unless the app needs it to resolve coded identities; without it, field and choice validation must be completed in REDCap. No new tracking instrument is required.

In REDCap, open **Applications → Data Import Tool**, choose a real-time import with the comparison table, keep the supplied record IDs, and set **Overwrite data with blank values: NO**. Upload `redcap_import.csv`, inspect the changes, and only then approve the import. Do not upload it to the Data Dictionary. Full instructions are in `MANUAL_REDCAP_IMPORT.md` and are downloadable in the app.

The file contains proposed new or changed values, not a replacement for the entire database. Existing source conflicts are preserved and flagged; blank cells do not request deletion. Mismatched rotations, unknown student records, and ambiguous repeat mappings prevent an import file from being released. An offline export cannot detect later server changes: do not allow intervening edits/imports, and get a new full export before preparing the next batch. A download is never treated as evidence that data was imported.

## Director tools

The sidebar's **Show director tools** option exposes exclusions, processing options, and connection diagnostics. It is a layout switch, not a separate authorization boundary. All app viewers must be authorized to access the student data.

The original three student–preceptor exclusions still load automatically. Additional rules can be added, deactivated, restored, and saved using the existing exclusion manager. Persistent rule saving still requires the previously documented `cst_exclusion_rules` Notes Box field; it is not required for routine reminders or for the three built-in rules. Unsaved changes and processing-option overrides are session-only. Save rules to REDCap to retain them between sessions, or download and later restore the exclusion-manager JSON backup when API saving is unavailable. The manual data-import CSV does not save exclusion configuration.

## Existing Power Automate flows

The default three-file layout is unchanged from version 1.1. The combined preceptor email should display all populated `cas_link`, `hp_link`, and `handoff_link` values; `blank_form_link` contains only the first available link. The older checklist fields are still present, with additional `participation_review_items` and `incomplete_items` fields. Include these in the current checklist email body.

For flows that still use the original separate clinical-assessment and observed-H&P files, the director workspace includes **Download legacy-flow reminder files**. This alternative ZIP preserves the original checklist/student headers and separate CAS and H&P preceptor layouts, including `observed_hp_reminders.csv`. New incomplete/unknown-participation issues are folded into the old checklist message field. Use either this legacy package or the default combined-preceptor package for a given run, not both. The legacy package does not send preceptor handoff reminders; the main combined output and student reminders retain handoff tracking. No handoff survey URL was supplied, so none is invented. Empty partial-form links are intentional unless the director explicitly enables the old rating-prefilled links.

Your actual Power Automate flows were not available to run or inspect. CSV schemas were checked against the supplied examples; test one reminder through the intended flow before bulk sending.

## Checks retained

The 8/2/1 targets, ten encounter categories, observation-only exceptions, 375-point scoring, imputation, automatic lowest-score drop, and manual exclusions are retained. The code still prevents fuzzy student matching, treats unconfirmed source coverage as unknown rather than zero, keeps stable existing REDCap repeat instances, and requires explicit approval for writes.

The July schedule and August checklist/matching samples remain a mismatched set and are intentionally not released as a complete mailing run. Use matching, current exports.

See `MANUAL_REDCAP_IMPORT.md` for the no-API import workflow, `ADMIN_QUICK_START.md` for the short administrator guide, `ONLINE_SETUP.md` for one-time configuration, `PORTFOLIO_NOTES.md` for the review-form mapping and limitations, and `VALIDATION_REPORT.md` for what was actually tested.
