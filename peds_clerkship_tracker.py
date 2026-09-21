"""Pediatric Clerkship — four files in, reminders and NEW-project REDCap CSV out.

Run: streamlit run peds_clerkship_tracker.py
No REDCap API, database export, repeat numbering, or external storage is used.
Scoring and mailing logic are retained from the user's supplied application.
The new REDCap design uses ordinary records with stable source-derived IDs.
Confidential: the three original student-specific exclusion rules are included.
Keep this source repository private and use an approved restricted-access host.
"""
from __future__ import annotations

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
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

VERSION = "2.0.0"
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
    legacy_partial_links: bool = False


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
            REMINDER_NAMES[2]: assessments}


def legacy_power_automate_files(run: Run) -> dict[str, bytes]:
    """Optional original layouts for flows set up before the combined-preceptor app.

    Never use these and the combined-preceptor file to send the same reminder run.
    A student/preceptor can have one CAS row and one HP row in separate flows.
    No handoff preceptor survey URL was provided in the user's original flow.
    """
    reports = output_files(run)
    checks = [r for r in run.reports["checklist_review"] if r["status"] == "Needs review" and r["email"]]
    check_rows = []
    for r in checks:
        r = dict(r)
        # Older encounter flows do not have the newer incomplete/unknown columns.
        # Put those issues into the original missing-items message as well.
        issues = [r["missing_items"]] if r["missing_items"] else []
        if r["participation_review_items"]:
            issues.append("Participation level needs review: " + r["participation_review_items"])
        if r["incomplete_items"]:
            issues.append("Logged but not marked complete: " + r["incomplete_items"])
        r["missing_items"] = "<br>".join(issues)
        check_rows.append(r)
    students = [r for r in run.reports["student_review"] if r["reminder_needed"] == "Yes" and r["email"]]
    cas_rows, hp_rows = [], []
    for row in run.reports["preceptor_reminders"]:
        pending = set(row["evaluation_type"].split("; "))
        for kind, target in (("cas", cas_rows), ("hp", hp_rows)):
            if LABELS[kind] not in pending:
                continue
            link = row[kind + "_link"]
            new = {**row, "evaluation_type": FORM_NAMES[kind], "expected_eval_count": 1,
                   "completed_eval_count": 0, "pending_eval_count": 1, "duplicate_match_flag": "",
                   "reminder_note": f"The student reported working with you. In the records through {row['data_through']}, we have not received the corresponding {LABELS[kind]} assessment.",
                   "blank_form_link": link,
                   "partial_form_link": link + "&complete=1&ph=3&ch=3&pp=3&cp=3" if run.settings.legacy_partial_links and link else ""}
            target.append(new)
    return {REMINDER_NAMES[0]: csv_bytes(check_rows, CHECKLIST_COLUMNS[:10], flow=True),
            REMINDER_NAMES[1]: csv_bytes(students, STUDENT_COLUMNS[:8], flow=True),
            REMINDER_NAMES[2]: csv_bytes(cas_rows, LEGACY_CAS_COLUMNS, flow=True),
            "observed_hp_reminders.csv": csv_bytes(hp_rows, LEGACY_HP_COLUMNS, flow=True)}


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
    """Keep the three current mailing layouts and messages unchanged."""
    checks = [r for r in run.reports["checklist_review"] if r["status"] == "Needs review" and r["email"]]
    students = [r for r in run.reports["student_review"] if r["reminder_needed"] == "Yes" and r["email"]]
    return {
        REMINDER_NAMES[0]: csv_bytes(checks, CHECKLIST_COLUMNS, flow=True),
        REMINDER_NAMES[1]: csv_bytes(students, STUDENT_COLUMNS, flow=True),
        REMINDER_NAMES[2]: csv_bytes(run.reports["preceptor_reminders"], PRECEPTOR_COLUMNS, flow=True),
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


REDCAP_HELP = """NEW PROJECT ONLY — MANUAL REDCAP IMPORT

ONE-TIME SETUP
Create a new classic (non-longitudinal) REDCap project. Upload
redcap_data_dictionary.csv as the project's Data Dictionary. This is a COMPLETE
new-project dictionary, NOT an addition to or replacement for your old project.
Do not enable repeating instruments or repeating events. Disable automatic
record numbering, or leave the import option that replaces record IDs disabled.

EACH RUN
1. Upload the four CSV source files to the Streamlit app and click Create files.
2. Download redcap_import.csv (keep the CSV unchanged; avoid resaving in Excel).
3. In the NEW REDCap project, choose Applications > Data Import Tool.
4. Choose a real-time import, show the comparison table, preserve supplied record
   IDs, and set Overwrite data with blank values = YES for this dedicated project.
5. Review REDCap's validation and comparison table, then confirm the import.

WHY BLANK OVERWRITING IS YES HERE
All included columns are generated by the app. A blank may intentionally clear a
previously dropped evaluator, an exclusion reason, a corrected N/A score, or a
completion date. YES keeps such fields accurate on reimport. Add manual notes,
NBME results and final-grade decisions on a SEPARATE instrument whose fields do
not appear in this import. Never apply this instruction to the old project.

DESIGN
Each row is an ordinary REDCap record: student summary, clinical evaluation,
observed H&P, handoff, checklist entry, or preceptor match. All rows for one
student/rotation share student_key. A summary includes readable detail notes as
well as numeric scores; detailed rows hold structured scores and source data.
Filter record_type = 1 for one progress row per student. Filter student_key for
all of a student's rows. No repeating-instance numbers need to be configured.

Stable IDs are based on source identities, not input order. Re-importing the
same source items targets the same generated record IDs. New OASIS Form Records
receive different IDs. Scores, comments and exclusion flags do not change IDs.
A 128-bit digest is used; same-run collisions/conflicting identities are checked.

LIMITS
Use complete, current exports for the selected rotations. A partial OASIS file
cannot produce a complete cumulative score. Import the newest batch last.
Importing a CSV does not delete records missing from it. Deleting an encounter,
changing an identifying field (such as checklist item/time, preceptor match name
or period), or changing a fallback evaluation identity can leave an old record
in REDCap. Filter batch_id to the desired run or reconcile those older records.
Never sum all historical detail rows blindly. Summary rows reflect the uploaded
source snapshot, not unseen REDCap data. The report_date is an analysis date;
data_through is an export reference date, not proof the export is complete.

The app includes ALL received clinical evaluations, including manually excluded
and automatically dropped evaluations, with explicit flags. Exclusions change
the clinical grade calculation, not reminder completion or feedback visibility.
The clinical score is out of 375; no NBME or final clerkship grade is invented.
Generating a reminder CSV does not send mail or record mail as sent.

The live import has not been tested in your REDCap installation. Test the
included SYNTHETIC example in an empty development project first. Require
approved hosting and authorized access for student information.
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
                    legacy_partial_links=bool(options.get("legacy_partial_links", False)))


def build_bundle(source_bytes: list[bytes], filenames: list[str], settings: Settings) -> dict:
    run, raw = process_uploads(source_bytes, filenames, settings)
    result = {"run": run, "reports": output_files(run), "redcap": b"", "redcap_error": "", "redcap_count": 0, "batch_id": ""}
    if run.messages.blocked:
        return result
    try:
        rows = redcap_rows(run, raw[1], raw[3])
        result.update(redcap=csv_bytes(rows, [s["name"] for s in field_specs()]), redcap_count=len(rows), batch_id=rows[0]["batch_id"] if rows else "")
    except ValueError as exc:
        result["redcap_error"] = str(exc)
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


def director_controls(st: Any, oasis_blob: bytes | None) -> None:
    """Optional controls, without APIs, database exports or automatic network writes."""
    with st.expander("Exclusions", expanded=False):
        st.caption("The three original student–preceptor exclusions load automatically. Rules affect clinical scoring, not received-evaluation counts or reminders.")
        rules = normalize_rules(st.session_state["rules"])
        editable = [{"rule_id": r["rule_id"], "active": r["active"], "student": r["student_name"] or r["record_id"],
                     "preceptor": r["evaluator"] or r["evaluator_email"], "form": r["form_record"] or "All for this pair",
                     "reason": r["reason"]} for r in rules]
        if editable:
            edited = st.data_editor(editable, hide_index=True, num_rows="fixed", key="rule_editor_" + digest(rules)[:12],
                                    disabled=["rule_id", "student", "preceptor", "form"], width="stretch")
            if st.button("Apply exclusion changes", key="apply_rules"):
                if hasattr(edited, "to_dict"):
                    edited = edited.to_dict("records")
                updates = {r["rule_id"]: r for r in edited}
                st.session_state["rules"] = normalize_rules([{**r, "active": updates[r["rule_id"]]["active"], "reason": updates[r["rule_id"]]["reason"]} for r in rules])
                st.session_state.pop("result", None)
                st.rerun()
        if oasis_blob:
            try:
                picks = exclusion_choices(read_csv_bytes(oasis_blob, "OASIS ME", Messages()))
                if picks:
                    rid = st.selectbox("Student", sorted(picks, key=lambda k: picks[k]["name"]), format_func=lambda k: picks[k]["name"], key="rule_student")
                    preceptors = picks[rid]["preceptors"]
                    pk = st.selectbox("Preceptor", list(preceptors), format_func=lambda k: display_name(preceptors[k]["evaluator"]), key="rule_preceptor_" + rid)
                    preceptor = preceptors[pk]
                    fk = st.selectbox("Exclude", ["all"] + list(preceptor["forms"]),
                                      format_func=lambda k: "All clinical evaluations for this student–preceptor pair" if k == "all" else f"Form {preceptor['forms'][k]['form_record'] or 'no ID'} — {preceptor['forms'][k]['submit_date']}", key="rule_form_" + pk)
                    reason = st.text_input("Reason for this exclusion", key="rule_reason")
                    if st.button("Add exclusion", key="add_rule"):
                        if not reason.strip():
                            st.error("Enter the reason for the exclusion.")
                        else:
                            scope = {} if fk == "all" else preceptor["forms"][fk]
                            added = {"record_id": rid, "student_name": picks[rid]["name"], "evaluator": preceptor["evaluator"],
                                     "evaluator_email": preceptor["email"], "reason": reason, "active": True, "origin": "Director", **scope}
                            st.session_state["rules"] = merge_rule_updates(rules, [added])
                            st.session_state.pop("result", None)
                            st.rerun()
            except ValueError as exc:
                st.warning(str(exc))
        backup = st.file_uploader("Restore saved exclusions (optional JSON)", type=["json"], key="restore_rules")
        if st.button("Restore exclusions", disabled=backup is None, key="restore_rules_button"):
            try:
                st.session_state["rules"] = normalize_rules(json.loads(backup.getvalue().decode("utf-8-sig")))
                st.session_state.pop("result", None)
                st.rerun()
            except (ValueError, UnicodeError) as exc:
                st.error("Could not read the backup: " + str(exc))
        st.download_button("Download exclusions backup", exclusion_backup(st.session_state["rules"]), "exclusions_backup.json", "application/json", on_click="ignore")
        st.caption("Changes last for this browser session. Download the backup after a change, then restore it next time. The owner can set EXCLUSIONS_JSON in Streamlit Secrets to make that rule set the startup default. REDCap imports also retain the rules used, but this app does not read REDCap.")
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
            partial = st.checkbox("Include original partially prefilled survey links", value=bool(current.get("legacy_partial_links", False)))
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


def render_downloads(st: Any, result: dict) -> None:
    run, reports = result["run"], result["reports"]
    if run.messages.blocked:
        for msg in run.messages.rows:
            if msg["level"] == "ERROR":
                st.error(msg["detail"])
        st.download_button("Download file-check report", reports["validation_report.csv"], "validation_report.csv", "text/csv", on_click="ignore")
        return
    readiness = daily_readiness(run)
    st.subheader("Your files")
    st.caption(f"{len(run.roster)} students • Reminder date {run.settings.as_of} • OASIS reference date {run.settings.data_through or run.settings.as_of}")
    labels = ("Student encounter reminders", "Student evaluation / H&P / handoff reminders", "Preceptor evaluation reminders")
    for name, label in zip(REMINDER_NAMES, labels):
        st.download_button(label, reports[name], name, "text/csv", disabled=not readiness[name], on_click="ignore", width="stretch", key="download_" + name)
    mail_files = {name: reports[name] for name in REMINDER_NAMES}
    st.download_button("Download all three reminder files", zipped(mail_files), "power_automate_reminders.zip", "application/zip", disabled=not all(readiness.values()), on_click="ignore", width="stretch")
    if not all(readiness.values()):
        st.warning("Some files do not cover the rotation dates in the schedule. Use the matching rotation exports; affected downloads are paused.")
    st.divider()
    st.download_button("Download REDCap import file", result["redcap"] or b"", "redcap_import.csv", "text/csv", disabled=not bool(result["redcap"]), on_click="ignore", width="stretch", key="download_redcap")
    if result["redcap"]:
        st.caption(f"{result['redcap_count']} records: student summaries, evaluations, H&Ps, handoffs, checklist entries and preceptor matches. For the NEW project only. No API or REDCap export needed.")
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
    if st.session_state.get("show_director", False):
        with st.expander("Scores and detailed outputs", expanded=False):
            st.dataframe(run.reports["scores"], hide_index=True, width="stretch")
            details = {**reports, "exclusions_backup.json": exclusion_backup(run.settings.exclusions)}
            if result["redcap"]:
                details["redcap_import.csv"] = result["redcap"]
            st.download_button("Download director package", zipped(details), "director_outputs.zip", "application/zip", on_click="ignore")
            st.caption("Contains grades and narratives. Do not use this ZIP as a mailing-input folder.")
            st.download_button("Download original separate-flow layouts", zipped(legacy_power_automate_files(run)), "legacy_power_automate.zip", "application/zip", disabled=not all(readiness.values()), on_click="ignore")
            st.caption("Optional older CAS/H&P layouts. Use these OR the combined-preceptor output, not both for the same mailing.")


def main() -> None:
    import streamlit as st
    st.set_page_config(page_title="Pediatric Clerkship Tracker", page_icon="📋", layout="centered")
    st.title("Pediatric Clerkship Tracker")
    if not authenticate(st):
        st.stop()
    if "rules" not in st.session_state:
        try:
            configured = app_secret(st, "EXCLUSIONS_JSON")
            st.session_state["rules"] = normalize_rules(json.loads(configured) if configured else LEGACY_EXCLUSIONS)
        except (ValueError, TypeError) as exc:
            st.error("The owner needs to correct EXCLUSIONS_JSON in Secrets: " + str(exc))
            st.stop()
    st.session_state.setdefault("options", {})
    st.caption("Upload four CSV files, create your files, and download. No REDCap connection or reference export required.")
    st.subheader("1. Upload your files")
    labels = ("Rotation schedule", "Updated checklist", "Preceptor match file", "OASIS ME evaluation export")
    uploads = [st.file_uploader(label, type=["csv"], key="source_" + str(i)) for i, label in enumerate(labels)]
    st.caption("Use complete exports for the same rotation. Keep the source student IDs and original OASIS Form Record column. Your mailing filenames and columns are unchanged.")
    if st.checkbox("Show director tools", key="show_director"):
        director_controls(st, uploads[3].getvalue() if uploads[3] else None)
    st.subheader("2. Create and download")
    ready = all(f is not None for f in uploads)
    blobs = [f.getvalue() for f in uploads] if ready else []
    names = [f.name for f in uploads] if ready else ["", "", "", ""]
    settings = settings_from_options(st.session_state["options"], names, st.session_state["rules"])
    signature = digest([[hashlib.sha256(b).hexdigest() for b in blobs], names, asdict(settings)])
    if st.button("Create files", type="primary", disabled=not ready, width="stretch", key="create_files"):
        st.session_state.pop("result", None)
        try:
            with st.spinner("Checking files and preparing downloads…"):
                result = build_bundle(blobs, names, settings)
            st.session_state["result"] = {**result, "signature": signature}
        except (ValueError, UnicodeError, csv.Error) as exc:
            st.error(str(exc))
    result = st.session_state.get("result")
    if result:
        if not ready or result["signature"] != signature:
            st.info("The files or settings changed. Click Create files again for updated downloads.")
        else:
            render_downloads(st, result)
    with st.expander("New REDCap project — one-time setup", expanded=False):
        st.write("Create a new classic project using this complete Data Dictionary. Do not configure repeating instruments. This template is not for your old project.")
        st.download_button("Download new-project Data Dictionary", csv_bytes(redcap_dictionary(), DD_COLUMNS), "redcap_data_dictionary.csv", "text/csv", on_click="ignore")
        st.download_button("Download REDCap setup and import instructions", REDCAP_HELP.encode(), "REDCAP_SETUP.txt", "text/plain", on_click="ignore")
        st.caption("The import uses one ordinary record per summary or source item, linked by student_key. In the new project's Data Import Tool, preserve the supplied record IDs and enable blank overwriting for these generated fields. Review the comparison table before confirming. Student summary rows include readable evaluation and checklist details.")
    st.caption("Nothing is emailed or uploaded automatically. Clinical scores are out of 375, not final clerkship grades. Keep this app and its source code restricted to authorized users.")
    if st.button("Clear files and sign out", key="signout"):
        st.session_state.clear()
        st.rerun()


if __name__ == "__main__":
    main()
