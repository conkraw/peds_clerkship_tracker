# One-time online setup — director only

## Already deployed

Replace the existing `peds_clerkship_tracker.py` in the repository and branch used by your deployed app. Keep the same file path. This version does not add dependencies. Keep the existing `requirements.txt` and Streamlit Secrets, or use the identical requirements file in this package.

Streamlit Community Cloud monitors committed repository changes. After committing the replacement, reopen the app. If the previous screen remains, reboot it from the app's menu. Do not reboot while another operator is using the app.

Official references (accessed September 20, 2026):
- https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/edit-your-app
- https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app/reboot-your-app

## Automatic REDCap lookup

In your deployed app's **Settings → Secrets**, keep the existing `APP_PASSWORD` and add the REDCap connection values if they are not already present:

```toml
REDCAP_API_URL = "https://redcap.ctsi.psu.edu/api/"
REDCAP_API_TOKEN = "REPLACE_WITH_YOUR_AUTHORIZED_PROJECT_TOKEN"
```

Do not paste real credentials into chat or GitHub. Configure these once; the routine user never sees an API-token input. The app uses the same secret names as version 1.1. Do not add a second duplicate entry for a secret that is already present.

Official reference:
https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management

Clicking **Create reminder files** makes a fresh read of the configured project and metadata. That reference supplies student identity information and saved exclusions. Download clicks do not re-fetch the database. A fresh read is also made before preparing an update, and the upload verifies the approved records did not change in the meantime.

The token must be authorized for the intended project and for the required read operations. Direct uploading additionally requires import rights. Saved-exclusion validation uses the repeating-forms/events configuration. Do not grant broader access than the institution approves. No live credentials were tested in this build.

Without a token, reminder generation works from the four inputs when those files identify the students. A collapsed optional-reference uploader is available for missing identities. The PDF review form is a design view, not a student-data export.

## No API access, or no API import permission

No new Secrets are needed for the manual fallback. Keep the existing app-authentication configuration. The admin can upload the four routine inputs, create reminders, then open **Download REDCap import file — no API required** and supply a fresh full raw-data CSV export from the destination project. The app prepares `redcap_import.csv` for REDCap's Data Import Tool without calling the API.

If read access already supplies a current reference, the manual download can use that reference without calling the import API. An API read failure does not hide the separate manual-export section. Uploaded references are parsed and kept only in the current session; they are not committed to GitHub.

The administrator still needs the REDCap permissions required to export the reference and use the Data Import Tool. Manual imports cannot verify changes made after the reference was taken. Coordinate users to prevent intervening edits, and use a new export for every later batch. Optional Data Dictionary validation is available inside the manual section, not part of ordinary reminder generation.

The manual data CSV does not update the saved exclusion-rule configuration. When API rule saving is unavailable, the director must download a JSON backup of added/changed rules and restore it in a later session. The original three built-in rules continue to load automatically. Saved rules already included in the full export are reconciled for the manual import.

## Privacy and access

Keep the source repository private because it includes the three original student-specific exclusions. Restrict the deployed app to authorized personnel on an institution-approved host. A private source repository does not, by itself, restrict deployed-app viewers. The app password is an additional gate, not a certification of institutional compliance. `Show director tools` changes the layout only; it is not role-based authorization.

The code does not cache student records globally. Inputs, downloaded-file contents, and reference reads are kept in the current app session. Uploaded student files are not automatically committed to GitHub. Clear the session/sign out when finished. Do not upload source records as repository files.

## Exclusions

The three original rules are built in. Add or edit rules in **Show director tools → Exclusions**. To save added/changed rules across sessions, retain or install the existing `cst_exclusion_rules` Notes Box on a non-repeating, internal instrument called `clerkship_exclusions`. The field-definition download remains inside the exclusion manager. Do not replace your complete REDCap Data Dictionary with a one-field fragment.

A failed saved-rule read does not silently approve potentially stale scoring. Reminder matching can continue because received assessments count regardless of grading exclusions; API-based score downloads and writes stay unavailable until the saved rules can be verified. The manual import path can separately verify saved rules in an uploaded complete export. Unsaved edits are not overwritten by a reference refresh.

## Power Automate: choose a layout once

The main three downloads use the combined layout introduced in version 1.1. The director should make the combined preceptor flow show all populated assessment links and the checklist flow show the participation/incomplete fields.

A compatibility ZIP is available under director tools for the original separate clinical-assessment and H&P flows. It has the original header names and order, and includes `observed_hp_reminders.csv`. Do not send both versions for the same run. Partial-form links remain blank unless the director opts into their old prefilled-rating behavior.

Review one test reminder in the actual flow before bulk mailing. No live email flow was invoked here.
