# MPF Provider Directory Validation: GitHub Actions automation

Two triggers, one validation script.

```
Schedule (every 15 min) ──┐
                          ├─> detect ──> validate <contract> (existing validate_2.py) ──> report ──> HTML email
Manual (Run workflow) ────┘                                                              └─> update state
```

## Folder structure

```
.github/workflows/mpf_validation.yml   workflow (schedule + workflow_dispatch)
scripts/mpf_monitor.py                 detect / run / merge-state
scripts/email_report.py                HTML email + SMTP
validate_2.py                          YOUR existing validation script (reused, not duplicated)
state/mpf_state.json                   last processed last_updated per contract
state/mpf_failures.json                failures already e-mailed (prevents repeat e-mails)
reports/                               generated files (also uploaded as run artifacts)
requirements.txt                       python-docx, openpyxl
```

If you prefer the suggested name, `git mv validate_2.py scripts/mpf_validation.py` and set the repository
variable `MPF_VALIDATION_SCRIPT` (or edit `VALIDATION_SCRIPT` in `mpf_monitor.py`).

## Where it connects to the existing script

1. The automation runs `python validate_2.py <org> <contract> <index_url>`. This is the script's existing
   single-contract mode. No validation rule was changed.
2. The script already produces `contract_summary_<C>.docx` and `validation_report_<C>.xlsx`. These are
   renamed `<C>_contract_summary_<date>.docx` / `<C>_validation_report_<date>.xlsx` and attached to the email.
3. One small addition to `validate_2.py`: `write_run_status_json()` (called from `main()`) writes
   `run_status_<C>.json` (pass/fail counts, failing codes, failed vs passed resource types, expected/actual).
   The email and the GitHub summary are built from that file.

## Change detection and state

* Each contract's `index.json` has a top-level `last_updated` (for example `2026-09-29T10:12:24.000Z`).
* Current equal to stored: **NO CHANGE**, no job is created, no report, no email.
* Current different from stored (or never processed): **UPDATED**, only that contract gets a job.
* `state/mpf_state.json` is advanced only after the validation completed (result COMPLETED, or FAILED meaning
  data failures were found). Script failures and download failures do **not** advance it, so the next cycle retries.
  Those failures are e-mailed once per update, not every 15 minutes (`mpf_failures.json`).
* Contracts are independent matrix jobs. A change in H1619 never triggers H3124, H5826 or H9207.

## Result types (shown in the GitHub summary and the email)

| Type | Meaning | Advances state | Job |
|---|---|---|---|
| COMPLETED | no failing checks | yes | green |
| FAILED (A) | validation failures found in the data | yes | green + warning annotation |
| SCRIPT_FAILURE (B) | script crashed / timed out / no report | no | red |
| DOWNLOAD_FAILURE (C) | C4001/C4002/C4003, data not downloadable | no | red |
| Email failure (D) | SMTP problem, validation itself completed | yes | red, reports stay in run artifacts |

## Setup

1. Put these files in the repository (`kkarthikeyank/maplancopy`) and push to the default branch.
   Scheduled workflows only run from the default branch.
2. **Settings > Secrets and variables > Actions > Secrets** (you already created these):
   `EMAIL_USERNAME` (your Gmail address), `EMAIL_PASSWORD` (a Gmail **App Password**, not your normal password),
   `EMAIL_TO` (recipients, separated by commas), `SMTP_SERVER` = `smtp.gmail.com`, `SMTP_PORT` = `587`
   (`465` also works and uses SSL).
3. **Gmail App Password:** turn on 2-Step Verification for the account, then open
   https://myaccount.google.com/apppasswords, create an app password (name it "MPF automation"), and paste the
   16-character value into the `EMAIL_PASSWORD` secret (no spaces). Workspace accounts may need the admin to allow
   app passwords. Gmail also limits each email to 25 MB.
4. **Settings > Actions > General > Workflow permissions**: allow read and write (needed to commit `state/`).
   The workflow already requests `contents: write` for that one job.
5. Secrets are masked by GitHub and this code never prints them.
6. First automatic run: state is empty, so all four contracts count as "never processed" and will be validated and
   e-mailed once. To avoid that, seed `state/mpf_state.json` with the current values, for example
   `{"H1619":"2026-09-26T01:36:46.000Z","H3124":"...","H5826":"2026-09-29T10:12:24.000Z","H9207":"..."}`
   (the detect step prints each current value in the run summary).

Attachment size: the H5826 Excel report is about 115 MB. Email limits are about 20 MB, so the tool attaches the
Word summary always, and the Excel report only if it (or a zip of it) fits under `MAX_ATTACH_MB` (default 14).
Otherwise the email says so and links to the run, where every report is kept for 30 days as an artifact.

## How to test

**Manual mode:** Actions > *MPF Provider Directory Validation* > Run workflow > Contract = `H5826` > Run.
Only H5826 is validated; expect email subject `[MPF MANUAL VALIDATION] H5826 – Validation Report`.
Choose `ALL` for all four contracts (four independent jobs, four emails).

**Automatic mode:** Run the workflow from the Actions tab, or wait for the cron. The detect step's summary table
shows each contract's status.

**Unchanged contracts are skipped:** with `mpf_state.json` equal to the current values, the run summary shows
`NO CHANGE` for all four, the `validate` and `update-state` jobs do not appear, and no email is sent.

**Only an updated contract reports:** edit `state/mpf_state.json`, set `H1619` to an older value
(`"2026-09-01T00:00:00.000Z"`), commit, and run the workflow. Only `validate H1619` runs and only an H1619
email arrives. Afterwards the state file is updated back to the current value.

**PC can be off:** everything runs on GitHub-hosted runners. Shut the PC down, then look at the Actions tab
later: scheduled runs continue every ~15 minutes (GitHub may delay cron by a few minutes at busy times).
Note: GitHub disables scheduled workflows after 60 days without repository activity; the state commits count as
activity.

## Example logs

Successful (H3124 updated):

```
Contract  Current      Previous     Status
H1619     09/26 01:36  09/26 01:36  NO CHANGE
H3124     09/30 08:42  09/29 11:30  UPDATED
H5826     09/29 10:12  09/29 10:12  NO CHANGE
H9207     09/26 01:39  09/26 01:39  NO CHANGE

RUNNING VALIDATION: H3124
Download files          PASS
Parse JSON              PASS
Reference validation    PASS
FHIR validation         PASS
Business validation     PASS
Generate report         PASS
Email report            PASS
H3124 = COMPLETED
```

Data failures found:

```
RUNNING VALIDATION: H5826
FHIR validation         FAIL
Business validation     PASS
Generate report         PASS
Email report            PASS
H5826 = VALIDATION FAILED (data issues found)
Failed checks: A2009 MissingProviderPhoneNumber | 125 | OrganizationAffiliation
```

Download failure: `::error title=H5826::DOWNLOAD FAILURE`, state not advanced, one email.
