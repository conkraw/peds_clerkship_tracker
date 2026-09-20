# Manual REDCap import — no API required

## In the Streamlit app

Complete the four-file reminder workflow first. Then open **Download REDCap import file — no API required**, upload a **current full REDCap export (CSV)** from the destination project, confirm its freshness/completeness, and click **Prepare REDCap import file**. Download **redcap_import.csv** after reviewing the checks. No API token is required for this path.

A full raw export includes all student parent records, fields, and repeating instances, with variable names and raw choice values. The reference lets the app preserve existing student IDs and repeat-instance numbers; a schedule or PDF cannot establish these. The 0959 sample is a format reference, not a permanently current database snapshot.

The app accepts an already uploaded reference or a current reference already read through the API, but does not make a new API call to prepare the manual CSV. If an uploaded reference is supplied, it takes priority. A changed file or changed exclusions requires preparing the CSV again.

**A Data Dictionary is optional.** It may be needed if the export uses coded assessment/item identities. Without the dictionary, this app cannot fully validate field types and choices; REDCap's validation and comparison table are required. Do not infer that successful CSV generation means the import is approved.

No actual student-data import CSV is included in this app package. Each CSV is generated from the files and reference supplied during your run.

## Importing the CSV

MANUAL REDCAP IMPORT — NO API REQUIRED

Use this file in the SAME REDCap project from which the reference was exported.
It is a DATA import, not a Data Dictionary or a project-structure replacement.
The app has not imported, emailed, or otherwise transmitted this file.

1. In REDCap, open Applications > Data Import Tool.
2. Choose real-time import and select the generated redcap_import.csv.
3. Display the data comparison table: YES.
4. Keep the existing record names/IDs; do NOT auto-number or rename records.
5. Overwrite data with blank values: NO. This is essential: blank CSV cells are
   omissions, NOT instructions to delete existing values.
6. Use comma-separated CSV, records in rows, and YMD dates (YYYY-MM-DD).
7. Review REDCap's validation and comparison table before clicking Import Data.
   Stop if it shows unexpected new student records, changed existing source
   values, wrong repeating instances, or field/code errors. Ask the director.
8. After importing, check the affected records. Download a NEW full raw REDCap
   export before preparing another batch. Do not reuse an old reference after
   anyone has edited or imported data into the project.

No concurrent edits/imports should occur between taking the reference export and
finishing this import. Offline files cannot check for changes on the server.
Upload the CSV directly; do not open and resave it in Excel (IDs, dates, and
free-text responses must retain their original content).

Existing manual grading exclusions and the automatic lowest-evaluation drop are
applied by the app. Manually excluded submissions are not newly imported, as in
the original scripts; submissions already in REDCap are NOT deleted. Excluded
submissions still count as received for reminders. Existing REDCap calculations,
NBME values, final grades, and unrelated manual fields are left to REDCap.
Without a Data Dictionary, choice codes/field types cannot be fully checked by
the app. Resolve all REDCap validation errors; never bypass the comparison step.
Saved-exclusion configuration is not changed by this data CSV. Use the exclusion
manager's JSON backup to retain session-only changes when API saving is unavailable.

Reference documentation (University of Colorado REDCap Help Center):
https://redcapucdenver.zendesk.com/hc/en-us/articles/31248111520276-Data-Import-Tool

## Exclusions and existing calculations

The manual file retains the existing source-import policy: explicitly manually excluded clinical submissions are not newly imported, while assessments already stored in REDCap are not deleted. The automatic lowest-scoring assessment is retained in the data; exclusion summaries remain available. In the app, manually excluded and automatically dropped assessments still count as received for reminders.

This change does not replace or verify the project's underlying portfolio formulas. The supplied PDF shows displayed fields but not every calculation expression. Confirm that REDCap's grade/exclusion calculations behave as intended in the existing project before using the results for final grading.

The manual data CSV does not persist new exclusion-rule configuration. Use **Director tools → Exclusions** to download a JSON backup of changes when API saving is unavailable. Retain the backup privately and restore it for later sessions. Saved rules present in the full export are reconciled during manual preparation; conflicts require review.

## Privacy

The generated CSV can contain student identifiers and assessment narratives. Keep it separate from the mailing-file ZIP and out of public repositories. Use institution-approved storage and access controls. The app package's source still contains the original requested student-specific exclusion defaults, so keep the repository private.
