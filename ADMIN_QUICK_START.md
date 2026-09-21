# Clerkship tracker: admin instructions

## Regular workflow
Open the approved staff-only app and sign in. Upload the rotation schedule,
updated checklist, preceptor match file, and OASIS ME export. Use complete files
covering the same rotation. Click **Create files**.

Download **all three reminder files**, then unzip them into the folder used by
Power Automate. Review the displayed row counts and file-check notices first.
The ZIP contains only these mailing files:

| File | Used for |
|---|---|
| `student_checklist_review.csv` | Students with missing or insufficient encounter logging |
| `feedback_reminders_power_automate.csv` | Student requests for evaluations, observed H&Ps and handoffs |
| `preceptor_eval_reminders.csv` | Missing preceptor assessments, combined per student–preceptor pair |

The filenames, columns and reminder text match the preceding Streamlit app.
Use the latest generated set; do not send both the combined-preceptor files and
the optional older separate-flow files for the same run. A file with only a
header means no reminders were found in the selected, verified source data.

## REDCap, only after the director creates the new project
Click **Download REDCap import file**. Import this unchanged CSV into the NEW
project using the settings in `REDCAP_SETUP.txt`. No API token, existing REDCap
export or dictionary upload to the app is needed. Do not use the old project.
Creating the CSV does not upload records or send messages.

## A file-check notice
Different rotation dates usually mean the wrong source files were selected.
Get the matching complete exports; do not assume missing source rows mean the
student did nothing. A director can confirm that an empty export really covers
the rotation. If a student's ID or email is unknown, the source needs correction.
The app will not invent an email recipient. Keep the OASIS Form Record column.

## Leave director tools alone during routine use
The director manages exclusions, target counts, dates and detailed score review.
Changing a file or an exclusion means clicking **Create files** again. The app
hides stale downloads after source/settings changes.

Keep source CSVs and generated outputs in approved restricted storage. They do
not belong in the GitHub repository. Sign out when finished.
