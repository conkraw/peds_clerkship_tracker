# Clerkship reminders and optional REDCap import

## Every reminder run

1. Open the app and sign in.
2. Upload the **rotation schedule**, **updated checklist**, **preceptor match file**, and **OASIS ME evaluation export** for the same rotation.
3. Click **Create reminder files** and check the warnings.
4. Download the three reminder CSVs, or **Download all three reminder files**, and use them in your configured Power Automate flows.

No API token, Data Dictionary, or full REDCap export is required for an ordinary run when the uploaded files identify all students. Ask the director about missing-email or mismatched-rotation warnings. A blank file does not establish completion when warnings are present.

## To update REDCap without an API

1. Get a fresh **full raw-data CSV export** from the same REDCap project. It must include all student records, fields, and repeating instances. Do not use only a rotation report or the student-review PDF.
2. In the app, open **Download REDCap import file — no API required**. Upload that export, confirm it is complete/current, then select **Prepare REDCap import file**.
3. Review the checks and download **redcap_import.csv**.
4. In REDCap, open **Applications → Data Import Tool**. Use **real-time import**, show the **comparison table**, **keep existing record IDs**, and set **Overwrite data with blank values: NO**. Upload the CSV directly, review it, then confirm the import.

Do not import when REDCap shows unexpected new student records, unexplained changes, or validation errors. Ask the director. Get a new full export before the next batch, or whenever someone changes the project data. Do not open and resave the import CSV in Excel. The full instructions are in `MANUAL_REDCAP_IMPORT.md`.

## API upload is still optional

When configured by the director, **Update REDCap — optional** retains the existing preview-and-confirm upload workflow. You do not need to use that section for manual imports.

This app does not send email. Downloading a reminder or import file does not mark it as sent or imported. Keep the files private and store them only in approved locations.
