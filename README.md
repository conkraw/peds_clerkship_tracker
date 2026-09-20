# Pediatric Clerkship Tracker — online edition

Version 1.1.0 · September 19, 2026

**Start with [ONLINE_SETUP.md](ONLINE_SETUP.md).** This version is prepared for a private, institution-approved browser-based Streamlit deployment. Your original three exclusions are built in; new rules are managed inside the app and can be saved in REDCap. After deployment you do not need a local terminal for routine use.

Routine workflow: **upload four CSVs → manage exclusions → review → download three reminder files → optionally update REDCap**. The app does not send email. No original script is executed, no input file is edited, and no grades or assessments are written simply because a file was uploaded.

The source contains confidential student-specific exclusion configuration. Use a private repository and restricted viewers; the app password is an additional gate, not a claim of institutional compliance. Configure `APP_PASSWORD` in Streamlit Secrets; use a unique value of at least 16 characters. Do not commit actual secrets.

## Optional local use

This remains available but is not needed for the online workflow. Install the dependencies with Python 3.10+; Python 3.12 is the suggested deployment setting. Privately copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and replace the password placeholder.

```powershell
python -m pip install -r requirements.txt
python -m streamlit run peds_clerkship_tracker.py --server.address 127.0.0.1
```

The Windows launcher performs the second command after installation. For a private institutional server, the included Dockerfile is a starting point for your administrator; authentication, authorization, TLS, and network access still need configuring.

## Files you upload each time

| Upload | Expected content |
|---|---|
| Rotation schedule | Student names and start dates; student IDs, email addresses, and end dates are helpful when available. The supplied `legal_name,start_date` format is supported. |
| Updated checklist | The raw school checklist export, including item, item status, participation columns, student external ID, and rotation start. |
| Preceptor matches | The raw matching export, including student/preceptor identifiers, course and evaluation-period dates, and pipe-separated `Manual Evaluations`. |
| OASIS ME evaluations | The raw question-level ME export, including `Form Record`, assessment type, evaluator, student ID, course dates, responses, and submission date. |

Names are normalized for credentials and `Last, First` formatting. IDs and email addresses are preferred for matching. A name-only schedule must resolve to a unique student using the uploaded sources or the REDCap snapshot. Ambiguous identities are flagged, not guessed. Scheduled students with no logged activity remain in the roster when their identity can be resolved.

The optional **REDCap reference / connection** section accepts the full `0959`-style export and a Data Dictionary, or reads current records and metadata through the API. The full export is useful for resolving student IDs/emails and planning safe imports. It is not a fifth routine reminder-processing source, and older REDCap evaluations are not silently substituted into the current ME report.

If no end date can be resolved, the default is start date plus 25 days, meaning a 26-calendar-day inclusive rotation. This fallback is visible in the roster and configurable. It should not be used unreviewed for a different rotation model.

### Check the rotation and export dates

The supplied schedule contains the July 6, 2026 cohort, whereas the checklist and matching samples contain the August 31, 2026 cohort. Those files intentionally produce coverage warnings. The app withholds the affected missing-item/solicitation reminders rather than declaring the July students deficient.

Use a schedule covering the same rotation as the checklist and matching files. Do not select the full-coverage attestation merely to bypass a cohort mismatch. That option is for a genuinely complete export in which an entire selected cohort has zero entries.

The supplied ME filename is dated September 10, 2026. For a September 19 review, refresh the ME export before sending reminders; a submission after the export cannot be recognized. The app displays the data-through date and source-freshness warning. The date is an operator assertion, not automatic proof that the export includes everything.

## The three reminder downloads

### 1. `student_checklist_review.csv`

One row per student needing action, provided a student email is available. Checks the ten encounter categories from the supplied processor. Observing is sufficient for Health Systems and Humanities; the other eight require assisting or performing.

Missing categories, observing-only categories, incomplete items, and unknown participation are distinguished. A blank participation value is not silently treated as completion. Missing email addresses are flagged in validation and withheld from send-ready student files.

**Power Automate:** existing fields such as `name`, `email`, `missing_items`, and `observing_only_items` remain. Include the new `participation_review_items` and `incomplete_items` in the email body when populated. Category lists retain the `<br>` separator.

### 2. `feedback_reminders_power_automate.csv`

One row per student who has not met at least one of these defaults:

- Eight Clinical Assessment of Student solicitations or submissions.
- Two observed H&P solicitations or submissions.
- One handoff solicitation or submission.

Requirement credit is the union of matched and submitted activity, counted once per distinct preceptor and assessment type within the selected rotation. A request and its resulting submission do not count twice. Separate matched-preceptor, submitted-form, and requirement-credit columns explain the totals. Multiple real submissions by one preceptor remain distinct for evaluation auditing/scoring but do not multiply student solicitation credit.

The `reminderob`, `remindercas`, `reminderhandoff`, `random_preceptor`, and `preceptor_shoutout` columns are retained. The shoutout selection is stable for the same student and review date rather than changing each time the page reruns.

### 3. `preceptor_eval_reminders.csv`

One row per preceptor–student pair, combining any missing assessment types for that rotation.

A submitted matching clinical assessment clears clinical-assessment reminders for that pair regardless of duplicate clinical matches. The same rule applies separately to H&P and handoff assessments. A clinical assessment does not clear a missing H&P. A dropped or manually excluded evaluation still counts as received for reminder suppression.

By default, reminders become eligible on the evaluation-period end date, with configurable grace days. Course dates establish the rotation; the submission date does not have to exactly equal the match-period date. Missing preceptor email addresses are flagged rather than redirected to the clerkship director.

**Power Automate:** the combined file includes `cas_link`, `hp_link`, and `handoff_link`. Include every populated link in the email body, not only `blank_form_link`. No handoff survey URL was defined in the supplied scripts, so that link is blank until configured. The ordinary CAS and H&P links are retained from the old scripts.

The old partial-form links prefill completion/rating fields. They are **off by default**, with an explicit warning if enabled. Ordinary links do not prefill assessment scores.

## Scoring and exclusions

The five original domains are `kp`, `cr`, `do`, `cp`, and `ct`. Each has weight 15 per score point, yielding a 375-point maximum. Missing domain scores are mean-imputed using that evaluation's scored domains for calculation only; original missing responses remain visible. The imputation value and total retain the original rounding rules. An entirely unscored form is received but has no numeric total.

The three original student–preceptor exclusion rules are now **built into the app**. No sidecar file is required. Use **Exclusions → Manage exclusions** to add rules from OASIS selectors or manual entry, restrict them to a specific submitted form or a student–preceptor pair, record a reason, deactivate a rule, or reactivate it later. Clinical assessments only: no rule applies to another student or to H&P/handoff requirements. Active manual rules are applied before the automatic lowest-evaluation drop. The affected submitted assessment remains visible in the audit and still suppresses reminders. It is omitted from new assessment imports; existing REDCap assessments are not deleted. The original script gave no reasons for its three exclusions, so the built-in descriptions explicitly say that.

With an authorized API connection and the new non-repeating notes field `cst_exclusion_rules`, saved rules load automatically each session. After editing, click **Save exclusions to REDCap**. Without that setup, edits are session-only and can be backed up/downloaded as JSON. See **ONLINE_SETUP.md** for the one-time field setup. Inactive saved rules stay inactive after restart, including a disabled original rule. Do not upload the field fragment as a replacement full Data Dictionary.

After manual exclusions, the single lowest total is dropped when at least four scorable clinical evaluations are eligible. With fewer than four, none are dropped. Ties use the earliest submission and then the source form ID for deterministic selection. The app displays before/after clinical scores and the excluded assessment. Professionalism and individual low-domain review flags are retained even when the assessment is dropped.

**This is the clinical evaluation component, not a new final clerkship grading system.** The supplied grading script calculates per-evaluation totals and an exclusion description; it does not establish the complete NBME/final-grade calculation. NBME, final-grade fields, late penalties, deadlines, and existing manual portfolio decisions are not replaced. The app's clinical summary is a transparent calculation, not a claim of parity with unseen REDCap formulas.

## REDCap: preview first, upload explicitly

You can use all reminder and score outputs without connecting to REDCap, after signing in to the app. In that mode, changes to exclusion rules are session-only. A connected session automatically reads saved exclusion configuration, but no network writes occur without explicit buttons.

For a live update, enter an authorized API token in the password input, set `REDCAP_API_TOKEN` as an environment variable, or copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in privately. Never share the token in chat or commit it to a repository. API permissions must allow the necessary record/metadata reads and record imports; actual institutional permission settings have not been checked here.

1. Read current REDCap records and field definitions in the app.
2. Build or refresh the results using that snapshot.
3. Open **REDCap update** and preview the proposed field changes.
4. Review any conflicts and explicitly authorize the import.

The app reuses existing `record_id`, instrument, and repeating-instance identities. New instances append after the existing maximum. It does not renumber, delete, or create student parent records. It rejects ambiguous identities and parent rotation mismatches. Metadata validates mapped choices and dates; calculated, read-only annotated, file, and checkbox fields are not written by this importer.

Existing conflicting nonblank source values are preserved by default. Replacing them requires a separate opt-in. Clearing stale tracker-owned summaries is also a separate opt-in. Unrelated portfolio fields are not included in the payload.

Before writing, the app rereads the selected records and metadata and stops if either changed after preview. Imports are sparse, use batches, and are followed by read-back verification. These checks are not a server-side transaction or lock. A timeout or partial failure may mean some batches saved; refresh the live snapshot and rebuild the preview before retrying. There is no automatic import retry, deletion, or rollback.

### One-time tracking-field setup

Your existing raw assessment, EPA, checklist, preceptor-matching, and supported summary fields can be used without adding a new tracking form. To store the app's additional counts, encounter review status, clinical summary, and generated-reminder details, the package includes **`tracking_fields_to_append.csv`**:

- 23 new `cst_` fields in a nonrepeating `clerkship_tracking` instrument.
- `oasis_form_record` in the existing `oasis_eval` instrument.
- `epa_form_record` in the existing `epa` instrument.

**This CSV is a field-definition fragment, NOT a replacement project Data Dictionary. Do not upload it alone as your complete dictionary.** Back up/export the full dictionary first and merge the new fields into their appropriate instrument groups, or add them using Online Designer with your REDCap administrator. Existing fields must be preserved. Confirm these names are unused and keep `clerkship_tracking` nonrepeating. After adding fields, reread the live metadata and rebuild the preview. The optional `cst_exclusion_rules` configuration field is supplied separately in `exclusion_field_to_append.csv`.

Without these optional fields, the app omits them with a warning and still provides `tracking_status.csv` locally. The JSON sync-plan download is a review artifact with separate ordinary-update and explicit-clear lists, not a CSV intended to be pasted blindly into Data Import.

### Existing legacy records

Your old script changed submission times to 23:59. A unique source form can be matched to a unique legacy record on the same day, preserving the existing repeat ID and legacy timestamp. If several forms could match the same date-only record, syncing stops rather than guessing. Adding source Form Record fields helps future imports but does not automatically resolve an existing ambiguity; that mapping needs review.

The supplied July records include one such ambiguity in the offline preview. No upload was performed. If REDCap contains clinical assessments absent from the uploaded ME export, clinical tracking/exclusion updates are withheld to avoid replacing a complete summary with partial-source calculations.

This version targets the classic, non-longitudinal project structure in the supplied `0959` export. An event-based project requires additional explicit event mapping.

## Other downloads and privacy

The report ZIP includes complete student status tables, clinical scores, evaluation and matching audits, tracking status, source summaries, and validation messages. These reports can contain student identifiers and evaluation narratives; keep them in approved private storage. The app does not record generated files as emails sent. Email-delivery tracking would require a confirmed result from Power Automate or another sending system, not a guess at generation time.

The application package contains no source student CSVs or API credentials. **The Python source itself embeds three confidential student-specific exclusion rules. Keep the repository and any container image private.** `.gitignore` does not remove those embedded identifiers and is not a substitute for repository privacy. Keep the deployed app restricted to authorized viewers as well.

CSV import tolerates duplicate activity headers and trailing empty cells. Unlike the old heuristic repair, it refuses rows with unexpected extra comma-separated fields rather than guessing which column to merge. Re-export or correct such a CSV, then rerun. No malformed row is silently skipped.

## Validation and limits

See `VALIDATION_REPORT.md` and `TEST_RESULTS.txt`. The original 58 regression tests plus 52 online-exclusion tests passed in this build. The new persistence tests use an in-memory fake API, never your live REDCap server. The Streamlit package could not be installed here, so browser rendering and real widget interactions were not tested. No cloud deployment, live REDCap write, email delivery, or final clerkship-grade parity test was performed.

```powershell
python -m unittest -v test_tracker test_online
```
