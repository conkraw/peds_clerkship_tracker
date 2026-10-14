"""Pediatric Clerkship — four files in, original reminders and REDCap evaluation CSV out.

Run: streamlit run peds_clerkship_tracker.py
No REDCap API is used. Original repeating-record numbering is saved encrypted.
Evaluation exclusions, faculty reminder exclusions and repeat-number reservations
use separate encrypted GitHub files with the existing credentials in Secrets.
Scoring and mailing logic are retained from the user's supplied application.
The default REDCap output retains the original 30 columns and appends reason.
Confidential: the three original student-specific exclusion rules are included.
Keep this source repository private and use an approved restricted-access host.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import io
import json
import math
import os
import re
import statistics
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

VERSION = "2.3.1"
SCHEMA_VERSION = "clerkship_flat_v1"


DOMAIN_KEYS = ("kp", "cr", "do", "cp", "ct")


KINDS = ("cas", "hp", "handoff")


LABELS = {"cas": "Clinical Assessment of Student", "hp": "Observed H&P", "handoff": "Pediatric Clerkship Handoff"}


FORM_NAMES = {"cas": "Clinical Assessment of Student", "hp": "PEDS History Taking & Physical Exam", "handoff": "PEDS Handoff"}


SURVEYS = {"cas": "https://redcap.ctsi.psu.edu/surveys/?s=C7EJ3MPDMCMCFJEP", "hp": "https://redcap.ctsi.psu.edu/surveys/?s=8C7DLPNX8LT9HTJP", "handoff": ""}


REQUIRED_ITEMS = [
    "[Ped] Acute Conditions e.g. Abdominal Pain, Fever, Seizure, Shortness of breath, Wheezing",
    "[Ped] Behavior e.g. Temper tantrums/aggressive behavior, ADHD, Developmental Delay, Autism Spectrum",
    "[Ped] Common Newborn Conditions e.g. Jaundice, Rash, Colic/Crying, Spit-up/Vomitting/Reflux, Poor Weight Gain",
    "[Ped] Dermatologic System e.g. Rash, Pallor", "[Ped] Gastrointestinal Tract",
    "[Ped] Health Supervision (Well Child Visit)", "[Ped] Health Systems Issue", "[Ped] Humanities Issue",
    "[Ped] Other e.g. Obesity/ Metabolic Syndrome",
    "[Ped] Upper and Lower Respiratory Tract e.g. Dental Caries, Sore Throat, Cough, Shortness of breath, Wheezing",
]


OBSERVING_ALLOWED = {REQUIRED_ITEMS[6], REQUIRED_ITEMS[7]}


def text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and not math.isfinite(value):
        return ""
    return str(value).strip().lstrip("\ufeff")


def norm(value: Any) -> str:
    return re.sub(r"\s+", " ", text(value)).casefold()


def key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", norm(value)).encode("ascii", "ignore").decode()).strip()


def display_name(value: Any) -> str:
    s = re.sub(r"\s*\((?:MD|PA|DO|MS|MD\d+)\)\s*$", "", text(value), flags=re.I).split(";", 1)[0].strip()
    if "," in s:
        last, first = s.split(",", 1)
        s = f"{first.strip()} {last.strip()}"
    return re.sub(r"\s+", " ", s)


def name_key(value: Any) -> str:
    return key(display_name(value))


def strip_html(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", text(value)))).strip()


def get(row: dict, *names: str) -> str:
    for n in names:
        if text(row.get(n)):
            return text(row[n])
    lookup = {key(k): v for k, v in row.items() if text(v)}
    for n in names:
        if key(n) in lookup:
            return text(lookup[key(n)])
    return ""


def dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time())
    s = text(value)
    if not s or s.lower() in {"nan", "nat", "none", "entire_course"}:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%m-%d-%Y %H:%M:%S", "%m-%d-%Y %H:%M", "%m-%d-%Y", "%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def day(value: Any) -> str:
    value = dt(value)
    return value.date().isoformat() if value else ""


def stamp(value: Any) -> str:
    value = dt(value)
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def number(value: Any) -> float | None:
    try:
        n = float(text(value))
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()


def kind_of(value: Any) -> str:
    s = key(strip_html(value))
    if "clinical assessment of student" in s:
        return "cas"
    if "history taking" in s and "physical exam" in s or s in {"observed h p", "observed hp"}:
        return "hp"
    if s in {"peds handoff", "pediatric clerkship handoff", "handoff"}:
        return "handoff"
    return ""


@dataclass
class Messages:
    rows: list[dict] = field(default_factory=list)

    def add(self, level: str, source: str, detail: str, record_id: str = "") -> None:
        entry = {"level": level, "source": source, "record_id": record_id, "detail": detail}
        if entry not in self.rows:
            self.rows.append(entry)

    @property
    def blocked(self) -> bool:
        return any(r["level"] == "ERROR" for r in self.rows)


def read_csv_bytes(data: bytes, label: str, log: Messages) -> list[dict]:
    """Keep duplicate headers and harmless trailing omissions; never guess shifted cells."""
    if not data:
        raise ValueError(f"{label}: file is empty.")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = data.decode("utf-16")
    else:
        try:
            decoded = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            decoded = data.decode("cp1252")
            log.add("INFO", label, "Decoded Windows-1252 export.")
    reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValueError(f"{label}: no header.") from exc
    counts: Counter = Counter()
    headers = []
    for h in header:
        h = text(h)
        counts[norm(h)] += 1
        headers.append(h if counts[norm(h)] == 1 else f"{h}__dup{counts[norm(h)]}")
    if len(headers) < 2:
        raise ValueError(f"{label}: expected a comma-separated file with headers.")
    short_count = 0
    rows = []
    for cells in reader:
        if not any(text(v) for v in cells):
            continue
        if len(cells) > len(headers):
            raise ValueError(f"{label}, physical line {reader.line_num}: {len(cells)} fields for {len(headers)} headers. Re-export this file; shifted fields are not guessed or silently dropped.")
        if len(cells) < len(headers):
            short_count += 1
            cells += [""] * (len(headers) - len(cells))
        rows.append(dict(zip(headers, (text(c) for c in cells))))
    if short_count:
        log.add("INFO", label, f"Padded trailing omitted cells in {short_count} rows; all rows retained.")
    if any(c > 1 for c in counts.values()):
        log.add("INFO", label, "Retained repeated headers as separate activity columns.")
    return rows


def require(rows: list[dict], label: str, groups: list[tuple]) -> None:
    if not rows:
        return
    headers = {key(h) for h in rows[0]}
    missing = [" / ".join(g) for g in groups if not any(key(n) in headers for n in g)]
    if missing:
        raise ValueError(f"{label}: missing required columns: {', '.join(missing)}")


LEGACY_EXCLUSIONS = [{'record_id': 'aqa6684',
  'evaluator': 'VanDuzer, Kayleigh',
  'form_record': '',
  'reason': 'Preserved manual exclusion from the supplied clerk_evals_v3 script. The original code '
            'does not state the reason.',
  'student_name': 'Ayah Aligabi',
  'active': True,
  'origin': 'Original grading script'},
 {'record_id': 'mgm6105',
  'evaluator': 'Daymont, Carrie',
  'form_record': '',
  'reason': 'Preserved manual exclusion from the supplied clerk_evals_v3 script. The original code '
            'does not state the reason.',
  'student_name': 'Michael McCormick',
  'active': True,
  'origin': 'Original grading script'},
 {'record_id': 'mgm6105',
  'evaluator': 'Younger, Lydia; PA',
  'form_record': '',
  'reason': 'Preserved manual exclusion from the supplied clerk_evals_v3 script. The original code '
            'does not state the reason.',
  'student_name': 'Michael McCormick',
  'active': True,
  'origin': 'Original grading script'}]


def active_rule(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if norm(value) in {"true", "1", "yes"}:
        return True
    if norm(value) in {"false", "0", "no"}:
        return False
    raise ValueError("An exclusion's active value must be true or false.")


def normalize_rules(value: Any) -> list[dict]:
    """Validate complete rule sets; never silently broaden a malformed rule."""
    if isinstance(value, dict):
        if value.get("version") != 1 or "rules" not in value:
            raise ValueError("Unsupported exclusions backup version.")
        value = value["rules"]
    if not isinstance(value, list):
        raise ValueError("Exclusions must be a JSON list of rules.")
    result = {}
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("Every exclusion must be an object with a student and preceptor.")
        r = {k: text(raw.get(k)) for k in ("record_id", "student_name", "evaluator", "evaluator_email", "rotation_start", "form_record", "submit_date", "reason", "origin")}
        r["record_id"], r["evaluator_email"] = norm(r["record_id"]), norm(r["evaluator_email"])
        if not r["record_id"] or not (r["evaluator"] or r["evaluator_email"]):
            raise ValueError("Each exclusion needs a student record_id and a preceptor name or email.")
        if r["record_id"] in {"*", "all", "all students"}:
            raise ValueError("Blanket exclusions are not supported; select one student.")
        for field_name, parse in (("rotation_start", day), ("submit_date", stamp)):
            if r[field_name]:
                parsed = parse(r[field_name])
                if not parsed:
                    raise ValueError(f"Invalid {field_name}; no changes were applied.")
                r[field_name] = parsed
        r["active"] = active_rule(raw.get("active", True))
        r["reason"] = r["reason"] or "Reason not supplied."
        r["origin"] = r["origin"] or "Imported rule"
        # Stable identity lets a disabled legacy rule override itself on reload.
        identity = [r["record_id"], name_key(r["evaluator"]), r["evaluator_email"], r["rotation_start"], r["form_record"], r["submit_date"]]
        r["rule_id"] = "ex_" + digest(identity)[:24]
        if r["rule_id"] in result and result[r["rule_id"]] != r:
            raise ValueError("Conflicting duplicate exclusion rules; review the backup before importing.")
        result[r["rule_id"]] = r
    return sorted(result.values(), key=lambda r: (r["record_id"], name_key(r["evaluator"]), r["rule_id"]))


def exclusion_matches(rule: dict, row: dict) -> bool:
    """CAS-only exclusion. Never affects another student or reminders."""
    if not active_rule(rule.get("active", True)) or row.get("kind", "cas") != "cas":
        return False
    if norm(rule.get("record_id")) != norm(row.get("record_id")):
        return False
    # Emails win when both are available; normalized names are the fallback.
    wanted_email, actual_email = norm(rule.get("evaluator_email")), norm(row.get("evaluator_email"))
    if wanted_email and actual_email:
        if wanted_email != actual_email:
            return False
    elif not name_key(rule.get("evaluator")) or name_key(rule.get("evaluator")) != name_key(row.get("evaluator")):
        return False
    if text(rule.get("form_record")) and text(rule["form_record"]) != text(row.get("form_record")):
        return False
    if text(rule.get("rotation_start")) and day(rule["rotation_start"]) != day(row.get("rotation_start")):
        return False
    if text(rule.get("submit_date")) and stamp(rule["submit_date"]) != stamp(row.get("submit_date")):
        return False
    return True


def merge_rule_updates(current: list[dict], updates: list[dict]) -> list[dict]:
    merged = {r["rule_id"]: r for r in normalize_rules(current)}
    merged.update({r["rule_id"]: r for r in normalize_rules(updates)})
    return normalize_rules(list(merged.values()))


def remove_evaluation_exclusions(rules: list[dict], rule_ids: list[str]) -> list[dict]:
    """Remove only selected grading rules; do not merge defaults back into the result.

    A complete JSON backup (including []) retains these removals when restored.
    Faculty-reminder exclusions and source evaluations are never modified here.
    """
    current = normalize_rules(rules)
    if not isinstance(rule_ids, (list, tuple)) or any(not isinstance(k, str) for k in rule_ids):
        raise ValueError("Select exclusions from the current list; no exclusions were removed.")
    selected = set(rule_ids)
    known = {r["rule_id"] for r in current}
    if selected - known:
        raise ValueError("The exclusion list changed. Select the current rows again; nothing was removed.")
    return normalize_rules([r for r in current if r["rule_id"] not in selected])


def overlapping_exclusions_after_removal(rules: list[dict], rule_ids: list[str]) -> list[dict]:
    """Return remaining active rules whose scopes can overlap a selected active rule.

    This is deliberately a warning, not permission to remove unselected rules.
    It handles an all-pair rule alongside a form-specific rule, as well as distinct
    email, rotation, Form Record and timestamp scopes.
    """
    current = normalize_rules(rules)
    remaining = remove_evaluation_exclusions(current, rule_ids)
    selected_ids = set(rule_ids)
    selected = [r for r in current if r["rule_id"] in selected_ids and r["active"]]
    def overlaps(a: dict, b: dict) -> bool:
        if a["record_id"] != b["record_id"]:
            return False
        if a["evaluator_email"] and b["evaluator_email"]:
            if a["evaluator_email"] != b["evaluator_email"]:
                return False
        elif not name_key(a["evaluator"]) or name_key(a["evaluator"]) != name_key(b["evaluator"]):
            return False
        return not any(a[k] and b[k] and a[k] != b[k]
                       for k in ("rotation_start", "form_record", "submit_date"))
    return [r for r in remaining if r["active"] and any(overlaps(r, old) for old in selected)]


def evaluation_exclusion_label(rule: dict) -> str:
    student = rule.get("student_name") or rule["record_id"]
    preceptor = rule.get("evaluator") or rule.get("evaluator_email", "")
    scope = ("Form " + rule["form_record"] if rule.get("form_record") else
             "Submission " + rule["submit_date"] if rule.get("submit_date") else "All for this pair")
    if rule.get("rotation_start"):
        scope += " / rotation " + rule["rotation_start"]
    return f"{student} [{rule['record_id']}] — {preceptor} — {scope}" + (" (inactive)" if not rule["active"] else "")


@dataclass
class Settings:
    as_of: str = field(default_factory=lambda: datetime.now(ZoneInfo("America/New_York")).date().isoformat())
    data_through: str = ""
    targets: dict = field(default_factory=lambda: {"cas": 8, "hp": 2, "handoff": 1})
    fallback_rotation_days: int = 26
    grace_days: int = 0
    cohort: str = "all"  # all or active
    confirm_coverage: bool = False  # explicitly attests absent whole-cohort data means zero
    exclusions: list[dict] = field(default_factory=lambda: normalize_rules(LEGACY_EXCLUSIONS))
    survey_urls: dict = field(default_factory=lambda: dict(SURVEYS))
    legacy_partial_links: bool = True


def source_identity(row: dict, role: str) -> dict:
    if role == "student":
        return {
            "id": norm(get(row, "Student External ID", "External ID", "record_id")),
            "name": get(row, "Student", "Student Name", "legal_name", "name", "epa_student"),
            "email": norm(get(row, "Student Email", "Email", "email_351155")),
            "start": day(get(row, "Start Date", "start_date", "start_date_cl")),
            "end": day(get(row, "End Date", "end_date")),
        }
    return {
        "id": norm(get(row, "Faculty External ID", "Evaluator External ID", "faculty_external_id")),
        "email": norm(get(row, "Faculty Email", "Evaluator Email", "epa_evaluator_email", "faculty_email")),
        "username": norm(get(row, "Faculty Username", "Evaluator Username", "faculty_username")),
        "name": get(row, "Faculty Name", "Evaluator", "epa_evaluator", "faculty_name"),
    }


class People:
    """Join strong identifier aliases; name-only fallback must be unique."""
    def __init__(self, persons: list[dict]):
        self.parent: dict[str, str] = {}
        self.names: dict[str, set] = defaultdict(set)
        for p in persons:
            tokens = self.tokens(p)
            for t in tokens:
                self.parent.setdefault(t, t)
            for t in tokens[1:]:
                a, b = self.root(tokens[0]), self.root(t)
                self.parent[max(a, b)] = min(a, b)
        for p in persons:
            tokens = self.tokens(p)
            if tokens and name_key(p.get("name")):
                self.names[name_key(p["name"])].add(self.root(tokens[0]))

    def root(self, t: str) -> str:
        while self.parent.get(t, t) != t:
            self.parent[t] = self.parent.get(self.parent[t], self.parent[t])
            t = self.parent[t]
        return t

    @staticmethod
    def tokens(p: dict) -> list[str]:
        return [f"{kind}:{norm(p.get(kind))}" for kind in ("id", "email", "username") if norm(p.get(kind)) and norm(p.get(kind)) not in {"all students", "n/a"}]

    def resolve(self, p: dict) -> str:
        tokens = self.tokens(p)
        if tokens:
            return self.root(tokens[0])
        nk = name_key(p.get("name"))
        possible = self.names.get(nk, set())
        if len(possible) == 1:
            return next(iter(possible))
        if nk and not possible:
            return "name:" + nk
        return ""


def build_roster(schedule: list[dict], checklist: list[dict], matches: list[dict], oasis: list[dict], settings: Settings, log: Messages) -> list[dict]:
    require(schedule, "Rotation schedule", [("legal_name", "name", "Student Name", "Student"), ("start_date", "Start Date")])
    candidates = [source_identity(r, "student") for r in checklist + matches + oasis]
    candidates = [r for r in candidates if r["id"] and r["id"] != "all students" and r["start"]]
    by_name, by_id, by_email = defaultdict(list), defaultdict(list), defaultdict(list)
    for c in candidates:
        by_name[(name_key(c["name"]), c["start"])].append(c)
        by_id[(c["id"], c["start"])].append(c)
        if c["email"]:
            by_email[(c["email"], c["start"])].append(c)
    roster = []
    seen = set()
    for i, r in enumerate(schedule, 1):
        who = source_identity(r, "student")
        if not who["start"]:
            log.add("ERROR", "Rotation schedule", f"Row {i}: missing/unrecognized rotation start date.")
            continue
        choices = []
        if who["id"]:
            choices = by_id.get((who["id"], who["start"]), [])
        elif who["email"]:
            choices = by_email.get((who["email"], who["start"]), [])
        if not choices and not who["id"]:
            choices = by_name.get((name_key(who["name"]), who["start"]), [])
        ids = {c["id"] for c in choices}
        if who["id"]:
            ids.add(who["id"])
        if len(ids) != 1:
            log.add("ERROR", "Rotation schedule", f"{display_name(who['name'])}: cannot resolve a unique student ID for {who['start']}. Add record_id and email to this schedule; no fuzzy matching is used.")
            continue
        rid = next(iter(ids))
        pair = (rid, who["start"])
        if pair in seen:
            continue
        seen.add(pair)
        end_candidates = {c["end"] for c in choices if c["end"]}
        if who["end"]:
            end_candidates = {who["end"]}
        if len(end_candidates) > 1:
            log.add("ERROR", "Rotation schedule", "Conflicting rotation end dates; supply an explicit end_date.", rid)
            continue
        end = next(iter(end_candidates), "")
        inferred = not bool(end)
        if not end:
            end = (dt(who["start"]) + timedelta(days=settings.fallback_rotation_days - 1)).date().isoformat()
            log.add("WARNING", "Rotation schedule", f"End date inferred as {end} using {settings.fallback_rotation_days} inclusive calendar days.", rid)
        if end < who["start"]:
            log.add("ERROR", "Rotation schedule", "End date precedes start date.", rid)
            continue
        emails = {c["email"] for c in choices if c["email"]}
        # Schedule contact wins; otherwise use one unambiguous source email.
        email = who["email"] or (next(iter(emails)) if len(emails) == 1 else "")
        if not email:
            log.add("WARNING", "Rotation schedule", "Missing/ambiguous student email: shown for review, not placed in a send-ready reminder file.", rid)
        if settings.cohort == "active" and not who["start"] <= settings.as_of <= end:
            continue
        roster.append({"record_id": rid, "student_name": display_name(who["name"]), "email": email, "start_date": who["start"], "end_date": end, "end_date_inferred": inferred})
    # Mailing output uses the original student ID; process one rotation per student per run.
    counts = Counter(r["record_id"] for r in roster)
    for rid, count in counts.items():
        if count > 1:
            log.add("ERROR", "Rotation schedule", "This student appears in multiple rotations. Process one rotation at a time.", rid)
    return sorted(roster, key=lambda r: (r["start_date"], r["student_name"]))


def in_rotation(row: dict, student: dict, *, date_field: str = "") -> bool:
    identity = source_identity(row, "student")
    if identity["id"] != student["record_id"]:
        return False
    if identity["start"]:
        return identity["start"] == student["start_date"]
    check_date = day(get(row, date_field)) if date_field else ""
    return bool(check_date and student["start_date"] <= check_date <= student["end_date"])


def map_question(question: str, kind: str) -> str:
    q = strip_html(question).lower()
    if kind == "cas":
        for prefix, result in (("kp2.1", "kp"), ("pc1.2/1.3/1.11", "cr"), ("ics4.1", "cp"), ("pc1.5/ics4.3/pc1.6/ics4.4", "do"), ("ics4.2/sbp6.1", "ct"), ("prof5.1/hh7.2", "prof"), ("describe at least one instance", "cas_strengths"), ("suggest one area of clinical performance", "cas_weaknesses")):
            if q.startswith(prefix):
                return result
    elif kind == "hp":
        if "confidence" in q and "history" in q:
            return "epa_obh_score"
        if "confidence" in q and "physical exam" in q:
            return "epa_obp_score"
        if "please add an area of improvement not listed" in q:
            if "history" in q:
                return "epaobh_weaknesses"
            if "physical exam" in q:
                return "epaobp_weaknesses"
    elif kind == "handoff":
        if "confidence" in q and ("oral presentation" in q or "handoff" in q):
            return "epa_obho_score"
        if "please add an area of improvement not listed" in q and "handoff" in q:
            return "epaobho_weaknesses"
    return ""


def score_evaluation(row: dict) -> None:
    available = [number(row.get(k)) for k in DOMAIN_KEYS]
    observed = [n for n in available if n is not None and 1 <= n <= 5]
    mean = statistics.mean(observed) if observed else None
    row["iv"] = round(mean, 2) if mean is not None and len(observed) < 5 else ""
    row["tot"] = round(mean * 75, 1) if mean is not None else ""
    for k, n in zip(DOMAIN_KEYS, available):
        row["effective_" + k] = n if n is not None and 1 <= n <= 5 else mean


def normalize_evaluations(oasis: list[dict], roster: list[dict], people: People, settings: Settings, log: Messages) -> list[dict]:
    require(oasis, "OASIS ME", [("Student External ID", "External ID"), ("Student",), ("Evaluator",), ("Evaluation",), ("Submit Date",), ("Question",), ("Multiple Choice Value", "Mult Choice Value")])
    roster_map = {(r["record_id"], r["start_date"]): r for r in roster}
    grouped = {}
    for raw in oasis:
        person = source_identity(raw, "student")
        kind = kind_of(get(raw, "Evaluation"))
        if not kind:
            continue
        student = roster_map.get((person["id"], person["start"]))
        if student is None:
            # Without course dates, only a within-rotation submission can be assigned.
            candidates = [s for s in roster if not person["start"] and person["id"] == s["record_id"] and s["start_date"] <= day(get(raw, "Submit Date")) <= s["end_date"]]
            student = candidates[0] if len(candidates) == 1 else None
        if student is None:
            continue
        submitted = stamp(get(raw, "Submit Date"))
        status = norm(get(raw, "Eval Status"))
        if not submitted or status in {"draft", "incomplete", "not submitted", "pending"}:
            continue
        if day(submitted) > settings.as_of:
            continue
        if day(submitted) < student["start_date"]:
            log.add("ERROR", "OASIS ME", "Submission date precedes its stated rotation.", student["record_id"])
            continue
        faculty = source_identity(raw, "faculty")
        fkey = people.resolve(faculty)
        if not fkey:
            log.add("ERROR", "OASIS ME", "Missing or ambiguous evaluator identity; reconcile this submission before sending reminders.", student["record_id"])
        form = get(raw, "Form Record")
        submission_key = (student["record_id"], student["start_date"], kind, form or digest([faculty, submitted]))
        if submission_key not in grouped:
            grouped[submission_key] = {
                "record_id": student["record_id"], "rotation_start": student["start_date"], "kind": kind,
                "form_record": form, "student": get(raw, "Student"), "student_email": person["email"],
                "evaluator": faculty["name"], "evaluator_email": faculty["email"],
                "faculty_key": fkey, "evaluation": get(raw, "Evaluation"), "submit_date": submitted,
                "source": "OASIS", "manual_excluded": False, "drop_lowest": False,
            }
        entry = grouped[submission_key]
        if entry["faculty_key"] != fkey or entry["submit_date"] != submitted:
            log.add("ERROR", "OASIS ME", "Conflicting metadata within a Form Record.", student["record_id"])
        q = map_question(get(raw, "Question"), kind)
        if not q:
            continue
        numeric = q in DOMAIN_KEYS or q == "prof" or q.endswith("_score")
        value = get(raw, "Multiple Choice Value", "Mult Choice Value") if numeric else get(raw, "Answer text", "Answer Text")
        if numeric and not value and q.startswith("epa_"):
            value = get(raw, "Answer text", "Answer Text")
        if numeric:
            value = number(value)
            if value is not None and q in DOMAIN_KEYS and not 1 <= value <= 5:
                log.add("ERROR", "OASIS ME", f"Out-of-range domain score for {q}; not converted to an N/A score.", student["record_id"])
                value = None
            value = value if value is not None else ""
        if q in entry and text(entry[q]) and text(value) and text(entry[q]) != text(value):
            log.add("ERROR", "OASIS ME", f"Conflicting answers for {q} within one submission; review source export.", student["record_id"])
        elif text(value):
            entry[q] = value
    result = sorted(grouped.values(), key=lambda r: (r["record_id"], r["submit_date"], r["form_record"]))
    for row in result:
        if row["kind"] == "cas":
            score_evaluation(row)
            for rule in settings.exclusions:
                if exclusion_matches(rule, row):
                    row["manual_excluded"] = True
                    row["manual_exclusion_reason"] = text(rule.get("reason")) or "Legacy manual exclusion"
    return result


def normalize_matches(raw_matches: list[dict], roster: list[dict], people: People, settings: Settings, log: Messages) -> list[dict]:
    require(raw_matches, "Preceptor matches", [("Student External ID",), ("Faculty Name",), ("Manual Evaluations",)])
    roster_map = {r["record_id"]: r for r in roster}
    result = {}
    for raw in raw_matches:
        who = source_identity(raw, "student")
        if who["id"] in {"", "all students"} or who["id"] not in roster_map:
            continue
        student = roster_map[who["id"]]
        if not in_rotation(raw, student, date_field="Evaluation Period Start Date"):
            continue
        if norm(get(raw, "Type of Association")) == "student_evaluates":
            continue
        if norm(get(raw, "Delete")) in {"1", "yes", "true", "delete", "deleted"}:
            continue
        faculty = source_identity(raw, "faculty")
        fkey = people.resolve(faculty)
        if not fkey:
            log.add("ERROR", "Preceptor matches", "Missing or ambiguous preceptor identity; no reminder is generated for that match.", who["id"])
            continue
        begin = day(get(raw, "Evaluation Period Start Date")) or student["start_date"]
        end = day(get(raw, "Evaluation Period End Date")) or student["end_date"]
        if begin > end:
            log.add("ERROR", "Preceptor matches", "Evaluation-period end precedes its start.", who["id"])
            continue
        for form in get(raw, "Manual Evaluations").split("|"):
            kind = kind_of(form)
            if not kind:
                continue
            row = {"record_id": who["id"], "rotation_start": student["start_date"], "faculty_name": faculty["name"], "faculty_email": faculty["email"], "faculty_external_id": faculty["id"], "faculty_username": faculty["username"], "faculty_key": fkey, "kind": kind, "manual_evaluations": FORM_NAMES[kind], "eval_period_start_date": begin, "eval_period_end_date": end, "type_of_association": get(raw, "Type of Association"), "classification": get(raw, "Classification"), "student_activity1": get(raw, "Student Activity")}
            token = (who["id"], student["start_date"], fkey, kind, begin, end)
            result.setdefault(token, row)
    return list(result.values())


CHECKLIST_MAP = {
    "student_name": ("Student name",), "external_id": ("External ID", "Student External ID"), "email_351155": ("Email",),
    "start_date_cl": ("Start Date",), "location_cl": ("Location",), "checklist": ("Checklist",), "checklist_status": ("Checklist status",),
    "item": ("Item",), "item_status": ("Item status",), "originalcopy": ("Original/Copy",), "signed_by": ("Signed By",),
    "time_signed": ("Time Signed",), "verified_by": ("Verified By",), "verification_comments": ("Verification Comments",),
    "verified_date": ("Verified Date",), "time_entered": ("Time entered",), "date_97fae7": ("Date",),
    "times_observed": ("Times observed",), "is_proficient": ("Is proficient",), "needs_practice": ("Needs Practice",), "comments": ("Comments",),
}


def activity_of(raw: dict) -> str:
    activities = []
    for h, v in raw.items():
        if h.startswith("*") and "comment" not in norm(h) and text(v):
            s = norm(v)
            # Mirrors the old script's Performing -> Assisting -> Observing order.
            if re.search(r"\bperform", s):
                activities.append("Performing")
            elif re.search(r"\bassist", s):
                activities.append("Assisting")
            elif re.search(r"\bobserv", s):
                activities.append("Observing")
            else:
                activities.append("Unknown")
    if not activities:
        return "Unknown"
    return next((a for a in ("Performing", "Assisting", "Observing") if a in activities), "Unknown")


def normalize_checklist(raw_checklist: list[dict], roster: list[dict], settings: Settings, log: Messages) -> list[dict]:
    require(raw_checklist, "Checklist", [("Student name", "Student Name"), ("External ID", "Student External ID"), ("Start Date",), ("Item",), ("Item status",)])
    by_id = {r["record_id"]: r for r in roster}
    entries = {}
    labels = {key(item): item for item in REQUIRED_ITEMS}
    for raw in raw_checklist:
        who = source_identity(raw, "student")
        student = by_id.get(who["id"])
        if not student or not in_rotation(raw, student):
            continue
        entered = stamp(get(raw, "Time entered"))
        if entered and day(entered) > settings.as_of:
            continue
        row = {k: get(raw, *src) for k, src in CHECKLIST_MAP.items()}
        row["record_id"] = who["id"]
        row["rotation_start"] = student["start_date"]
        row["time_entered"] = entered
        row["start_date_cl"] = student["start_date"]
        for k in ("time_signed", "verified_date"):
            if row[k]:
                converted = stamp(row[k])
                if not converted:
                    log.add("ERROR", "Checklist", f"Unrecognized {k} date.", who["id"])
                row[k] = converted
        row["date_97fae7"] = day(row["date_97fae7"])
        row["student_activity"] = activity_of(raw)
        row["canonical_item"] = labels.get(key(row["item"]), "")
        if not row["canonical_item"]:
            log.add("WARNING", "Checklist", f"Unrecognized required-item label: {row['item']}. Retained for review; does not satisfy another category.", who["id"])
        # Keep raw activity text in existing REDCap fields where available.
        for prefix, target in (("*Assisted or Above", "assisted_or_above"), ("*Observed or Above", "observed_or_above")):
            vals = [text(v) for h, v in raw.items() if key(re.sub(r"__dup\d+$", "", h)) == key(prefix) and text(v)]
            if vals:
                row[target] = " | ".join(dict.fromkeys(vals))
        entries[digest({k: v for k, v in row.items() if k != "canonical_item"})] = row
    return list(entries.values())


def coverage(raw: list[dict], roster: list[dict], settings: Settings, source: str, log: Messages) -> dict[str, bool]:
    starts = {source_identity(r, "student")["start"] for r in raw} - {""}
    result = {}
    for student in roster:
        start = student["start_date"]
        if start in result:
            continue
        result[start] = start in starts or settings.confirm_coverage
        if not result[start]:
            log.add("WARNING", source, f"No rows cover rotation {start}; its {source.lower()} reminders and zero-count tracking are withheld. Upload the matching export, or explicitly confirm complete coverage.")
    return result


def flow_value(value: Any) -> str:
    """Legacy Flow split(',') compatibility; do not use for REDCap data."""
    s = re.sub(r"\s+", " ", text(value)).replace(",", " -").replace('"', "")
    return "'" + s if s.startswith(("=", "+", "-", "@")) else s


def csv_bytes(rows: list[dict], columns: list[str] | None = None, flow: bool = False) -> bytes:
    if columns is None:
        columns = list(dict.fromkeys(k for r in rows for k in r))
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: flow_value(row.get(k, "")) if flow else text(row.get(k, "")) for k in columns})
    return out.getvalue().encode("utf-8-sig")


def analyze_scores(evaluations: list[dict], roster: list[dict]) -> list[dict]:
    summaries = []
    for student in roster:
        cas = [e for e in evaluations if e["record_id"] == student["record_id"] and e["kind"] == "cas"]
        eligible = [e for e in cas if not e["manual_excluded"] and number(e.get("tot")) is not None]
        for e in cas:
            e["drop_lowest"] = False
        dropped = min(eligible, key=lambda e: (number(e["tot"]), e["submit_date"], e["form_record"])) if len(eligible) >= 4 else None
        if dropped is not None:
            dropped["drop_lowest"] = True
        kept = [e for e in eligible if not e["drop_lowest"]]
        before = round(statistics.mean(number(e["tot"]) for e in eligible), 2) if eligible else ""
        # Clinical total is the sum of five equally weighted effective-domain means.
        means = {k: statistics.mean(e["effective_" + k] for e in kept) if kept else None for k in DOMAIN_KEYS}
        after = round(sum(means.values()) * 15, 2) if kept else ""
        summary = {**student, "cas_submissions": len(cas), "scorable_evaluations": len(eligible), "manual_exclusions": sum(bool(e["manual_excluded"]) for e in cas), "unscorable_submissions": sum(number(e.get("tot")) is None for e in cas), "score_before_drop": before, "clinical_score_375": after, "dropped_form_record": dropped["form_record"] if dropped else "", "dropped_evaluator": dropped["evaluator"] if dropped else "", "exclude": f"Exclude lowest of {len(eligible)}: {dropped['evaluator']} (total={number(dropped['tot']):.1f})" if dropped else "", "professionalism_review": any(number(e.get("prof")) is not None and number(e["prof"]) < 5 for e in cas), "individual_domain_below_3": any(number(e.get(k)) is not None and number(e[k]) < 3 for e in cas for k in DOMAIN_KEYS)}
        for k in DOMAIN_KEYS:
            summary[k + "_mean_after_drop"] = round(means[k], 3) if means[k] is not None else ""
        summary["domain_mean_below_3"] = any(v is not None and v < 3 for v in means.values())
        summaries.append(summary)
    return summaries


CHECKLIST_COLUMNS = ["record_id", "name", "email", "missing_items", "observing_only_items", "missing_items_delimiter", "observing_only_items_delimiter", "status", "missing_count", "observing_only_count", "participation_review_items", "incomplete_items", "data_through"]


STUDENT_COLUMNS = ["record_id", "student_name", "email", "reminderob", "remindercas", "reminderhandoff", "random_preceptor", "preceptor_shoutout", "cas_matched", "cas_submitted", "cas_credit", "hp_matched", "hp_submitted", "hp_credit", "handoff_matched", "handoff_submitted", "handoff_credit", "data_through"]


PRECEPTOR_COLUMNS = ["faculty_email", "faculty_name", "student_name", "reminder_note", "blank_form_link", "partial_form_link", "record_id", "student_email", "evaluation_type", "expected_eval_count", "completed_eval_count", "pending_eval_count", "cas_link", "hp_link", "handoff_link", "data_through", "rotation_start"]


def build_reports(roster: list[dict], entries: list[dict], matches: list[dict], evaluations: list[dict], cover: dict, people: People, settings: Settings, log: Messages) -> dict:
    checklist_review, student_review, preceptor_rows, match_audit = [], [], [], []
    summaries = analyze_scores(evaluations, roster)
    effective_cutoff = settings.data_through or settings.as_of
    for student in roster:
        rid = student["record_id"]
        cs = [e for e in entries if e["record_id"] == rid]
        ms = [m for m in matches if m["record_id"] == rid and m["eval_period_start_date"] <= settings.as_of]
        es = [e for e in evaluations if e["record_id"] == rid]
        missing, observing, unknown, incomplete = [], [], [], []
        has_checklist = cover["checklist"][student["start_date"]]
        if has_checklist:
            for item in REQUIRED_ITEMS:
                found = [e for e in cs if e["canonical_item"] == item]
                completed = [e for e in found if norm(e.get("item_status")) in {"complete", "completed", "2"}]
                if not found:
                    missing.append(item)
                elif not completed:
                    incomplete.append(item)
                else:
                    allowed = {"Performing", "Assisting", "Observing"} if item in OBSERVING_ALLOWED else {"Performing", "Assisting"}
                    if any(e["student_activity"] in allowed for e in completed):
                        continue
                    if all(e["student_activity"] == "Observing" for e in completed):
                        observing.append(item)
                    else:
                        unknown.append(item)
        checklist_review.append({"record_id": rid, "name": student["student_name"], "email": student["email"], "missing_items": "<br>".join(missing) if missing else "All required items completed" if has_checklist and not (observing or unknown or incomplete) else "", "observing_only_items": "<br>".join(observing) if observing else "No observing-only issues", "missing_items_delimiter": "<br>", "observing_only_items_delimiter": "<br>", "status": "Coverage not confirmed" if not has_checklist else "Needs review" if missing or observing or unknown or incomplete else "Complete", "missing_count": len(missing) if has_checklist else "", "observing_only_count": len(observing) if has_checklist else "", "participation_review_items": "<br>".join(unknown), "incomplete_items": "<br>".join(incomplete), "data_through": effective_cutoff})
        counts, notes = {}, {}
        all_names = set()
        all_known = cover["matches"][student["start_date"]] and cover["oasis"][student["start_date"]]
        needs = False
        for kind in KINDS:
            typed_m = [m for m in ms if m["kind"] == kind]
            typed_e = [e for e in es if e["kind"] == kind]
            matched = {m["faculty_key"] for m in typed_m if m["faculty_key"]}
            submitted = {e["faculty_key"] for e in typed_e if e["faculty_key"]}
            credit = matched | submitted
            counts.update({kind + "_matched": len(matched), kind + "_submitted": len(typed_e), kind + "_credit": len(credit) if all_known else ""})
            names = sorted({display_name(m["faculty_name"]) for m in typed_m} | {display_name(e["evaluator"]) for e in typed_e})
            all_names.update(n for n in names if n)
            if not all_known:
                notes[kind] = f"{LABELS[kind]}: source coverage not confirmed; no missing requirement is inferred."
            elif len(credit) >= settings.targets[kind]:
                notes[kind] = f"{LABELS[kind]} requirement complete. Thank you for requesting and documenting feedback."
            else:
                needs = True
                extra = f" Documented activity is associated with {' ; '.join(names)}." if names else ""
                notes[kind] = f"{LABELS[kind]} - our records show {len(credit)} documented solicitations or submissions toward {settings.targets[kind]} required. Please arrange {settings.targets[kind] - len(credit)} more.{extra}"
        shoutout = sorted(all_names)[int(digest([rid, settings.as_of])[:8], 16) % len(all_names)] if all_names else ""
        student_review.append({**student, **counts, "reminder_needed": "Yes" if needs and all_known else "No" if all_known else "Coverage not confirmed", "reminderob": notes["hp"], "remindercas": notes["cas"], "reminderhandoff": notes["handoff"], "random_preceptor": shoutout, "preceptor_shoutout": "https://redcap.ctsi.psu.edu/surveys/?" + urlencode({"s": "YHLPMA48Y9HNWKCA", "des": 1, "preceptor_name": shoutout}) if shoutout else "", "data_through": effective_cutoff})
        grouped = defaultdict(list)
        for m in ms:
            grouped[(m["faculty_key"], m["kind"])].append(m)
        pending_by_person = defaultdict(list)
        for (fk, kind), expected in grouped.items():
            received = [e for e in es if e["kind"] == kind and e["faculty_key"] == fk]
            due = any((dt(m["eval_period_end_date"]) + timedelta(days=settings.grace_days)).date().isoformat() <= settings.as_of for m in expected)
            outcome = "Received — no reminder" if received else "Not due" if not due else "Coverage not confirmed" if not all_known else "Pending"
            m = expected[0]
            match_audit.append({"record_id": rid, "student_name": student["student_name"], "faculty_name": display_name(m["faculty_name"]), "faculty_email": m["faculty_email"], "evaluation_type": LABELS[kind], "unique_association_rows": len(expected), "expected_eval_count": 1, "completed_eval_count": len(received), "status": outcome})
            if outcome == "Pending":
                pending_by_person[fk].append(m)
        for fk, pending in pending_by_person.items():
            faculty = next((m for m in pending if m["faculty_email"]), pending[0])
            faculty_name = display_name(faculty["faculty_name"])
            email = faculty["faculty_email"]
            if not email:
                # Do not replace a missing preceptor address with the clerkship director.
                log.add("WARNING", "Preceptor reminders", "Missing preceptor email: reminder kept only in match audit, not in a send-ready file.", rid)
                continue
            kinds = sorted({m["kind"] for m in pending}, key=KINDS.index)
            links = {}
            for k in kinds:
                base = settings.survey_urls.get(k, "")
                links[k] = base + ("&" if "?" in base else "?") + urlencode({"student": student["student_name"], "preceptor": faculty_name}) if base else ""
            first_link = next((links[k] for k in kinds if links[k]), "")
            partial = first_link + "&complete=1&ph=3&ch=3&pp=3&cp=3" if settings.legacy_partial_links and first_link else ""
            note = f"The student reported working with you. In the supplied records through {effective_cutoff}, we have not received: {'; '.join(LABELS[k] for k in kinds)}. Please complete the listed assessment(s)."
            preceptor_rows.append({"faculty_email": email, "faculty_name": faculty_name, "student_name": student["student_name"], "reminder_note": note, "blank_form_link": first_link, "partial_form_link": partial, "record_id": rid, "student_email": student["email"], "evaluation_type": "; ".join(LABELS[k] for k in kinds), "expected_eval_count": len(kinds), "completed_eval_count": 0, "pending_eval_count": len(kinds), "cas_link": links.get("cas", ""), "hp_link": links.get("hp", ""), "handoff_link": links.get("handoff", ""), "data_through": effective_cutoff, "rotation_start": student["start_date"]})
    return {"checklist_review": checklist_review, "student_review": student_review, "preceptor_reminders": preceptor_rows, "match_audit": match_audit, "scores": summaries}


@dataclass
class Run:
    settings: Settings
    roster: list[dict]
    evaluations: list[dict]
    entries: list[dict]
    matches: list[dict]
    reports: dict
    coverage: dict
    messages: Messages
    people: People
    source_stats: list[dict]


REMINDER_NAMES = (
    "student_checklist_review.csv",
    "feedback_reminders_power_automate.csv",
    "preceptor_eval_reminders.csv",
    "observed_hp_reminders.csv",
)


LEGACY_CAS_COLUMNS = [
    "faculty_email", "faculty_name", "student_name", "evaluation_type",
    "expected_eval_count", "completed_eval_count", "pending_eval_count",
    "duplicate_match_flag", "reminder_note", "blank_form_link", "partial_form_link",
]


LEGACY_HP_COLUMNS = [
    "faculty_email", "faculty_name", "student_name", "reminder_note",
    "blank_form_link", "partial_form_link",
]


def inferred_export_date(filename: str, as_of: str) -> str:
    """A filename date is a convenient default, not proof of export completeness."""
    for candidate in re.findall(r"(?<!\d)(20\d{6})(?!\d)", filename):
        try:
            value = datetime.strptime(candidate, "%Y%m%d").date().isoformat()
            return min(value, as_of)
        except ValueError:
            continue
    return as_of


def daily_readiness(run: Run) -> dict[str, bool]:
    valid = bool(run.roster) and not run.messages.blocked
    checks = valid and all(run.coverage["checklist"].get(s["start_date"], False) for s in run.roster)
    assessments = valid and all(
        run.coverage["matches"].get(s["start_date"], False)
        and run.coverage["oasis"].get(s["start_date"], False) for s in run.roster)
    return {REMINDER_NAMES[0]: checks, REMINDER_NAMES[1]: assessments,
            REMINDER_NAMES[2]: assessments, REMINDER_NAMES[3]: assessments}


# Fixed mailing contracts copied from the four CSV examples supplied by the owner.
# Never add audit or REDCap fields to these lists. The detailed exports are separate.
LEGACY_CHECKLIST_COLUMNS = [
    "record_id", "name", "email", "missing_items", "observing_only_items",
    "missing_items_delimiter", "observing_only_items_delimiter", "status",
    "missing_count", "observing_only_count",
]
LEGACY_STUDENT_COLUMNS = [
    "record_id", "student_name", "email", "reminderob", "remindercas",
    "reminderhandoff", "random_preceptor", "preceptor_shoutout",
]
REMINDER_COLUMNS = {
    "student_checklist_review.csv": LEGACY_CHECKLIST_COLUMNS,
    "feedback_reminders_power_automate.csv": LEGACY_STUDENT_COLUMNS,
    "preceptor_eval_reminders.csv": LEGACY_CAS_COLUMNS,
    "observed_hp_reminders.csv": LEGACY_HP_COLUMNS,
}


def reminder_csv_bytes(rows: list[dict], columns: list[str]) -> bytes:
    """Match the supplied CSVs: UTF-8 without BOM, CRLF, one row per line.

    Retain the original comma-free Flow values and <br> delimiters. All fields
    pass through flow_value; REDCap and audit exports do not use this serializer.
    Even an empty result has the complete, fixed header in the original order.
    """
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore", lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: flow_value(row.get(name, "")) for name in columns})
    return out.getvalue().encode("utf-8")


def original_faculty_list(names: list[str]) -> str:
    names = sorted({text(name) for name in names if text(name)})
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return names[0] + " and " + names[1]
    return " ; ".join(names[:-1]) + " ; and " + names[-1]


def original_student_note(kind: str, count: int, target: int, names: list[str]) -> str:
    """Restore the wording in the uploaded student-reminder template.

    Counts still come from the deduplicated request/submission union. No stale
    example counts, recipients or preceptors are copied into a new mailing.
    """
    complete = {
        "hp": "Observed H&P requirement complete. Thank you for documenting your observed history and physical experiences.",
        "cas": "Clinical Assessment of Student requirement complete. Thank you for consistently requesting and documenting feedback during the clerkship.",
        "handoff": "Pediatric Clerkship Handoff requirement complete. Thank you for documenting your handoff experience during the clerkship.",
    }
    if count >= target:
        return complete[kind]
    label = LABELS[kind]
    activity = "observed history and physical" if kind == "hp" else label
    noun = "solicitation" if kind == "handoff" else "solicitations"
    faculty = original_faculty_list(names)
    middle = f" Documented {activity} activity is currently associated with {faculty}." if faculty else ""
    return (f"{label} - our records currently show {count} documented {noun} or submission"
            + ("." if kind == "handoff" else "s.")
            + middle + f" Students are expected to have {target} documented {activity} "
            + ("solicitation or submission" if kind == "handoff" else "solicitations or submissions")
            + " during the clerkship.")


def reminder_files(run: Run) -> dict[str, bytes]:
    """Four original mailing files; CAS and observed H&P are NEVER combined.

    Matching, receipt checks and grading stay in the processing core. This is an
    export adapter only: one pending CAS row and/or one pending H&P row per
    student/preceptor, and no reminder for an assessment already received.
    Handoffs remain in the student reminder and in the detailed audit/REDCap data.
    """
    check_rows = []
    for original in run.reports["checklist_review"]:
        # The owner's checklist template includes Complete rows as well as
        # Needs review rows. Keep both; do not represent unknown coverage as zero.
        if original["status"] not in {"Complete", "Needs review"} or not original["email"]:
            continue
        row = dict(original)
        issues = [row["missing_items"]] if row["missing_items"] else []
        # Keep these actionable checks visible without adding new Flow columns.
        if row.get("participation_review_items"):
            issues.append("Participation level needs review: " + row["participation_review_items"])
        if row.get("incomplete_items"):
            issues.append("Logged but not marked complete: " + row["incomplete_items"])
        row["missing_items"] = "<br>".join(issues)
        check_rows.append(row)
    check_rows.sort(key=lambda r: (r["name"], r["record_id"]))

    faculty = defaultdict(set)
    for match in run.matches:
        if match["eval_period_start_date"] <= run.settings.as_of:
            faculty[(match["record_id"], match["kind"])].add(display_name(match["faculty_name"]))
    for evaluation in run.evaluations:
        faculty[(evaluation["record_id"], evaluation["kind"])].add(display_name(evaluation["evaluator"]))
    students = []
    for original in run.reports["student_review"]:
        if original["reminder_needed"] != "Yes" or not original["email"]:
            continue
        row = dict(original)
        for kind, column in (("hp", "reminderob"), ("cas", "remindercas"), ("handoff", "reminderhandoff")):
            row[column] = original_student_note(kind, int(row[kind + "_credit"]),
                                                run.settings.targets[kind], list(faculty[(row["record_id"], kind)]))
        # Match the shoutout URL encoding in the user's original export.
        row["preceptor_shoutout"] = text(row.get("preceptor_shoutout")).replace("+", "%20")
        students.append(row)
    students.sort(key=lambda r: (r["start_date"], r["student_name"], r["record_id"]))

    cas_rows, hp_rows = [], []
    seen = {"cas": set(), "hp": set()}
    for row in run.reports["preceptor_reminders"]:
        pending = set(row["evaluation_type"].split("; "))
        for kind, target in (("cas", cas_rows), ("hp", hp_rows)):
            if LABELS[kind] not in pending:
                continue
            identity = (row["record_id"], row["rotation_start"], norm(row["faculty_email"]))
            if identity in seen[kind]:
                continue
            seen[kind].add(identity)
            link = row[kind + "_link"]
            note = ("The student reported working with you - but we have not yet received the corresponding evaluation."
                    if kind == "cas" else
                    "The student indicated that you observed an H&P encounter with them - but we have not yet received the corresponding formative assessment.")
            target.append({**row, "evaluation_type": FORM_NAMES[kind],
                           "expected_eval_count": 1, "completed_eval_count": 0, "pending_eval_count": 1,
                           "duplicate_match_flag": "", "reminder_note": note, "blank_form_link": link,
                           "partial_form_link": link + "&complete=1&ph=3&ch=3&pp=3&cp=3"
                           if run.settings.legacy_partial_links and link else ""})
    for rows in (cas_rows, hp_rows):
        rows.sort(key=lambda r: (r["student_name"], r["faculty_name"], r["faculty_email"]))
    rows_by_file = {"student_checklist_review.csv": check_rows,
                    "feedback_reminders_power_automate.csv": students,
                    "preceptor_eval_reminders.csv": cas_rows, "observed_hp_reminders.csv": hp_rows}
    return {name: reminder_csv_bytes(rows_by_file[name], REMINDER_COLUMNS[name]) for name in REMINDER_NAMES}


def legacy_power_automate_files(run: Run) -> dict[str, bytes]:
    """Backward-compatible function name: the original layouts are now the default."""
    return reminder_files(run)


def exclusion_choices(raw_oasis: list[dict]) -> dict[str, dict]:
    """Metadata-only picker. Does not treat raw question rows as separate evaluations."""
    students = {}
    for raw in raw_oasis:
        if kind_of(get(raw, "Evaluation")) != "cas" or not stamp(get(raw, "Submit Date")):
            continue
        if norm(get(raw, "Eval Status")) in {"draft", "incomplete", "pending", "not submitted"}:
            continue
        rid = norm(get(raw, "Student External ID", "External ID"))
        evaluator, email = get(raw, "Evaluator"), norm(get(raw, "Evaluator Email"))
        if not rid or not (evaluator or email):
            continue
        student = students.setdefault(rid, {"name": display_name(get(raw, "Student")) or rid, "preceptors": {}})
        fk = digest([name_key(evaluator), email])[:20]
        preceptor = student["preceptors"].setdefault(fk, {"evaluator": evaluator, "email": email, "forms": {}})
        form = {"form_record": get(raw, "Form Record"), "rotation_start": day(get(raw, "Start Date")), "submit_date": stamp(get(raw, "Submit Date"))}
        form_key = digest(form)[:20]
        preceptor["forms"][form_key] = form
    return students


def app_secret(st: Any, name: str, fallback: str = "") -> str:
    try:
        return text(st.secrets.get(name, os.getenv(name, fallback)))
    except (FileNotFoundError, KeyError):
        return os.getenv(name, fallback)


# ---------------------------------------------------------------------------
# Fresh project: no dependency on an existing REDCap project or API.
# ---------------------------------------------------------------------------
def process(schedule: list[dict], checklist: list[dict], matches: list[dict], oasis: list[dict],
            settings: Settings | None = None, log: Messages | None = None) -> Run:
    settings, log = settings or Settings(), log or Messages()
    if not day(settings.as_of):
        raise ValueError("A valid reminder date is required.")
    if settings.data_through and (not day(settings.data_through) or settings.data_through > settings.as_of):
        raise ValueError("The OASIS export date cannot be later than the reminder date.")
    if any(not isinstance(v, int) or v < 1 for v in settings.targets.values()):
        raise ValueError("Assessment targets must be positive whole numbers.")
    roster = build_roster(schedule, checklist, matches, oasis, settings, log)
    if not roster:
        log.add("ERROR", "Rotation schedule", "No students could be matched. Use files for the same rotation.")
    people = People([source_identity(r, "faculty") for r in matches + oasis])
    evaluations = normalize_evaluations(oasis, roster, people, settings, log)
    associations = normalize_matches(matches, roster, people, settings, log)
    entries = normalize_checklist(checklist, roster, settings, log)
    cover = {name: coverage(rows, roster, settings, name, log)
             for name, rows in (("checklist", checklist), ("matches", matches), ("oasis", oasis))}
    reports = build_reports(roster, entries, associations, evaluations, cover, people, settings, log)
    stats = [{"source": name, "input_rows": len(raw), "selected_entries_or_forms": len(selected),
              "rotation_starts_present": "; ".join(sorted({source_identity(r, "student")["start"] for r in raw} - {""}))}
             for name, raw, selected in (("Checklist", checklist, entries), ("Preceptor matches", matches, associations), ("OASIS ME", oasis, evaluations))]
    return Run(settings, roster, evaluations, entries, associations, reports, cover, log, people, stats)


def process_uploads(source_bytes: list[bytes], filenames: list[str], settings: Settings) -> tuple[Run, list[list[dict]]]:
    if len(source_bytes) != 4 or len(filenames) != 4:
        raise ValueError("Upload the four source CSV files.")
    log = Messages()
    raw = [read_csv_bytes(data, name, log) for data, name in zip(source_bytes, filenames)]
    # Verify headers even when an export correctly contains zero data rows.
    header_groups = [
        [("legal_name", "name", "Student Name", "Student"), ("start_date", "Start Date")],
        [("Student name", "Student Name"), ("External ID", "Student External ID"), ("Start Date",), ("Item",), ("Item status",)],
        [("Student External ID",), ("Faculty Name",), ("Manual Evaluations",)],
        [("Student External ID", "External ID"), ("Student",), ("Evaluator",), ("Evaluation",), ("Submit Date",), ("Question",), ("Multiple Choice Value", "Mult Choice Value")],
    ]
    for blob, name, rows, required in zip(source_bytes, filenames, raw, header_groups):
        if not rows:
            encoding = "utf-16" if blob.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            try:
                content = blob.decode(encoding)
            except UnicodeDecodeError:
                content = blob.decode("cp1252")
            headers = next(csv.reader(io.StringIO(content)), [])
            require([dict.fromkeys(headers, "")], name, required)
    return process(*raw, settings=settings, log=log), raw


def tracking_rows(run: Run) -> list[dict]:
    out = []
    for score, check, reminder in zip(run.reports["scores"], run.reports["checklist_review"], run.reports["student_review"]):
        rid = score["record_id"]
        row = {"record_id": rid, "cst_checked_at": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S"), "cst_data_through": run.settings.data_through or run.settings.as_of}
        for kind in KINDS:
            for metric in ("matched", "submitted", "credit"):
                v = reminder.get(kind + "_" + metric)
                # Do not publish unverified zeros from a wrong-cohort source export.
                if reminder["reminder_needed"] != "Coverage not confirmed":
                    row["cst_" + kind + "_" + metric] = v
        if run.coverage["oasis"][score["start_date"]]:
            row.update(cst_scorable=score["scorable_evaluations"], cst_manual_excluded=score["manual_exclusions"], cst_score_before=score["score_before_drop"], cst_score_after=score["clinical_score_375"], cst_drop_description=score["exclude"])
        if check["status"] != "Coverage not confirmed":
            row.update(cst_missing_count=check["missing_count"], cst_missing_items=check["missing_items"], cst_checklist_status=check["status"], cst_participation_issues="\n".join(text(check[k]) for k in ("observing_only_items", "participation_review_items", "incomplete_items") if text(check[k])))
        if reminder["reminder_needed"] != "Coverage not confirmed":
            row["cst_student_reminder"] = "\n".join(reminder[k] for k in ("reminderob", "remindercas", "reminderhandoff"))
            pending = [r for r in run.reports["preceptor_reminders"] if r["record_id"] == rid]
            row["cst_preceptor_count"] = len(pending)
            row["cst_preceptor_reminders"] = "\n".join(f"{r['faculty_name']}: {r['evaluation_type']}" for r in pending)
        out.append(row)
    return out


def output_files(run: Run) -> dict[str, bytes]:
    """Original four mailing layouts plus unchanged, separately named detailed files."""
    return {
        **reminder_files(run),
        "all_student_checklist_status.csv": csv_bytes(run.reports["checklist_review"], CHECKLIST_COLUMNS, flow=True),
        "all_student_requirement_status.csv": csv_bytes(run.reports["student_review"], flow=True),
        "clinical_scores.csv": csv_bytes(run.reports["scores"]),
        "evaluation_audit.csv": csv_bytes(run.evaluations),
        "checklist_entries.csv": csv_bytes(run.entries),
        "preceptor_matches.csv": csv_bytes(run.matches),
        "preceptor_matching_audit.csv": csv_bytes(run.reports["match_audit"]),
        "rotation_roster.csv": csv_bytes(run.roster),
        "validation_report.csv": csv_bytes(run.messages.rows, ["level", "source", "record_id", "detail"]),
        "source_summary.csv": csv_bytes(run.source_stats),
        "tracking_status.csv": csv_bytes(tracking_rows(run)),
    }


def zipped(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for filename, data in files.items():
            z.writestr(filename, data)
    return out.getvalue()


# REDCap schema. All imported fields are generated fields on ONE non-repeating
# instrument. Director-entered notes/grades should be placed on another instrument.
DD_COLUMNS = ["Variable / Field Name", "Form Name", "Section Header", "Field Type", "Field Label",
              "Choices, Calculations, OR Slider Labels", "Field Note", "Text Validation Type OR Show Slider Number",
              "Text Validation Min", "Text Validation Max", "Identifier?", "Branching Logic (Show field only if...)",
              "Required Field?", "Custom Alignment", "Question Number (surveys only)", "Matrix Group Name",
              "Matrix Ranking?", "Field Annotation"]
RECORD_TYPES = {"1": "Student summary", "2": "Clinical evaluation", "3": "Observed H&P",
                "4": "Handoff", "5": "Checklist entry", "6": "Preceptor match"}
NEW_FORM = "clerkship_data"


def field_specs() -> list[dict]:
    specs = []
    def add(name: str, label: str, validation: str = "", *, kind: str = "text", branch: str = "",
            section: str = "", choices: str = "", note: str = "", identifier: bool = False):
        specs.append(dict(name=name, label=label, validation=validation, kind=kind, branch=branch,
                          section=section, choices=choices, note=note, identifier=identifier))
    add("record_id", "Record ID — preserve this value during import", section="Record and student", identifier=True)
    add("record_type", "Record type", kind="dropdown", choices=" | ".join(f"{k}, {v}" for k, v in RECORD_TYPES.items()))
    for name, label in (("student_key", "Student / rotation key — links all rows for the same rotation"),
                        ("student_external_id", "Student external ID"), ("student_name", "Student name")):
        add(name, label, identifier=True)
    add("student_email", "Student email", "email", identifier=True)
    add("start_date", "Rotation start", "date_ymd")
    add("end_date", "Rotation end", "date_ymd")
    add("report_date", "Reminder / analysis date", "date_ymd")
    add("data_through", "OASIS export reference date", "date_ymd", note="Derived from the filename unless changed. Not proof of complete source coverage.")
    add("batch_id", "Import batch ID", note="Filter on this ID to review exactly the rows included in this run. Rows absent from a later import are not deleted.")
    add("schema_version", "Output schema version")
    add("source_key", "Stable source identity", kind="notes", note="Identity, not row number. Do not edit.")
    summary = "[record_type] = '1'"
    add("summary_review", "Student review — clinical scores, feedback and requirements", kind="notes", branch=summary, section="Student progress summary")
    add("checklist_status", "Encounter requirements status", branch=summary)
    add("requirements_status", "Student needs assessment-solicitation reminders (Yes/No)", branch=summary)
    for name, label in (("cas_submissions", "Clinical evaluation submissions — including excluded evaluations"),
                        ("scorable_evaluations", "Scorable clinical evaluations after manual exclusions"),
                        ("manual_exclusions", "Manually excluded clinical evaluations"),
                        ("unscorable_submissions", "Clinical submissions without any numeric domain score"),
                        ("evaluations_kept", "Clinical evaluations contributing to the adjusted score")):
        add(name, label, "integer", branch=summary)
    add("score_before_drop", "Clinical score before automatic lowest-score drop (out of 375)", "number", branch=summary)
    add("clinical_score_375", "Clinical score after exclusions and lowest-score drop (out of 375)", "number", branch=summary,
        note="Clinical assessment component only; this is not a final clerkship grade.")
    for k, label in zip(DOMAIN_KEYS, ("Knowledge for Practice", "Clinical Reasoning", "Documentation and Oral Presentation", "Communication with Patients and Families", "Collaboration with Care Team")):
        add(k + "_mean_after_drop", label + " — adjusted mean (out of 5)", "number", branch=summary)
    for name, label in (("dropped_form_record", "Automatically dropped Form Record"), ("dropped_evaluator", "Automatically dropped evaluator")):
        add(name, label, branch=summary)
    add("exclude", "Automatic lowest-score drop description", kind="notes", branch=summary)
    for name, label in (("professionalism_review", "Professionalism flag needs director review"),
                        ("individual_domain_below_3", "At least one individual observed domain score below 3"),
                        ("domain_mean_below_3", "At least one adjusted domain mean below 3")):
        add(name, label, kind="yesno", branch=summary, note="A review flag, not an automatic failing grade. Excluded evaluations remain in feedback review.")
    for k in KINDS:
        for suffix, label in (("matched", "unique matched preceptors"), ("submitted", "submitted forms"), ("credit", "unique preceptors matched or submitted"), ("required", "solicitation target")):
            add(k + "_" + suffix, f"{LABELS[k]} — {label}", "integer", branch=summary)
    for k, label in (("missing_count", "Missing encounter categories"), ("observing_only_count", "Observing-only categories needing participation"),
                     ("checklist_entry_count", "Number of logged checklist entries"), ("completed_categories", "Encounter categories meeting requirements"),
                     ("pending_preceptor_count", "Preceptor–student reminders in this run")):
        add(k, label, "integer", branch=summary)
    for k, label in (("missing_items", "Missing encounter categories"), ("observing_only_items", "Observing-only issues"),
                     ("participation_review_items", "Participation level needs review"), ("incomplete_items", "Logged but not complete"),
                     ("reminderob", "Student observed H&P reminder text"), ("remindercas", "Student clinical evaluation reminder text"),
                     ("reminderhandoff", "Student handoff reminder text"), ("pending_preceptors", "Pending preceptor reminders"),
                     ("all_cas_strengths", "Clinical evaluation strengths — all received evaluations"),
                     ("all_cas_weaknesses", "Clinical evaluation growth feedback — all received evaluations"),
                     ("evaluation_details", "Individual evaluation review"), ("epa_details", "Observed H&P and handoff review"),
                     ("checklist_details", "Encounter log review"), ("exclusions_json", "Exclusion rules used for this student")):
        add(k, label, kind="notes", branch=summary)
    add("last_checklist_entry", "Latest logged checklist entry", "datetime_seconds_ymd", branch=summary)
    add("submitted_ce", "Latest entry timestamp when ALL required categories are complete", "datetime_seconds_ymd", branch=summary,
        note="Blank when incomplete. This is the latest entry timestamp in a complete snapshot, not the historical first-completion time.")
    for short, label in (("acute", "Acute conditions"), ("behavior", "Behavior"), ("newborn", "Common newborn conditions"),
                         ("derm", "Dermatologic system"), ("gi", "Gastrointestinal tract"), ("well_child", "Health supervision"),
                         ("health_systems", "Health systems"), ("humanities", "Humanities"), ("other", "Other"), ("respiratory", "Respiratory tract")):
        add("enc_" + short, label + " — requirement status", branch=summary)
    evals = "[record_type] = '2' or [record_type] = '3' or [record_type] = '4'"
    for name, label in (("form_record", "OASIS Form Record"), ("evaluator", "Evaluator"), ("evaluation", "Evaluation type")):
        add(name, label, branch=evals, section="Individual evaluation" if name == "form_record" else "")
    add("evaluator_email", "Evaluator email", "email", branch=evals, identifier=True)
    add("submit_date", "Evaluation submitted", "datetime_seconds_ymd", branch=evals)
    add("source_answers_json", "Original OASIS question / answer records", kind="notes", branch=evals,
        note="Preserves questions and responses not used in the numeric calculation; do not interpret an unscored answer as zero.")
    cas = "[record_type] = '2'"
    for k in DOMAIN_KEYS:
        add(k, k.upper() + " — original observed score", "number", branch=cas)
        add("effective_" + k, k.upper() + " — score used after mean imputation", "number", branch=cas)
    add("prof", "Professional Behavior — original response value", "number", branch=cas)
    add("tot", "Evaluation total (out of 375)", "number", branch=cas)
    add("iv", "Mean imputed for unobserved domains", "number", branch=cas)
    for name, label in (("scorable", "At least one numeric domain score"), ("manual_excluded", "Manual exclusion active"),
                        ("drop_lowest", "Automatically dropped lowest evaluation"), ("included_in_score", "Included in adjusted clinical score")):
        add(name, label, kind="yesno", branch=cas)
    for name, label in (("manual_exclusion_reason", "Manual exclusion reason"), ("cas_strengths", "Strengths"), ("cas_weaknesses", "Growth feedback")):
        add(name, label, kind="notes", branch=cas)
    for name, label in (("epa_obh_score", "Observed history score"), ("epa_obp_score", "Observed physical exam score"), ("epa_obho_score", "Handoff score")):
        add(name, label, "number", branch=evals)
    for name, label in (("epaobh_weaknesses", "Observed history feedback"), ("epaobp_weaknesses", "Physical exam feedback"), ("epaobho_weaknesses", "Handoff feedback")):
        add(name, label, kind="notes", branch=evals)
    ck = "[record_type] = '5'"
    for name, label in (("checklist", "Checklist"), ("checklist_source_status", "Source checklist status"), ("item", "Encounter item"),
                        ("item_status", "Item status"), ("canonical_item", "Recognized requirement category"),
                        ("student_activity", "Student participation"), ("originalcopy", "Original / copy"), ("location_cl", "Location"),
                        ("signed_by", "Signed by"), ("verified_by", "Verified by"), ("times_observed", "Times observed"),
                        ("is_proficient", "Proficiency source value"), ("needs_practice", "Needs practice source value")):
        add(name, label, branch=ck, section="Individual checklist entry" if name == "checklist" else "")
    for name, label in (("time_entered", "Time entered"), ("time_signed", "Time signed"), ("verified_date", "Verified date")):
        add(name, label, "datetime_seconds_ymd", branch=ck)
    add("encounter_date", "Encounter date", "date_ymd", branch=ck)
    for name, label in (("comments", "Encounter comments"), ("verification_comments", "Verification comments"),
                        ("assisted_or_above", "Assisted or above — source activity"), ("observed_or_above", "Observed or above — source activity"),
                        ("source_entry_json", "Original checklist entry, including extra source fields")):
        add(name, label, kind="notes", branch=ck)
    ma = "[record_type] = '6'"
    for name, label in (("faculty_name", "Preceptor name"), ("faculty_external_id", "Preceptor external ID"), ("faculty_username", "Preceptor username"),
                        ("manual_evaluations", "Matched assessment type"), ("type_of_association", "Association type"),
                        ("classification", "Classification"), ("student_activity1", "Student association activity"),
                        ("match_status", "Matched assessment status")):
        add(name, label, branch=ma, section="Preceptor match" if name == "faculty_name" else "")
    add("faculty_email", "Preceptor email", "email", branch=ma, identifier=True)
    for name, label in (("eval_period_start_date", "Evaluation-period start"), ("eval_period_end_date", "Evaluation-period end")):
        add(name, label, "date_ymd", branch=ma)
    return specs


def redcap_dictionary() -> list[dict]:
    return [{**dict.fromkeys(DD_COLUMNS, ""), "Variable / Field Name": s["name"], "Form Name": NEW_FORM,
             "Section Header": s["section"], "Field Type": s["kind"], "Field Label": s["label"],
             "Choices, Calculations, OR Slider Labels": s["choices"], "Field Note": s["note"],
             "Text Validation Type OR Show Slider Number": s["validation"], "Identifier?": "y" if s["identifier"] else "",
             "Branching Logic (Show field only if...)": s["branch"], "Required Field?": "y" if s["name"] == "record_id" else ""}
            for s in field_specs()]


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def record_key(prefix: str, identity: list) -> str:
    """128-bit content-derived identity, not position or sequential repeat numbering."""
    return prefix + "_" + digest([SCHEMA_VERSION, *identity])[:32]


def evaluation_identity(e: dict) -> list:
    base = [e["record_id"], e["rotation_start"], e["kind"]]
    if text(e.get("form_record")):
        return base + ["form", text(e["form_record"])]
    # With no source form ID, identity changes to name/time require reconciliation.
    return base + ["name_time", name_key(e["evaluator"]), e["submit_date"]]


def checklist_identity(e: dict) -> list:
    if not e.get("time_entered"):
        raise ValueError("A checklist entry has no Time entered. Re-export with that column so repeated imports can identify the same entry.")
    return [e["record_id"], e["rotation_start"], key(e["checklist"]), key(e["item"]), e["time_entered"]]


def category_status(entries: list[dict], item: str, confirmed: bool) -> str:
    if not confirmed:
        return "Coverage not confirmed"
    found = [e for e in entries if e["canonical_item"] == item]
    if not found:
        return "Missing"
    complete = [e for e in found if norm(e["item_status"]) in {"complete", "completed", "2"}]
    if not complete:
        return "Logged but incomplete"
    allowed = {"Performing", "Assisting", "Observing"} if item in OBSERVING_ALLOWED else {"Performing", "Assisting"}
    if any(e["student_activity"] in allowed for e in complete):
        return "Complete"
    return "Observing only" if all(e["student_activity"] == "Observing" for e in complete) else "Participation needs review"


def _source_answer_index(raw_oasis: list[dict], roster: list[dict]) -> dict:
    by_key = defaultdict(list)
    starts = defaultdict(list)
    for s in roster:
        starts[s["record_id"]].append(s)
    for r in raw_oasis:
        who, kind = source_identity(r, "student"), kind_of(get(r, "Evaluation"))
        if not kind or who["id"] not in starts:
            continue
        start = who["start"]
        if not start:
            options = [s["start_date"] for s in starts[who["id"]] if s["start_date"] <= day(get(r, "Submit Date")) <= s["end_date"]]
            if len(options) != 1:
                continue
            start = options[0]
        e = {"record_id": who["id"], "rotation_start": start, "kind": kind, "form_record": get(r, "Form Record"),
             "evaluator": get(r, "Evaluator"), "submit_date": stamp(get(r, "Submit Date"))}
        answer = {k: get(r, k) for k in ("Question Number", "Question ID", "Question", "Answer text", "Multiple Choice Order", "Multiple Choice Value", "Multiple Choice Label")}
        answer["Answer text"] = get(r, "Answer text", "Answer Text")
        answer["Multiple Choice Value"] = get(r, "Multiple Choice Value", "Mult Choice Value")
        by_key[json_text(evaluation_identity(e))].append(answer)
    return {k: sorted({json_text(r): r for r in rows}.values(), key=json_text) for k, rows in by_key.items()}


def redcap_rows(run: Run, raw_checklist: list[dict] | None = None, raw_oasis: list[dict] | None = None) -> list[dict]:
    """Flat records for a NEW project. Excluded evaluations are retained with flags."""
    if run.messages.blocked or not run.roster:
        raise ValueError("Resolve the file errors before creating the REDCap import.")
    if not all(daily_readiness(run).values()):
        raise ValueError("The source files do not cover all selected rotation dates. Use files for the same rotation before creating the REDCap import.")
    roster = {s["record_id"]: s for s in run.roster}
    answers = _source_answer_index(raw_oasis or [], run.roster)
    originals = defaultdict(list)
    for raw in raw_checklist or []:
        who = source_identity(raw, "student")
        k = [who["id"], who["start"], key(get(raw, "Checklist")), key(get(raw, "Item")), stamp(get(raw, "Time entered"))]
        originals[json_text(k)].append(raw)
    rows, seen = [], {}
    def base(rid: str, row_type: str, identity: list, prefix: str) -> dict:
        s = roster[rid]
        return {"record_id": record_key(prefix, identity), "record_type": row_type,
                "student_key": record_key("s", [rid, s["start_date"]]), "student_external_id": rid,
                "student_name": s["student_name"], "student_email": s["email"], "start_date": s["start_date"], "end_date": s["end_date"],
                "report_date": run.settings.as_of, "data_through": run.settings.data_through or run.settings.as_of,
                "schema_version": SCHEMA_VERSION, "source_key": json_text(identity)}
    def append(row: dict):
        row = {k: str(int(v)) if isinstance(v, bool) else text(v) for k, v in row.items()}
        previous = seen.get(row["record_id"])
        if previous and previous != row:
            raise ValueError("Two different entries have the same source identity. Re-export or reconcile the conflicting entries; no record number was guessed.")
        if not previous:
            seen[row["record_id"]] = row
            rows.append(row)
    for score, check, reminder in zip(run.reports["scores"], run.reports["checklist_review"], run.reports["student_review"]):
        rid = score["record_id"]
        row = base(rid, "1", [rid, score["start_date"]], "s")
        for k in ("cas_submissions", "scorable_evaluations", "manual_exclusions", "unscorable_submissions", "score_before_drop", "clinical_score_375", "dropped_form_record", "dropped_evaluator", "exclude", "professionalism_review", "individual_domain_below_3", "domain_mean_below_3", *(k + "_mean_after_drop" for k in DOMAIN_KEYS)):
            row[k] = score[k]
        row["evaluations_kept"] = score["scorable_evaluations"] - int(bool(score["exclude"]))
        row["checklist_status"], row["requirements_status"] = check["status"], reminder["reminder_needed"]
        for k in ("missing_items", "observing_only_items", "missing_count", "observing_only_count", "participation_review_items", "incomplete_items"):
            row[k] = check[k]
        for k in ("reminderob", "remindercas", "reminderhandoff", *(k + "_" + suffix for k in KINDS for suffix in ("matched", "submitted", "credit"))):
            row[k] = reminder[k]
        for k in KINDS:
            row[k + "_required"] = run.settings.targets[k]
        cs = sorted((e for e in run.entries if e["record_id"] == rid), key=lambda e: (e["time_entered"], e["item"]))
        es = [e for e in run.evaluations if e["record_id"] == rid]
        cas = [e for e in es if e["kind"] == "cas"]
        pending = [r for r in run.reports["preceptor_reminders"] if r["record_id"] == rid]
        row["pending_preceptor_count"] = len(pending)
        row["pending_preceptors"] = "\n".join(f"{r['faculty_name']}: {r['evaluation_type']}" for r in pending)
        row["checklist_entry_count"] = len(cs)
        row["last_checklist_entry"] = max((e["time_entered"] for e in cs if e["time_entered"]), default="")
        row["submitted_ce"] = row["last_checklist_entry"] if check["status"] == "Complete" else ""
        cat_keys = ("acute", "behavior", "newborn", "derm", "gi", "well_child", "health_systems", "humanities", "other", "respiratory")
        statuses = [category_status(cs, item, True) for item in REQUIRED_ITEMS]
        row.update({"enc_" + k: v for k, v in zip(cat_keys, statuses)})
        row["completed_categories"] = statuses.count("Complete")
        row["all_cas_strengths"] = "\n\n".join(f"{display_name(e['evaluator'])}: {e['cas_strengths']}" for e in cas if text(e.get("cas_strengths")))
        row["all_cas_weaknesses"] = "\n\n".join(f"{display_name(e['evaluator'])}: {e['cas_weaknesses']}" for e in cas if text(e.get("cas_weaknesses")))
        row["evaluation_details"] = "\n\n".join(
            f"{display_name(e['evaluator'])} | {e['submit_date']} | Form {e['form_record'] or 'not provided'}\n"
            + " | ".join(f"{k.upper()}: {e.get(k, 'N/A')}" for k in DOMAIN_KEYS)
            + f"\nTotal: {e['tot'] if text(e['tot']) else 'unscorable'}/375 | Imputed value: {e['iv']}"
            + f"\nStatus: {'Manually excluded' if e['manual_excluded'] else 'Automatic lowest-score drop' if e['drop_lowest'] else 'Included' if number(e['tot']) is not None else 'Received but unscorable'}"
            + (f"\nReason: {e.get('manual_exclusion_reason', '')}" if e['manual_excluded'] else "") for e in cas)
        row["epa_details"] = "\n\n".join(
            f"{LABELS[e['kind']]} — {display_name(e['evaluator'])} — {e['submit_date']}\n"
            + "\n".join(f"{k}: {v}" for k, v in e.items() if k.startswith(("epa_", "epaobh_", "epaobp_", "epaobho_")))
            for e in es if e["kind"] != "cas")
        row["checklist_details"] = "\n\n".join(f"{e['item']}\n{e['date_97fae7']} | {e['student_activity']} | {e['item_status']}\n{e.get('comments', '')}" for e in cs)
        row["exclusions_json"] = json_text([r for r in run.settings.exclusions if r["record_id"] == rid])
        row["summary_review"] = (
            f"{score['student_name']} | {score['start_date']} to {score['end_date']}\n"
            f"Clinical score: {score['clinical_score_375'] if text(score['clinical_score_375']) else 'Not yet scorable'} / 375\n"
            f"Received: {len(cas)} | Manual exclusions: {score['manual_exclusions']} | {score['exclude'] or 'No automatic drop'}\n"
            f"Encounters: {row['completed_categories']} / 10 categories complete\n"
            + "\n".join(f"{LABELS[k]}: {reminder[k + '_credit']} / {run.settings.targets[k]} matched-or-submitted preceptors; {reminder[k + '_submitted']} submitted forms" for k in KINDS)
            + "\n\nClinical component only. Individual below-expectation scores and professionalism flags require director review, including excluded evaluations.")
        append(row)
    for e in run.evaluations:
        identity = evaluation_identity(e)
        row = base(e["record_id"], {"cas": "2", "hp": "3", "handoff": "4"}[e["kind"]], identity, "e")
        for k in ("form_record", "evaluator", "evaluator_email", "evaluation", "submit_date", *DOMAIN_KEYS,
                  *("effective_" + k for k in DOMAIN_KEYS), "prof", "tot", "iv", "manual_excluded", "drop_lowest", "manual_exclusion_reason",
                  "cas_strengths", "cas_weaknesses", "epa_obh_score", "epa_obp_score", "epa_obho_score", "epaobh_weaknesses", "epaobp_weaknesses", "epaobho_weaknesses"):
            if k in e:
                row[k] = e[k]
        if e["kind"] == "cas":
            row["scorable"] = number(e.get("tot")) is not None
            row["included_in_score"] = bool(row["scorable"] and not e["manual_excluded"] and not e["drop_lowest"])
        row["source_answers_json"] = json_text(answers.get(json_text(identity), []))
        append(row)
    for e in run.entries:
        identity = checklist_identity(e)
        row = base(e["record_id"], "5", identity, "c")
        for k in ("checklist", "item", "item_status", "canonical_item", "student_activity", "originalcopy", "location_cl", "signed_by", "verified_by",
                  "times_observed", "is_proficient", "needs_practice", "time_entered", "time_signed", "verified_date", "comments", "verification_comments", "assisted_or_above", "observed_or_above"):
            row[k] = e.get(k, "")
        row["checklist_source_status"] = e.get("checklist_status", "")
        row["encounter_date"] = e.get("date_97fae7", "")
        row["source_entry_json"] = json_text(sorted({json_text(r): r for r in originals.get(json_text(identity), [])}.values(), key=json_text))
        append(row)
    for m in run.matches:
        # Use the source preceptor name, not the union-find root, whose aliases can
        # expand in a later export. A corrected name/period requires reconciliation.
        identity = [m["record_id"], m["rotation_start"], name_key(m["faculty_name"]), m["kind"], m["eval_period_start_date"], m["eval_period_end_date"]]
        row = base(m["record_id"], "6", identity, "m")
        for k in ("faculty_name", "faculty_email", "faculty_external_id", "faculty_username", "manual_evaluations", "type_of_association", "classification", "student_activity1", "eval_period_start_date", "eval_period_end_date"):
            row[k] = m[k]
        received = any(e["record_id"] == m["record_id"] and e["kind"] == m["kind"] and e["faculty_key"] == m["faculty_key"] for e in run.evaluations)
        due = (dt(m["eval_period_end_date"]) + timedelta(days=run.settings.grace_days)).date().isoformat() <= run.settings.as_of
        row["match_status"] = "Received — no reminder" if received else "Pending" if due else "Not due"
        append(row)
    rows.sort(key=lambda r: (r["student_key"], int(r["record_type"]), r["record_id"]))
    batch = "b_" + digest(rows)[:24]
    for row in rows:
        row["batch_id"] = batch
    validate_redcap_rows(rows)
    return rows


def validate_redcap_rows(rows: list[dict]) -> None:
    specs = {s["name"]: s for s in field_specs()}
    if len(specs) != len(field_specs()):
        raise ValueError("Duplicate schema field name.")
    ids = set()
    summaries = {r["record_id"] for r in rows if r["record_type"] == "1"}
    for row in rows:
        if not re.fullmatch(r"[secm]_[a-f0-9]{32}", row.get("record_id", "")) or row["record_id"] in ids:
            raise ValueError("Invalid or duplicate generated record ID.")
        ids.add(row["record_id"])
        if row.get("record_type") not in RECORD_TYPES or row.get("student_key") not in summaries:
            raise ValueError("A detail row is missing its student summary.")
        for k, value in row.items():
            if k not in specs:
                raise ValueError(f"Unknown export field: {k}")
            s, value = specs[k], text(value)
            if not value:
                continue
            if s["kind"] == "yesno" and value not in {"0", "1"}:
                raise ValueError(f"{k}: invalid yes/no code.")
            if s["validation"] in {"integer", "number"}:
                n = number(value)
                if n is None or s["validation"] == "integer" and not n.is_integer():
                    raise ValueError(f"{k}: invalid numeric value.")
            if s["validation"] == "date_ymd" and day(value) != value:
                raise ValueError(f"{k}: invalid date.")
            if s["validation"] == "datetime_seconds_ymd" and stamp(value) != value:
                raise ValueError(f"{k}: invalid timestamp.")
            if s["validation"] == "email" and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
                raise ValueError(f"Invalid email in {k}; correct the source email before importing.")


# ---------------------------------------------------------------------------
# Exact original REDCap layout. Student IDs + repeating instruments, not flat IDs.
# Numbering is append-only and stored separately from grades/reminder preferences.
# ---------------------------------------------------------------------------
LEGACY_EVAL_FILENAME = "redcap_oasis_eval_clerk_upload.csv"
LEGACY_EVAL_COLUMNS = [
    "record_id", "redcap_repeat_instrument", "redcap_repeat_instance", "student",
    "evaluator", "evaluator_email", "submit_date", "evaluation", "ct", "cp", "cr",
    "do", "kp", "prof", "cas_strengths", "cas_weaknesses", "iv", "tot", "epa_student",
    "epa_evaluator", "epa_evaluator_email", "epa_submit_date", "epa_evaluation",
    "epa_obh_score", "epaobh_weaknesses", "epa_obp_score", "epaobp_weaknesses",
    "epa_obho_score", "epaobho_weaknesses", "exclude", "reason",
]
REPEAT_COLUMNS = LEGACY_EVAL_COLUMNS[:3]
CAS_EXPORT_COLUMNS = LEGACY_EVAL_COLUMNS[3:18]
EPA_EXPORT_COLUMNS = LEGACY_EVAL_COLUMNS[18:29]
CHECKLIST_EXPORT_COLUMNS = REPEAT_COLUMNS + list(CHECKLIST_MAP) + [
    "student_activity", "assisted_or_above", "observed_or_above",
]
MATCH_EXPORT_COLUMNS = REPEAT_COLUMNS + [
    "faculty_name", "faculty_username", "faculty_external_id", "faculty_email",
    "type_of_association", "eval_period_start_date", "eval_period_end_date",
    "classification", "manual_evaluations", "student_activity1",
]
SUPPORTED_REPEAT_FORMS = {"oasis_eval", "epa", "checklist_entry", "preceptor_matching"}
NUMBERING_PURPOSE = "pediatric_clerkship_redcap_repeat_numbers_v1"
NUMBERING_PATH = "private_config/redcap_repeat_numbers.json.enc"


class NumberingError(ValueError):
    """Do not guess an existing repeat number when identity is ambiguous."""


def empty_number_registry() -> dict:
    return {"version": 1, "purpose": NUMBERING_PURPOSE, "entries": []}


def validate_number_registry(registry: Any) -> dict:
    if (not isinstance(registry, dict) or registry.get("version") != 1 or
            registry.get("purpose") != NUMBERING_PURPOSE or not isinstance(registry.get("entries"), list)):
        raise NumberingError("The saved REDCap numbering file has an unsupported format; it was not reset.")
    if len(registry["entries"]) > 50000:
        raise NumberingError("The REDCap numbering file exceeds this app's supported size.")
    slots, aliases = set(), set()
    for e in registry["entries"]:
        if not isinstance(e, dict) or set(e) != {"rid", "form", "n", "legacy", "content", "aliases", "seeded"}:
            raise NumberingError("Invalid REDCap numbering entry; restore a known-good encrypted backup.")
        if (not isinstance(e["rid"], str) or not re.fullmatch(r"[A-Za-z0-9_-]+", e["rid"]) or
                e["form"] not in SUPPORTED_REPEAT_FORMS or type(e["n"]) is not int or e["n"] < 1 or
                not isinstance(e["aliases"], list) or type(e["seeded"]) is not bool):
            raise NumberingError("Invalid student ID, instrument or repeat number in the saved configuration.")
        if any(not isinstance(h, str) or not re.fullmatch(r"[0-9a-f]{64}", h)
               for h in [e["legacy"], e["content"], *e["aliases"]]):
            raise NumberingError("Invalid source-identity hash in the saved numbering configuration.")
        slot = (e["rid"], e["form"], e["n"])
        if slot in slots or len(set(e["aliases"])) != len(e["aliases"]):
            raise NumberingError("Conflicting repeating-instance numbers in the saved configuration.")
        slots.add(slot)
        for alias in e["aliases"]:
            if alias in aliases:
                raise NumberingError("One source item is assigned to more than one repeat number.")
            aliases.add(alias)
    return registry


def legacy_datetime(value: Any) -> str:
    """Match the reference CSV's date-only-at-23:59 convention; preserve source time in audit."""
    parsed = dt(value)
    if not parsed:
        raise NumberingError("A submitted assessment has no usable submission date.")
    return parsed.strftime("%m-%d-%Y") + " 23:59"


def repeat_legacy_key(row: dict) -> str:
    rid, form = norm(get(row, "record_id")), get(row, "redcap_repeat_instrument")
    if not rid or rid == "all students":
        raise NumberingError("A source item is missing its actual Student External ID.")
    if form in {"oasis_eval", "epa"}:
        prefix = "epa_" if form == "epa" else ""
        email = norm(get(row, prefix + "evaluator_email"))
        person = email or name_key(get(row, prefix + "evaluator"))
        kind = kind_of(get(row, prefix + "evaluation"))
        submitted = day(get(row, prefix + "submit_date"))
        if not person or not submitted or kind not in ({"cas"} if form == "oasis_eval" else {"hp", "handoff"}):
            raise NumberingError("An evaluation is missing its preceptor, assessment type or submission date.")
        identity = [rid, form, person, kind, submitted]
    elif form == "checklist_entry":
        entered, item = stamp(get(row, "time_entered")), key(get(row, "item"))
        if not entered or not item:
            raise NumberingError("A checklist entry needs its item and Time entered for stable numbering.")
        identity = [rid, form, entered, item]
    elif form == "preceptor_matching":
        person = norm(get(row, "faculty_email")) or name_key(get(row, "faculty_name"))
        kind = kind_of(get(row, "manual_evaluations"))
        first, last = day(get(row, "eval_period_start_date")), day(get(row, "eval_period_end_date"))
        if not person or not kind or not first or not last:
            raise NumberingError("A preceptor match needs a preceptor, assessment type and period dates.")
        identity = [rid, form, person, kind, first, last]
    else:
        raise NumberingError("An unrecognized repeating instrument cannot be numbered.")
    return digest(identity)


def repeat_content_hash(row: dict) -> str:
    form = get(row, "redcap_repeat_instrument")
    if form == "oasis_eval":
        fields = [*DOMAIN_KEYS, "prof", "iv", "tot", "cas_strengths", "cas_weaknesses"]
    elif form == "epa":
        fields = EPA_EXPORT_COLUMNS[5:]
    else:
        fields = (CHECKLIST_EXPORT_COLUMNS if form == "checklist_entry" else MATCH_EXPORT_COLUMNS)[3:]
    values = []
    for f in fields:
        v = get(row, f)
        if f in {*DOMAIN_KEYS, "prof", "iv", "tot", "epa_obh_score", "epa_obp_score", "epa_obho_score"}:
            v = number(v) if number(v) is not None else ""
        values.append(v)
    return digest([form, values])


def registry_from_reference(rows: list[dict], existing: dict | None = None) -> dict:
    """Read existing numbers once, storing hashes/IDs only, not comments or scores."""
    registry = json.loads(json.dumps(validate_number_registry(existing or empty_number_registry())))
    occupied = {(e["rid"], e["form"], e["n"]): e for e in registry["entries"]}
    for row in rows:
        form = get(row, "redcap_repeat_instrument")
        if not form:
            continue
        if form not in SUPPORTED_REPEAT_FORMS:
            continue
        if get(row, "redcap_event_name"):
            raise NumberingError("This exporter expects a classic, non-longitudinal REDCap project.")
        raw_n = get(row, "redcap_repeat_instance")
        if not re.fullmatch(r"[1-9][0-9]*", raw_n):
            raise NumberingError("The reference has an invalid repeating-instance number.")
        rid = norm(get(row, "record_id"))
        e = {"rid": rid, "form": form, "n": int(raw_n), "legacy": repeat_legacy_key(row),
             "content": repeat_content_hash(row), "aliases": [], "seeded": True}
        slot = (rid, form, int(raw_n))
        if slot in occupied:
            prior = occupied[slot]
            if prior["legacy"] != e["legacy"] or prior["content"] != e["content"]:
                raise NumberingError("Two reference rows disagree about the same repeating-instance number.")
            continue
        # A legacy identity may legitimately have >1 same-day entry; resolve by content, never arbitrary order.
        registry["entries"].append(e)
        occupied[slot] = e
    registry["entries"].sort(key=lambda e: (e["rid"], e["form"], e["n"]))
    return validate_number_registry(registry)


def legacy_candidates(run: Run) -> list[dict]:
    result = []
    for e in run.evaluations:
        if e["kind"] == "cas":
            row = {"record_id": e["record_id"], "redcap_repeat_instrument": "oasis_eval",
                   "student": e["student"], "evaluator": e["evaluator"], "evaluator_email": e["evaluator_email"],
                   "submit_date": legacy_datetime(e["submit_date"]), "evaluation": e["evaluation"]}
            for k in [*DOMAIN_KEYS, "prof", "cas_strengths", "cas_weaknesses", "iv", "tot"]:
                row[k] = e.get(k, "")
        else:
            row = {"record_id": e["record_id"], "redcap_repeat_instrument": "epa",
                   "epa_student": e["student"], "epa_evaluator": e["evaluator"],
                   "epa_evaluator_email": e["evaluator_email"], "epa_submit_date": legacy_datetime(e["submit_date"]),
                   "epa_evaluation": e["evaluation"]}
            for k in EPA_EXPORT_COLUMNS[5:]:
                row[k] = e.get(k, "")
        identity = [e["record_id"], e["rotation_start"], e["kind"],
                    ["form", e["form_record"]] if e["form_record"] else ["fallback", name_key(e["evaluator"]), norm(e["evaluator_email"]), e["submit_date"]]]
        result.append({"row": row, "alias": digest(identity), "has_form": bool(e["form_record"]),
                       "manual": bool(e["manual_excluded"]), "source_time": e["submit_date"]})
    for e in run.entries:
        row = {k: e.get(k, "") for k in CHECKLIST_EXPORT_COLUMNS if k not in REPEAT_COLUMNS}
        row.update(record_id=e["record_id"], redcap_repeat_instrument="checklist_entry")
        identity = [e["record_id"], e["rotation_start"], "checklist", stamp(e["time_entered"]), key(e["item"])]
        result.append({"row": row, "alias": digest(identity), "has_form": False, "manual": False, "source_time": e["time_entered"]})
    for e in run.matches:
        row = {k: e.get(k, "") for k in MATCH_EXPORT_COLUMNS if k not in REPEAT_COLUMNS}
        row.update(record_id=e["record_id"], redcap_repeat_instrument="preceptor_matching")
        result.append({"row": row, "alias": digest([e["rotation_start"], repeat_legacy_key(row)]),
                       "has_form": False, "manual": False, "source_time": e["eval_period_start_date"]})
    for c in result:
        c["legacy"] = repeat_legacy_key(c["row"])
        c["content"] = repeat_content_hash(c["row"])
    return result


def evaluation_exclusion_reasons(run: Run) -> dict[str, str]:
    """Readable parent-level reasons for active exclusions affecting this run.

    Match with the SAME student/preceptor/form/date logic used for grading. Do
    not copy a student's rule to another student or rotation. Keep every distinct
    applicable rule/reason, including overlapping pair-wide and form-specific
    rules. Nothing from the faculty-reminder archive is included.
    """
    roster = {norm(s["record_id"]): s for s in run.roster}
    blocks: dict[str, list[str]] = {rid: [] for rid in roster}
    by_student: dict[str, list[dict]] = defaultdict(list)
    for e in run.evaluations:
        if e["kind"] == "cas":
            by_student[norm(e["record_id"])].append(e)
    for rule in normalize_rules(run.settings.exclusions):
        rid = rule["record_id"]
        if not rule["active"] or rid not in roster:
            continue
        student = roster[rid]
        if rule["rotation_start"] and rule["rotation_start"] != student["start_date"]:
            continue
        matched = [e for e in by_student[rid] if exclusion_matches(rule, e)]
        if not matched:
            continue  # an inactive, stale, or other-rotation form was not excluded in this run
        student_name = student.get("student_name") or rule["student_name"] or rid
        preceptor = rule["evaluator"] or matched[0].get("evaluator") or rule["evaluator_email"]
        forms = sorted({text(e.get("form_record")) for e in matched if text(e.get("form_record"))})
        missing_ids = sorted({text(e.get("submit_date")) for e in matched if not text(e.get("form_record"))})
        if rule["form_record"]:
            form_text = rule["form_record"]
        elif rule["submit_date"]:
            form_text = (", ".join(forms) or "Form Record unavailable") + " (submitted " + rule["submit_date"] + ")"
        else:
            form_text = "All clinical evaluations for this student–preceptor pair"
            if forms:
                form_text += "; matched Form Records: " + ", ".join(forms)
        if missing_ids and not rule["form_record"] and not rule["submit_date"]:
            form_text += "; Form Record unavailable for submission(s): " + ", ".join(missing_ids)
        block = (f"Student: {student_name} (record_id: {rid})\n"
                 f"Preceptor: {preceptor}\n"
                 f"Form: {form_text}\n"
                 f"Reason: {rule['reason']}")
        blocks[rid].append(block)
    return {rid: "\n\n".join(items) for rid, items in blocks.items()}


def plan_legacy_exports(run: Run, registry: dict) -> dict:
    """Pure plan. Callers must persist the returned registry before handing out numbered imports."""
    if run.messages.blocked or not run.roster or not all(daily_readiness(run).values()):
        raise NumberingError("Resolve file/rotation coverage checks before creating the REDCap import.")
    reg = json.loads(json.dumps(validate_number_registry(registry)))
    entries = reg["entries"]
    by_alias = {a: e for e in entries for a in e["aliases"]}
    by_legacy = defaultdict(list)
    maximum = defaultdict(int)
    for e in entries:
        by_legacy[e["legacy"]].append(e)
        maximum[(e["rid"], e["form"])] = max(maximum[(e["rid"], e["form"])], e["n"])
    candidates = legacy_candidates(run)
    incoming_counts = Counter(c["legacy"] for c in candidates)
    seen_sources, handled, rendered, omitted_manual = {}, set(), [], 0
    warnings = []
    # Exact known aliases first; new entries then chronological so newly created projects start predictably at 1.
    candidates.sort(key=lambda c: (c["alias"] not in by_alias, c["row"]["record_id"], c["row"]["redcap_repeat_instrument"], c["source_time"], c["alias"]))
    for c in candidates:
        row = dict(c["row"])
        rid, form = row["record_id"], row["redcap_repeat_instrument"]
        if c["alias"] in seen_sources:
            if seen_sources[c["alias"]] != c["content"]:
                raise NumberingError("Conflicting source rows have the same identity; correct the source rather than overwrite one.")
            continue
        seen_sources[c["alias"]] = c["content"]
        found = by_alias.get(c["alias"])
        if found and (found["rid"], found["form"]) != (rid, form):
            raise NumberingError("A saved source identity belongs to a different student or instrument.")
        if found is None:
            unbound = [e for e in by_legacy[c["legacy"]] if not e["aliases"] and (e["rid"], e["form"], e["n"]) not in handled]
            if len(unbound) == 1 and incoming_counts[c["legacy"]] == 1:
                found = unbound[0]
            elif unbound:
                same_content = [e for e in unbound if e["content"] == c["content"]]
                if len(same_content) == 1:
                    found = same_content[0]
                else:
                    raise NumberingError(f"{rid}/{form}: multiple same-day entries cannot be matched to your original numbers unambiguously. Reconcile this pair; no number was guessed.")
            # Known different OASIS Form Records on the same day are distinct submissions.
            elif by_legacy[c["legacy"]] and not c["has_form"]:
                bound = by_legacy[c["legacy"]]
                if len(bound) == 1 and (bound[0]["rid"], bound[0]["form"], bound[0]["n"]) not in handled:
                    found = bound[0]
                else:
                    raise NumberingError(f"{rid}/{form}: a source without a Form Record is ambiguous against saved entries.")
        if found is None:
            maximum[(rid, form)] += 1
            found = {"rid": rid, "form": form, "n": maximum[(rid, form)], "legacy": c["legacy"],
                     "content": c["content"], "aliases": [], "seeded": False}
            entries.append(found)
            by_legacy[c["legacy"]].append(found)
        slot = (rid, form, found["n"])
        if slot in handled:
            raise NumberingError("Two incoming source items would overwrite the same repeat number.")
        handled.add(slot)
        if c["alias"] not in found["aliases"]:
            found["aliases"].append(c["alias"])
        by_alias[c["alias"]] = found
        row["redcap_repeat_instance"] = str(found["n"])
        if c["manual"]:
            omitted_manual += 1
            if found["seeded"]:
                warnings.append(f"{rid}: a manually excluded evaluation was in the original reference. Omitting it does not delete it from REDCap; review its existing record separately.")
            continue
        rendered.append(row)
    # A selected student's previously numbered clinical assessments must be represented
    # before updating the parent's dropped-evaluation value or final clinical summary.
    selected = {r["record_id"] for r in run.roster}
    absent_cas = {e["rid"] for e in entries if e["rid"] in selected and e["form"] == "oasis_eval" and (e["rid"], e["form"], e["n"]) not in handled}
    if absent_cas:
        raise NumberingError("The OASIS upload is missing previously numbered clinical assessments for: " + ", ".join(sorted(absent_cas)) + ". Use the complete source export; no incomplete score/exclusion update was generated.")
    for form in ("epa", "checklist_entry", "preceptor_matching"):
        absent = sum(e["rid"] in selected and e["form"] == form and (e["rid"], e["form"], e["n"]) not in handled for e in entries)
        if absent:
            warnings.append(f"{form}: {absent} previously numbered entries are absent from this run. An import will not delete them; check completeness or reconcile deleted/corrected source items.")
    rendered.sort(key=lambda r: (r["record_id"], r["redcap_repeat_instrument"], int(r["redcap_repeat_instance"])))
    # Parent rows combine the automatic-drop description with manual-exclusion
    # reasons. A separate, narrowly scoped update also carries intentional blanks
    # when the last rule/reason is removed. Repeating assessment rows stay intact.
    reasons = evaluation_exclusion_reasons(run)
    exclusions = [{"record_id": s["record_id"], "exclude": s["exclude"],
                   "reason": reasons.get(norm(s["record_id"]), "")} for s in run.reports["scores"]]
    evaluation_rows = [r for r in rendered if r["redcap_repeat_instrument"] in {"oasis_eval", "epa"}]
    evaluation_rows += [dict(r) for r in exclusions if r["exclude"] or r["reason"]]
    if len(set(LEGACY_EVAL_COLUMNS)) != 31 or LEGACY_EVAL_COLUMNS[-2:] != ["exclude", "reason"]:
        raise NumberingError("The evaluation export must retain its original 30 columns plus reason.")
    files = {
        LEGACY_EVAL_FILENAME: csv_bytes(evaluation_rows, LEGACY_EVAL_COLUMNS),
        "redcap_checklist_entries.csv": csv_bytes([r for r in rendered if r["redcap_repeat_instrument"] == "checklist_entry"], CHECKLIST_EXPORT_COLUMNS),
        "redcap_preceptor_matching.csv": csv_bytes([r for r in rendered if r["redcap_repeat_instrument"] == "preceptor_matching"], MATCH_EXPORT_COLUMNS),
        "redcap_exclusion_updates.csv": csv_bytes(exclusions, ["record_id", "exclude", "reason"]),
    }
    summary_columns = ["record_id", "start_date", "end_date", "name", "email"]
    summary_keys = ["cas_submissions", "scorable_evaluations", "manual_exclusions", "unscorable_submissions", "score_before_drop", "clinical_score_375", *(k + "_mean_after_drop" for k in DOMAIN_KEYS), "dropped_evaluator", "dropped_form_record", "professionalism_review", "individual_domain_below_3", "domain_mean_below_3"]
    summary_columns += ["cst_" + k for k in summary_keys]
    summaries = []
    for s in run.reports["scores"]:
        row = {"record_id": s["record_id"], "start_date": s["start_date"], "end_date": s["end_date"], "name": s["student_name"], "email": s["email"]}
        for k in summary_keys:
            value = s.get(k, "")
            row["cst_" + k] = str(int(value)) if isinstance(value, bool) else value
        summaries.append(row)
    files["redcap_clinical_summary.csv"] = csv_bytes(summaries, summary_columns)
    # Authoritative form-specific updates avoid broad blank clearing across forms.
    files["redcap_clinical_form_updates.csv"] = csv_bytes([r for r in rendered if r["redcap_repeat_instrument"] == "oasis_eval"], REPEAT_COLUMNS + CAS_EXPORT_COLUMNS)
    files["redcap_epa_form_updates.csv"] = csv_bytes([r for r in rendered if r["redcap_repeat_instrument"] == "epa"], REPEAT_COLUMNS + EPA_EXPORT_COLUMNS)
    reg["entries"].sort(key=lambda e: (e["rid"], e["form"], e["n"]))
    validate_number_registry(reg)
    return {"registry": reg, "files": files, "evaluation_rows": len(evaluation_rows), "manual_omitted": omitted_manual, "warnings": list(dict.fromkeys(warnings))}


REDCAP_HELP = """ORIGINAL REDCAP EVALUATION LAYOUT

MAIN FILE
redcap_oasis_eval_clerk_upload.csv retains the original 30 columns in their original
order and appends one new column: reason (31 columns total).
Student External ID is record_id. Clinical assessments repeat in oasis_eval.
Observed H&P AND handoff assessments repeat in epa, sharing that instrument's
number sequence for each student. The parent exclude and reason fields share a
separate non-repeating row under the student's actual record_id. reason contains
Student, Preceptor, Form and Reason for each active rule applied to this run.
Multiple rules are separated by a blank line inside the same CSV cell. Inactive,
removed and nonmatching rules are not included; the automatic drop stays in exclude.
Add a Notes Box field named reason to a NON-repeating instrument in REDCap. Do
not put it on oasis_eval or epa. No data dictionary replacement is needed.
Raw N/A domain scores stay blank; iv contains the mean-imputation value, and
tot is the per-evaluation score out of 375. Source timestamps remain in the audit;
the import dates follow the reference's MM-DD-YYYY 23:59 format.

NORMAL USE
Use a classic project with oasis_eval and epa configured as repeating instruments.
Do not import into the previous flat-record project without rebuilding its schema.
Use REDCap's Data Import Tool; preserve supplied record IDs, choose the M-D-Y date
format where requested, and review the comparison table. For the mixed original
31-column CSV, keep Overwrite with blank values = NO unless you have verified
exactly how blank clearing affects each form in your project.

CONTROLLED CLEARING / CORRECTIONS
redcap_exclusion_updates.csv contains only record_id, exclude and reason, including
blank exclude values when no automatic drop applies and blank reason values when
no manual rule applies to the current source. After removing the last applicable
rule, import this companion separately with blank overwriting enabled to clear the
old REDCap reason. It does not include scores, narratives or other fields. Review
both managed fields before importing; do not clear manual director notes saved in
them. A download does not change REDCap; changes occur only on confirmed import.
To replace changed scores or clear a domain back to N/A, use the form-specific
redcap_clinical_form_updates.csv or redcap_epa_form_updates.csv (only that form's
fields) and review before allowing blanks to overwrite. These are full source
values for included submissions; do not use to overwrite intentional manual edits.

ONE-TIME NUMBERING
Initialize the encrypted number registry from the LAST SUCCESSFULLY IMPORTED
original-format CSV, or from a current REDCap export containing its repeat numbers.
For a genuinely empty project, explicitly initialize empty numbering instead.
For existing checklist/matching instruments, include their last imports/exports
in the same one-time setup. Do not claim they are empty when they contain records.
The uploaded CSV itself, grades and narratives are NOT stored in GitHub. The
registry contains encrypted student IDs, reserved numbers and identity hashes.
After initialization, Create files reuses and extends this encrypted registry.
A downloaded file is NOT recorded as an import or as a received evaluation.
Never share one numbering registry between differently numbered REDCap projects.
An outside writer can cause collisions: the registry is not a live REDCap export.
Reconcile such changes before importing. Keep a secure copy of the encrypted file
and key. Existing faculty reminder exclusions use a different encrypted file.

SEPARATE COMPANIONS
The exact original header has NO checklist or matching columns. Those imports
are in redcap_checklist_entries.csv and redcap_preceptor_matching.csv. Configure
checklist_entry and preceptor_matching as repeating forms before using them.
redcap_clinical_summary.csv provides pre/post-drop scores and domain means in
cst_* fields on a nonrepeating summary form; add these fields only as needed.
The supplied setup dictionaries are templates, not a replacement for an existing
project dictionary. The original evaluation CSV does not reveal REDCap formulas,
branching logic, calculated-text actions, or the whole student-review form.
Those are not recreated or inferred. No NBME/final-grade formula is changed.

EXCLUSIONS AND HISTORY
Manually excluded clinical evaluations are omitted from the primary import, as
in the owner's original Python script. They remain in the audit and still count
as received for reminder suppression. Auto-dropped evaluations stay in the file;
the parent exclude text identifies the drop. Omitting a row never deletes an
already imported REDCap entry. Remove selected exclusions deletes rules from the
app list, not source assessments or existing REDCap records. Other overlapping
active rules may still exclude a submission; the app warns about this. Rebuild
with Create files after changes; a restored assessment can still be the automatic
lowest-score drop. Save evaluation exclusions to retain edits and removals in a
separate encrypted GitHub file. Wait for the green SAVED confirmation before
closing; a JSON backup remains an optional recovery copy.
Full current source exports are needed for scores.
The download includes the selected rotation(s), not all historic student records.
The number registry preserves older numbers, not older grades or narratives.
Importing CSVs does not delete source items missing from a later run.
"""


def exclusion_backup(rules: list[dict]) -> bytes:
    return json.dumps({"version": 1, "rules": normalize_rules(rules)}, ensure_ascii=False, indent=2).encode("utf-8")


def settings_from_options(options: dict, filenames: list[str], rules: list[dict]) -> Settings:
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    as_of = options.get("as_of") or today
    return Settings(as_of=as_of, data_through=options.get("data_through") or inferred_export_date(filenames[3], as_of),
                    targets={k: int(options.get(k + "_target", v)) for k, v in {"cas": 8, "hp": 2, "handoff": 1}.items()},
                    fallback_rotation_days=int(options.get("rotation_days", 26)), grace_days=int(options.get("grace_days", 0)),
                    confirm_coverage=bool(options.get("confirm_coverage", False)), exclusions=normalize_rules(rules),
                    survey_urls={k: options.get(k + "_url", SURVEYS[k]) for k in KINDS},
                    legacy_partial_links=bool(options.get("legacy_partial_links", True)))


def build_bundle(source_bytes: list[bytes], filenames: list[str], settings: Settings, registry: dict | None = None) -> dict:
    run, raw = process_uploads(source_bytes, filenames, settings)
    result = {"run": run, "reports": output_files(run), "redcap": b"", "redcap_error": "",
              "redcap_count": 0, "batch_id": "", "redcap_files": {}, "redcap_warnings": []}
    if run.messages.blocked:
        return result
    if registry is not None:
        try:
            attach_legacy_plan(result, plan_legacy_exports(run, registry))
        except ValueError as exc:
            result["redcap_error"] = str(exc)
    else:
        result["redcap_error"] = "Complete the one-time REDCap numbering setup; the four reminder outputs are unaffected."
    return result


def authenticate(st: Any) -> bool:
    password = app_secret(st, "APP_PASSWORD")
    trusted = norm(app_secret(st, "TRUSTED_HOST_AUTH", "false")) == "true"
    if not password:
        if trusted:
            return True
        st.info("One-time owner setup: add APP_PASSWORD in Streamlit Secrets. Use an approved host restricted to authorized clerkship staff.")
        return False
    if len(password) < 16 or password.startswith("CHANGE_ME"):
        st.error("The owner must set APP_PASSWORD to a unique password of at least 16 characters.")
        return False
    target = digest(password)
    if st.session_state.get("auth_digest") == target:
        return True
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submit = st.form_submit_button("Open tracker")
    if submit:
        if hmac.compare_digest(entered.encode(), password.encode()):
            st.session_state["auth_digest"] = target
            st.rerun()
        else:
            st.error("Incorrect password.")
    return False


def replace_evaluation_exclusion(rules: list[dict], rule_id: str, changes: dict,
                                  *, confirm_target_change: bool = False) -> list[dict]:
    """Edit one rule, replacing its old identity rather than leaving a duplicate.

    Reasons/status/display-name changes keep the same matching identity. Changing
    a student, preceptor or scope requires explicit confirmation. Other rules and
    the original source/provenance are preserved. Nothing is written here.
    """
    current = normalize_rules(rules)
    existing = next((r for r in current if r["rule_id"] == rule_id), None)
    if existing is None:
        raise ValueError("The selected exclusion no longer exists. Reopen the current list before editing.")
    allowed = {"record_id", "student_name", "evaluator", "evaluator_email", "form_record",
               "rotation_start", "submit_date", "reason", "active"}
    if not isinstance(changes, dict) or set(changes) - allowed:
        raise ValueError("Only the displayed exclusion details can be edited; no changes were saved.")
    updated = {**existing, **changes}
    if not text(updated.get("reason")):
        raise ValueError("Enter a reason for this exclusion before saving.")
    # normalize_rules performs the student, preceptor, date and status checks.
    replacement = normalize_rules([updated])[0]
    if replacement["rule_id"] != existing["rule_id"] and not confirm_target_change:
        raise ValueError("Confirm the change to the student, preceptor, form or scope before saving. "
                         "No confirmation is needed for a reason or Active change alone.")
    if any(r["rule_id"] == replacement["rule_id"] and r["rule_id"] != rule_id for r in current):
        raise ValueError("An exclusion already exists for that student, preceptor and scope. "
                         "Edit that exclusion instead; no duplicate was created.")
    return normalize_rules([r for r in current if r["rule_id"] != rule_id] + [replacement])


def begin_evaluation_edit(st: Any, rule_id: str) -> None:
    """Freeze the rule and save baseline while a form is open (including reruns).

    The editor keys use a monotonically increasing session revision, not edited
    values. A fresh GitHub read cannot silently replace an open draft or erase
    the baseline needed for same-rule conflict detection.
    """
    if st.session_state.get("evaluation_edit_context"):
        raise ValueError("Save or cancel the current edit before opening another exclusion.")
    rules = normalize_rules(st.session_state.get("rules", []))
    rule = next((r for r in rules if r["rule_id"] == rule_id), None)
    if rule is None:
        raise ValueError("Choose an exclusion from the current saved list.")
    revision = int(st.session_state.get("evaluation_edit_revision", 0)) + 1
    st.session_state["evaluation_edit_revision"] = revision
    base = coerce_saved_evaluation_rules(st.session_state.get("evaluation_saved_base", SavedEvaluationRules()))
    st.session_state["evaluation_edit_context"] = {
        "rule_id": rule_id, "rule": dict(rule), "rules": rules, "revision": revision,
        "base": SavedEvaluationRules(normalize_rules(base.rules), base.sha, base.exists, base.saved_at),
        "pending_before": bool(st.session_state.get("evaluation_draft_pending")),
        "error_before": st.session_state.get("evaluation_save_error", ""),
    }
    st.session_state.pop("evaluation_rules_feedback", None)
    st.session_state.pop("result", None)


def cancel_evaluation_edit(st: Any) -> None:
    """Discard the local form only. A previously accepted remote save is not undone."""
    context = st.session_state.pop("evaluation_edit_context", None)
    if not context:
        return
    st.session_state["rules"] = normalize_rules(context["rules"])
    st.session_state["evaluation_saved_base"] = coerce_saved_evaluation_rules(context["base"])
    if context["pending_before"]:
        st.session_state["evaluation_draft_pending"] = True
    else:
        st.session_state.pop("evaluation_draft_pending", None)
    if context["error_before"]:
        st.session_state["evaluation_save_error"] = context["error_before"]
    else:
        st.session_state.pop("evaluation_save_error", None)
    st.session_state.pop("result", None)
    st.session_state["evaluation_edit_notice"] = "Editing closed. No additional change was sent to GitHub; the saved list is rechecked."


def save_selected_evaluation_edit(st: Any, state: dict, changes: dict,
                                  *, confirm_target_change: bool = False) -> bool:
    context = st.session_state.get("evaluation_edit_context")
    if not context:
        raise ValueError("Open an existing exclusion before saving changes.")
    desired = replace_evaluation_exclusion(context["rules"], context["rule_id"], changes,
                                           confirm_target_change=confirm_target_change)
    # Use the snapshot when editing began, never the latest read as our baseline.
    st.session_state["evaluation_saved_base"] = coerce_saved_evaluation_rules(context["base"])
    if save_evaluation_rule_action(st, state, desired):
        st.session_state["evaluation_rules_feedback"] = (
            "SAVED — the existing exclusion was updated in encrypted GitHub storage and read back successfully. "
            "No duplicate exclusion was added. Click Create files again to update scores and the REDCap reason column.")
        return True
    return False


def render_selected_evaluation_editor(st: Any, state: dict) -> None:
    context = st.session_state["evaluation_edit_context"]
    rule = context["rule"]
    prefix = "evaluation_edit_" + str(context["revision"]) + "_"
    can_save = state.get("archive") is not None and not state.get("error")
    st.markdown("### Edit this exclusion")
    st.write(evaluation_exclusion_label(rule))
    overlaps = overlapping_exclusions_after_removal(context["rules"], [context["rule_id"]])
    if overlaps:
        st.warning("Other active exclusions overlap this rule. Deactivating it alone may not restore the evaluation: "
                   + " ; ".join(evaluation_exclusion_label(r) for r in overlaps))
    st.caption("The current values are filled in below. Edit the reason or Active status, then click "
               "Save changes to this exclusion. The original saved rule is replaced, not duplicated. "
               "You do not need to upload an OASIS file to edit a saved rule.")
    # Streamlit batches values until Submit; no mutation/key regeneration per keystroke.
    with st.form(prefix + "form", clear_on_submit=False, enter_to_submit=False):
        reason = st.text_area("Reason for this exclusion", value=rule["reason"], height=180,
                              key=prefix + "reason")
        active = st.checkbox("Exclusion active", value=rule["active"], key=prefix + "active")
        with st.expander("Student, preceptor and form details — change only if correcting the target", expanded=False):
            student_name = st.text_input("Student name", value=rule["student_name"], key=prefix + "student_name")
            record_id = st.text_input("Student record_id", value=rule["record_id"], key=prefix + "record_id")
            evaluator = st.text_input("Preceptor name", value=rule["evaluator"], key=prefix + "evaluator")
            evaluator_email = st.text_input("Preceptor email (optional)", value=rule["evaluator_email"], key=prefix + "evaluator_email")
            form_record = st.text_input("OASIS Form Record (blank = no form-number filter)", value=rule["form_record"], key=prefix + "form_record")
            rotation_start = st.text_input("Rotation start filter (optional, YYYY-MM-DD)", value=rule["rotation_start"], key=prefix + "rotation_start")
            submit_date = st.text_input("Submission timestamp filter (optional, YYYY-MM-DD HH:MM:SS)", value=rule["submit_date"], key=prefix + "submit_date")
            st.caption("All populated filters must match. Leaving both Form Record and submission timestamp blank "
                       "applies the rule to all clinical evaluations for the pair, within any supplied rotation filter. "
                       "When correcting the form, check its student, preceptor and date filters too.")
            confirm_target = st.checkbox("I confirm any change to the student, preceptor, form or scope.", key=prefix + "confirm_target")
        save = st.form_submit_button("Save changes to this exclusion", key="evaluation_edit_save", type="primary", disabled=not can_save)
        cancel = st.form_submit_button("Cancel editing", key="evaluation_edit_cancel")
    if cancel:
        cancel_evaluation_edit(st)
        st.rerun()
    if save:
        try:
            changes = dict(reason=reason, active=active, student_name=student_name, record_id=record_id,
                           evaluator=evaluator, evaluator_email=evaluator_email, form_record=form_record,
                           rotation_start=rotation_start, submit_date=submit_date)
            if save_selected_evaluation_edit(st, state, changes, confirm_target_change=confirm_target):
                st.rerun()
            else:
                st.error("NOT SAVED / NOT VERIFIED. " + st.session_state.get("evaluation_save_error", "Please retry saving."))
                st.caption("Your submitted edit is still available below as a backup. Retry this same Save button after the connection recovers.")
        except ValueError as exc:
            st.error(str(exc))
    st.caption("Changes typed into this form are not stored until you click Save changes to this exclusion. "
               "Scores and REDCap downloads pause while an edit is open. Cancel editing to keep the saved version.")


def director_controls(st: Any, oasis_blob: bytes | None, evaluation_state: dict | None = None) -> None:
    """Grading-rule edits are explicit encrypted saves, not session-only changes."""
    if evaluation_state is None:
        evaluation_state = evaluation_archive_state(st)
        sync_evaluation_rules(st, evaluation_state)
    with st.expander("Evaluation exclusions — save for future visits", expanded=False):
        st.caption("These rules affect clinical scoring, not received-evaluation counts or reminders. "
                   "Saved rules load automatically on future visits, even without source files. "
                   "Faculty reminder exclusions remain separate.")
        feedback = st.session_state.pop("evaluation_rules_feedback", "")
        if feedback:
            st.success(feedback)
        can_save = evaluation_state.get("archive") is not None and not evaluation_state.get("error")
        rules = normalize_rules(st.session_state["rules"])
        editable = [{"rule_id": r["rule_id"], "active": r["active"], "student": r["student_name"] or r["record_id"],
                     "preceptor": r["evaluator"] or r["evaluator_email"], "form": r["form_record"] or "All for this pair",
                     "reason": r["reason"]} for r in rules]
        if editable:
            # Overview only: editing is done in a stable, prefilled form below.
            st.dataframe(editable, hide_index=True, width="stretch",
                         column_config={"rule_id": None, "active": "Active", "student": "Student",
                                        "preceptor": "Preceptor", "form": "Form / scope", "reason": "Reason"})
        else:
            st.info("No evaluation exclusions are configured. All received assessments remain eligible for the existing grading rules.")
        notice = st.session_state.pop("evaluation_edit_notice", "")
        if notice:
            st.info(notice)
        render_evaluation_save_status(st, evaluation_state)
        editing = bool(st.session_state.get("evaluation_edit_context"))
        if editing:
            render_selected_evaluation_editor(st, evaluation_state)
            st.download_button("Download current exclusions backup", exclusion_backup(st.session_state["rules"]),
                               "exclusions_backup.json", "application/json", on_click="ignore")
            st.caption("Backup contains the current list and any submitted failed-save draft, not text still unsubmitted in the editor. "
                       "Finish or cancel editing before adding, removing, restoring, or refreshing other rules.")
            return
        else:
            if rules:
                st.markdown("### Edit an existing exclusion")
                by_id = {r["rule_id"]: r for r in rules}
                selected = st.selectbox("Saved exclusion to edit", list(by_id),
                                         format_func=lambda k: evaluation_exclusion_label(by_id[k]),
                                         key="evaluation_edit_choice_" + digest(list(by_id))[:12])
                if st.button("Edit selected exclusion", key="evaluation_edit_open"):
                    begin_evaluation_edit(st, selected)
                    st.rerun()
                st.caption("Use Edit selected exclusion to change its full reason, Active status, or matching details. "
                           "You do not need to remove and add the rule again.")
            if st.button("Save evaluation exclusions", key="apply_rules", type="primary", width="stretch", disabled=not can_save):
                save_evaluation_rule_action(st, evaluation_state, rules)
                st.rerun()
            st.caption("Save evaluation exclusions saves the entire current list after a backup restore or first-time setup. "
                       "For an existing rule, use Edit selected exclusion. Adding or removing below also saves immediately.")
            if rules:
                by_id = {r["rule_id"]: r for r in rules}
                removals = st.multiselect("Evaluation exclusions to remove", list(by_id),
                                          format_func=lambda k: evaluation_exclusion_label(by_id[k]),
                                          key="evaluation_rules_remove_" + digest(rules)[:12])
                overlaps = overlapping_exclusions_after_removal(rules, removals)
                if overlaps:
                    st.warning("Another active exclusion still overlaps the selected rule(s). To restore those evaluations, also remove or deactivate the overlapping rule(s): "
                               + " ; ".join(evaluation_exclusion_label(r) for r in overlaps))
                if st.button("Remove selected exclusions and save", key="remove_evaluation_rules", disabled=not removals or not can_save):
                    try:
                        desired = remove_evaluation_exclusions(rules, removals)
                        save_evaluation_rule_action(st, evaluation_state, desired)
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
            if oasis_blob:
                try:
                    picks = exclusion_choices(read_csv_bytes(oasis_blob, "OASIS ME", Messages()))
                    if picks:
                        rid = st.selectbox("Student", sorted(picks, key=lambda k: picks[k]["name"]), format_func=lambda k: picks[k]["name"], key="rule_student")
                        preceptors = picks[rid]["preceptors"]
                        pk = st.selectbox("Preceptor", list(preceptors), format_func=lambda k: display_name(preceptors[k]["evaluator"]), key="rule_preceptor_" + rid)
                        preceptor = preceptors[pk]
                        fk = st.selectbox("Exclude", ["all"] + list(preceptor["forms"]),
                                          format_func=lambda k: "All clinical evaluations for this student–preceptor pair" if k == "all" else f"Form {preceptor['forms'][k]['form_record'] or 'no ID'} — {preceptor['forms'][k]['submit_date']}", key="rule_form_" + rid + "_" + pk)
                        reason = st.text_input("Reason for this exclusion", key="rule_reason")
                        if st.button("Add exclusion and save", key="add_rule", disabled=not can_save):
                            if not reason.strip():
                                st.error("Enter the reason for the exclusion.")
                            else:
                                scope = {} if fk == "all" else preceptor["forms"][fk]
                                added = {"record_id": rid, "student_name": picks[rid]["name"], "evaluator": preceptor["evaluator"],
                                         "evaluator_email": preceptor["email"], "reason": reason, "active": True, "origin": "Director", **scope}
                                save_evaluation_rule_action(st, evaluation_state, merge_rule_updates(rules, [added]))
                                st.rerun()
                except ValueError as exc:
                    st.warning(str(exc))
            # Restoring is intentionally a reviewable draft, not a silent archive replacement.
            backup = st.file_uploader("Restore saved exclusions (optional JSON)", type=["json"], key="restore_rules")
            if st.button("Restore backup for review", disabled=backup is None, key="restore_rules_button"):
                try:
                    st.session_state["rules"] = normalize_rules(json.loads(backup.getvalue().decode("utf-8-sig")))
                    st.session_state["evaluation_draft_pending"] = True
                    st.session_state.pop("result", None)
                    st.session_state.pop("evaluation_save_error", None)
                    st.rerun()
                except (ValueError, UnicodeError) as exc:
                    st.error("Could not read the backup: " + str(exc))
            st.download_button("Download exclusions backup", exclusion_backup(st.session_state["rules"]), "exclusions_backup.json", "application/json", on_click="ignore")
            st.caption("Backup includes the current list, including any unsaved edits. Restore an older backup here once, review it, "
                       "then click Save evaluation exclusions. Routine saved changes no longer require restoring a backup each visit. "
                       "The REDCap reason field is an export, not the app's storage.")
            if st.button("Refresh saved evaluation list", key="evaluation_refresh"):
                st.rerun()  # sync_evaluation_rules preserves local unsaved changes.
            base = st.session_state.get("evaluation_saved_base")
            if evaluation_state["saved"].exists and st.session_state["rules"] != evaluation_state["saved"].rules:
                discard = st.checkbox("Discard my unsaved edits and load the saved GitHub list.", key="evaluation_confirm_discard")
                if st.button("Discard unsaved edits and reload", key="evaluation_discard", disabled=not discard or not evaluation_state.get("ready")):
                    accept_saved_evaluation_rules(st, evaluation_state["saved"])
                    st.rerun()
    with st.expander("Dates, requirements and reminder links", expanded=False):
        current = st.session_state["options"]
        with st.form("options_form"):
            today = datetime.now(ZoneInfo("America/New_York")).date()
            as_of = st.date_input("Reminder date", value=dt(current.get("as_of") or today).date())
            through = st.text_input("OASIS export date — leave blank to use filename (YYYY-MM-DD)", value=current.get("data_through", ""))
            targets = {}
            for k, v in {"cas": 8, "hp": 2, "handoff": 1}.items():
                targets[k + "_target"] = st.number_input(LABELS[k] + " target", min_value=1, max_value=100, value=int(current.get(k + "_target", v)))
            duration = st.number_input("Rotation length if the end date is not supplied (inclusive calendar days)", min_value=1, max_value=365, value=int(current.get("rotation_days", 26)))
            grace = st.number_input("Preceptor reminder grace days", min_value=0, max_value=365, value=int(current.get("grace_days", 0)))
            confirm = st.checkbox("These exports cover the selected rotation even if an entire source has zero rows for that rotation", value=bool(current.get("confirm_coverage", False)))
            urls = {k + "_url": st.text_input(LABELS[k] + " reminder link", value=current.get(k + "_url", SURVEYS[k])) for k in KINDS}
            partial = st.checkbox("Include original partially prefilled survey links", value=bool(current.get("legacy_partial_links", True)))
            if st.form_submit_button("Apply settings"):
                if through and (day(through) != through or through > as_of.isoformat()):
                    st.error("Enter an export date in YYYY-MM-DD format, no later than the reminder date.")
                elif any(u and not u.startswith("https://") for u in urls.values()):
                    st.error("Reminder links must start with https://, or be blank.")
                else:
                    st.session_state["options"] = dict(as_of=as_of.isoformat(), data_through=through, rotation_days=int(duration), grace_days=int(grace),
                                                       confirm_coverage=confirm, legacy_partial_links=partial, **targets, **urls)
                    st.session_state.pop("result", None)
                    st.rerun()


# ---------------------------------------------------------------------------
# Faculty REMINDER exclusions — distinct from student/evaluation grading rules.
# Only encrypted names are written to GitHub. Source files, grading exclusions,
# grades and the REDCap import are never sent to this archive.
# ---------------------------------------------------------------------------
REMINDER_ARCHIVE_PATH = "private_config/faculty_reminder_exclusions.json.enc"
REMINDER_ARCHIVE_PURPOSE = "pediatric_clerkship_faculty_reminder_exclusions"
FACULTY_REMINDER_FILES = ("preceptor_eval_reminders.csv", "observed_hp_reminders.csv")


class ReminderArchiveError(ValueError):
    """User-safe error: messages must never include tokens, keys or response bodies."""


@dataclass(frozen=True)
class ReminderArchiveConfig:
    repo: str
    github_token: str = field(repr=False)
    encryption_key: str = field(repr=False)
    branch: str = "main"

    @classmethod
    def from_mapping(cls, raw: Any) -> "ReminderArchiveConfig":
        if not hasattr(raw, "get"):
            raise ReminderArchiveError("The owner needs to add [reminder_archive] in Streamlit Secrets.")
        repo = text(raw.get("repo"))
        token, encryption_key = text(raw.get("github_token")), text(raw.get("encryption_key"))
        branch = text(raw.get("branch")) or "main"
        if not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repo) or repo.split("/")[-1] in {".", ".."}:
            raise ReminderArchiveError("Set reminder_archive.repo to the GitHub owner/repository name, not a URL.")
        if not token or not encryption_key or any(v.startswith(("PASTE_", "YOUR_", "CHANGE_ME")) for v in (token, encryption_key)):
            raise ReminderArchiveError("Add the GitHub token and a generated encryption key to [reminder_archive] in Secrets.")
        if len(branch) > 200 or any(ord(c) < 32 for c in branch):
            raise ReminderArchiveError("The reminder archive branch setting is invalid.")
        cfg = cls(repo=repo, github_token=token, encryption_key=encryption_key, branch=branch)
        reminder_cipher(cfg.encryption_key)  # Validate before making any network call.
        return cfg


def reminder_cipher(encryption_key: str) -> Any:
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        raise ReminderArchiveError("Update requirements.txt to install the cryptography package, then restart the app.") from None
    try:
        return Fernet(encryption_key.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        raise ReminderArchiveError("The archive encryption key is invalid. Use a Fernet-generated key, not the app password.") from None


def reminder_name_key(value: str) -> str:
    # These are faculty_name values from the FINAL mailing CSVs. Match case and
    # spacing, not fuzzy names, email addresses, credentials or unrelated people.
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip().casefold()


def normalize_reminder_names(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 5000:
        raise ReminderArchiveError("The faculty reminder list has an invalid format or size; nothing was replaced.")
    names = {}
    for item in value:
        if not isinstance(item, str) or len(item) > 300 or any(ord(c) < 32 for c in item):
            raise ReminderArchiveError("The faculty reminder list contains an invalid name; nothing was replaced.")
        label = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", item)).strip()
        if not label or label.casefold() in {"*", "all", "all faculty", "all preceptors", "all students"}:
            raise ReminderArchiveError("Select actual faculty names; a blanket exclusion is not supported.")
        names.setdefault(reminder_name_key(label), label)
    return sorted(names.values(), key=reminder_name_key)


def encrypt_reminder_names(names: list[str], encryption_key: str) -> bytes:
    payload = {"version": 1, "purpose": REMINDER_ARCHIVE_PURPOSE,
               "faculty_names": normalize_reminder_names(names)}
    plaintext = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return reminder_cipher(encryption_key).encrypt(plaintext)


def decrypt_reminder_names(ciphertext: bytes, encryption_key: str) -> list[str]:
    from cryptography.fernet import InvalidToken
    if not isinstance(ciphertext, bytes) or not ciphertext or len(ciphertext) > 900_000:
        raise ReminderArchiveError("The encrypted reminder file is empty or invalid. Restore the saved file; no empty list was assumed.")
    try:
        payload = json.loads(reminder_cipher(encryption_key).decrypt(ciphertext).decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        raise ReminderArchiveError("The reminder list could not be decrypted. Check the original encryption key; the archive was not reset.") from None
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1
            or payload.get("purpose") != REMINDER_ARCHIVE_PURPOSE or "faculty_names" not in payload):
        raise ReminderArchiveError("This encrypted file is not a supported faculty reminder list. Nothing was replaced.")
    return normalize_reminder_names(payload["faculty_names"])


@dataclass(frozen=True)
class SavedReminderNames:
    names: tuple[str, ...] = ()
    sha: str = ""
    exists: bool = False


class GitHubReminderArchive:
    """Small encrypted Contents-API store; only explicit actions write to GitHub.

    Read failures never become an empty allow-list. Saves merge an add/remove
    operation with a fresh read and use SHA compare-and-swap; concurrent sessions
    do not overwrite each other's unrelated changes. No plaintext fallback exists.
    """
    def __init__(self, config: ReminderArchiveConfig, transport: Any = None):
        import requests
        self.config = config
        self.transport = transport if transport is not None else requests
        reminder_cipher(config.encryption_key)
        self.repo_url = "https://api.github.com/repos/" + config.repo
        self.file_url = self.repo_url + "/contents/" + REMINDER_ARCHIVE_PATH

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        import requests
        try:
            return self.transport.request(method, url, headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + self.config.github_token,
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "pediatric-clerkship-reminder-archive",
                "Cache-Control": "no-cache",
            }, timeout=(5, 20), allow_redirects=False, **kwargs)
        except requests.RequestException:
            raise ReminderArchiveError("GitHub could not be reached. No save was confirmed. Refresh the saved list before downloading preceptor reminders.") from None

    @staticmethod
    def _json(response: Any) -> Any:
        try:
            return response.json()
        except (ValueError, TypeError):
            raise ReminderArchiveError("GitHub returned an unreadable response. No exclusion list was assumed.") from None

    @staticmethod
    def _check(response: Any, expected: tuple[int, ...] = (200,)) -> None:
        if response.status_code not in expected:
            status = int(response.status_code)
            raise ReminderArchiveError(f"GitHub archive access failed (HTTP {status}). Check the repository, branch and token permissions. No save was confirmed.")

    def load(self) -> SavedReminderNames:
        # Confirm the selected repository and branch exist before interpreting a
        # missing file as first-time setup. GitHub also returns 404 for bad access.
        repo = self._request("GET", self.repo_url)
        self._check(repo)
        metadata = self._json(repo)
        if not isinstance(metadata, dict) or not metadata.get("full_name"):
            raise ReminderArchiveError("GitHub returned invalid repository metadata; no exclusion list was assumed.")
        branch = self._request("GET", self.repo_url + "/branches/" + quote(self.config.branch, safe=""))
        self._check(branch)
        response = self._request("GET", self.file_url, params={"ref": self.config.branch})
        if response.status_code == 404:
            return SavedReminderNames()  # NOT ready: explicit first-time initialization is required.
        self._check(response)
        obj = self._json(response)
        if (not isinstance(obj, dict) or obj.get("type") != "file" or obj.get("encoding") != "base64"
                or not isinstance(obj.get("sha"), str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", obj["sha"])
                or not isinstance(obj.get("content"), str) or len(obj["content"]) > 1_300_000):
            raise ReminderArchiveError("The saved reminder file has an unexpected GitHub format. It was not treated as an empty list.")
        try:
            ciphertext = base64.b64decode(re.sub(r"\s+", "", obj["content"]), validate=True)
        except (ValueError, TypeError):
            raise ReminderArchiveError("The encrypted reminder file is damaged. Nothing was replaced.") from None
        return SavedReminderNames(tuple(decrypt_reminder_names(ciphertext, self.config.encryption_key)), obj["sha"], True)

    def _write(self, names: list[str], previous: SavedReminderNames) -> Any:
        # The *only* content written is base64 wrapping authenticated ciphertext.
        # Neither names nor counts are placed in filenames or commit messages.
        ciphertext = encrypt_reminder_names(names, self.config.encryption_key)
        payload = {"message": "Update encrypted faculty reminder preferences",
                   "content": base64.b64encode(ciphertext).decode("ascii"), "branch": self.config.branch}
        if previous.exists:
            payload["sha"] = previous.sha
        return self._request("PUT", self.file_url, json=payload)

    def initialize(self) -> SavedReminderNames:
        previous = self.load()
        if previous.exists:
            return previous  # Never erase an existing archive through initialization.
        response = self._write([], previous)
        if response.status_code in (409, 422):
            current = self.load()
            if current.exists:
                return current
        self._check(response, (200, 201))
        current = self.load()
        if not current.exists:
            raise ReminderArchiveError("The new archive could not be verified. Do not assume it was saved.")
        return current

    def change(self, *, add: list[str] | None = None, remove: list[str] | None = None) -> SavedReminderNames:
        additions, removals = normalize_reminder_names(add or []), normalize_reminder_names(remove or [])
        added_keys, removed_keys = {reminder_name_key(n) for n in additions}, {reminder_name_key(n) for n in removals}
        if added_keys & removed_keys:
            raise ReminderArchiveError("A faculty name cannot be excluded and restored in the same change.")
        for _ in range(3):
            previous = self.load()
            if not previous.exists:
                raise ReminderArchiveError("The saved reminder list is missing. Restore it or complete first-time setup; no replacement was created.")
            merged = {reminder_name_key(n): n for n in previous.names if reminder_name_key(n) not in removed_keys}
            for name in additions:
                merged.setdefault(reminder_name_key(name), name)
            names = normalize_reminder_names(list(merged.values()))
            if names == list(previous.names):
                return previous  # Idempotent action: no redundant commit.
            response = self._write(names, previous)
            if response.status_code in (409, 422):
                continue  # Re-read and apply this operation, never replay a stale full list.
            self._check(response, (200, 201))
            verified = self.load()
            current_keys = {reminder_name_key(n) for n in verified.names}
            if not verified.exists or not added_keys <= current_keys or removed_keys & current_keys:
                raise ReminderArchiveError("The saved list changed again before verification. Refresh it and check the selected names.")
            return verified
        raise ReminderArchiveError("Another update or a branch protection rule prevented saving. Refresh the list and try again; no successful save was confirmed.")


def read_faculty_mailing_rows(data: bytes, filename: str) -> list[dict]:
    if filename not in FACULTY_REMINDER_FILES:
        raise ReminderArchiveError("Only the two faculty reminder files may be filtered.")
    try:
        reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""))
        if reader.fieldnames != REMINDER_COLUMNS[filename]:
            raise ReminderArchiveError("A preceptor reminder layout changed unexpectedly. Download is paused to protect the original format.")
        rows = list(reader)
        if any(None in r or any(v is None for v in r.values()) for r in rows):
            raise ReminderArchiveError("A preceptor reminder row is malformed. Download is paused.")
        return rows
    except (csv.Error, UnicodeError):
        raise ReminderArchiveError("A preceptor reminder file could not be read. Download is paused.") from None


def faculty_reminder_choices(reports: dict[str, bytes]) -> list[str]:
    # Derive choices from the same faculty_name columns the administrator sees.
    names = [r["faculty_name"] for filename in FACULTY_REMINDER_FILES if filename in reports
             for r in read_faculty_mailing_rows(reports[filename], filename) if r["faculty_name"].strip()]
    return normalize_reminder_names(names)


def apply_faculty_reminder_exclusions(reports: dict[str, bytes], names: list[str]) -> tuple[dict[str, bytes], dict[str, int]]:
    """Filter final mailing rows only. Never mutate Run, scores or REDCap data.

    The original columns, values, ordering, encoding and CRLF line endings remain.
    With no exclusions (or no matching name), the original bytes are returned.
    """
    blocked = {reminder_name_key(n) for n in normalize_reminder_names(names)}
    result, removed = dict(reports), {filename: 0 for filename in FACULTY_REMINDER_FILES}
    for filename in FACULTY_REMINDER_FILES:
        if filename not in reports:
            raise ReminderArchiveError("A required preceptor reminder file is missing. Download is paused.")
        original = read_faculty_mailing_rows(reports[filename], filename)
        kept = [r for r in original if reminder_name_key(r["faculty_name"]) not in blocked]
        removed[filename] = len(original) - len(kept)
        if removed[filename]:
            out = io.StringIO(newline="")
            writer = csv.DictWriter(out, fieldnames=REMINDER_COLUMNS[filename], lineterminator="\r\n")
            writer.writeheader()
            writer.writerows(kept)  # Already Flow-safe: do not sanitize a second time.
            result[filename] = out.getvalue().encode("utf-8")
    return result, removed


def reminder_archive_state(st: Any) -> dict:
    """Fresh per-rerun read; never share decrypted names in a global Streamlit cache."""
    state = {"ready": False, "archive": None, "saved": SavedReminderNames(), "error": "", "configured": False}
    try:
        raw = st.secrets.get("reminder_archive", None)
    except (FileNotFoundError, KeyError):
        raw = None
    if raw is None:
        state["error"] = "One-time setup is needed to save faculty reminder exclusions in GitHub."
        return state
    state["configured"] = True
    try:
        archive = GitHubReminderArchive(ReminderArchiveConfig.from_mapping(raw))
        state["archive"] = archive
        state["saved"] = archive.load()
        state["ready"] = state["saved"].exists
    except ReminderArchiveError as exc:
        state["error"] = str(exc)
    return state


def render_faculty_reminder_controls(st: Any, result: dict | None, state: dict) -> None:
    with st.expander("Faculty reminder exclusions — saved for future runs", expanded=False):
        st.write("Choose a faculty name to stop reminders in BOTH observed_hp_reminders.csv and preceptor_eval_reminders.csv, for all students and rotations. Restore the name here whenever reminders should resume.")
        st.caption("This is separate from grading exclusions. Scores, received evaluations, student requirement counts and the REDCap import are not changed. Name matching ignores case and extra spaces; two people with the same displayed name share the rule.")
        flash = st.session_state.pop("faculty_reminder_flash", "")
        if flash:
            st.success(flash)
        if state["error"]:
            st.warning(state["error"])
        if not state["configured"]:
            st.caption("Your owner configures this once in Streamlit Settings → Secrets. No names or credentials need to be put into the Python source.")
            st.code('[reminder_archive]\nrepo = "YOUR_GITHUB_USERNAME/YOUR_REPOSITORY"\nbranch = "main"\ngithub_token = "PASTE_GITHUB_TOKEN"\nencryption_key = "PASTE_GENERATED_KEY"', language="toml")
        if state["archive"] is None:
            if st.button("Generate one-time encryption key", key="faculty_reminder_generate_key"):
                try:
                    from cryptography.fernet import Fernet
                    st.session_state["faculty_reminder_setup_key"] = Fernet.generate_key().decode("ascii")
                except ImportError:
                    st.error("Install the updated requirements.txt first.")
            generated = st.session_state.get("faculty_reminder_setup_key")
            if generated:
                st.code('encryption_key = "' + generated + '"', language="toml")
                st.warning("Copy this into Streamlit Secrets and keep a secure backup. Never commit this key to GitHub. Do not replace an existing archive's key with a new one.")
            st.caption("Use a fine-grained GitHub token limited to the selected repository, with Contents: Read and write. Existing APP_PASSWORD and grading-exclusion Secrets stay unchanged. See REMINDER_EXCLUSIONS_SETUP.md.")
            return
        if state["error"]:
            st.button("Retry loading saved list", key="faculty_reminder_retry")
            return
        saved, archive = state["saved"], state["archive"]
        if not saved.exists:
            st.warning("No encrypted reminder list was found on this branch. Create an empty list only for FIRST-TIME setup. If a list existed before, restore that file and its original key instead.")
            first_time = st.checkbox("This is first-time setup; there is no prior reminder exclusion list to preserve.", key="faculty_reminder_first_time")
            if st.button("Initialize encrypted reminder list", key="faculty_reminder_initialize", disabled=not first_time):
                try:
                    archive.initialize()
                except ReminderArchiveError as exc:
                    state["ready"], state["error"] = False, str(exc)
                    st.error(str(exc))
                else:
                    st.session_state.pop("faculty_reminder_setup_key", None)
                    st.session_state["faculty_reminder_flash"] = "Encrypted storage is ready. Faculty selections will be saved across visits."
                    st.rerun()
            return
        st.caption(f"{len(saved.names)} faculty name(s) currently excluded. Loaded from encrypted GitHub storage.")
        st.button("Refresh saved list", key="faculty_reminder_refresh")
        try:
            candidates = faculty_reminder_choices(result["reports"]) if result and not result["run"].messages.blocked else []
        except ReminderArchiveError as exc:
            st.error(str(exc))
            candidates = []
        excluded = {reminder_name_key(n) for n in saved.names}
        available = [n for n in candidates if reminder_name_key(n) not in excluded]
        widget_version = saved.sha[:12]
        if available:
            additions = st.multiselect("Faculty to exclude from future reminders", available, key="faculty_reminder_add_" + widget_version)
            if st.button("Exclude selected faculty and save", key="faculty_reminder_save_add", disabled=not additions):
                try:
                    archive.change(add=additions)
                except ReminderArchiveError as exc:
                    state["ready"], state["error"] = False, str(exc)
                    st.error(str(exc))
                else:
                    st.session_state["faculty_reminder_flash"] = "Saved in encrypted GitHub storage. The two preceptor reminder downloads now use the updated list."
                    st.rerun()
        else:
            st.info("Create files to list faculty from the two reminder outputs. Names already excluded stay available below even without uploaded files.")
        if saved.names:
            st.dataframe([{"Excluded faculty": n} for n in saved.names], hide_index=True, width="stretch")
            removals = st.multiselect("Faculty to restore to reminders", list(saved.names), key="faculty_reminder_remove_" + widget_version)
            if st.button("Remove selected exclusions and save", key="faculty_reminder_save_remove", disabled=not removals):
                try:
                    archive.change(remove=removals)
                except ReminderArchiveError as exc:
                    state["ready"], state["error"] = False, str(exc)
                    st.error(str(exc))
                else:
                    st.session_state["faculty_reminder_flash"] = "Saved. Restored faculty may appear again when an assessment is still outstanding."
                    st.rerun()
        st.caption("Restore removes a name from the active list; older encrypted versions remain in GitHub history. Download new CSVs after changes—files already downloaded or emailed cannot be recalled.")


# Grading exclusions use their OWN file; never reuse faculty names or number storage.
EVALUATION_ARCHIVE_PATH = "private_config/evaluation_exclusions.json.enc"
EVALUATION_ARCHIVE_PURPOSE = "pediatric-clerkship-evaluation-exclusions"


class EvaluationArchiveError(ReminderArchiveError):
    """A grading save/load is not confirmed. Never replace errors with defaults."""


@dataclass
class SavedEvaluationRules:
    rules: list[dict] = field(default_factory=list)
    sha: str = ""
    exists: bool = False
    saved_at: str = ""


def coerce_saved_evaluation_rules(value: Any) -> SavedEvaluationRules:
    """Rehydrate by values, not class identity, across Streamlit script reruns.

    A dataclass declared in this single-file app is recreated on every rerun.
    An instance saved in Session State therefore need not pass isinstance with
    the current definition. Validate the data and rebuild a current instance;
    do not mistake that ordinary lifecycle for lost/corrupt archive storage.
    """
    names = ("rules", "sha", "exists", "saved_at")
    try:
        values = {k: value[k] for k in names} if isinstance(value, dict) else {k: getattr(value, k) for k in names}
        if (not isinstance(values["rules"], list) or not isinstance(values["sha"], str)
                or type(values["exists"]) is not bool or not isinstance(values["saved_at"], str)):
            raise ValueError
        rules = normalize_rules(values["rules"])
        if values["exists"]:
            if not re.fullmatch(r"[a-fA-F0-9]{40,64}", values["sha"]):
                raise ValueError
            _evaluation_payload(rules, values["saved_at"])
        elif values["sha"] or values["saved_at"] or rules:
            raise ValueError
        return SavedEvaluationRules(rules, values["sha"], values["exists"], values["saved_at"])
    except (AttributeError, KeyError, TypeError, ValueError):
        raise EvaluationArchiveError("The evaluation save snapshot is invalid. Back up the current list and reload it; "
                                     "the encrypted archive was not reset.") from None


def _evaluation_payload(rules: Any, saved_at: str) -> dict:
    if not isinstance(rules, list) or len(rules) > 2000:
        raise EvaluationArchiveError("Invalid evaluation exclusion list or more than 2,000 rules; nothing was replaced.")
    try:
        cleaned = normalize_rules(rules)
        when = datetime.fromisoformat(saved_at.replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError, OverflowError):
        raise EvaluationArchiveError("Evaluation exclusions or their saved timestamp are invalid; nothing was replaced.") from None
    payload = {"version": 1, "purpose": EVALUATION_ARCHIVE_PURPOSE,
               "saved_at": saved_at, "rules": cleaned}
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 550_000:
        raise EvaluationArchiveError("The evaluation exclusion archive is too large; shorten reasons before saving.")
    return payload


def encrypt_evaluation_rules(rules: list[dict], encryption_key: str, saved_at: str) -> bytes:
    payload = _evaluation_payload(rules, saved_at)
    return reminder_cipher(encryption_key).encrypt(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def decrypt_evaluation_rules(ciphertext: bytes, encryption_key: str) -> dict:
    from cryptography.fernet import InvalidToken
    if not isinstance(ciphertext, bytes) or not ciphertext or len(ciphertext) > 900_000:
        raise EvaluationArchiveError("The encrypted evaluation file is empty or invalid. It was not replaced with defaults.")
    try:
        payload = json.loads(reminder_cipher(encryption_key).decrypt(ciphertext).decode("utf-8"))
    except (InvalidToken, UnicodeError, ValueError, TypeError):
        raise EvaluationArchiveError("Saved evaluation exclusions could not be decrypted. Check the original key; nothing was reset.") from None
    if (not isinstance(payload, dict) or type(payload.get("version")) is not int or payload["version"] != 1
            or payload.get("purpose") != EVALUATION_ARCHIVE_PURPOSE or "rules" not in payload):
        raise EvaluationArchiveError("This file is not a supported evaluation exclusion archive. Nothing was reset.")
    return _evaluation_payload(payload["rules"], payload.get("saved_at", ""))


def merge_evaluation_save(base: list[dict], desired: list[dict], current: list[dict]) -> list[dict]:
    """Apply this session's changes only; conflict rather than lose same-rule edits.

    Rules absent from the current upload are preserved. Deletions are intentional
    changes, including deletion of the final rule. No legacy defaults are merged.
    """
    before = {r["rule_id"]: r for r in normalize_rules(base)}
    wanted = {r["rule_id"]: r for r in normalize_rules(desired)}
    remote = {r["rule_id"]: r for r in normalize_rules(current)}
    changed = {k for k in before.keys() | wanted.keys() if before.get(k) != wanted.get(k)}
    for k in changed:
        if remote.get(k) != before.get(k) and remote.get(k) != wanted.get(k):
            raise EvaluationArchiveError("Another session changed the same evaluation exclusion. Your edits are NOT SAVED. "
                                         "Download a backup of your edits, reload the saved list, and reapply the intended change.")
    for k in changed:
        if k in wanted:
            remote[k] = wanted[k]
        else:
            remote.pop(k, None)
    return normalize_rules(list(remote.values()))


class GitHubEvaluationArchive(GitHubReminderArchive):
    """Durable encrypted grading rules, separate from the two other archives.

    Fresh reads and SHA-conditional PUTs protect concurrent users. A successful
    PUT alone is not enough: rules are re-read and decrypted before confirming.
    """
    def __init__(self, config: ReminderArchiveConfig, transport: Any = None):
        super().__init__(config, transport)
        self.file_url = self.repo_url + "/contents/" + EVALUATION_ARCHIVE_PATH

    def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return super()._request(method, url, **kwargs)
        except ReminderArchiveError:
            raise EvaluationArchiveError("GitHub could not be reached. Evaluation exclusions were NOT confirmed saved. "
                                         "Your current edits remain in this session; download a backup before leaving.") from None

    def load_rules(self) -> SavedEvaluationRules:
        repo = self._request("GET", self.repo_url)
        self._check(repo)
        obj = self._json(repo)
        if not isinstance(obj, dict) or not obj.get("full_name"):
            raise EvaluationArchiveError("Invalid GitHub repository response; evaluation exclusions were not reset.")
        branch = self._request("GET", self.repo_url + "/branches/" + quote(self.config.branch, safe=""))
        self._check(branch)
        response = self._request("GET", self.file_url, params={"ref": self.config.branch})
        if response.status_code == 404:
            return SavedEvaluationRules()  # first save must be explicit; reads never write
        self._check(response)
        obj = self._json(response)
        if (not isinstance(obj, dict) or obj.get("type") != "file" or obj.get("encoding") != "base64"
                or not isinstance(obj.get("sha"), str) or not re.fullmatch(r"[a-fA-F0-9]{40,64}", obj["sha"])
                or not isinstance(obj.get("content"), str) or len(obj["content"]) > 1_300_000):
            raise EvaluationArchiveError("The saved evaluation file has an unexpected format. Nothing was reset.")
        try:
            ciphertext = base64.b64decode(re.sub(r"\s+", "", obj["content"]), validate=True)
        except (ValueError, TypeError):
            raise EvaluationArchiveError("The saved evaluation file is damaged. Nothing was reset.") from None
        payload = decrypt_evaluation_rules(ciphertext, self.config.encryption_key)
        return SavedEvaluationRules(payload["rules"], obj["sha"], True, payload["saved_at"])

    def save_rules(self, desired: list[dict], base: SavedEvaluationRules) -> SavedEvaluationRules:
        desired = normalize_rules(desired)
        changed = {r["rule_id"] for r in desired + base.rules}
        before = {r["rule_id"]: r for r in base.rules}
        wanted = {r["rule_id"]: r for r in desired}
        changed = {k for k in changed if before.get(k) != wanted.get(k)}
        for _ in range(3):
            current = self.load_rules()
            if base.exists and not current.exists:
                raise EvaluationArchiveError("The previously saved evaluation archive is missing. Restore it instead of starting over; no replacement was created.")
            if not base.exists and current.exists:
                # First-save race: do not silently restore defaults over an existing list.
                if current.rules == desired:
                    return current
                raise EvaluationArchiveError("An evaluation archive was created by another session. Download your draft backup and reload the saved list before saving.")
            merged = merge_evaluation_save(base.rules, desired, current.rules) if base.exists else desired
            if current.exists and current.rules == merged:
                return current  # fresh verified no-op, without a redundant commit
            saved_at = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
            ciphertext = encrypt_evaluation_rules(merged, self.config.encryption_key, saved_at)
            body = {"message": "Update encrypted evaluation exclusion settings", "branch": self.config.branch,
                    "content": base64.b64encode(ciphertext).decode("ascii")}
            if current.exists:
                body["sha"] = current.sha
            response = self._request("PUT", self.file_url, json=body)
            if response.status_code in (409, 422):
                continue
            self._check(response, (200, 201))
            verified = self.load_rules()
            final = {r["rule_id"]: r for r in verified.rules}
            # On first save verify the complete list; later verify each intended edit.
            if (not verified.exists or (not base.exists and verified.rules != merged)
                    or (base.exists and any(final.get(k) != wanted.get(k) for k in changed))):
                raise EvaluationArchiveError("The saved evaluation list changed before verification. Save is NOT confirmed; reload and review your edits.")
            return verified
        raise EvaluationArchiveError("GitHub did not accept the evaluation save after three attempts. No save was confirmed. "
                                     "Check branch protection or concurrent changes, keep your backup, and retry.")


def evaluation_archive_state(st: Any) -> dict:
    state = {"configured": False, "ready": False, "archive": None, "saved": SavedEvaluationRules(),
             "error": "", "config_id": ""}
    try:
        raw = st.secrets.get("reminder_archive", None)
    except (FileNotFoundError, KeyError):
        raw = None
    if raw is None:
        state["error"] = "Evaluation saving is not connected. Configure the existing [reminder_archive] GitHub settings in Streamlit Secrets."
        return state
    state["configured"] = True
    try:
        cfg = ReminderArchiveConfig.from_mapping(raw)
        state["config_id"] = digest([cfg.repo, cfg.branch, cfg.encryption_key])
        state["archive"] = GitHubEvaluationArchive(cfg)
        state["saved"] = state["archive"].load_rules()
        state["ready"] = state["saved"].exists
    except ReminderArchiveError as exc:
        state["error"] = str(exc)
    return state


def accept_saved_evaluation_rules(st: Any, saved: SavedEvaluationRules) -> None:
    st.session_state.pop("evaluation_edit_context", None)
    st.session_state["rules"] = normalize_rules(saved.rules)
    st.session_state["evaluation_saved_base"] = SavedEvaluationRules(normalize_rules(saved.rules), saved.sha, saved.exists, saved.saved_at)
    st.session_state.pop("evaluation_save_error", None)
    st.session_state.pop("evaluation_draft_pending", None)
    st.session_state.pop("evaluation_rules_unloaded", None)
    st.session_state.pop("result", None)


def sync_evaluation_rules(st: Any, state: dict) -> None:
    """Load before source uploads; a saved [] is authoritative, not 'missing'.

    Old EXCLUSIONS_JSON/defaults seed only a never-initialized list. Unsaved edits
    survive reruns and failed saves, but cannot masquerade as durable storage.
    """
    saved = state["saved"]
    previous_config = st.session_state.get("evaluation_config_id", "")
    if previous_config and state.get("config_id") and previous_config != state["config_id"]:
        state["error"], state["ready"] = ("The archive repository, branch or encryption key changed during this session. "
            "Download your exclusions backup, sign out, and reopen the app before using the new archive."), False
        return
    if state.get("config_id"):
        st.session_state["evaluation_config_id"] = state["config_id"]
    if "rules" not in st.session_state:
        if saved.exists and not state["error"]:
            accept_saved_evaluation_rules(st, saved)
            return
        # Do not substitute original exclusions after a configured archive read failure.
        if state["configured"] and state["error"]:
            st.session_state["rules"] = []
            st.session_state["evaluation_rules_unloaded"] = True
        else:
            configured = app_secret(st, "EXCLUSIONS_JSON")
            st.session_state["rules"] = normalize_rules(json.loads(configured) if configured else LEGACY_EXCLUSIONS)
        st.session_state["evaluation_saved_base"] = SavedEvaluationRules()
    elif "evaluation_saved_base" not in st.session_state:
        # Preserve a live pre-upgrade browser's full draft for the first explicit save.
        st.session_state["rules"] = normalize_rules(st.session_state["rules"])
        st.session_state["evaluation_saved_base"] = saved if saved.exists else SavedEvaluationRules()
    try:
        base = coerce_saved_evaluation_rules(st.session_state["evaluation_saved_base"])
        st.session_state["evaluation_saved_base"] = base
    except EvaluationArchiveError as exc:
        state["error"], state["ready"] = str(exc), False
        return
    if state["error"]:
        return
    if base.exists and not saved.exists:
        state["error"], state["ready"] = "The previously saved evaluation archive is missing. Restore its encrypted file; it was not reset.", False
        return
    if st.session_state.get("evaluation_edit_context"):
        return  # Keep the editor snapshot and its original save baseline until Save or Cancel.
    was_unloaded = st.session_state.pop("evaluation_rules_unloaded", False)
    pending = st.session_state.get("evaluation_draft_pending", False)
    if saved.exists:
        local = normalize_rules(st.session_state["rules"])
        if was_unloaded and pending:
            # A backup was explicitly restored during the outage. Keep that draft,
            # establish the actual remote baseline, and require Save after review.
            st.session_state["evaluation_saved_base"] = saved
        elif local == saved.rules or (local == base.rules and not pending) or (was_unloaded and not pending):
            if local != saved.rules or base.sha != saved.sha:
                accept_saved_evaluation_rules(st, saved)
    elif was_unloaded and not pending:
        configured = app_secret(st, "EXCLUSIONS_JSON")
        st.session_state["rules"] = normalize_rules(json.loads(configured) if configured else LEGACY_EXCLUSIONS)


def evaluation_rules_saved(st: Any, state: dict) -> bool:
    return bool(not st.session_state.get("evaluation_edit_context")
                and not state.get("error") and state.get("ready") and state["saved"].exists
                and normalize_rules(st.session_state.get("rules", [])) == state["saved"].rules
                and not st.session_state.get("evaluation_save_error"))


def saved_evaluation_time(value: str) -> str:
    if not value:
        return "unknown time"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %Y at %I:%M:%S %p %Z")


def render_evaluation_save_status(st: Any, state: dict) -> None:
    error = st.session_state.get("evaluation_save_error") or state.get("error")
    if error:
        st.error("EVALUATION EXCLUSIONS — NOT SAVED / NOT VERIFIED. " + error)
        st.caption("Your current list is kept only in this session until saving succeeds. Download a backup before closing the app. "
                   "Scores and REDCap downloads are paused; reminder files are not changed by this check.")
    elif st.session_state.get("evaluation_edit_context"):
        st.warning("EVALUATION EXCLUSIONS — EDITING, NOT SAVED. Finish with Save changes to this exclusion, "
                   "or Cancel editing to keep the saved version. Opening a rule does not change the encrypted archive.")
    elif evaluation_rules_saved(st, state):
        saved = state["saved"]
        count = sum(r["active"] for r in saved.rules)
        st.success(f"EVALUATION EXCLUSIONS — SAVED. {count} active / {len(saved.rules)} total rule(s). "
                   f"Saved {saved_evaluation_time(saved.saved_at)}. Loaded from encrypted GitHub storage and available on future visits.")
    elif state["saved"].exists:
        st.warning("EVALUATION EXCLUSIONS — UNSAVED CHANGES. Click Save evaluation exclusions under Show director tools. "
                   "Current edits will not be remembered until the green SAVED confirmation appears.")
    else:
        st.warning("EVALUATION EXCLUSIONS — NOT SAVED YET. Review the list under Show director tools, restore your previous backup if needed, "
                   "then click Save evaluation exclusions once. This creates a separate encrypted file for future visits.")


def save_evaluation_rule_action(st: Any, state: dict, desired: list[dict]) -> bool:
    """Preserve drafts on failure; report success only after encrypted readback."""
    desired = normalize_rules(desired)
    st.session_state["rules"] = desired
    st.session_state["evaluation_draft_pending"] = True
    st.session_state.pop("result", None)
    st.session_state.pop("evaluation_rules_feedback", None)
    try:
        if state.get("error") or state.get("archive") is None:
            raise EvaluationArchiveError(state.get("error") or "Encrypted evaluation saving is not configured.")
        base = coerce_saved_evaluation_rules(st.session_state.get("evaluation_saved_base", state["saved"]))
        verified = state["archive"].save_rules(desired, base)
        accept_saved_evaluation_rules(st, verified)
        state.update(saved=verified, ready=True, error="")
        st.session_state["evaluation_rules_feedback"] = (
            "SAVED to encrypted GitHub storage and verified. These evaluation exclusions will load on future visits. "
            "Click Create files again to update scores and the REDCap reason column.")
        return True
    except ReminderArchiveError as exc:
        st.session_state["evaluation_save_error"] = str(exc)
        return False


# Separate encrypted configuration from the faculty reminder suppression list.
@dataclass
class SavedNumberRegistry:
    registry: dict = field(default_factory=empty_number_registry)
    sha: str = ""
    exists: bool = False


class GitHubNumberingArchive(GitHubReminderArchive):
    """Encrypted append-only numbering, reusing credentials but NEVER the reminder file."""
    def __init__(self, config: ReminderArchiveConfig, transport: Any = None):
        super().__init__(config, transport)
        self.file_url = self.repo_url + "/contents/" + NUMBERING_PATH

    def load_numbers(self) -> SavedNumberRegistry:
        meta = self._request("GET", self.repo_url)
        self._check(meta)
        if not isinstance(self._json(meta), dict) or not self._json(meta).get("full_name"):
            raise NumberingError("Invalid GitHub repository response; numbering was not reset.")
        branch = self._request("GET", self.repo_url + "/branches/" + quote(self.config.branch, safe=""))
        self._check(branch)
        response = self._request("GET", self.file_url, params={"ref": self.config.branch})
        if response.status_code == 404:
            return SavedNumberRegistry()
        self._check(response)
        obj = self._json(response)
        if (not isinstance(obj, dict) or obj.get("type") != "file" or
                not isinstance(obj.get("sha"), str) or not re.fullmatch(r"[a-f0-9]{40,64}", obj["sha"])):
            raise NumberingError("The saved numbering file has an unexpected GitHub format.")
        sha = obj["sha"]
        # Contents responses omit Base64 for larger files. Fetch the immutable Git
        # blob by SHA so a full year's registry remains usable without stale reads.
        if obj.get("encoding") != "base64":
            blob = self._request("GET", self.repo_url + "/git/blobs/" + sha)
            self._check(blob)
            obj = self._json(blob)
            if not isinstance(obj, dict) or obj.get("sha") != sha:
                raise NumberingError("GitHub returned a different numbering snapshot; nothing was assumed.")
        if obj.get("encoding") != "base64" or not isinstance(obj.get("content"), str) or len(obj["content"]) > 12_000_000:
            raise NumberingError("The encrypted numbering file is unreadable or too large.")
        try:
            from cryptography.fernet import InvalidToken
            ciphertext = base64.b64decode(re.sub(r"\s+", "", obj["content"]), validate=True)
            registry = json.loads(reminder_cipher(self.config.encryption_key).decrypt(ciphertext))
        except (ValueError, TypeError, UnicodeError, InvalidToken):
            raise NumberingError("The numbering archive cannot be decrypted. Check the existing key; no empty replacement was created.") from None
        return SavedNumberRegistry(validate_number_registry(registry), sha, True)

    def _write_numbers(self, registry: dict, previous: SavedNumberRegistry) -> Any:
        raw = json.dumps(validate_number_registry(registry), separators=(",", ":"), sort_keys=True).encode()
        ciphertext = reminder_cipher(self.config.encryption_key).encrypt(raw)
        payload = {"message": "Update encrypted import numbering", "branch": self.config.branch,
                   "content": base64.b64encode(ciphertext).decode("ascii")}
        if previous.exists:
            payload["sha"] = previous.sha
        return self._request("PUT", self.file_url, json=payload)

    def initialize_numbers(self, registry: dict) -> SavedNumberRegistry:
        validate_number_registry(registry)
        previous = self.load_numbers()
        if previous.exists:
            raise NumberingError("Numbering is already initialized. It was not overwritten or reset.")
        response = self._write_numbers(registry, previous)
        self._check(response, (200, 201))
        saved = self.load_numbers()
        if not saved.exists or saved.registry != registry:
            raise NumberingError("The initialized numbering could not be verified; refresh before exporting.")
        return saved

    def prepare(self, run: Run) -> dict:
        """Called only by Create files after explicit first-time storage consent."""
        for _ in range(3):
            previous = self.load_numbers()
            if not previous.exists:
                raise NumberingError("Complete the one-time REDCap numbering setup below; no number was guessed.")
            plan = plan_legacy_exports(run, previous.registry)
            if plan["registry"] == previous.registry:
                return plan
            response = self._write_numbers(plan["registry"], previous)
            if response.status_code in (409, 422):
                continue
            self._check(response, (200, 201))
            saved = self.load_numbers()
            if not saved.exists:
                raise NumberingError("The saved numbering could not be verified. No numbered download is available.")
            # Concurrent writers can append other students; required assignments
            # must remain identical to those in this export.
            now = {(e["rid"], e["form"], a): e["n"] for e in saved.registry["entries"] for a in e["aliases"]}
            if any(now.get((e["rid"], e["form"], a)) != e["n"] for e in plan["registry"]["entries"] for a in e["aliases"]):
                raise NumberingError("The saved numbers changed before verification; rebuild the download.")
            return plan
        raise NumberingError("Concurrent changes or branch protection prevented saving the REDCap numbers; refresh and retry.")


def numbering_archive_state(st: Any) -> dict:
    state = {"ready": False, "archive": None, "saved": SavedNumberRegistry(), "error": ""}
    try:
        raw = st.secrets.get("reminder_archive", None)
        config = ReminderArchiveConfig.from_mapping(raw)
        state["archive"] = GitHubNumberingArchive(config)
        state["saved"] = state["archive"].load_numbers()
        state["ready"] = state["saved"].exists
    except (ReminderArchiveError, NumberingError, FileNotFoundError, KeyError) as exc:
        state["error"] = str(exc)
    return state


def render_numbering_setup(st: Any, state: dict) -> None:
    with st.expander("REDCap export setup — once, not every run", expanded=not state["ready"]):
        if state["ready"]:
            st.success("Your original repeating-record layout is ready. Saved numbers will be reused on future runs.")
            st.caption("The archive stores encrypted IDs, reserved numbers and identity hashes, not scores or narratives. It is separate from faculty reminder exclusions.")
            st.download_button("Download encrypted numbering backup", reminder_cipher(state["archive"].config.encryption_key).encrypt(json.dumps(state["saved"].registry).encode()), "redcap_repeat_numbers.json.enc", "application/octet-stream", on_click="ignore")
        elif state["error"]:
            st.info("REDCap exports need the existing GitHub archive connection. Reminders can still be created.")
            st.caption(state["error"])
        else:
            st.write("Use your last successfully imported original-format file once so evaluation 1 stays evaluation 1. This is not a recurring full REDCap export requirement.")
            choice = st.radio("Where will you import?", ["Choose one", "Existing numbered REDCap project", "New empty repeating-form project"], key="legacy_numbering_mode")
            refs = []
            if choice == "Existing numbered REDCap project":
                refs = st.file_uploader("Last imported evaluation CSV; include existing checklist/match imports too when applicable", type=["csv"], accept_multiple_files=True, key="legacy_numbering_refs") or []
                st.caption("Use the original redcap_oasis_eval_clerk_upload CSV, NOT the newer flat redcap_import.csv. A saved file must reflect actual project numbering; a download alone is not proof of import.")
            consent = st.checkbox("These files reflect the existing numbers; any unrepresented checklist/matching instruments are empty. Save encrypted numbering in GitHub and extend it when I choose REDCap export.", key="legacy_numbering_consent")
            enabled = consent and choice != "Choose one" and (choice == "New empty repeating-form project" or bool(refs))
            if st.button("Save REDCap numbering setup", disabled=not enabled, key="initialize_numbering"):
                try:
                    registry = empty_number_registry()
                    for ref in refs:
                        rows = read_csv_bytes(ref.getvalue(), "Numbering reference", Messages())
                        if not rows or not set(REPEAT_COLUMNS) <= set(rows[0]):
                            raise NumberingError("The reference must contain record_id, redcap_repeat_instrument and redcap_repeat_instance.")
                        registry = registry_from_reference(rows, registry)
                    if refs and not registry["entries"]:
                        raise NumberingError("The reference contains no supported repeating records; it was not treated as an empty project.")
                    state["archive"].initialize_numbers(registry)
                    st.session_state.pop("result", None)
                    st.success("Numbering saved. Your original CSV was not uploaded to GitHub. Choose REDCap export after this refresh.")
                    st.rerun()
                except (ValueError, ReminderArchiveError, csv.Error) as exc:
                    st.error(str(exc))
        st.download_button("Download original-layout field reference", csv_bytes([], LEGACY_EVAL_COLUMNS), "redcap_oasis_eval_clerk_upload_TEMPLATE.csv", "text/csv", on_click="ignore")
        st.download_button("Download REDCap import instructions", REDCAP_HELP.encode(), "REDCAP_SETUP.txt", "text/plain", on_click="ignore")
        st.caption("The primary CSV has your original 30 columns plus reason. Checklist entries, preceptor matches and clinical summary scores are separate companion files. Existing REDCap calculations are not replaced.")


def attach_legacy_plan(result: dict, plan: dict) -> None:
    result.update(redcap=plan["files"][LEGACY_EVAL_FILENAME], redcap_count=plan["evaluation_rows"],
                  redcap_files=plan["files"], redcap_warnings=plan["warnings"], redcap_error="",
                  registry=plan["registry"], manual_omitted=plan["manual_omitted"])


def render_downloads(st: Any, result: dict, reminder_state: dict | None = None, *, evaluation_ready: bool = True) -> None:
    if not evaluation_ready:
        result = {**result, "redcap": b"", "redcap_files": {}, "redcap_error":
                  "Evaluation exclusions are not saved and verified. Open Show director tools, save them, then click Create files."}
    run, reports = result["run"], result["reports"]
    reminder_state = reminder_state or {"ready": False, "saved": SavedReminderNames(), "error": "Saved reminder exclusions have not been loaded."}
    archive_ready = bool(reminder_state["ready"])
    removed = {name: 0 for name in FACULTY_REMINDER_FILES}
    if archive_ready:
        try:
            reports, removed = apply_faculty_reminder_exclusions(reports, list(reminder_state["saved"].names))
        except ReminderArchiveError as exc:
            archive_ready = False
            st.error(str(exc))
    if not archive_ready:
        # Do not hand an unfiltered CSV payload to even a disabled download widget.
        reports = {**reports, **{name: reminder_csv_bytes([], REMINDER_COLUMNS[name]) for name in FACULTY_REMINDER_FILES}}
    if run.messages.blocked:
        for msg in run.messages.rows:
            if msg["level"] == "ERROR":
                st.error(msg["detail"])
        st.download_button("Download file-check report", reports["validation_report.csv"], "validation_report.csv", "text/csv", on_click="ignore")
        return
    readiness = daily_readiness(run)
    coverage_ready = all(readiness.values())
    if not archive_ready:
        for name in FACULTY_REMINDER_FILES:
            readiness[name] = False
    st.subheader("Your files")
    st.caption(f"{len(run.roster)} students • Reminder date {run.settings.as_of} • OASIS reference date {run.settings.data_through or run.settings.as_of}")
    labels = ("Student checklist review", "Student evaluation / H&P / handoff reminders", "Preceptor clinical-evaluation reminders", "Preceptor observed H&P reminders")
    for name, label in zip(REMINDER_NAMES, labels):
        st.download_button(label + " — " + name, reports[name], name, "text/csv", disabled=not readiness[name], on_click="ignore", width="stretch", key="download_" + name)
    mail_files = {name: reports[name] for name in REMINDER_NAMES}
    st.download_button("Download all four reminder files", zipped(mail_files), "power_automate_reminders.zip", "application/zip", disabled=not all(readiness.values()), on_click="ignore", width="stretch")
    if not coverage_ready:
        st.warning("Some files do not cover the rotation dates in the schedule. Use the matching rotation exports; affected downloads are paused.")
    if not archive_ready:
        st.warning("The two preceptor reminder downloads are paused until the encrypted faculty exclusion list is loaded. Open Faculty reminder exclusions above. Student reminder and REDCap downloads are unaffected by this check.")
    elif any(removed.values()):
        st.caption(f"Saved faculty exclusions removed {removed['preceptor_eval_reminders.csv']} clinical-evaluation reminder row(s) and {removed['observed_hp_reminders.csv']} observed H&P reminder row(s).")
    st.divider()
    st.download_button("Download original-layout evaluation import", result["redcap"] or b"", LEGACY_EVAL_FILENAME, "text/csv", disabled=not bool(result["redcap"]), on_click="ignore", width="stretch", key="download_redcap")
    if result["redcap"]:
        st.caption(f"{result['redcap_count']} rows with your original 30 columns plus reason. Each student's active evaluation-exclusion details share one non-repeating row with their automatic-drop summary. Original student IDs and saved repeat numbers are preserved.")
        st.info("REDCap needs a Notes Box field named reason on a NON-repeating instrument. It contains student, preceptor, form and reason. Do not create it on the repeating oasis_eval or epa instrument.")
        with st.expander("Additional REDCap imports — checklists, matching and score summaries", expanded=False):
            for filename in ("redcap_checklist_entries.csv", "redcap_preceptor_matching.csv", "redcap_clinical_summary.csv", "redcap_exclusion_updates.csv", "redcap_clinical_form_updates.csv", "redcap_epa_form_updates.csv"):
                st.download_button(filename, result["redcap_files"][filename], filename, "text/csv", on_click="ignore", key="extra_" + filename)
            st.caption("After removing the last applicable evaluation exclusion, use redcap_exclusion_updates.csv to clear any old reason already in REDCap. That file contains only record_id, exclude and reason; review it before importing with blank overwriting enabled. Keep blank overwriting OFF for the mixed main export. Form-specific updates can clear corrected N/A scores.")
            st.download_button("Download all REDCap import files", zipped(result["redcap_files"]), "redcap_import_files.zip", "application/zip", on_click="ignore")
        for warning in result.get("redcap_warnings", []):
            st.warning(warning)
    elif result["redcap_error"]:
        st.warning("REDCap file: " + result["redcap_error"])
    notices = [r for r in run.messages.rows if r["level"] == "WARNING"]
    if notices:
        st.warning(f"{len(notices)} file-check notice(s). Review them before sending reminders.")
    with st.expander("Review counts and file checks", expanded=False):
        st.dataframe(run.source_stats, hide_index=True, width="stretch")
        if run.messages.rows:
            st.dataframe(run.messages.rows, hide_index=True, width="stretch")
        for name in REMINDER_NAMES:
            count = max(0, len(list(csv.reader(io.StringIO(reports[name].decode("utf-8-sig"))))) - 1)
            st.write(f"{name}: {count} reminder rows")
    if st.session_state.get("show_director", False) and evaluation_ready:
        with st.expander("Scores and detailed outputs", expanded=False):
            st.dataframe(run.reports["scores"], hide_index=True, width="stretch")
            details = {**reports, "exclusions_backup.json": exclusion_backup(run.settings.exclusions)}
            if not archive_ready:
                for filename in FACULTY_REMINDER_FILES:
                    details.pop(filename, None)
            if result["redcap"]:
                details.update(result["redcap_files"])
            st.download_button("Download director package", zipped(details), "director_outputs.zip", "application/zip", on_click="ignore")
            st.caption("Contains grades and narratives. Do not use this ZIP as a mailing-input folder.")


def legacy_main() -> None:
    import streamlit as st
    st.set_page_config(page_title="Pediatric Clerkship Tracker", page_icon="📋", layout="centered")
    st.title("Pediatric Clerkship Tracker")
    if not authenticate(st):
        st.stop()
    evaluation_state = evaluation_archive_state(st)
    try:
        sync_evaluation_rules(st, evaluation_state)
    except (ValueError, TypeError) as exc:
        st.error("The owner needs to correct the initial EXCLUSIONS_JSON in Secrets or restore the encrypted evaluation archive: " + str(exc))
        st.stop()
    st.session_state.setdefault("options", {})
    st.caption("Upload four CSV files, create your files, and download. Your original reminder layouts and REDCap evaluation layout are retained.")
    st.subheader("1. Upload your files")
    labels = ("Rotation schedule", "Updated checklist", "Preceptor match file", "OASIS ME evaluation export")
    source_urls = (
        "https://redcap.ctsi.psu.edu/redcap_v17.3.9/DataExport/index.php?pid=19389&report_id=70188",
        "https://oasis.pennstatehealth.net/admin/course/experience_requirement/view_distribution_setup.html",
        "https://oasis.pennstatehealth.net/admin/course/e_manage/manage_evaluators.html",
        "https://oasis.pennstatehealth.net/admin/course/e_manage/student_performance/setup_analysis_report.html",
    )
    uploads = []
    for i, (label, url) in enumerate(zip(labels, source_urls)):
        st.markdown(f"**{i + 1}. {label}** — [Open export website ↗]({url})")
        uploads.append(st.file_uploader(f"Upload {label} CSV", type=["csv"], key="source_" + str(i)))
    st.caption("Use complete exports for the same rotation. Keep the source student IDs and original OASIS Form Record column. The four mailing files use your original columns; clinical-evaluation and observed H&P reminders stay separate.")
    if st.checkbox("Show director tools", key="show_director", disabled=bool(st.session_state.get("evaluation_edit_context"))):
        director_controls(st, uploads[3].getvalue() if uploads[3] else None, evaluation_state)
    render_evaluation_save_status(st, evaluation_state)
    grading_ready = evaluation_rules_saved(st, evaluation_state)
    numbering_state = numbering_archive_state(st)
    st.subheader("2. Create and download")
    ready = all(f is not None for f in uploads)
    blobs = [f.getvalue() for f in uploads] if ready else []
    names = [f.name for f in uploads] if ready else ["", "", "", ""]
    settings = settings_from_options(st.session_state["options"], names, st.session_state["rules"])
    signature = digest([VERSION, [hashlib.sha256(b).hexdigest() for b in blobs], names, asdict(settings)])
    if st.button("Create files", type="primary", disabled=not ready or bool(st.session_state.get("evaluation_edit_context")), width="stretch", key="create_files"):
        st.session_state.pop("result", None)
        try:
            with st.spinner("Checking files and preparing downloads…"):
                result = build_bundle(blobs, names, settings)
                if not result["run"].messages.blocked and numbering_state["ready"] and grading_ready:
                    try:
                        attach_legacy_plan(result, numbering_state["archive"].prepare(result["run"]))
                    except (ReminderArchiveError, NumberingError) as exc:
                        result["redcap_error"] = str(exc)
            if not grading_ready:
                result["redcap_error"] = "Save and verify evaluation exclusions under Show director tools before preparing scores or REDCap downloads."
            st.session_state["result"] = {**result, "signature": signature}
        except (ValueError, UnicodeError, csv.Error) as exc:
            st.error(str(exc))
    result = st.session_state.get("result")
    current_result = result if result and ready and result["signature"] == signature else None
    reminder_state = reminder_archive_state(st)
    render_faculty_reminder_controls(st, current_result, reminder_state)
    if result:
        if not ready or result["signature"] != signature:
            st.info("The files or settings changed. Click Create files again for updated downloads.")
        else:
            render_downloads(st, result, reminder_state, evaluation_ready=grading_ready)
    render_numbering_setup(st, numbering_state)
    st.caption("No reminders are emailed and no student data is uploaded automatically. Explicit save actions store evaluation exclusions (including their reasons) and faculty reminder preferences in separate encrypted GitHub files; after numbering setup, Create files also stores encrypted repeat-number reservations. No source assessment narratives or clinical scores are uploaded to GitHub. Clinical scores are out of 375, not final clerkship grades. Keep this app and its source code restricted to authorized users.")
    base = st.session_state.get("evaluation_saved_base", SavedEvaluationRules())
    unsaved = (bool(st.session_state.get("evaluation_edit_context"))
               or normalize_rules(st.session_state.get("rules", [])) != base.rules
               or bool(st.session_state.get("evaluation_save_error")))
    allow_exit = not unsaved
    if unsaved:
        st.warning("Evaluation exclusion edits have not been confirmed saved. Save them or download a backup before signing out.")
        allow_exit = st.checkbox("I understand these unsaved evaluation edits will be lost when I sign out.", key="allow_unsaved_signout")
    if st.button("Clear files and sign out", key="signout", disabled=not allow_exit):
        st.session_state.clear()
        st.rerun()



# ---------------------------------------------------------------------------
# Dashboard interface. This layer changes presentation only: calculations,
# matching, CSV contracts, and the three encrypted stores are unchanged.
# ---------------------------------------------------------------------------
DASHBOARD_VERSION = "1.0"
DASHBOARD_SOURCES = (
    ("Rotation schedule", "https://redcap.ctsi.psu.edu/redcap_v17.3.9/DataExport/index.php?pid=19389&report_id=70188"),
    ("Updated checklist", "https://oasis.pennstatehealth.net/admin/course/experience_requirement/view_distribution_setup.html"),
    ("Preceptor match file", "https://oasis.pennstatehealth.net/admin/course/e_manage/manage_evaluators.html"),
    ("OASIS ME evaluation export", "https://oasis.pennstatehealth.net/admin/course/e_manage/student_performance/setup_analysis_report.html"),
)
DASHBOARD_CSS = """
<style>
/* Leave room for Streamlit's fixed toolbar on the sign-in and dashboard screens. */
.block-container {max-width: 1580px; padding-top: calc(4.75rem + env(safe-area-inset-top, 0px)); padding-bottom: 1.5rem;}
.clerkship-head {display:flex; align-items:center; gap:14px; margin-bottom:12px;}
.clerkship-mark {background:#183954; color:white; border-radius:14px; padding:12px 15px;
  font-size:22px; font-weight:750; line-height:1;}
.clerkship-head h1 {font-size:1.8rem; padding:0; margin:0; line-height:1.2;}
.clerkship-head p {margin:4px 0 0; opacity:.7; font-size:.9rem;}
.st-key-dashboard_uploads [data-testid="stVerticalBlockBorderWrapper"] {border-radius:12px;}
.st-key-dashboard_uploads [data-testid="stFileUploaderDropzone"] {padding:8px; min-height:100px;}
.st-key-dashboard_uploads [data-testid="stFileUploaderDropzoneInstructions"] {font-size:.78rem;}
.st-key-dashboard_uploads [data-testid="stFileUploaderDropzoneInstructions"] small {font-size:.68rem;}
.st-key-dashboard_actions button {min-height:44px; border-radius:10px; font-weight:650;}
.st-key-dashboard_actions {margin:4px 0 8px;}
.st-key-dashboard_actions button[kind="primary"] {background:#145b68; border-color:#145b68; color:white;}
.st-key-dashboard_downloads [data-testid="stMetric"] {padding:0;}
[data-testid="stAlert"] {border-radius:10px;}
@media (max-width: 700px) {
  .block-container {padding-left:1rem; padding-right:1rem;}
  .clerkship-head h1 {font-size:1.45rem;}
}
</style>
"""


def dashboard_edit_open(st: Any) -> bool:
    return bool(st.session_state.get("evaluation_edit_context") or st.session_state.get("dashboard_add_context"))


def dashboard_grading_ready(st: Any, state: dict) -> bool:
    return not dashboard_edit_open(st) and evaluation_rules_saved(st, state)


def dashboard_selection(event: Any, count: int) -> list[int]:
    """Read row selections without trusting indexes from an older table."""
    selection = event.get("selection", {}) if isinstance(event, dict) else getattr(event, "selection", {})
    rows = selection.get("rows", []) if isinstance(selection, dict) else getattr(selection, "rows", [])
    return sorted({i for i in rows if isinstance(i, int) and not isinstance(i, bool) and 0 <= i < count})


def dashboard_table(st: Any, rows: list[dict], widget: str, *, single: bool = False, hidden: tuple = ()) -> list[int]:
    if not rows:
        return []
    event = st.dataframe(rows, hide_index=True, width="stretch", height=min(285, 40 + 36 * len(rows)),
                         on_select="rerun", selection_mode="single-row" if single else "multi-row",
                         column_config={name: None for name in hidden},
                         key=widget + "_" + digest(rows)[:12])
    return dashboard_selection(event, len(rows))


def dashboard_notice(st: Any, message: str, details: str = "", *, error: bool = False) -> None:
    (st.error if error else st.warning)(message)
    if details:
        with st.expander("Details", expanded=False):
            st.write(details)


def dashboard_evaluation_status(st: Any, state: dict) -> None:
    error = st.session_state.get("evaluation_save_error") or state.get("error")
    if error:
        dashboard_notice(st, "Not saved or verified. Scores and REDCap files are paused.", error, error=True)
    elif dashboard_edit_open(st):
        st.warning("Unsaved edit — choose Save or Cancel before leaving this screen.")
    elif evaluation_rules_saved(st, state):
        saved = state["saved"]
        st.success(f"Saved · {sum(r['active'] for r in saved.rules)} active exclusions · {saved_evaluation_time(saved.saved_at)}")
    else:
        st.warning("This list is not saved yet. Choose Save list to remember it for future visits.")


def dashboard_inputs(st: Any) -> list[Any]:
    """Render all four widgets on every route so uploads survive navigation."""
    uploads = []
    with st.container(key="dashboard_uploads"):
        columns = st.columns(4, gap="small")
        for i, (label, url) in enumerate(DASHBOARD_SOURCES):
            with columns[i]:
                with st.container(border=True):
                    short = ("Rotation schedule", "Checklist", "Preceptor matches", "OASIS evaluations")[i]
                    st.markdown(f"**{i + 1} · {short}**")
                    st.link_button("Open export website ↗", url, width="stretch")
                    f = st.file_uploader("Upload " + label + " CSV", type=["csv"], key="source_" + str(i),
                                         label_visibility="collapsed", disabled=dashboard_edit_open(st))
                    uploads.append(f)
    return uploads


def dashboard_source_context(st: Any, uploads: list[Any]) -> tuple:
    ready = all(f is not None for f in uploads)
    blobs = [f.getvalue() for f in uploads] if ready else []
    names = [f.name for f in uploads] if ready else ["", "", "", ""]
    settings = settings_from_options(st.session_state["options"], names, st.session_state["rules"])
    signature = digest([VERSION, [hashlib.sha256(b).hexdigest() for b in blobs], names, asdict(settings)])
    return ready, blobs, names, settings, signature


def dashboard_build(st: Any, blobs: list[bytes], names: list[str], settings: Settings, signature: str,
                    evaluation_state: dict, numbering_state: dict, *, prepare_redcap: bool) -> dict | None:
    """Only an explicit REDCap button reserves numbers. No REDCap API upload."""
    if len(blobs) != 4 or dashboard_edit_open(st):
        return None
    st.session_state.pop("result", None)
    try:
        with st.spinner("Preparing your files…"):
            result = build_bundle(blobs, names, settings)
            if prepare_redcap and not result["run"].messages.blocked:
                if not dashboard_grading_ready(st, evaluation_state):
                    result["redcap_error"] = "Save evaluation exclusions first."
                elif not numbering_state["ready"]:
                    result["redcap_error"] = "Complete REDCap setup in Settings first."
                else:
                    try:
                        attach_legacy_plan(result, numbering_state["archive"].prepare(result["run"]))
                    except (ReminderArchiveError, NumberingError) as exc:
                        result["redcap_error"] = str(exc)
            result["signature"] = signature
            st.session_state["result"] = result
            return result
    except (ValueError, UnicodeError, csv.Error) as exc:
        st.error(str(exc))
        return None


def dashboard_filtered_reports(result: dict, reminder_state: dict) -> tuple[dict, dict, dict]:
    reports = dict(result["reports"])
    readiness = dict(daily_readiness(result["run"]))
    removed = {n: 0 for n in FACULTY_REMINDER_FILES}
    archive_ready = bool(reminder_state.get("ready"))
    if archive_ready:
        try:
            reports, removed = apply_faculty_reminder_exclusions(reports, list(reminder_state["saved"].names))
        except ReminderArchiveError:
            archive_ready = False
    if not archive_ready:
        # Even disabled widgets must not contain an unfiltered mailing payload.
        for name in FACULTY_REMINDER_FILES:
            reports[name] = reminder_csv_bytes([], REMINDER_COLUMNS[name])
            readiness[name] = False
    if result["run"].messages.blocked:
        readiness = {n: False for n in REMINDER_NAMES}
    return reports, readiness, removed


def dashboard_count(data: bytes) -> int:
    return max(0, sum(1 for _ in csv.reader(io.StringIO(data.decode("utf-8-sig")))) - 1)


def dashboard_checks(st: Any, result: dict, *, detailed: bool = False) -> bool:
    messages = result["run"].messages.rows
    errors = [m for m in messages if m["level"] == "ERROR"]
    warnings = [m for m in messages if m["level"] == "WARNING"]
    for msg in errors:
        st.error(msg["detail"])
    if warnings and not detailed:
        st.warning(f"{len(warnings)} file-check notice(s). Open Review → File checks before sending.")
    if detailed:
        st.dataframe(result["run"].source_stats, hide_index=True, width="stretch")
        if messages:
            st.dataframe(messages, hide_index=True, width="stretch")
        else:
            st.success("No file-check notices.")
        st.download_button("Download file checks", result["reports"]["validation_report.csv"],
                           "validation_report.csv", "text/csv", on_click="ignore", key="dash_check_report")
    elif errors:
        st.download_button("Download file checks", result["reports"]["validation_report.csv"],
                           "validation_report.csv", "text/csv", on_click="ignore", key="dash_error_report")
    return not errors


def dashboard_reminders(st: Any, result: dict | None, reminder_state: dict, ready: bool) -> None:
    st.subheader("Reminder files")
    if not result:
        st.info("Choose Generate reminders above." if ready else "Add the four CSV files above to get started.")
        return
    if not dashboard_checks(st, result):
        return
    run = result["run"]
    reports, readiness, removed = dashboard_filtered_reports(result, reminder_state)
    st.caption(f"{len(run.roster)} students · Reminders: {run.settings.as_of} · OASIS data through {run.settings.data_through or run.settings.as_of}")
    labels = ("Student checklist", "Student requests", "Clinical evaluations", "Observed H&Ps")
    with st.container(key="dashboard_downloads"):
        for col, name, label in zip(st.columns(4, gap="small"), REMINDER_NAMES, labels):
            with col:
                with st.container(border=True):
                    st.markdown("**" + label + "**")
                    st.metric("Rows", dashboard_count(reports[name]) if readiness[name] else "Paused")
                    st.download_button("Download CSV", reports[name] if readiness[name] else b"", name, "text/csv",
                                       key="download_" + name, disabled=not readiness[name], width="stretch",
                                       help=name, on_click="ignore")
    all_ready = all(readiness.values())
    st.download_button("Download all four reminder files", zipped({n: reports[n] for n in REMINDER_NAMES}) if all_ready else b"",
                       "power_automate_reminders.zip", "application/zip", disabled=not all_ready, width="stretch", on_click="ignore")
    if not all(daily_readiness(run).values()):
        st.warning("Some uploads cover different rotations. Affected downloads are paused.")
    if not reminder_state.get("ready"):
        st.warning("Faculty reminder list unavailable. Clinical and H&P reminders are paused; open Exclude faculty.")
    if any(removed.values()):
        st.caption(f"Saved faculty preferences removed {sum(removed.values())} reminder rows.")
    st.caption("Your four original filenames and columns. The checklist file includes Complete and Needs review rows.")


def dashboard_redcap(st: Any, result: dict | None, grading_ready: bool, numbering_state: dict, ready: bool) -> None:
    st.subheader("REDCap import files")
    if not grading_ready:
        st.warning("Save evaluation exclusions before preparing REDCap files.")
        return
    if not numbering_state.get("ready"):
        st.warning("REDCap setup needs attention. Open Settings → Storage & REDCap.")
        return
    if not result:
        st.info("Choose REDCap export above to prepare your files." if ready else "Add the four CSV files above first.")
        return
    if not dashboard_checks(st, result):
        return
    if not result.get("redcap"):
        dashboard_notice(st, "REDCap files are not ready. Choose REDCap export to rebuild.", result.get("redcap_error", ""))
        return
    st.caption("Download these CSVs, then import them in REDCap. This app does not upload them automatically.")
    rows = result["redcap_count"]
    st.download_button(f"Download evaluations + reasons · {rows} rows", result["redcap"], LEGACY_EVAL_FILENAME, "text/csv",
                       key="download_redcap", type="primary", width="stretch", on_click="ignore")
    companions = (("Checklist entries", "redcap_checklist_entries.csv"),
                  ("Preceptor matches", "redcap_preceptor_matching.csv"),
                  ("Clinical scores", "redcap_clinical_summary.csv"))
    for col, (label, filename) in zip(st.columns(3), companions):
        with col:
            st.download_button(label, result["redcap_files"][filename], filename, "text/csv",
                               key="extra_" + filename, width="stretch", on_click="ignore")
    st.info("Main evaluation import: overwrite blank values OFF. Use the correction files below only when needed.")
    with st.expander("Correction files & import help", expanded=False):
        st.warning("These correction files intentionally clear old values. Review them, then import with blank overwriting ON.")
        for label, filename in (("Update exclusion reasons", "redcap_exclusion_updates.csv"),
                                 ("Correct clinical scores / N/A", "redcap_clinical_form_updates.csv"),
                                 ("Correct H&P / handoff values", "redcap_epa_form_updates.csv")):
            st.download_button(label, result["redcap_files"][filename], filename, "text/csv",
                               key="extra_" + filename, on_click="ignore")
        st.caption("The reason Notes Box belongs on a non-repeating student instrument. It includes student, preceptor, form and reason.")
        st.download_button("Import instructions", REDCAP_HELP.encode(), "REDCAP_SETUP.txt", "text/plain", on_click="ignore")
    st.download_button("Download all REDCap files", zipped(result["redcap_files"]), "redcap_import_files.zip",
                       "application/zip", key="dash_redcap_zip", on_click="ignore", width="stretch")
    for warning in result.get("redcap_warnings", []):
        st.warning(warning)


def dashboard_edit_evaluation(st: Any, state: dict) -> None:
    ctx = st.session_state["evaluation_edit_context"]
    rule = ctx["rule"]
    prefix = "evaluation_edit_" + str(ctx["revision"]) + "_"
    st.markdown("#### Edit exclusion")
    st.write(f"{rule['student_name'] or rule['record_id']} · {display_name(rule['evaluator'])} · Form {rule['form_record'] or 'all for this pair'}")
    overlaps = overlapping_exclusions_after_removal(ctx["rules"], [ctx["rule_id"]])
    if overlaps:
        st.warning("Another active rule also covers this evaluation. Disabling this one may not restore it.")
    can_save = state.get("archive") is not None and not state.get("error")
    with st.form(prefix + "form", clear_on_submit=False, enter_to_submit=False):
        reason = st.text_area("Reason", value=rule["reason"], height=130, key=prefix + "reason")
        active = st.checkbox("Active exclusion", value=rule["active"], key=prefix + "active")
        values = {}
        with st.expander("Change student, preceptor or form", expanded=False):
            st.warning("Change these details only to correct which evaluation is excluded.")
            fields = (("student_name", "Student name"), ("record_id", "Student ID"), ("evaluator", "Preceptor"),
                      ("evaluator_email", "Preceptor email"), ("form_record", "Form Record"),
                      ("rotation_start", "Rotation start (YYYY-MM-DD)"), ("submit_date", "Submitted (YYYY-MM-DD HH:MM:SS)"))
            columns = st.columns(2)
            for i, (field_name, label) in enumerate(fields):
                with columns[i % 2]:
                    values[field_name] = st.text_input(label, value=rule[field_name], key=prefix + field_name)
            st.caption("All supplied filters must match. Blank form and submission filters cover the pair, within any rotation filter.")
            confirm = st.checkbox("I confirm this change to the evaluation or scope.", key=prefix + "confirm_target")
        left, right = st.columns(2)
        with left:
            save = st.form_submit_button("Save changes", type="primary", width="stretch", disabled=not can_save, key="evaluation_edit_save")
        with right:
            cancel = st.form_submit_button("Cancel", width="stretch", key="evaluation_edit_cancel")
    if cancel:
        cancel_evaluation_edit(st)
        st.rerun()
    if save:
        try:
            if save_selected_evaluation_edit(st, state, {"reason": reason, "active": active, **values}, confirm_target_change=confirm):
                st.session_state["dashboard_flash"] = "Saved. Rebuild files to include the updated exclusion."
                st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        if st.session_state.get("evaluation_save_error"):
            dashboard_notice(st, "Not saved. Your edit is still open; retry after checking the connection.", st.session_state["evaluation_save_error"], error=True)


def dashboard_begin_add(st: Any) -> None:
    st.session_state.pop("dashboard_remove_context", None)
    if dashboard_edit_open(st):
        raise ValueError("Save or cancel the current edit first.")
    st.session_state["dashboard_add_revision"] = int(st.session_state.get("dashboard_add_revision", 0)) + 1
    st.session_state["dashboard_add_context"] = {
        "rules": normalize_rules(st.session_state["rules"]),
        "base": coerce_saved_evaluation_rules(st.session_state.get("evaluation_saved_base", SavedEvaluationRules())),
        "pending": bool(st.session_state.get("evaluation_draft_pending")),
        "error": st.session_state.get("evaluation_save_error", ""),
        "revision": st.session_state["dashboard_add_revision"],
    }
    st.session_state.pop("result", None)


def dashboard_cancel_add(st: Any) -> None:
    context = st.session_state.pop("dashboard_add_context", None)
    if context:
        st.session_state["rules"] = normalize_rules(context["rules"])
        st.session_state["evaluation_saved_base"] = coerce_saved_evaluation_rules(context["base"])
        st.session_state["evaluation_draft_pending"] = context["pending"]
        st.session_state["evaluation_save_error"] = context["error"]
        st.session_state.pop("result", None)


def dashboard_add_targets(oasis_blob: bytes) -> list[dict]:
    picks = exclusion_choices(read_csv_bytes(oasis_blob, "OASIS ME", Messages()))
    targets = []
    for rid, student in sorted(picks.items(), key=lambda pair: pair[1]["name"]):
        for pk, preceptor in student["preceptors"].items():
            for fk, form in preceptor["forms"].items():
                targets.append({"Student": student["name"], "Preceptor": display_name(preceptor["evaluator"]),
                                "Form": form["form_record"] or "No ID", "Submitted": form["submit_date"],
                                "record_id": rid, "student_name": student["name"], "evaluator": preceptor["evaluator"],
                                "evaluator_email": preceptor["email"], "scope": dict(form), "choice_key": digest([rid, pk, fk])})
    return targets


def dashboard_add_evaluation(st: Any, oasis_blob: bytes | None, state: dict) -> None:
    ctx = st.session_state["dashboard_add_context"]
    st.markdown("#### Add exclusion")
    if not oasis_blob:
        st.info("Upload the OASIS evaluation CSV to choose an evaluation.")
        if st.button("Cancel", key="dashboard_add_cancel_empty"):
            dashboard_cancel_add(st); st.rerun()
        return
    try:
        targets = dashboard_add_targets(oasis_blob)
    except ValueError as exc:
        st.error(str(exc))
        if st.button("Cancel", key="dashboard_add_cancel_error"):
            dashboard_cancel_add(st); st.rerun()
        return
    query = st.text_input("Find student, preceptor or form", key="dashboard_add_search").strip().casefold()
    candidates = [t for t in targets if not query or query in " ".join(str(t[k]) for k in ("Student", "Preceptor", "Form", "Submitted")).casefold()]
    display = [{k: t[k] for k in ("Student", "Preceptor", "Form", "Submitted", "choice_key")} for t in candidates]
    selected = dashboard_table(st, display, "dashboard_add_picker_" + str(ctx["revision"]), single=True, hidden=("choice_key",))
    target = candidates[selected[0]] if selected else None
    st.caption("Select one row. Use search to narrow the list.")
    can_save = state.get("archive") is not None and not state.get("error")
    # A new target gets its own form, avoiding an explanation being saved to the wrong row.
    prefix = "dash_new_" + str(ctx["revision"]) + "_" + (target["choice_key"] if target else "none")
    with st.form(prefix, clear_on_submit=False, enter_to_submit=False):
        reason = st.text_area("Reason", height=115, key=prefix + "_reason")
        broad = st.checkbox("Apply to all clinical evaluations for this student–preceptor pair", key=prefix + "_all")
        left, right = st.columns(2)
        with left:
            save = st.form_submit_button("Exclude and save", type="primary", width="stretch", key="dash_new_save", disabled=not target or not can_save)
        with right:
            cancel = st.form_submit_button("Cancel", width="stretch", key="dash_new_cancel")
    if cancel:
        dashboard_cancel_add(st); st.rerun()
    if save and target:
        if not reason.strip():
            st.error("Enter a reason before saving.")
            return
        rule = {k: target[k] for k in ("record_id", "student_name", "evaluator", "evaluator_email")}
        rule.update(reason=reason, active=True, origin="Director", **({} if broad else target["scope"]))
        normalized = normalize_rules([rule])[0]
        if any(r["rule_id"] == normalized["rule_id"] for r in ctx["rules"]):
            st.warning("This exclusion already exists. Cancel, then select it and choose Edit selected.")
            return
        st.session_state["evaluation_saved_base"] = coerce_saved_evaluation_rules(ctx["base"])
        if save_evaluation_rule_action(st, state, merge_rule_updates(ctx["rules"], [rule])):
            st.session_state.pop("dashboard_add_context", None)
            st.session_state["dashboard_flash"] = "Exclusion saved. Rebuild files to apply it."
            st.rerun()
        else:
            dashboard_notice(st, "Not saved. Your entry is still open.", st.session_state.get("evaluation_save_error", ""), error=True)


def dashboard_evaluations(st: Any, oasis_blob: bytes | None, state: dict) -> None:
    st.subheader("Evaluation exclusions")
    st.caption("Affects clinical scores and REDCap reasons—not reminder counts.")
    dashboard_evaluation_status(st, state)
    if st.session_state.get("evaluation_edit_context"):
        dashboard_edit_evaluation(st, state)
        return
    if st.session_state.get("dashboard_add_context"):
        dashboard_add_evaluation(st, oasis_blob, state)
        return
    rules = normalize_rules(st.session_state["rules"])
    can_save = state.get("archive") is not None and not state.get("error")
    query = st.text_input("Find a saved exclusion", placeholder="Student, preceptor, form or reason", key="dashboard_rule_search").strip().casefold()
    shown = [r for r in rules if not query or query in evaluation_exclusion_label(r).casefold() or query in r["reason"].casefold()]
    display = [{"Student": r["student_name"] or r["record_id"], "Preceptor": display_name(r["evaluator"]) or r["evaluator_email"],
                "Form": r["form_record"] or "All for this pair", "Active": r["active"], "Reason": r["reason"], "rule_id": r["rule_id"]} for r in shown]
    indices = dashboard_table(st, display, "dashboard_rule_picker", hidden=("rule_id",))
    selected = [shown[i]["rule_id"] for i in indices]
    if not shown:
        st.info("No matching exclusions." if rules else "No evaluation exclusions.")
    else:
        st.caption("Select a row to edit. Select one or more rows to remove.")
    left, mid, right, refresh = st.columns(4)
    with left:
        if st.button("＋ Add exclusion", key="dashboard_add_open", width="stretch", disabled=not oasis_blob):
            dashboard_begin_add(st); st.rerun()
    with mid:
        if st.button("Edit selected", key="dashboard_edit_open", width="stretch", disabled=len(selected) != 1):
            st.session_state.pop("dashboard_remove_context", None)
            begin_evaluation_edit(st, selected[0]); st.rerun()
    with right:
        if st.button("Remove selected", key="dashboard_remove_open", width="stretch", disabled=not selected or not can_save):
            st.session_state["dashboard_remove_context"] = {"ids": selected, "rules": rules,
                "base": coerce_saved_evaluation_rules(st.session_state.get("evaluation_saved_base", state["saved"]))}
            st.rerun()
    with refresh:
        if st.button("Refresh list", key="evaluation_refresh", width="stretch"):
            st.rerun()
    if not oasis_blob:
        st.caption("To add a new exclusion, upload the OASIS evaluation file above. Saved exclusions can be edited now.")
    removal = st.session_state.get("dashboard_remove_context")
    if removal:
        ids = removal["ids"]
        overlaps = overlapping_exclusions_after_removal(removal["rules"], ids)
        if overlaps:
            st.warning("Another active rule still covers the same evaluation(s). Removing these alone may not restore them.")
        st.warning(f"Remove {len(ids)} exclusion(s) and save? Source evaluations are not deleted.")
        confirm_col, cancel_col = st.columns(2)
        with confirm_col:
            if st.button("Confirm removal and save", key="dashboard_remove_confirm", type="primary", disabled=not can_save):
                st.session_state["evaluation_saved_base"] = coerce_saved_evaluation_rules(removal["base"])
                desired = remove_evaluation_exclusions(removal["rules"], ids)
                if save_evaluation_rule_action(st, state, desired):
                    st.session_state.pop("dashboard_remove_context", None)
                    st.session_state["dashboard_flash"] = "Exclusions removed and saved. Rebuild files to apply the change."
                st.rerun()
        with cancel_col:
            if st.button("Cancel removal", key="dashboard_remove_cancel"):
                st.session_state.pop("dashboard_remove_context", None); st.rerun()
    if not evaluation_rules_saved(st, state):
        if st.button("Save list", type="primary", key="apply_rules", disabled=not can_save):
            save_evaluation_rule_action(st, state, rules); st.rerun()
    with st.expander("Backup & recovery", expanded=False):
        st.download_button("Download exclusions backup", exclusion_backup(st.session_state["rules"]), "exclusions_backup.json", "application/json", on_click="ignore")
        backup = st.file_uploader("Restore exclusions backup", type=["json"], key="restore_rules")
        if st.button("Restore for review", key="restore_rules_button", disabled=backup is None):
            try:
                st.session_state["rules"] = normalize_rules(json.loads(backup.getvalue().decode("utf-8-sig")))
                st.session_state["evaluation_draft_pending"] = True
                st.session_state.pop("result", None)
                st.session_state.pop("evaluation_save_error", None)
                st.rerun()
            except (ValueError, UnicodeError) as exc:
                st.error("Could not read backup: " + str(exc))
        if state["saved"].exists and rules != state["saved"].rules:
            consent = st.checkbox("Discard my unsaved list and use the saved list.", key="evaluation_confirm_discard")
            if st.button("Discard unsaved list", key="evaluation_discard", disabled=not consent or not state.get("ready")):
                accept_saved_evaluation_rules(st, state["saved"]); st.rerun()


def dashboard_faculty(st: Any, result: dict | None, state: dict) -> None:
    st.subheader("Faculty reminder exclusions")
    st.caption("Stops clinical-evaluation and observed H&P reminders for the selected names. Grades do not change.")
    if not state.get("ready"):
        dashboard_notice(st, "Saved faculty list unavailable. Open Settings → Storage & REDCap.", state.get("error", "First-time setup may be needed."))
        st.button("Retry loading list", key="faculty_reminder_retry")
        return
    saved = state["saved"]
    st.success(f"Saved · {len(saved.names)} faculty excluded from reminders")
    try:
        choices = faculty_reminder_choices(result["reports"]) if result and not result["run"].messages.blocked else []
    except ReminderArchiveError as exc:
        st.error(str(exc)); choices = []
    excluded = {reminder_name_key(n) for n in saved.names}
    available = [n for n in choices if reminder_name_key(n) not in excluded]
    left, right = st.columns(2, gap="large")
    with left:
        st.markdown("#### Stop reminders")
        search = st.text_input("Find faculty", key="dashboard_faculty_search").strip().casefold()
        names = [n for n in available if not search or search in n.casefold()]
        selections = dashboard_table(st, [{"Faculty": n} for n in names], "dashboard_faculty_add")
        additions = [names[i] for i in selections]
        if not names:
            st.info("No matching faculty." if choices else "Generate reminders to load faculty names.")
        if st.button("Exclude selected and save", key="faculty_reminder_save_add", disabled=not additions, type="primary", width="stretch"):
            try:
                state["archive"].change(add=additions)
                st.session_state["dashboard_flash"] = "Saved. Download fresh reminder files to use the updated list."
                st.rerun()
            except ReminderArchiveError as exc:
                state["ready"], state["error"] = False, str(exc)
                dashboard_notice(st, "Not saved. Please retry.", str(exc), error=True)
    with right:
        st.markdown("#### Resume reminders")
        search = st.text_input("Find excluded faculty", key="dashboard_faculty_restore_search").strip().casefold()
        names = [n for n in saved.names if not search or search in n.casefold()]
        selections = dashboard_table(st, [{"Excluded faculty": n} for n in names], "dashboard_faculty_remove")
        removals = [names[i] for i in selections]
        if not names:
            st.info("No excluded faculty match." if saved.names else "No faculty excluded.")
        if st.button("Restore selected and save", key="faculty_reminder_save_remove", disabled=not removals, width="stretch"):
            try:
                state["archive"].change(remove=removals)
                st.session_state["dashboard_flash"] = "Saved. Faculty can receive reminders again when an assessment is outstanding."
                st.rerun()
            except ReminderArchiveError as exc:
                state["ready"], state["error"] = False, str(exc)
                dashboard_notice(st, "Not saved. Please retry.", str(exc), error=True)
    st.caption("Applies to both preceptor files, all students and rotations. Identical displayed names share the rule. Previously downloaded files do not change.")


def dashboard_subnav(st: Any, options: tuple[str, ...], state_key: str) -> str:
    current = st.session_state.get(state_key, options[0])
    for col, label in zip(st.columns(len(options)), options):
        with col:
            if st.button(label, key=state_key + "_" + label, type="primary" if current == label else "secondary", width="stretch"):
                st.session_state[state_key] = label
                st.rerun()
    return current


def dashboard_review(st: Any, result: dict | None, reminder_state: dict, grading_ready: bool) -> None:
    st.subheader("Review")
    if not result:
        st.info("Generate reminders or a REDCap export to review this run."); return
    choice = dashboard_subnav(st, ("Student progress", "Clinical scores", "File checks"), "dashboard_review_page")
    if choice == "File checks":
        dashboard_checks(st, result, detailed=True); return
    if not dashboard_checks(st, result):
        return
    if choice == "Student progress":
        st.dataframe(result["run"].reports["student_review"], hide_index=True, width="stretch", height=360)
        st.download_button("Download student progress", result["reports"]["all_student_requirement_status.csv"],
                           "all_student_requirement_status.csv", "text/csv", on_click="ignore")
        return
    if not grading_ready:
        st.warning("Save evaluation exclusions before viewing scores."); return
    st.caption("Clinical component only · 375 possible points · not the final clerkship grade")
    scores = result["run"].reports["scores"]
    concise = [{"Student": s["student_name"], "Received": s["cas_submissions"], "Manually excluded": s["manual_exclusions"],
                "Score / 375": s["clinical_score_375"], "Automatic drop": s["dropped_evaluator"]} for s in scores]
    st.dataframe(concise, hide_index=True, width="stretch", height=320)
    reports, readiness, removed = dashboard_filtered_reports(result, reminder_state)
    details = dict(reports)
    for name in REMINDER_NAMES:
        if not readiness[name]:
            details.pop(name, None)
    details["exclusions_backup.json"] = exclusion_backup(result["run"].settings.exclusions)
    if result.get("redcap"):
        details.update(result["redcap_files"])
    a, b = st.columns(2)
    with a:
        st.download_button("Download clinical scores", reports["clinical_scores.csv"], "clinical_scores.csv", "text/csv", on_click="ignore")
    with b:
        st.download_button("Download director package", zipped(details), "director_outputs.zip", "application/zip", on_click="ignore")
    st.caption("Director package contains grades and narratives—not a Power Automate input.")


def dashboard_settings(st: Any, evaluation_state: dict, reminder_state: dict, numbering_state: dict) -> None:
    st.subheader("Settings")
    choice = dashboard_subnav(st, ("Dates & requirements", "Storage & REDCap", "Sign out"), "dashboard_settings_page")
    if choice == "Storage & REDCap":
        st.caption("One-time setup and recovery. Existing connections do not need to be changed.")
        # Existing setup tools retain their full safeguards, but are off the daily screen.
        render_numbering_setup(st, numbering_state)
        if reminder_state.get("ready"):
            st.success("Faculty reminder storage connected.")
        else:
            render_faculty_reminder_controls(st, None, reminder_state)
        dashboard_evaluation_status(st, evaluation_state)
        st.download_button("Original REDCap field reference", csv_bytes([], LEGACY_EVAL_COLUMNS),
                           "redcap_oasis_eval_clerk_upload_TEMPLATE.csv", "text/csv", on_click="ignore", key="dash_field_ref")
        st.download_button("REDCap import guide", REDCAP_HELP.encode(), "REDCAP_SETUP.txt", "text/plain", on_click="ignore", key="dash_redcap_help")
        return
    if choice == "Sign out":
        base = coerce_saved_evaluation_rules(st.session_state.get("evaluation_saved_base", SavedEvaluationRules()))
        unsaved = (dashboard_edit_open(st) or normalize_rules(st.session_state.get("rules", [])) != base.rules
                   or bool(st.session_state.get("evaluation_save_error")))
        allowed = not unsaved
        if unsaved:
            st.warning("You have unsaved evaluation exclusions. Save or download a backup before signing out.")
            st.download_button("Download exclusions backup", exclusion_backup(st.session_state.get("rules", [])), "exclusions_backup.json", "application/json", on_click="ignore")
            allowed = st.checkbox("Discard these unsaved changes when I sign out.", key="allow_unsaved_signout")
        if st.button("Clear files and sign out", key="signout", disabled=not allowed, type="primary"):
            st.session_state.clear(); st.rerun()
        return
    current = st.session_state["options"]
    with st.form("options_form", enter_to_submit=False):
        left, right = st.columns(2)
        with left:
            as_of = st.date_input("Reminder date", value=dt(current.get("as_of") or datetime.now(ZoneInfo("America/New_York")).date()).date())
        with right:
            through = st.text_input("OASIS data through (optional YYYY-MM-DD)", value=current.get("data_through", ""))
        targets = {}
        for col, k, default in zip(st.columns(3), KINDS, (8, 2, 1)):
            with col:
                targets[k + "_target"] = int(st.number_input(LABELS[k], min_value=1, max_value=100, value=int(current.get(k + "_target", default))))
        with st.expander("Advanced dates & reminder links", expanded=False):
            duration = st.number_input("Rotation length (calendar days)", min_value=1, max_value=365, value=int(current.get("rotation_days", 26)))
            grace = st.number_input("Preceptor grace days", min_value=0, max_value=365, value=int(current.get("grace_days", 0)))
            confirm = st.checkbox("The exports cover this rotation even when a whole source has zero rows.", value=bool(current.get("confirm_coverage", False)))
            urls = {k + "_url": st.text_input(LABELS[k] + " link", value=current.get(k + "_url", SURVEYS[k])) for k in KINDS}
            partial = st.checkbox("Keep original partially prefilled links", value=bool(current.get("legacy_partial_links", True)))
        if st.form_submit_button("Apply settings", type="primary"):
            if through and (day(through) != through or through > as_of.isoformat()):
                st.error("Use YYYY-MM-DD, no later than the reminder date.")
            elif any(u and not u.startswith("https://") for u in urls.values()):
                st.error("Links must start with https:// or be blank.")
            else:
                st.session_state["options"] = dict(as_of=as_of.isoformat(), data_through=through, rotation_days=int(duration),
                    grace_days=int(grace), confirm_coverage=confirm, legacy_partial_links=partial, **targets, **urls)
                st.session_state.pop("result", None)
                st.session_state["dashboard_flash"] = "Settings applied for this session. Rebuild your files."
                st.rerun()


def main() -> None:
    import streamlit as st
    st.set_page_config(page_title="Pediatric Clerkship Tracker", page_icon="📋", layout="wide")
    st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)
    st.markdown('<div class="clerkship-head"><div class="clerkship-mark">P</div><div><h1>Pediatric Clerkship</h1>'
                '<p>Upload. Review. Download.</p></div></div>', unsafe_allow_html=True)
    if not authenticate(st):
        st.stop()
    evaluation_state = evaluation_archive_state(st)
    try:
        sync_evaluation_rules(st, evaluation_state)
    except (ValueError, TypeError) as exc:
        dashboard_notice(st, "Your saved evaluation settings need attention.", str(exc), error=True)
        st.stop()
    st.session_state.setdefault("options", {})
    st.session_state.setdefault("dashboard_page", "reminders")
    uploads = dashboard_inputs(st)
    ready, blobs, names, settings, signature = dashboard_source_context(st, uploads)
    numbering_state = numbering_archive_state(st)
    reminder_state = reminder_archive_state(st)
    grading_ready = dashboard_grading_ready(st, evaluation_state)
    editing = dashboard_edit_open(st)
    page = st.session_state["dashboard_page"]
    if editing:
        page = st.session_state["dashboard_page"] = "evaluations"
    requested = None
    buttons = (("Generate reminders", "reminders", "create_files"), ("REDCap export", "redcap", "dashboard_generate_redcap"),
               ("Exclude evaluations", "evaluations", "dashboard_nav_evaluations"),
               ("Exclude faculty", "faculty", "dashboard_nav_faculty"), ("Review", "review", "dashboard_nav_review"),
               ("Settings", "settings", "dashboard_nav_settings"))
    with st.container(key="dashboard_actions"):
        for col, (label, route, widget) in zip(st.columns([1.25, 1.15, 1.35, 1.15, .8, .8], gap="small"), buttons):
            with col:
                disabled = editing or (route in {"reminders", "redcap"} and not ready)
                if st.button(label, key=widget, type="primary" if page == route else "secondary", disabled=disabled, width="stretch"):
                    page = route
                    st.session_state["dashboard_page"] = route
                    if route != "evaluations":
                        st.session_state.pop("dashboard_remove_context", None)
                    requested = route
    flash = st.session_state.pop("dashboard_flash", "")
    if flash:
        st.success(flash)
    # Old UI success notices are replaced with a short equivalent; no save logic changes.
    if st.session_state.pop("evaluation_rules_feedback", "") and not flash:
        st.success("Exclusions saved. Rebuild your files to apply the changes.")
    st.session_state.pop("evaluation_edit_notice", None)
    st.session_state.pop("faculty_reminder_flash", None)
    existing = st.session_state.get("result")
    current = existing if existing and ready and existing.get("signature") == signature else None
    if requested in {"reminders", "redcap"}:
        current = dashboard_build(st, blobs, names, settings, signature, evaluation_state, numbering_state, prepare_redcap=requested == "redcap")
    elif requested == "faculty" and not current and ready:
        # Only a local calculation to populate the names; never reserves REDCap numbers.
        current = dashboard_build(st, blobs, names, settings, signature, evaluation_state, numbering_state, prepare_redcap=False)
    elif existing and not current:
        st.info("Files or settings changed. Generate fresh files before downloading.")
    if page == "reminders":
        dashboard_reminders(st, current, reminder_state, ready)
    elif page == "redcap":
        dashboard_redcap(st, current, grading_ready, numbering_state, ready)
    elif page == "evaluations":
        dashboard_evaluations(st, uploads[3].getvalue() if uploads[3] else None, evaluation_state)
    elif page == "faculty":
        dashboard_faculty(st, current, reminder_state)
    elif page == "review":
        dashboard_review(st, current, reminder_state, grading_ready)
    else:
        dashboard_settings(st, evaluation_state, reminder_state, numbering_state)
    st.caption("Downloads only · No emails sent · No automatic REDCap upload")


if __name__ == "__main__":
    main()
