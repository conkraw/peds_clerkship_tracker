# Online setup — Pediatric Clerkship Tracker

Version 1.1.0 · September 19, 2026

The app is prepared for browser-based Streamlit hosting. After deployment, upload your CSVs and use the app in your browser; running Python on your own computer is not part of routine use. This package has not been deployed to your account.

## Before putting student data online

Use hosting approved by your institution for identifiable student assessments, a **private code repository**, and restricted, authorized viewers. This version embeds your three original student–preceptor exclusions in the Python source. Do not put the source, its container image, uploaded CSVs, or exclusion backups in public storage. There are no API credentials or source student CSVs in this package.

Streamlit's current documentation describes private apps and viewer restrictions, while the current marketing page also says Community Cloud is for public apps only. Confirm that **Only specific people can view this app** is available in your account before using Community Cloud for this workflow. Do not make it public to work around a private-app limit. Use an institution-approved private Streamlit server instead when restricted hosting is unavailable or not approved. A Dockerfile is included for an administrator; TLS, user authorization, network policy, and persistence are hosting responsibilities. The package is not a claim of institutional, HIPAA, or FERPA approval.

## 1. Put the app files in a private repository

Extract the ZIP. Put the contents of the `peds_clerkship_tracker_online` folder in your **private** GitHub repository. The required deployment files are:

```
peds_clerkship_tracker.py
requirements.txt
.streamlit/config.toml
```

The README, setup guide, tests, and field-definition fragments may also remain in that private repository. Do not upload the original student CSVs or the actual Secrets file. An old `private_exclusions.json` is **not required**: the original rules are now built in. A stale copy of that sidecar is not automatically loaded; use the backup importer for any additional saved rules.

For an existing private Streamlit deployment, replace its app file with this version and update `requirements.txt`. The entry-point filename remains `peds_clerkship_tracker.py`.

## 2. Create or update the Streamlit deployment

In your Streamlit workspace, create an app from the private repository. Select the repository, branch, and the entry-point file. Use Python 3.12 in Advanced settings (the pure processing code also supports Python 3.10+).

If you uploaded the containing folder rather than its contents, enter the full relative path, such as `peds_clerkship_tracker_online/peds_clerkship_tracker.py`. Keep `requirements.txt` next to the app.

Set viewer restrictions before uploading any student data. Only authorize the clerkship staff who should see the complete dataset and manage exclusions.

## 3. Configure Secrets

In Streamlit's Advanced settings during deployment, or the deployed app's Settings → Secrets, paste the following and replace the password. Add your authorized REDCap API token there, not in the code or chat.

```toml
APP_PASSWORD = "CHANGE_ME_to_a_unique_long_password"
REDCAP_API_URL = "https://redcap.ctsi.psu.edu/api/"
REDCAP_API_TOKEN = ""
```

The app rejects the placeholder and requires a unique password of at least 16 characters before displaying uploads or exclusion details. This is an additional shared-password gate, not enterprise authentication or individual user auditing. Keep platform-level access restricted as well. A logout button clears the current session.

A REDCap token is optional for offline scoring and reminders. It is required for persistent exclusion saving, automatic loading of previously saved rules, and live REDCap imports. Give the API account only the project permissions it needs for record/metadata/repeating-configuration reads and record imports. The host must be permitted to reach your institutional REDCap endpoint; network approval has not been tested here. Do not disable certificate verification to bypass a connection failure.

An institution-administered host with its own authentication and authorization may set `TRUSTED_HOST_AUTH = true` instead of the shared password. That flag does not itself protect the app. The default is false.

## 4. Add one REDCap field to remember exclusions

Your original three exclusions work immediately, without a REDCap change. Additional or deactivated rules otherwise remain **session-only**, as the app clearly indicates.

For durable saving, use REDCap Online Designer to add a **non-repeating internal instrument** and one **Notes Box** field:

| Setting | Value |
|---|---|
| Instrument name | `clerkship_exclusions` |
| Field name | `cst_exclusion_rules` |
| Field type | Notes Box |
| Use as a survey | No |
| Repeating instrument | No |

Do not reuse an existing clinical score, exclusion-description, or narrative field. Leave this new notes field blank initially. The app writes its JSON content. Ensure the API account can read and write this instrument.

`exclusion_field_to_append.csv` provides the same field definition. It is **only a fragment**. Do not upload it alone as your complete project Data Dictionary. Add the field in Online Designer, or merge it into a backup of the full dictionary while preserving every existing field.

After adding it, open **Exclusions → Manage exclusions** and choose **Reload saved exclusions**. With the token and field configured, the app automatically reads saved rules on first opening each session. Reads do not change REDCap. A connection or stored-JSON error blocks processing rather than silently ignoring a saved exclusion.

## Routine exclusion workflow

Upload the OASIS ME file. In **Exclusions → Manage exclusions**, select the student and preceptor. Choose either **This submitted evaluation only** (the default) or **All clinical evaluations for this student–preceptor pair**. Enter a reason and click **Add exclusion**. Manual entry is available when the desired student/preceptor is not in the current OASIS file.

To remove an exclusion, select its rule, uncheck **Exclusion active**, and apply the change. The inactive rule remains visible and can be reactivated. A saved inactive original rule overrides the built-in default on the next session; it does not silently reappear.

Click **Save exclusions to REDCap** to retain additions, reasons, and active/inactive changes for future sessions. The app verifies the stored values. Existing student records must already exist; this operation does not create students, delete assessments, or update grade fields. It stores a timestamp and a generic app-operator label, not an invented individual identity.

Then rebuild results. Before a grading/raw-data upload, reread the current REDCap reference after saving exclusions; the earlier live snapshot is deliberately discarded. Scoring excludes active manual rules before applying the existing automatic lowest-evaluation drop. Submitted excluded evaluations remain received for reminder suppression and visible in the audit. They are omitted from new assessment imports; previously stored assessments are not deleted.

Avoid editing the same student's rules in multiple sessions at once. The app checks for intervening changes, but the REDCap API operation is not an atomic compare-and-swap transaction. On an error/timeout, download a rules backup, reload and inspect saved state, and reconcile before retrying. Do not assume a failed response means nothing was written.

## What persists

Saved rules live in REDCap, not the Streamlit server filesystem. No local-disk persistence is assumed. New browser sessions reload saved rules; an already-open second session must use **Reload saved exclusions** to pick up changes. The three embedded originals are the fallback only for students without a saved rule set.

A downloaded JSON backup is also supported. Use **Merge uploaded backup**, review the resulting rules, and explicitly save to REDCap. Without persistence setup, session edits can disappear on a browser reconnection/reload, app restart, or redeployment. Source uploads and result tables are session-local and are not saved to GitHub.

## Validation before production use

The package includes synthetic tests with a fake REDCap API. No live REDCap connection, hosted deployment, browser interaction, institutional network, or cloud privacy setting was exercised in this build environment. Test with synthetic files first and use an approved test project or small reviewed import before production uploading. See `VALIDATION_REPORT.md`.

## Official references checked September 19, 2026

- Deployment: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy
- Private apps and viewer restrictions: https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app
- Marketing page with differing public-app wording: https://streamlit.io/cloud
- Secrets management: https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management
- Local storage is not guaranteed: https://docs.streamlit.io/develop/concepts/connections/connecting-to-data
- Private-server Docker deployment: https://docs.streamlit.io/deploy/tutorials/docker
