# How the attached student review form informs REDCap updates

Source: `PEDIATRIC CLERKSHIP GRADER DATABASE 2026-2027 _ REDCap.pdf`, 13 pages, supplied in this conversation. It shows the Online Designer view of `pediatric_clerkship_achievement_portfolio`.

## Confirmed from the form

Pages 1–3 display assessment solicitations/submissions (`oasissolicit`, `distinct_count`, `obhp_s`, `obhp_submissions`, `obho_s`, `obho_submissions`), five competency summaries, evaluator feedback, and preceptor lists.

Pages 4–5 display individual repeating clinical assessment fields (`evaluator`, `kp`, `cr`, `do`, `cp`, `ct`, `tot`, `iv`), the `exclude` note, and clinical-grade displays. The rendered table explicitly references repeat instances 1–20.

Pages 5–6 display the repeating EPA fields (`epa_evaluator`, `epa_obh_score`, `epa_obp_score`, `epa_obho_score`). Pages 6–8 display the ten encounter categories and related participation fields. Pages 9–13 contain submission timing, course grading, and additional formative-assessment summaries.

## What this build writes

The upload planner continues to use the existing raw-data instruments identified in the earlier CSV/scripts: `oasis_eval`, `epa`, `checklist_entry`, and `preceptor_matching`. It also prepares the existing source-derived `exclude` and, only when the checklist is complete, `submitted_ce` summaries. Existing installed tracker fields can be updated, but their installation is not required.

The app fetches actual field definitions from REDCap before a live upload. Calculated, `@CALCTEXT`, `@READONLY`, descriptive, file, and checkbox fields are not overwritten by the generic importer. Conflicting nonblank source values are preserved for review. Existing repeat instances are reused; ambiguous same-day legacy records cause an upload-review issue rather than guessed instance numbers.

## What the PDF does not establish

The document identifies labels, field names, and some action tags; it does not expose the complete formulas or all field validation/choice definitions. It is therefore not a replacement for live project metadata or the full Data Dictionary.

The app does not reconstruct or change the portfolio's formulas, NBME calculation, final grade, penalties, professionalism decisions, or manually entered adjustments. Do not assume that a source-data import or the PDF alone proves every calculated display has refreshed correctly or applies the same exclusions as the Python score calculation. Verify the portfolio after a small approved test update.

The app's manual exclusions affect Python scoring and omit the excluded assessments from new imports. They do not delete assessments already present in REDCap. Existing REDCap formulas might still include previously imported excluded assessments. Reconcile those formulas/records with the director before using the portfolio's displayed grade as a final result.

The form's detailed clinical and EPA tables show only instances 1–20. The app preserves numbering rather than squeezing records into that display. Later instances can exist without appearing in that fixed table; changing the form's display is a separate REDCap-design task.

## Small submission-date safeguard

The form labels `submitted_ce` as the date/time ALL clinical encounters were submitted (page 9). Earlier app versions populated it from the latest checklist entry even when requirements remained incomplete. Version 1.2 prepares that field only when all ten categories satisfy the current completion/participation checks. For a complete checklist, it retains the prior latest-entry-date approach; it does not attempt to reconstruct a different historical completion-time policy. Existing values are not automatically cleared.

The standard update also does not automatically clear an old `exclude` summary when it becomes empty. It flags this for director review rather than changing stored grade-related values without explicit handling.
