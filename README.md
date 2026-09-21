# Pediatric Clerkship Tracker — fresh project edition

Version 2.0.0 • schema `clerkship_flat_v1`

**Four CSV uploads → Create files → reminder downloads + REDCap import CSV.**

No REDCap API, full-project export, metadata upload, old-project reconciliation,
repeat-instance numbering or import-history file is required. REDCap does not
need to exist before the app can process data. The existing processing, scoring
and mailing rules were retained; the interface and REDCap export are rebuilt.

## Replace the deployed app
Replace **both** `peds_clerkship_tracker.py` and `requirements.txt` in the current
private repository. Keep the existing entry-point filename and folder. Commit
the changes; Streamlit Community Cloud monitors its connected repository.
Use Python 3.11 or 3.12. Existing valid `APP_PASSWORD` Secrets can remain.
No REDCap API setting is used; any old REDCap Secrets are unnecessary.
Do not commit a real `.streamlit/secrets.toml` file. The included `.example` is
only a template. An app password does not replace institution-approved hosting
and authorized viewer access.

Only the app and requirements are required for deployment. The optional
`.streamlit/config.toml` caps uploads and disables usage statistics. No local
exclusion sidecar file is needed. Keep the repository **private**: the Python
source retains the three original student-specific exclusion rules by request.
Never add source student CSVs, output CSVs, passwords or tokens to the repository.

Local development (not required for online users):

```bash
python -m pip install -r requirements.txt
python -m streamlit run peds_clerkship_tracker.py
```

## Inputs and normal outputs
See `ADMIN_QUICK_START.md`. The routine upload fields are rotation schedule,
updated checklist, preceptor matches, and OASIS ME evaluation export. The rotation
schedule selects the students. Include an external ID and email when available;
otherwise the other sources must resolve them uniquely. A name-only student with
no other source activity cannot be reliably assigned an ID or mailing address.
An omitted end date is resolved from matching sources, then defaults to a
26-calendar-day inclusive rotation. The director can change that fallback.

The three primary mailing CSVs keep their preceding Streamlit layouts. Director
downloads include clinical scores, evaluation audit, checklist entries, matches,
all-student statuses, tracking status, validation notices, roster and source
summary. The optional older separate CAS/H&P flow layouts remain available, but
must not be sent alongside the combined-preceptor file for the same batch.

Existing clinical/observed-H&P survey links are retained. No handoff survey URL
was supplied: that link remains blank until configured by the director.
Prefilled score shortcuts remain off unless the director enables the legacy
option. The app never emails anyone or marks a mailing as sent.

## Scoring and exclusions
Five equally weighted clinical domains yield up to 375 points. Missing domains
are mean-imputed from the available observed domains for calculation only;
original scores are preserved. All missing means unscorable, not zero.
Manual exclusions occur before the automatic lowest-evaluation drop. Exactly
one lowest evaluation is dropped if at least four scorable, non-manually-excluded
evaluations remain. Ties use submission date and source Form Record. Clinical
scores are not final clerkship grades; this app does not invent NBME, late-penalty
or final-grade inputs. Individual low scores and professionalism flags remain
visible for director review, including on excluded evaluations.

The three original student–preceptor exclusion rules load automatically.
**Show director tools → Exclusions** allows adding a pair-specific or submitted-
form-specific rule, toggling its active flag, and updating a reason. Rules never
exclude a preceptor globally across other students. Received evaluations still
suppress reminders even when excluded or automatically dropped.

New changes are browser-session-only unless saved externally. Download the
exclusions backup after editing; restore it next time, or paste its complete JSON
into the `EXCLUSIONS_JSON` Streamlit Secret to use it as the startup default.
That Secret replaces the entire startup rule set, including deactivated rules.
Keep it absent to retain the built-in defaults. There is no automatic database
save. The REDCap summary stores the rules used for audit, but this app does not
read them back from REDCap.

## New REDCap project
The package includes a **complete matching Data Dictionary** with 142 fields,
an empty column template, and a synthetic example import. Upload the dictionary
to a NEW classic project, not the existing project. No repeating instruments or
events are used. See `REDCAP_SETUP.txt` for detailed import settings.

Every item has a separate ordinary record: student summary, clinical evaluation,
observed H&P, handoff, checklist entry, or preceptor match. The `student_key`
links all records for one student and rotation. Filter `record_type = 1` to
review one summary row per student; summaries also contain readable individual
assessment and checklist detail. A summary record is not a repeating parent.

The importer contains all evaluations, INCLUDING manual exclusions, with clear
numeric inclusion flags. This deliberately differs from the original script's
omission of manually excluded evaluations from its REDCap upload. No received
assessment is erased merely because its score is excluded.

Each `record_id` is a 128-bit digest of the source identity, with a record-type
prefix. It is not a row number and does not change when source rows are reordered.
Existing items keep their IDs when scores, comments or exclusion flags change.
Preserve these IDs during import. Fields containing raw source JSON preserve
additional OASIS answers and checklist columns without requiring extra fields.

**Overwrite data with blank values = YES in this new generated-only project.**
This clears obsolete generated values (for example, a previously dropped form).
Store any manual notes, NBME data and final decisions on a separate instrument,
with field names absent from the import. Do not reuse this overwrite instruction
in the old project. Review the real-time import comparison table before saving.

### Limits of an offline import
Use complete source exports. A file download is not an import confirmation.
No offline app can see edits made directly in REDCap. Missing rows are not deleted
by later imports. Changes to identifying fields can leave older rows in REDCap;
filter `batch_id` for the desired run or reconcile those rows manually. Import the
newest batch last. Do not calculate grades by summing every historical detail
record. Current summary rows reflect the uploaded snapshot and exclusions.

## Tests and examples
Run `python -m unittest discover -v` in this directory. Processing tests require
only the standard library; interface tests use a small fake Streamlit, NOT a real
browser or Streamlit AppTest. See `VALIDATION_REPORT.md` for the actual results.
The `examples/` folder contains fictional data only and is not a mailing input.
Use it to check a new development REDCap project before any real student import.
The live Streamlit/cloud UI and the live REDCap import have not been tested here.

## Documentation references
Streamlit repository updates:
https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/edit-your-app

Streamlit Secrets:
https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management

CU Anschutz REDCap Data Import Tool:
https://redcapucdenver.zendesk.com/hc/en-us/articles/31248111520276-Data-Import-Tool
