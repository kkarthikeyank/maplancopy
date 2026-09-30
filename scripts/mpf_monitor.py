#!/usr/bin/env python3
"""MPF Provider Directory automation: change detection + per-contract run.

Sub-commands (all used by .github/workflows/mpf_validation.yml):

  detect        Read each contract's index.json, compare last_updated with
                state/mpf_state.json, decide which contracts to run, and write
                the job matrix. Contracts with NO change are never run.
  run           Run the EXISTING validation script for ONE contract, build the
                report files, send the HTML email, write the GitHub summary.
  merge-state   Fold the per-contract results into state/mpf_state.json.

Contracts are fully independent: a change in H1619 only ever produces an
H1619 run, report and email.
"""
import argparse
import datetime as dt
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import email_report  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(ROOT, "state", "mpf_state.json")
FAIL_FILE = os.path.join(ROOT, "state", "mpf_failures.json")
REPORTS_DIR = os.path.join(ROOT, "reports")
WORK_DIR = os.path.join(ROOT, "work")
RESULTS_DIR = os.path.join(ROOT, "results")
# The EXISTING validation script -- reused as-is. Override with MPF_VALIDATION_SCRIPT
# if you move/rename it (e.g. to scripts/mpf_validation.py).
VALIDATION_SCRIPT = os.environ.get("MPF_VALIDATION_SCRIPT", os.path.join(ROOT, "validate_2.py"))
PLAN_YEAR = "2027"

CONTRACTS = {   # contract -> (org, index.json URL)
    "H1619": ("JHP", "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h1619/2027/index.json"),
    "H3124": ("JHP", "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h3124/2027/index.json"),
    "H5826": ("CHPW", "https://medicare-advantage-plan-finder-provider-directory.interop.chpw.org/h5826/2027/index.json"),
    "H9207": ("JHP", "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h9207/2027/index.json"),
}
ORDER = ["H1619", "H3124", "H5826", "H9207"]
DOWNLOAD_CODES = {"C4001", "C4002", "C4003"}          # could not reach/download the data
STAGE_CODES = [                                        # console "stage" -> error-code prefixes
    ("Download files", ("C4001", "C4002", "C4003", "C4011", "C4012", "C4013", "C4014", "P1017")),
    ("Parse JSON", ("C4004", "C4015", "C4016", "C4017", "C4018", "N3015")),
    ("Reference validation", ("F5001", "F5002", "F5003", "F5004", "F5005", "F5006", "F5007", "F5008", "F5009")),
    ("FHIR validation", ("A2001", "A2002", "A2003", "A2004", "A2005", "A2006", "A2007", "A2008", "A2009", "A2010", "P1013", "P1014")),
    ("Business validation", ("N3001", "N3002", "N3003", "N3004", "N3005", "N3006", "N3007", "N3008", "N3011", "N3012", "N3013", "N3014",
                              "P1001", "P1004", "P1006", "P1007", "P1008", "P1009", "P1010", "P1011", "P1012", "P1016", "P1018")),
]


# ---------------------------------------------------------------- helpers
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def set_output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")


def summary(md):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    print(md)


def short(ts):
    """'2026-09-29T10:12:24.000Z' -> '09/29 10:12' (for the summary table)."""
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return d.strftime("%m/%d %H:%M")
    except Exception:
        return ts or "-"


def fetch_last_updated(url):
    """Returns (last_updated, error). Primary signal: index.json 'last_updated'.
    Fallback (field absent): the Last-Modified header."""
    err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mpf-monitor/1.0", "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                lu = body.get("last_updated") or resp.headers.get("Last-Modified")
            if lu:
                return str(lu), None
            err = "index.json has no last_updated value"
        except Exception as e:     # network / TLS / JSON -- reported, never fatal to the other contracts
            err = f"{type(e).__name__}: {e}"
        time.sleep(3 * (attempt + 1))
    return None, err


# ---------------------------------------------------------------- detect
def cmd_detect(a):
    state = load_json(STATE_FILE, {})
    selected = ORDER if a.contract.upper() == "ALL" else [a.contract.upper()]
    rows, include, statuses = [], [], {}
    for c in ORDER:
        org, url = CONTRACTS[c]
        cur, err = fetch_last_updated(url)
        prev = state.get(c)
        if a.mode == "manual":
            if c not in selected:
                st = "NOT SELECTED"
            else:
                st = "MANUAL RUN"
                include.append({"contract": c, "org": org, "url": url, "previous": prev or "", "current": cur or ""})
        else:
            if err:
                st = "CHECK FAILED"
                print(f"::warning title={c} index check failed::{err}")
            elif cur == prev:
                st = "NO CHANGE"
            else:
                st = "UPDATED"
                include.append({"contract": c, "org": org, "url": url, "previous": prev or "", "current": cur})
        statuses[c] = st
        rows.append((c, cur, prev, st))

    lines = [f"## MPF Provider Directory Validation", "",
             f"**Run mode:** {a.mode.upper()}  |  **Run time:** {now_utc():%Y-%m-%d %H:%M} UTC", "",
             "### Contract status", "",
             "| Contract | Current update | Previous update | Status |", "|---|---|---|---|"]
    for c, cur, prev, st in rows:
        lines.append(f"| {c} | {short(cur) if cur else 'unavailable'} | {short(prev) if prev else 'never processed'} | **{st}** |")
    skipped = [c for c, _, _, st in rows if st == "NO CHANGE"]
    if skipped:
        lines += ["", "```"] + [f"{c} = SKIPPED - NO CHANGE" for c in skipped] + ["```"]
    if not include:
        lines += ["", "No contract needs validation. No report and no email were generated."]
    summary("\n".join(lines))

    set_output("matrix", json.dumps({"include": include}))
    set_output("has_work", "true" if include else "false")
    set_output("statuses", json.dumps(statuses))


# ---------------------------------------------------------------- run
def classify(status_json, returncode, have_files):
    """A = data validation failures, B = script/runtime failure, C = download failure."""
    if returncode != 0 or status_json is None or not have_files:
        return "SCRIPT_FAILURE"
    failing = [c for c in status_json["codes"] if c["status"] == "FAIL_SEEN"]
    if any(c["code"] in DOWNLOAD_CODES for c in failing):
        return "DOWNLOAD_FAILURE"
    if any(not c["warning_only"] for c in failing):
        return "FAILED"
    return "COMPLETED"


def stages_from(status_json, script_ok):
    out = []
    failing = {c["code"] for c in (status_json or {}).get("codes", []) if c["status"] == "FAIL_SEEN" and not c["warning_only"]}
    tested = {c["code"] for c in (status_json or {}).get("codes", [])}
    for name, codes in STAGE_CODES:
        if not script_ok or not status_json:
            out.append((name, "NOT RUN"))
        elif failing & set(codes):
            out.append((name, "FAIL"))
        elif tested & set(codes):
            out.append((name, "PASS"))
        else:
            out.append((name, "N/A"))
    return out


def cmd_run(a):
    c = a.contract.upper()
    org, url = CONTRACTS[c]
    work = os.path.join(WORK_DIR, c)
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    today = now_utc().strftime("%Y-%m-%d")
    statuses = json.loads(os.environ.get("STATUSES_JSON") or "{}")

    print(f"=== RUNNING VALIDATION: {c} ({a.mode}) ===")
    log_path = os.path.join(work, "validation_console.log")
    t0 = time.time()
    try:
        with open(log_path, "w", encoding="utf-8") as log:
            proc = subprocess.run([sys.executable, VALIDATION_SCRIPT, org, c, url], cwd=work,
                                  stdout=log, stderr=subprocess.STDOUT, timeout=a.timeout_min * 60)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    elapsed = int(time.time() - t0)
    with open(log_path, encoding="utf-8", errors="replace") as f:
        tail = f.read().splitlines()[-60:]
    print("\n".join(tail))

    status_json = load_json(os.path.join(work, f"run_status_{c}.json"), None)
    docx_src = os.path.join(work, f"contract_summary_{c}.docx")
    xlsx_src = next(iter(glob.glob(os.path.join(work, "validation_report_*.xlsx"))), None)
    have_files = os.path.exists(docx_src) and xlsx_src is not None
    result = classify(status_json, rc, have_files)
    stages = stages_from(status_json, rc == 0 and status_json is not None)
    stages.append(("Generate report", "PASS" if have_files else "FAIL"))

    # ---- report files (renamed with contract + date)
    attachments, artifact_note = [], ""
    if have_files:
        docx_dst = os.path.join(REPORTS_DIR, f"{c}_contract_summary_{today}.docx")
        xlsx_dst = os.path.join(REPORTS_DIR, f"{c}_validation_report_{today}.xlsx")
        shutil.copy(docx_src, docx_dst)
        shutil.copy(xlsx_src, xlsx_dst)
        attachments.append(docx_dst)
        max_bytes = int(float(os.environ.get("MAX_ATTACH_MB", "14")) * 1024 * 1024)
        room = max_bytes - os.path.getsize(docx_dst)
        if os.path.getsize(xlsx_dst) <= room:
            attachments.append(xlsx_dst)
        else:
            zip_dst = xlsx_dst[:-5] + ".zip"
            with zipfile.ZipFile(zip_dst, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
                z.write(xlsx_dst, os.path.basename(xlsx_dst))
            if os.path.getsize(zip_dst) <= room:
                attachments.append(zip_dst)
            else:
                artifact_note = "The Excel validation report is too large to attach; download it from the workflow run."
    if status_json:
        shutil.copy(os.path.join(work, f"run_status_{c}.json"), os.path.join(REPORTS_DIR, f"{c}_run_status_{today}.json"))
    shutil.copy(log_path, os.path.join(REPORTS_DIR, f"{c}_console_{today}.log"))

    run_url = "{}/{}/actions/runs/{}".format(os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
                                             os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", ""))
    ctx = {
        "mode": a.mode, "contract": c, "plan_year": PLAN_YEAR, "previous": a.previous or "", "current": a.current or "",
        "result": result, "status": status_json, "stages": stages, "elapsed": elapsed, "run_url": run_url,
        "report_names": [os.path.basename(p) for p in attachments], "artifact_note": artifact_note,
        "other_statuses": {k: v for k, v in statuses.items() if k != c}, "actor": os.environ.get("GITHUB_ACTOR", ""),
        "log_tail": "\n".join(tail[-15:]) if result == "SCRIPT_FAILURE" else "",
    }
    html, text, subject = email_report.build(ctx)
    html_path = os.path.join(REPORTS_DIR, f"{c}_validation_{today}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    # ---- email (B/C failures are emailed once per update, not every 15 minutes)
    failures = load_json(FAIL_FILE, {})
    repeat_failure = result in ("SCRIPT_FAILURE", "DOWNLOAD_FAILURE") and a.mode == "auto" and failures.get(c) == a.current
    email_state = "SKIPPED (already notified for this update)" if repeat_failure else None
    if email_state is None:
        ok, err = email_report.send(subject, html, text, attachments)
        email_state = "SENT" if ok else f"FAILED ({err})"
    stages.append(("Email report", "PASS" if email_state == "SENT" else ("N/A" if email_state.startswith("SKIPPED") else "FAIL")))

    # ---- state: advance ONLY when validation itself completed (A or success)
    processed = result in ("COMPLETED", "FAILED")
    save_json(os.path.join(RESULTS_DIR, f"{c}.json"), {
        "contract": c, "processed": processed, "current": a.current, "result": result,
        "notify_failure_for": a.current if result in ("SCRIPT_FAILURE", "DOWNLOAD_FAILURE") else None})

    # ---- GitHub summary
    label = {"COMPLETED": "COMPLETED", "FAILED": "VALIDATION FAILED (data issues found)",
             "SCRIPT_FAILURE": "SCRIPT/RUNTIME FAILURE", "DOWNLOAD_FAILURE": "DOWNLOAD FAILURE"}[result]
    md = ["", "```", "=" * 50, f"RUNNING VALIDATION: {c}", "=" * 50, ""]
    md += [f"{n:<30} {s}" for n, s in stages]
    md += ["", "FINAL RESULT", "-" * 50, f"{c} = {label}", f"Email = {email_state}", "=" * 50, "```"]
    if status_json:
        failing = [x for x in status_json["codes"] if x["status"] == "FAIL_SEEN"]
        if failing:
            md += ["", "**Failed checks**", "", "| Code | Name | Failing records | Failed resource types |", "|---|---|---|---|"]
            md += [f"| {x['code']} | {x['name']} | {x['failing_records'] or x['fail_count']} | {x['failed_resource_types']}"
                   f"{' (warning)' if x['warning_only'] else ''} |" for x in failing]
    summary("\n".join(md))

    if result == "FAILED":
        print(f"::warning title={c}::Validation completed and found data failures (see report).")
    if result in ("SCRIPT_FAILURE", "DOWNLOAD_FAILURE"):
        print(f"::error title={c}::{label}")
        sys.exit(1)
    if not email_state.startswith(("SENT", "SKIPPED")):
        print(f"::error title={c}::Email failure (validation itself completed).")
        sys.exit(1)


# ---------------------------------------------------------------- state
def cmd_merge_state(a):
    state = load_json(STATE_FILE, {})
    failures = load_json(FAIL_FILE, {})
    for path in sorted(glob.glob(os.path.join(a.results, "**", "*.json"), recursive=True)):
        r = load_json(path, {})
        c = r.get("contract")
        if c not in CONTRACTS:
            continue
        if r.get("processed") and r.get("current"):
            state[c] = r["current"]
            failures.pop(c, None)
            print(f"{c}: state -> {r['current']}")
        elif r.get("notify_failure_for"):
            failures[c] = r["notify_failure_for"]
            print(f"{c}: failure recorded, state NOT advanced")
    save_json(STATE_FILE, state)
    save_json(FAIL_FILE, failures)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("detect"); d.add_argument("--mode", choices=["auto", "manual"], required=True)
    d.add_argument("--contract", default="ALL")
    r = sub.add_parser("run"); r.add_argument("--contract", required=True)
    r.add_argument("--mode", choices=["auto", "manual"], required=True)
    r.add_argument("--previous", default=""); r.add_argument("--current", default="")
    r.add_argument("--timeout-min", type=int, default=110)
    m = sub.add_parser("merge-state"); m.add_argument("--results", default=RESULTS_DIR)
    a = p.parse_args()
    {"detect": cmd_detect, "run": cmd_run, "merge-state": cmd_merge_state}[a.cmd](a)


if __name__ == "__main__":
    main()
