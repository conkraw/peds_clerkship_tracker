"""Pediatric Clerkship Tracker — one upload/review/export/sync workflow.

Run: streamlit run peds_clerkship_tracker.py
No code executes network calls or writes student data simply by being imported.
Original rules: user's four Python scripts supplied September 19, 2026.
See README.md and PORTFOLIO_NOTES.md for routine use, data coverage, and REDCap limitations.
"""
from __future__ import annotations

import csv
import hashlib
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
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

VERSION = "1.3.0"
API_URL = "https://redcap.ctsi.psu.edu/api/"
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
REPEAT = ("record_id", "redcap_repeat_instrument", "redcap_repeat_instance")


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


@dataclass
class Settings:
    as_of: str = field(default_factory=lambda: datetime.now(ZoneInfo("America/New_York")).date().isoformat())
    data_through: str = ""
    targets: dict = field(default_factory=lambda: {"cas": 8, "hp": 2, "handoff": 1})
    fallback_rotation_days: int = 26
    grace_days: int = 0
    cohort: str = "all"  # all or active
    confirm_coverage: bool = False  # explicitly attests absent whole-cohort data means zero
    exclusions: list[dict] = field(default_factory=list)
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


def build_roster(schedule: list[dict], checklist: list[dict], matches: list[dict], oasis: list[dict], snapshot: list[dict], settings: Settings, log: Messages) -> list[dict]:
    require(schedule, "Rotation schedule", [("legal_name", "name", "Student Name", "Student"), ("start_date", "Start Date")])
    candidates = [source_identity(r, "student") for r in checklist + matches + oasis]
    parents = [r for r in snapshot if not get(r, "redcap_repeat_instrument")]
    for r in parents:
        base = source_identity(r, "student")
        for nm in (get(r, "legal_name"), get(r, "name")):
            if nm:
                candidates.append({**base, "name": nm})
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
        # Prefer explicitly supplied schedule email, then existing parent contact.
        parent_email = next((norm(get(p, "email")) for p in parents if norm(get(p, "record_id")) == rid and day(get(p, "start_date")) == who["start"] and get(p, "email")), "")
        email = who["email"] or parent_email or (next(iter(emails)) if len(emails) == 1 else "")
        if not email:
            log.add("WARNING", "Rotation schedule", "Missing/ambiguous student email: shown for review, not placed in a send-ready reminder file.", rid)
        if settings.cohort == "active" and not who["start"] <= settings.as_of <= end:
            continue
        roster.append({"record_id": rid, "student_name": display_name(who["name"]), "email": email, "start_date": who["start"], "end_date": end, "end_date_inferred": inferred})
    # A single REDCap record cannot safely represent two concurrent parent rotations.
    counts = Counter(r["record_id"] for r in roster)
    for rid, count in counts.items():
        if count > 1:
            log.add("ERROR", "Rotation schedule", "Multiple rotations use this record_id. Process one rotation at a time or assign distinct REDCap records.", rid)
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


def process(schedule: list[dict], checklist: list[dict], matches: list[dict], oasis: list[dict], snapshot: list[dict] | None = None, settings: Settings | None = None, log: Messages | None = None) -> Run:
    settings, log, snapshot = settings or Settings(), log or Messages(), snapshot or []
    if not day(settings.as_of):
        raise ValueError("A valid as-of date is required.")
    if settings.data_through and settings.data_through > settings.as_of:
        raise ValueError("Data-through date cannot be after the as-of date.")
    if any(not isinstance(v, int) or v < 1 for v in settings.targets.values()):
        raise ValueError("Assessment targets must be positive integers.")
    roster = build_roster(schedule, checklist, matches, oasis, snapshot, settings, log)
    if not roster:
        log.add("ERROR", "Rotation schedule", "No students resolved for the selected scope.")
    persons = [source_identity(r, "faculty") for r in matches + oasis + [r for r in snapshot if get(r, "redcap_repeat_instrument") in {"oasis_eval", "epa", "preceptor_matching"}]]
    people = People(persons)
    ev = normalize_evaluations(oasis, roster, people, settings, log)
    ma = normalize_matches(matches, roster, people, settings, log)
    ce = normalize_checklist(checklist, roster, settings, log)
    cover = {"checklist": coverage(checklist, roster, settings, "Checklist", log), "matches": coverage(matches, roster, settings, "Matches", log), "oasis": coverage(oasis, roster, settings, "OASIS", log)}
    report = build_reports(roster, ce, ma, ev, cover, people, settings, log)
    latest = max((e["submit_date"] for e in ev), default="")
    if latest:
        log.add("INFO", "OASIS ME", f"Latest submission in selected data: {latest}. This is not proof of export completeness.")
    stats = []
    for name, raw, selected in (("Checklist", checklist, ce), ("Preceptor matches", matches, ma), ("OASIS ME", oasis, ev)):
        starts = sorted({source_identity(r, "student")["start"] for r in raw} - {""})
        stats.append({"source": name, "input_rows": len(raw), "selected_entries_or_forms": len(selected), "rotation_starts_present": "; ".join(starts)})
    return Run(settings, roster, ev, ce, ma, report, cover, log, people, stats)

# ---------------------------------------------------------------------------
# REDCap: metadata-aware, sparse writes with stable existing repeat instances.
# Snapshot CSVs are references, not a substitute for a fresh read before writing.
# ---------------------------------------------------------------------------

TRACKING_FIELDS = {
    "cst_checked_at": ("datetime_seconds_ymd", "Tracker processing timestamp"),
    "cst_data_through": ("date_ymd", "Source export coverage date — entered by operator"),
    "cst_cas_matched": ("integer", "Clinical assessments: distinct preceptors matched"),
    "cst_cas_submitted": ("integer", "Clinical assessments: distinct submitted forms"),
    "cst_cas_credit": ("integer", "Clinical assessment requirement credit: unique preceptors requested or submitted"),
    "cst_hp_matched": ("integer", "Observed H&P: distinct preceptors matched"),
    "cst_hp_submitted": ("integer", "Observed H&P: distinct submitted forms"),
    "cst_hp_credit": ("integer", "Observed H&P requirement credit: unique preceptors requested or submitted"),
    "cst_handoff_matched": ("integer", "Handoff: distinct preceptors matched"),
    "cst_handoff_submitted": ("integer", "Handoff: distinct submitted forms"),
    "cst_handoff_credit": ("integer", "Handoff requirement credit: unique preceptors requested or submitted"),
    "cst_scorable": ("integer", "Clinical evaluations eligible for automatic drop rule"),
    "cst_manual_excluded": ("integer", "Manually excluded clinical evaluations"),
    "cst_score_before": ("number", "Clinical score before automatic drop — maximum 375"),
    "cst_score_after": ("number", "Clinical score after automatic drop — maximum 375; not final clerkship grade"),
    "cst_drop_description": ("notes", "Automatic lowest-evaluation exclusion"),
    "cst_missing_count": ("integer", "Missing required encounter categories"),
    "cst_checklist_status": ("", "Encounter checklist review status"),
    "cst_missing_items": ("notes", "Missing encounter categories"),
    "cst_participation_issues": ("notes", "Observing-only, unknown participation, or incomplete encounter categories"),
    "cst_student_reminder": ("notes", "Generated student requirement reminder; not evidence an email was sent"),
    "cst_preceptor_count": ("integer", "Generated preceptor-student reminder rows; not emails sent"),
    "cst_preceptor_reminders": ("notes", "Generated preceptor reminder details; not evidence an email was sent"),
}
DD_COLUMNS = ["Variable / Field Name", "Form Name", "Section Header", "Field Type", "Field Label", "Choices, Calculations, OR Slider Labels", "Field Note", "Text Validation Type OR Show Slider Number", "Text Validation Min", "Text Validation Max", "Identifier?", "Branching Logic (Show field only if...)", "Required Field?", "Custom Alignment", "Question Number (surveys only)", "Matrix Group Name", "Matrix Ranking?", "Field Annotation"]


def tracking_dictionary() -> list[dict]:
    rows = []
    for name, (validation, label) in TRACKING_FIELDS.items():
        rows.append({"Variable / Field Name": name, "Form Name": "clerkship_tracking", "Field Type": "notes" if validation == "notes" else "text", "Field Label": label, "Text Validation Type OR Show Slider Number": "" if validation == "notes" else validation, "Field Note": "Updated by Pediatric Clerkship Tracker. Generated reminders do not indicate delivery." if name == "cst_checked_at" else ""})
    # Append these to the indicated EXISTING instruments, not to clerkship_tracking.
    for name, form in (("oasis_form_record", "oasis_eval"), ("epa_form_record", "epa")):
        rows.append({"Variable / Field Name": name, "Form Name": form, "Field Type": "text", "Field Label": "OASIS Form Record — source submission identifier", "Field Note": "Retains source identity across imports, including multiple same-day submissions."})
    return rows


class Metadata:
    def __init__(self, rows: list[dict]):
        self.fields = {}
        for r in rows:
            name = get(r, "field_name", "Variable / Field Name")
            if not name:
                continue
            self.fields[name] = {
                "form": get(r, "form_name", "Form Name"),
                "type": get(r, "field_type", "Field Type"),
                "validation": get(r, "text_validation_type_or_show_slider_number", "Text Validation Type OR Show Slider Number"),
                "choices": get(r, "select_choices_or_calculations", "Choices, Calculations, OR Slider Labels"),
                "annotation": get(r, "field_annotation", "Field Annotation"),
            }

        for form in {d["form"] for d in self.fields.values() if d["form"]}:
            self.fields.setdefault(form + "_complete", {"form": form, "type": "dropdown", "validation": "", "choices": "0, Incomplete | 1, Unverified | 2, Complete", "annotation": ""})

    def encode(self, name: str, value: Any) -> str:
        definition = self.fields[name]
        field_type, validation = definition["type"], definition["validation"]
        s = text(value)
        if field_type in {"calc", "descriptive", "file", "checkbox"} or any(tag in definition["annotation"].upper() for tag in ("@CALC", "@READONLY")):
            raise ValueError("read-only, file, checkbox, or calculated field is not written")
        if not s:
            return ""
        if field_type in {"radio", "dropdown", "yesno", "truefalse"}:
            choices = {}
            if field_type == "yesno":
                choices = {"1": "yes", "0": "no"}
            elif field_type == "truefalse":
                choices = {"1": "true", "0": "false"}
            else:
                for choice in definition["choices"].split("|"):
                    if "," in choice:
                        code, label = choice.split(",", 1)
                        choices[text(code)] = norm(strip_html(label))
            if s in choices:
                return s
            found = [c for c, label in choices.items() if norm(strip_html(s)) == label]
            if len(found) == 1:
                return found[0]
            if name in {"evaluation", "epa_evaluation", "manual_evaluations"} and kind_of(s):
                kinds = [code for code, label in choices.items() if kind_of(label) == kind_of(s)]
                if len(kinds) == 1:
                    return kinds[0]
            # Numeric 3.0 values from Pandas-based legacy files are codes, not labels.
            if number(s) is not None and number(s).is_integer() and str(int(number(s))) in choices:
                return str(int(number(s)))
            raise ValueError(f"value does not match a REDCap choice for {name}")
        if validation.startswith(("date", "datetime")):
            parsed = dt(s)
            if not parsed:
                raise ValueError(f"invalid date for {name}")
            return parsed.strftime("%Y-%m-%d %H:%M:%S" if "seconds" in validation else "%Y-%m-%d %H:%M" if validation.startswith("datetime") else "%Y-%m-%d")
        if validation in {"integer", "number", "number_1dp", "number_2dp", "number_3dp", "number_4dp"}:
            n = number(s)
            if n is None or validation == "integer" and not n.is_integer():
                raise ValueError(f"invalid numeric value for {name}")
            return str(int(n)) if n.is_integer() else str(n)
        return s

    def decode(self, name: str, value: Any) -> str:
        """Identity keys need human-readable stored evaluation types."""
        definition = self.fields.get(name, {})
        for choice in definition.get("choices", "").split("|"):
            if "," in choice:
                code, label = choice.split(",", 1)
                if text(code) == text(value):
                    return text(label)
        return text(value)


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


def import_candidates(run: Run, include_tracking: bool = True) -> list[dict]:
    result = []
    for e in run.evaluations:
        if e["manual_excluded"]:
            continue  # preserve the old script's manual no-import exclusions
        if e["kind"] == "cas":
            row = {"record_id": e["record_id"], "redcap_repeat_instrument": "oasis_eval", "student": e["student"], "student_email": e["student_email"], "evaluator": e["evaluator"], "evaluator_email": e["evaluator_email"], "evaluation": e["evaluation"], "submit_date": e["submit_date"], "oasis_form_record": e["form_record"]}
            for k in (*DOMAIN_KEYS, "prof", "cas_strengths", "cas_weaknesses", "tot", "iv"):
                if k in e:
                    row[k] = e[k]
        else:
            row = {"record_id": e["record_id"], "redcap_repeat_instrument": "epa", "epa_student": e["student"], "epa_evaluator": e["evaluator"], "epa_evaluator_email": e["evaluator_email"], "epa_evaluation": e["evaluation"], "epa_submit_date": e["submit_date"], "epa_form_record": e["form_record"]}
            for k, v in e.items():
                if k.startswith(("epa_", "epaobh_", "epaobp_", "epaobho_")):
                    row[k] = v
        result.append(row)
    for e in run.entries:
        row = {"record_id": e["record_id"], "redcap_repeat_instrument": "checklist_entry"}
        for k in (*CHECKLIST_MAP, "student_activity", "assisted_or_above", "observed_or_above"):
            if text(e.get(k)):
                row[k] = e[k]
        row["checklist_entry_complete"] = "2"
        result.append(row)
    for m in run.matches:
        row = {"record_id": m["record_id"], "redcap_repeat_instrument": "preceptor_matching"}
        for k in ("faculty_name", "faculty_email", "faculty_external_id", "faculty_username", "manual_evaluations", "eval_period_start_date", "eval_period_end_date", "type_of_association", "classification", "student_activity1"):
            if text(m.get(k)):
                row[k] = m[k]
        result.append(row)
    summary_map = {s["record_id"]: {"record_id": s["record_id"], "redcap_repeat_instrument": "", "redcap_repeat_instance": ""} for s in run.roster}
    for s in run.reports["scores"]:
        if run.coverage["oasis"][s["start_date"]]:
            summary_map[s["record_id"]]["exclude"] = s["exclude"]
    for s in run.roster:
        dates = [e["time_entered"] for e in run.entries if e["record_id"] == s["record_id"] and e["time_entered"]]
        complete = next((c["status"] == "Complete" for c in run.reports["checklist_review"] if c["record_id"] == s["record_id"]), False)
        # The portfolio labels this as the date ALL clinical encounters were
        # submitted. Never fill it from a partially completed checklist.
        if dates and complete:
            summary_map[s["record_id"]]["submitted_ce"] = day(max(dates))
    if include_tracking:
        for row in tracking_rows(run):
            summary_map[row["record_id"]].update(row)
    result.extend(summary_map.values())
    return result


@dataclass
class SyncPlan:
    records: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    clear_records: list[dict] = field(default_factory=list)
    snapshot_fingerprint: str = ""
    metadata_fingerprint: str = ""
    skipped_existing: int = 0


def snapshot_hash(snapshot: list[dict], record_ids: set[str]) -> str:
    rows = [{k: text(v) for k, v in r.items() if text(v)} for r in snapshot if norm(get(r, "record_id")) in record_ids]
    return digest(sorted(rows, key=lambda r: (norm(r.get("record_id")), text(r.get("redcap_repeat_instrument")), text(r.get("redcap_repeat_instance")))))


def normalized_repeat(value: Any) -> str:
    v = number(value)
    if v is None or v < 1 or not v.is_integer():
        return ""
    return str(int(v))


def business_key(row: dict, people: People, metadata: Metadata | None, date_only: bool = False, ignore_form: bool = False) -> tuple | None:
    inst, rid = get(row, "redcap_repeat_instrument"), norm(get(row, "record_id"))
    if not inst:
        return (rid, "")
    kind_field = "evaluation" if inst == "oasis_eval" else "epa_evaluation" if inst == "epa" else "manual_evaluations"
    field_value = get(row, kind_field)
    kind = kind_of(metadata.decode(kind_field, field_value) if metadata else field_value)
    if inst in {"oasis_eval", "epa"}:
        form = get(row, "oasis_form_record" if inst == "oasis_eval" else "epa_form_record")
        if form and not ignore_form:
            return (rid, inst, "form", form)
        person = source_identity(row, "faculty")
        faculty = people.resolve(person)
        submitted = get(row, "submit_date" if inst == "oasis_eval" else "epa_submit_date")
        when = day(submitted) if date_only else stamp(submitted)
        return (rid, inst, faculty, kind, when) if faculty and kind and when else None
    if inst == "preceptor_matching":
        faculty = people.resolve(source_identity(row, "faculty"))
        begin, end = day(get(row, "eval_period_start_date")), day(get(row, "eval_period_end_date"))
        return (rid, inst, faculty, kind, begin, end) if faculty and kind and begin and end else None
    if inst == "checklist_entry":
        item_value = get(row, "item")
        item = key(metadata.decode("item", item_value) if metadata else item_value)
        when = stamp(get(row, "time_entered"))
        # Original/copy is provenance, not a reason to duplicate the same source entry.
        return (rid, inst, when, item) if when and item else None
    return None


def plan_sync(run: Run, snapshot: list[dict], metadata: Metadata | None = None, *, replace_conflicts: bool = False, allow_clears: bool = False, include_tracking: bool = True) -> SyncPlan:
    plan = SyncPlan()
    if run.messages.blocked:
        plan.errors.append("Resolve processing errors before preparing a REDCap import.")
        return plan
    ids = {r["record_id"] for r in run.roster}
    plan.snapshot_fingerprint = snapshot_hash(snapshot, ids)
    plan.metadata_fingerprint = digest(metadata.fields) if metadata else ""
    parents = {norm(get(r, "record_id")): r for r in snapshot if not get(r, "redcap_repeat_instrument")}
    if not parents:
        plan.errors.append("A current full-project REDCap export with parent rows is required. New student records are never guessed or auto-created.")
        return plan
    if any(get(r, "redcap_event_name") for r in snapshot):
        plan.errors.append("This build expects the classic, non-longitudinal project shown in your 0959 export. Event-based projects need explicit event mapping.")
        return plan
    for s in run.roster:
        parent = parents.get(s["record_id"])
        if parent is None or day(get(parent, "start_date")) != s["start_date"]:
            plan.errors.append(f"{s['record_id']}: existing REDCap parent record/rotation does not match the selected schedule.")
    if plan.errors:
        return plan
    allowed = set(metadata.fields) if metadata else set(k for r in snapshot for k in r)
    candidates = import_candidates(run, include_tracking)
    by_business, by_exact_time, by_day = defaultdict(list), defaultdict(list), defaultdict(list)
    max_instance = defaultdict(int)
    known_tuples = set()
    for r in snapshot:
        rid, inst = norm(get(r, "record_id")), get(r, "redcap_repeat_instrument")
        if rid not in ids:
            continue
        if inst:
            repeat = normalized_repeat(get(r, "redcap_repeat_instance"))
            if not repeat:
                plan.errors.append(f"{rid}/{inst}: invalid existing repeat instance.")
                continue
            token = (rid, inst, repeat)
            if token in known_tuples:
                plan.errors.append(f"{rid}/{inst}/{repeat}: duplicate existing repeat identity.")
            known_tuples.add(token)
            max_instance[(rid, inst)] = max(max_instance[(rid, inst)], int(repeat))
        bk = business_key(r, run.people, metadata)
        if bk:
            by_business[bk].append(r)
        if inst in {"oasis_eval", "epa"}:
            by_exact_time[business_key(r, run.people, metadata, ignore_form=True)].append(r)
            by_day[business_key(r, run.people, metadata, date_only=True, ignore_form=True)].append(r)
    incoming_day_count = Counter(business_key(r, run.people, None, date_only=True, ignore_form=True) for r in candidates if get(r, "redcap_repeat_instrument") in {"oasis_eval", "epa"})
    handled = set()
    # Existing submissions absent from the uploaded ME file must not silently
    # disappear from a recomputed exclusion. Protect record-level score summaries.
    source_day_keys = set(incoming_day_count)
    incomplete_eval_ids = set()
    for r in snapshot:
        if get(r, "redcap_repeat_instrument") == "oasis_eval" and norm(get(r, "record_id")) in ids:
            # Manual exclusions are intentionally not imported and not graded.
            manual = any(exclusion_matches(x, {
                "kind": "cas", "record_id": get(r, "record_id"), "evaluator": get(r, "evaluator"),
                "evaluator_email": get(r, "evaluator_email"), "form_record": get(r, "oasis_form_record"),
                "rotation_start": day(get(parents.get(norm(get(r, "record_id")), {}), "start_date")),
                "submit_date": get(r, "submit_date"),
            }) for x in run.settings.exclusions)
            if not manual and business_key(r, run.people, metadata, date_only=True, ignore_form=True) not in source_day_keys:
                incomplete_eval_ids.add(norm(get(r, "record_id")))
    for rid in sorted(incomplete_eval_ids):
        plan.warnings.append(f"{rid}: REDCap contains clinical evaluations absent from the uploaded OASIS source. Grade/exclusion and clinical tracking summaries are withheld; use a complete ME export.")
    optional_missing = set()
    for incoming in candidates:
        incoming = dict(incoming)
        rid, inst = incoming["record_id"], incoming.get("redcap_repeat_instrument", "")
        if not inst and rid in incomplete_eval_ids:
            for name in list(incoming):
                if name == "exclude" or name.startswith(("cst_score", "cst_cas_", "cst_drop", "cst_scorable", "cst_manual")):
                    incoming.pop(name)
        bk = business_key(incoming, run.people, None)
        if bk is None:
            plan.errors.append(f"{rid}/{inst}: source entry lacks a stable identity (preceptor, assessment type, timestamp, or checklist item).")
            continue
        found = by_business.get(bk, [])
        if not found and inst in {"oasis_eval", "epa"}:
            found = by_exact_time.get(business_key(incoming, run.people, None, ignore_form=True), [])
            if not found:
                dk = business_key(incoming, run.people, None, date_only=True, ignore_form=True)
                old = by_day.get(dk, [])
                if old:
                    if len(old) == 1 and incoming_day_count[dk] == 1:
                        old_time = get(old[0], "submit_date" if inst == "oasis_eval" else "epa_submit_date")
                        # Legacy code truncated submission time to 23:59.
                        parsed = dt(old_time)
                        if parsed and (parsed.hour, parsed.minute) in {(23, 59), (0, 0)}:
                            found = old
                        elif get(old[0], "oasis_form_record", "epa_form_record"):
                            found = []  # distinct precise source identities can share a day
                        else:
                            plan.errors.append(f"{rid}/{inst}: same-day source identity is ambiguous; add source Form Record fields or reconcile this row.")
                            continue
                    else:
                        plan.errors.append(f"{rid}/{inst}: multiple same-day evaluations cannot be mapped safely to legacy date-only entries. No instance is guessed.")
                        continue
        if len(found) > 1:
            plan.errors.append(f"{rid}/{inst}: duplicate existing business identity; reconcile before syncing.")
            continue
        existing = found[0] if found else None
        if existing:
            instance = normalized_repeat(get(existing, "redcap_repeat_instance")) if inst else ""
        elif inst:
            max_instance[(rid, inst)] += 1
            instance = str(max_instance[(rid, inst)])
        else:
            plan.errors.append(f"{rid}: no existing parent record.")
            continue
        token = (rid, inst, instance)
        if token in handled:
            plan.errors.append(f"{rid}/{inst}/{instance}: two incoming entries would target the same record.")
            continue
        handled.add(token)
        payload = dict(zip(REPEAT, token))
        clears = dict(payload)
        for name, value in incoming.items():
            if name in REPEAT:
                continue
            if name not in allowed:
                if name.startswith("cst_") or name in {"oasis_form_record", "epa_form_record", "email_351155", "date_97fae7", "start_date_cl"}:
                    optional_missing.add(name)
                elif text(value):
                    plan.errors.append(f"{name}: field is absent from this REDCap project.")
                continue
            if metadata:
                definition = metadata.fields[name]
                if definition["type"] in {"calc", "descriptive", "file", "checkbox"} or any(tag in definition["annotation"].upper() for tag in ("@CALC", "@READONLY")):
                    continue
                if inst and definition["form"] != inst:
                    plan.errors.append(f"{name}: belongs to {definition['form']}, not {inst}; not written.")
                    continue
                if not inst and definition["form"] in {"oasis_eval", "epa", "preceptor_matching", "checklist_entry", "nbme"}:
                    plan.errors.append(f"{name}: cannot write a repeating field to a parent row.")
                    continue
            old_raw = existing.get(name, "") if existing else ""
            try:
                new = metadata.encode(name, value) if metadata else text(value)
                old = metadata.encode(name, old_raw) if metadata else text(old_raw)
            except ValueError as exc:
                plan.errors.append(f"{rid}/{inst}/{name}: {exc}")
                continue
            # Preserve the legacy stored 23:59 timestamp when matching by day;
            # the precise source identity is retained in the new Form Record field.
            if name in {"submit_date", "epa_submit_date"} and existing and day(old) == day(new) and old and old != new:
                continue
            equal_numeric = number(old) is not None and number(new) is not None and number(old) == number(new)
            equal_date = name in {"submit_date", "epa_submit_date", "time_entered", "start_date_cl", "eval_period_start_date", "eval_period_end_date", "submitted_ce"} and stamp(old) and stamp(old) == stamp(new)
            if old == new or equal_numeric or equal_date:
                continue
            # Repeating source values never clear existing information implicitly.
            if not new and inst:
                continue
            if not new and old:
                if allow_clears and (name == "exclude" or name.startswith("cst_")):
                    clears[name] = ""
                    plan.changes.append({"action": "Clear", **payload, "field": name, "old": old, "new": ""})
                else:
                    plan.warnings.append(f"{rid}/{name}: an old value would need clearing; left unchanged unless explicit clearing is enabled.")
                continue
            # Tracker-owned parent values are updates; source row conflicts need opt-in.
            managed = not inst and (name in {"exclude", "submitted_ce"} or name.startswith("cst_"))
            if old and new and not managed and not replace_conflicts:
                plan.changes.append({"action": "Conflict — preserved", **payload, "field": name, "old": old, "new": new})
                continue
            payload[name] = new
            plan.changes.append({"action": "Add" if not existing else "Update", **dict(zip(REPEAT, token)), "field": name, "old": old, "new": new})
        if len(payload) > 3:
            plan.records.append(payload)
        else:
            plan.skipped_existing += 1
        if len(clears) > 3:
            plan.clear_records.append(clears)
    if optional_missing:
        plan.warnings.append("Optional fields not installed; omitted from imports: " + ", ".join(sorted(optional_missing)))
    if metadata is None:
        plan.warnings.append("Snapshot-only plan: field types and choice codes have NOT been validated. Live upload requires project metadata.")
    plan.warnings = list(dict.fromkeys(plan.warnings))
    plan.errors = list(dict.fromkeys(plan.errors))
    return plan


class RedcapClient:
    def __init__(self, url: str, token: str):
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Use the HTTPS REDCap API endpoint without credentials, a query, or a fragment.")
        if not text(token):
            raise ValueError("A REDCap API token is required.")
        self.url, self.token = url, token

    def call(self, content: str, **kwargs) -> Any:
        import requests
        payload = {"token": self.token, "content": content, "format": "json", "returnFormat": "json", **kwargs}
        try:
            response = requests.post(self.url, data=payload, timeout=(15, 120), allow_redirects=False)
        except requests.RequestException as exc:
            # No automatic POST retry: a timeout does not prove an import failed.
            raise RuntimeError("REDCap connection failed or timed out. An import may have reached the server. Refresh the live snapshot and rebuild the preview before any retry.") from exc
        if response.status_code != 200:
            raise RuntimeError(f"REDCap HTTP {response.status_code}. Check endpoint and permissions. No token or request body is logged.")
        try:
            result = response.json()
        except ValueError as exc:
            raise RuntimeError("REDCap returned a non-JSON response. Check API endpoint and permissions.") from exc
        if isinstance(result, dict) and result.get("error"):
            message = text(result["error"]).replace(self.token, "[REDACTED]")
            raise RuntimeError("REDCap: " + message[:1500])
        return result

    def read(self) -> tuple[list[dict], Metadata]:
        rows = self.call("record", type="flat", rawOrLabel="raw", rawOrLabelHeaders="raw", exportCheckboxLabel="false", exportSurveyFields="false", exportDataAccessGroups="false")
        meta_rows = self.call("metadata")
        if not isinstance(rows, list) or not isinstance(meta_rows, list):
            raise RuntimeError("Unexpected REDCap export/metadata response.")
        if not rows or not meta_rows:
            raise RuntimeError("REDCap returned no records or no metadata. Check project and access permissions.")
        return rows, Metadata(meta_rows)

    def upload(self, plan: SyncPlan, run: Run) -> list[dict]:
        if plan.errors:
            raise ValueError("A plan with unresolved errors cannot be uploaded.")
        fresh, metadata = self.read()
        ids = {r["record_id"] for r in run.roster}
        if snapshot_hash(fresh, ids) != plan.snapshot_fingerprint:
            raise RuntimeError("The selected REDCap records changed after preview. Nothing was uploaded; refresh and rebuild the preview.")
        if plan.metadata_fingerprint and digest(metadata.fields) != plan.metadata_fingerprint:
            raise RuntimeError("The REDCap field definitions changed after preview. Nothing was uploaded; refresh and rebuild the preview.")
        # Field definitions must still support exactly the payload being approved.
        for row in plan.records + plan.clear_records:
            for name, value in row.items():
                if name not in REPEAT:
                    if name not in metadata.fields or metadata.encode(name, value) != text(value):
                        raise RuntimeError("The REDCap field definitions changed after preview. Refresh and rebuild the preview.")
        receipts = []
        for behavior, rows in (("normal", plan.records), ("overwrite", plan.clear_records)):
            for offset in range(0, len(rows), 100):
                batch = rows[offset:offset + 100]
                response = self.call("record", action="import", type="flat", overwriteBehavior=behavior, forceAutoNumber="false", dateFormat="YMD", returnContent="count", data=json.dumps(batch, ensure_ascii=False, allow_nan=False))
                receipts.append({"operation": "import", "overwrite_behavior": behavior, "batch": offset // 100 + 1, "rows_submitted": len(batch), "server_response": json.dumps(response)})
        verified, _ = self.read()
        lookup = {(norm(get(r, "record_id")), get(r, "redcap_repeat_instrument"), normalized_repeat(get(r, "redcap_repeat_instance")) if get(r, "redcap_repeat_instrument") else ""): r for r in verified}
        failures = []
        for wanted in plan.records + plan.clear_records:
            actual = lookup.get(tuple(wanted.get(k, "") for k in REPEAT), {})
            for name, value in wanted.items():
                if name in REPEAT:
                    continue
                try:
                    same = metadata.encode(name, actual.get(name, "")) == text(value)
                except ValueError:
                    same = False
                if not same:
                    failures.append({"record_id": wanted["record_id"], "instrument": wanted["redcap_repeat_instrument"], "instance": wanted["redcap_repeat_instance"], "field": name})
        if failures:
            raise RuntimeError("Imports were sent, but read-back verification found differences in " + str(len(failures)) + " fields. Do not resend blindly. Refresh and inspect the preview. First affected fields: " + ", ".join(f['field'] for f in failures[:8]))
        receipts.append({"operation": "read-back verification", "rows_submitted": len(plan.records) + len(plan.clear_records), "server_response": "All submitted fields verified"})
        return receipts


def output_files(run: Run) -> dict[str, bytes]:
    """No imports/tokens/source patient narratives in the reminder-only outputs."""
    checks = [r for r in run.reports["checklist_review"] if r["status"] == "Needs review" and r["email"]]
    students = [r for r in run.reports["student_review"] if r["reminder_needed"] == "Yes" and r["email"]]
    return {
        "student_checklist_review.csv": csv_bytes(checks, CHECKLIST_COLUMNS, flow=True),
        "feedback_reminders_power_automate.csv": csv_bytes(students, STUDENT_COLUMNS, flow=True),
        "preceptor_eval_reminders.csv": csv_bytes(run.reports["preceptor_reminders"], PRECEPTOR_COLUMNS, flow=True),
        "all_student_checklist_status.csv": csv_bytes(run.reports["checklist_review"], CHECKLIST_COLUMNS, flow=True),
        "all_student_requirement_status.csv": csv_bytes(run.reports["student_review"], flow=True),
        "clinical_scores.csv": csv_bytes(run.reports["scores"]),
        "evaluation_audit.csv": csv_bytes(run.evaluations),
        "preceptor_matching_audit.csv": csv_bytes(run.reports["match_audit"]),
        "tracking_status.csv": csv_bytes(tracking_rows(run)),
        "validation_report.csv": csv_bytes(run.messages.rows, ["level", "source", "record_id", "detail"]),
        "source_summary.csv": csv_bytes(run.source_stats),
    }


def zipped(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


# These three rules were explicitly requested by the app owner. This source file
# contains confidential student-specific configuration: keep its repository PRIVATE.
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
EXCLUSION_FIELD = "cst_exclusion_rules"
EXCLUSION_FORM = "clerkship_exclusions"


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


def rules_by_student(rules: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for rule in normalize_rules(rules):
        grouped[rule["record_id"]].append(rule)
    return dict(grouped)


def overlay_saved_rules(defaults: list[dict], stored: dict[str, list[dict]]) -> list[dict]:
    # A saved student's list is authoritative, including an explicitly empty list.
    return normalize_rules([r for r in defaults if norm(r["record_id"]) not in stored] + [r for group in stored.values() for r in group])


def merge_rule_updates(current: list[dict], updates: list[dict]) -> list[dict]:
    merged = {r["rule_id"]: r for r in normalize_rules(current)}
    merged.update({r["rule_id"]: r for r in normalize_rules(updates)})
    return normalize_rules(list(merged.values()))


def changed_rule_students(before: list[dict], after: list[dict]) -> set[str]:
    a, b = rules_by_student(before), rules_by_student(after)
    return {rid for rid in set(a) | set(b) if a.get(rid, []) != b.get(rid, [])}


def load_private_rules(path: Path | None = None) -> list[dict]:
    """Built-in rules work online without a sidecar file. Explicit old backups work too."""
    if path is None:
        return normalize_rules(LEGACY_EXCLUSIONS)
    if not path.exists():
        raise ValueError("The requested exclusions backup does not exist.")
    return normalize_rules(json.loads(path.read_text(encoding="utf-8-sig")))


def exclusion_dictionary() -> list[dict]:
    return [{"Variable / Field Name": EXCLUSION_FIELD, "Form Name": EXCLUSION_FORM,
             "Field Type": "notes", "Field Label": "Clerkship Tracker: saved manual evaluation exclusions",
             "Field Note": "Internal configuration JSON. Edit using the app's Exclusion manager. Do not enable this instrument as a survey or make it repeating.",
             "Field Annotation": "@HIDDEN-SURVEY"}]


@dataclass
class ExclusionSnapshot:
    installed: bool = False
    values: dict[str, str] = field(default_factory=dict)
    stored: dict[str, list[dict]] = field(default_factory=dict)
    parents: dict[str, dict] = field(default_factory=dict)
    form: str = ""


def parse_exclusion_snapshot(rows: list[dict], *, form: str = EXCLUSION_FORM) -> ExclusionSnapshot:
    result = ExclusionSnapshot(installed=True, form=form)
    for row in rows:
        if get(row, "redcap_event_name"):
            raise ValueError("Exclusion persistence requires the non-longitudinal project structure used by this app.")
        if get(row, "redcap_repeat_instrument"):
            if text(row.get(EXCLUSION_FIELD)):
                raise ValueError("The exclusions field must be on a non-repeating instrument.")
            continue
        rid = norm(get(row, "record_id"))
        if not rid:
            raise ValueError("REDCap returned an exclusions row without a record ID.")
        if rid in result.parents:
            raise ValueError("Duplicate parent records; exclusions cannot be loaded safely.")
        result.parents[rid] = row
        result.values[rid] = text(row.get(EXCLUSION_FIELD))
        if result.values[rid]:
            try:
                rules = normalize_rules(json.loads(result.values[rid]))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Saved exclusions for {rid} are invalid. Correct the stored JSON before processing.") from exc
            if any(r["record_id"] != rid for r in rules):
                raise ValueError(f"Saved exclusions for {rid} contain another student's rules.")
            result.stored[rid] = rules
    return result


class ExclusionStore:
    """Small REDCap configuration store; never writes grades or assessment records."""
    def __init__(self, client: RedcapClient):
        self.client = client

    def read(self) -> ExclusionSnapshot:
        meta_rows = self.client.call("metadata")
        if not isinstance(meta_rows, list):
            raise RuntimeError("Unexpected REDCap metadata response.")
        meta = Metadata(meta_rows)
        if EXCLUSION_FIELD not in meta.fields:
            return ExclusionSnapshot()
        definition = meta.fields[EXCLUSION_FIELD]
        if definition["type"] != "notes" or not definition["form"]:
            raise ValueError("cst_exclusion_rules must be a notes field on a non-repeating instrument.")
        meta.encode(EXCLUSION_FIELD, "{}")  # rejects calculated/read-only definitions
        repeating = self.client.call("repeatingFormsEvents")
        if not isinstance(repeating, list):
            raise RuntimeError("Unable to verify REDCap repeating-instrument configuration.")
        if any(get(r, "form_name") == definition["form"] for r in repeating):
            raise ValueError("The exclusions instrument is configured to repeat; make it non-repeating before use.")
        fields = [name for name in ("record_id", "name", "legal_name", "start_date", EXCLUSION_FIELD) if name in meta.fields]
        if "record_id" not in fields:
            raise ValueError("This app requires the existing REDCap record_id field.")
        selected = {f"fields[{i}]": name for i, name in enumerate(fields)}
        rows = self.client.call("record", type="flat", rawOrLabel="raw", rawOrLabelHeaders="raw", exportSurveyFields="false", **selected)
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected REDCap exclusions response.")
        return parse_exclusion_snapshot(rows, form=definition["form"])

    def save(self, rules: list[dict], baseline: ExclusionSnapshot, changed_ids: set[str], *, actor: str = "App operator (not individually authenticated)") -> ExclusionSnapshot:
        rules = normalize_rules(rules)
        if not baseline.installed:
            raise ValueError("Install the exclusions field and reload saved rules before saving.")
        if not changed_ids:
            return baseline
        fresh = self.read()
        if not fresh.installed or fresh.form != baseline.form:
            raise ValueError("The exclusions field configuration changed. Reload before saving.")
        grouped = rules_by_student(rules)
        for rid in changed_ids:
            if rid not in fresh.parents:
                raise ValueError(f"Student {rid} is not an existing REDCap record. No student records were created.")
            if fresh.values.get(rid, "") != baseline.values.get(rid, ""):
                raise ValueError(f"Saved exclusions for {rid} changed in another session. Reload and reconcile before saving.")
        rows = []
        when = datetime.now(ZoneInfo("America/New_York")).isoformat(timespec="seconds")
        for rid in sorted(changed_ids):
            payload = {"version": 1, "rules": grouped.get(rid, []), "updated_at": when, "updated_by": text(actor)}
            rows.append({"record_id": rid, "redcap_repeat_instrument": "", "redcap_repeat_instance": "", EXCLUSION_FIELD: json.dumps(payload, ensure_ascii=False, sort_keys=True)})
        # One explicit import. There is deliberately no automatic retry on timeout.
        self.client.call("record", action="import", type="flat", overwriteBehavior="normal", forceAutoNumber="false", dateFormat="YMD", returnContent="count", data=json.dumps(rows, ensure_ascii=False))
        verified = self.read()
        for rid in changed_ids:
            if rid not in verified.stored or verified.stored[rid] != grouped.get(rid, []):
                raise RuntimeError("Exclusions were sent but read-back verification failed. Reload and inspect before retrying.")
        return verified


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


def invalidate_rule_results(st: Any) -> None:
    for k in ("result", "sync_plan", "upload_receipts", "manual_import"):
        st.session_state.pop(k, None)


def render_exclusion_manager(st: Any, api_url: str, token: str, oasis_file: Any) -> list[dict]:
    context = digest([api_url, token])[:24] if token else "offline"
    state_key = "exclusions_" + context
    defaults = load_private_rules()
    if state_key not in st.session_state:
        state = {"rules": defaults, "reference": defaults, "snapshot": None, "error": "", "notice": "", "attempted": False}
        st.session_state[state_key] = state
    state = st.session_state[state_key]
    if token and not state["attempted"]:
        state["attempted"] = True
        try:
            with st.spinner("Loading saved exclusions from REDCap..."):
                stored = ExclusionStore(RedcapClient(api_url, token)).read()
            state["snapshot"] = stored
            state["rules"] = overlay_saved_rules(defaults, stored.stored)
            state["reference"] = state["rules"]
        except Exception as exc:
            state["error"] = str(exc)
    dirty_ids = changed_rule_students(state["reference"], state["rules"])
    active_count = sum(r["active"] for r in state["rules"])
    st.subheader("Evaluation exclusions")
    with st.expander(f"Manage exclusions · {active_count} active rules", expanded=bool(dirty_ids) or bool(state["error"])):
        st.write("Your three original student–preceptor exclusions are built in. Add a rule, deactivate it to remove the exclusion, or reactivate it later. No Python editing is needed.")
        st.caption("Clinical assessments only. Exclusions are applied before the automatic lowest-score drop. A received excluded evaluation still suppresses reminders; it is retained in the audit and omitted from new assessment imports.")
        if state["notice"]:
            st.info(state.pop("notice"))
            state["notice"] = ""
        if state["error"]:
            st.error(state["error"])
            st.warning("The saved rules could not be checked. Processing is blocked until the connection/rules are corrected and reloaded.")
        elif not token:
            st.info("Built-in rules are active. Added/changed rules are session-only until saved to REDCap. Configure the API token in Secrets for automatic loading each time you open the app.")
        elif not state["snapshot"] or not state["snapshot"].installed:
            st.warning("REDCap is connected, but cst_exclusion_rules has not been added yet. Edits work in this session only. Add the single notes field below, then reload.")
        else:
            st.caption("Saved exclusions loaded from REDCap. Use Save exclusions to REDCap after editing to retain changes across browser sessions and app restarts.")
        if dirty_ids:
            st.warning(f"Unsaved exclusion changes for {len(dirty_ids)} student(s). They apply to this session; save them to retain them online.")
        controls = st.columns(3)
        installed = bool(state["snapshot"] and state["snapshot"].installed)
        if controls[0].button("Save exclusions to REDCap", key="save_rules_" + context, disabled=not (token and installed and dirty_ids and not state["error"])):
            try:
                with st.spinner("Saving only exclusion rules, then checking the saved values..."):
                    saved = ExclusionStore(RedcapClient(api_url, token)).save(state["rules"], state["snapshot"], dirty_ids)
                state["snapshot"] = saved
                # Also incorporate other students' freshly saved rules.
                state["rules"] = overlay_saved_rules(defaults, saved.stored)
                state["reference"] = state["rules"]
                state["notice"] = "Exclusions saved to REDCap and verified. They will load automatically next time. Re-read the REDCap reference before a grading upload, and rebuild results before exporting."
                st.session_state.pop("live", None)
                state["error"] = ""
                invalidate_rule_results(st)
                st.rerun()
            except Exception as exc:
                state["error"] = str(exc)
                st.error(str(exc))
                st.warning("A failed verification/timeout does not prove nothing was saved. Download your backup, discard session edits, and reload before retrying.")
        if controls[1].button("Reload saved exclusions", key="reload_rules_" + context, disabled=not token or bool(dirty_ids)):
            state["attempted"] = False
            state["error"] = ""
            invalidate_rule_results(st)
            st.rerun()
        if controls[2].button("Discard unsaved edits", key="discard_rules_" + context, disabled=not bool(dirty_ids)):
            state["rules"] = normalize_rules(state["reference"])
            state["error"] = ""
            state["attempted"] = False if token else True
            invalidate_rule_results(st)
            st.rerun()
        table = [{"Active": r["active"], "Student": r["student_name"] or r["record_id"], "Student ID": r["record_id"], "Preceptor": r["evaluator"] or r["evaluator_email"], "Form Record": r["form_record"] or "All for pair", "Rotation": r["rotation_start"] or "All rotations", "Reason": r["reason"]} for r in state["rules"]]
        st.dataframe(table, hide_index=True, use_container_width=True)
        st.markdown("**Add an exclusion**")
        choices = {}
        if oasis_file:
            try:
                cache_key = "exclusion_picker_" + hashlib.sha256(oasis_file.getvalue()).hexdigest()
                cached = st.session_state.get("exclusion_picker", {})
                if cached.get("key") != cache_key:
                    choices = exclusion_choices(read_csv_bytes(oasis_file.getvalue(), "OASIS picker", Messages()))
                    st.session_state.exclusion_picker = {"key": cache_key, "choices": choices}
                else:
                    choices = cached["choices"]
            except Exception as exc:
                st.warning(f"Could not prepare the OASIS selector: {exc}")
        mode = st.radio("Choose how to add a rule", ["Select from uploaded OASIS evaluations", "Enter student and preceptor manually"], key="rule_mode_" + context)
        candidate = None
        if mode.startswith("Select"):
            if not choices:
                st.caption("Upload the OASIS ME file above to select a student and preceptor, or use manual entry.")
            else:
                rid = st.selectbox("Student", sorted(choices, key=lambda rid: choices[rid]["name"]), format_func=lambda rid: choices[rid]["name"] + " · " + rid, key="rule_student_" + context)
                preceptors = choices[rid]["preceptors"]
                fk = st.selectbox("Preceptor", sorted(preceptors, key=lambda fk: preceptors[fk]["evaluator"]), format_func=lambda fk: display_name(preceptors[fk]["evaluator"]) + (" · " + preceptors[fk]["email"] if preceptors[fk]["email"] else ""), key="rule_preceptor_" + context + rid)
                p = preceptors[fk]
                scope = st.radio("Scope", ["This submitted evaluation only", "All clinical evaluations for this student–preceptor pair"], key="rule_scope_" + context)
                candidate = {"record_id": rid, "student_name": choices[rid]["name"], "evaluator": p["evaluator"], "evaluator_email": p["email"], "active": True, "origin": "Added in app"}
                if scope.startswith("This"):
                    forms = p["forms"]
                    form_key = st.selectbox("Submitted evaluation", sorted(forms, key=lambda f: forms[f]["submit_date"]), format_func=lambda f: forms[f]["submit_date"] + " · Form " + (forms[f]["form_record"] or "(no ID)") + " · Rotation " + (forms[f]["rotation_start"] or "unknown"), key="rule_form_" + context + rid + fk)
                    chosen = dict(forms[form_key])
                    # A source Form Record is sufficient; don't require identical exported times too.
                    if chosen["form_record"]:
                        chosen["submit_date"] = ""
                    candidate.update(chosen)
                else:
                    st.caption("This scope applies across rotations for this student only. It does not exclude this preceptor's evaluations of other students.")
        with st.form("new_exclusion_" + context):
            if mode.startswith("Enter"):
                candidate = {"record_id": st.text_input("Student record ID"), "student_name": st.text_input("Student name (display only)"), "evaluator": st.text_input("Preceptor name, as shown in OASIS"), "evaluator_email": st.text_input("Preceptor email (optional)"), "form_record": st.text_input("Only this Form Record (optional; blank means all for this pair)"), "rotation_start": st.text_input("Limit to rotation start (optional, YYYY-MM-DD)"), "active": True, "origin": "Added in app"}
            reason = st.text_input("Reason for excluding", key="new_rule_reason_" + context)
            submitted = st.form_submit_button("Add exclusion", disabled=bool(state["error"]) or candidate is None)
            if submitted:
                try:
                    if not text(reason):
                        raise ValueError("Enter a reason for the new exclusion.")
                    candidate["reason"] = reason
                    state["rules"] = merge_rule_updates(state["rules"], [candidate])
                    state["notice"] = "Exclusion added for this session. Save it to REDCap to keep it for future sessions, then rebuild results."
                    invalidate_rule_results(st)
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
        if state["rules"]:
            st.markdown("**Change or remove an existing exclusion**")
            options = {r["rule_id"]: r for r in state["rules"]}
            rid = st.selectbox("Rule to edit", list(options), format_func=lambda k: (options[k]["student_name"] or options[k]["record_id"]) + " — " + display_name(options[k]["evaluator"]) + " — " + (options[k]["form_record"] or "all for pair") + (" [inactive]" if not options[k]["active"] else ""), key="edit_rule_choice_" + context)
            selected = options[rid]
            with st.form("edit_rule_" + context + rid + digest(selected)[:10]):
                enabled = st.checkbox("Exclusion active (uncheck to restore this evaluation to scoring)", value=selected["active"])
                edit_reason = st.text_input("Exclusion reason", value=selected["reason"])
                if st.form_submit_button("Apply rule change", disabled=bool(state["error"])):
                    state["rules"] = merge_rule_updates(state["rules"], [{**selected, "active": enabled, "reason": edit_reason}])
                    state["notice"] = "Rule updated. Save exclusions to REDCap to retain this change, then rebuild results."
                    invalidate_rule_results(st)
                    st.rerun()
        st.download_button("Download exclusions backup", json.dumps({"version": 1, "rules": state["rules"]}, indent=2), "exclusion_rules_backup.json", "application/json", key="rules_backup_" + context)
        with st.container(border=True):
            st.markdown("**Persistence setup and restore a backup**")
            st.write("In REDCap Online Designer, add a non-repeating, internal-only instrument named clerkship_exclusions, then a Notes Box field named cst_exclusion_rules. Do not enable that instrument as a survey. Alternatively, merge this one-row fragment into your full Data Dictionary; never upload it alone as the full project dictionary.")
            st.download_button("Download exclusion field definition", csv_bytes(exclusion_dictionary(), DD_COLUMNS), "exclusion_field_to_append.csv", "text/csv", key="rules_dd_" + context)
            backup = st.file_uploader("Optional exclusions backup to merge", type="json", key="restore_rules_" + context)
            if st.button("Merge uploaded backup", disabled=not backup or bool(state["error"]), key="merge_rules_" + context):
                try:
                    new_rules = normalize_rules(json.loads(backup.getvalue().decode("utf-8-sig")))
                    state["rules"] = merge_rule_updates(state["rules"], new_rules)
                    state["notice"] = "Backup merged into this session. Save exclusions to REDCap to retain the changes."
                    invalidate_rule_results(st)
                    st.rerun()
                except (ValueError, UnicodeDecodeError) as exc:
                    st.error(str(exc))
            st.caption("No local-disk persistence is assumed online. Saving rules updates only cst_exclusion_rules for the changed, already-existing students. It does not delete REDCap assessments or change grades. Avoid simultaneous rule editing from multiple sessions.")
    if state["error"]:
        st.stop()
    return normalize_rules(state["rules"])


# ---------------------------------------------------------------------------
# Streamlit interface: saved exclusion configuration loads after authentication.
# Processing and network writes require explicit buttons. Results are session-local,
# not a global cache shared between users.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Routine workflow. REDCap reads are optional for reminders and never import data.
# Configuration and exclusion editing are kept in the director's workspace.
# ---------------------------------------------------------------------------
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
PORTFOLIO_FIELDS = (
    "distinct_count", "oasissolicit", "obhp_s", "obho_s",
    "obhp_submissions", "obho_submissions", "knowledge_for_practice",
    "clinical_reasoning", "documentation_oralpresentation",
    "communication_ptsfamilies", "communication_care_team", "strengths",
    "weaknesses", "oasis_cas", "obhp", "obho", "exclude",
    "pre_adj_ass_grade", "clin_ass_grade", "submitted_ce", "final_grade",
)


def inferred_export_date(filename: str, as_of: str) -> str:
    """A filename date is a convenient default, not proof of export completeness."""
    for candidate in re.findall(r"(?<!\d)(20\d{6})(?!\d)", filename):
        try:
            value = datetime.strptime(candidate, "%Y%m%d").date().isoformat()
            return min(value, as_of)
        except ValueError:
            continue
    return as_of


def exclusion_state_key(api_url: str, token: str) -> str:
    return "exclusions_" + (digest([api_url, token])[:24] if token else "offline")


def ensure_exclusion_state(session: Any, api_url: str, token: str) -> dict:
    name = exclusion_state_key(api_url, token)
    if name not in session:
        defaults = load_private_rules()
        session[name] = {"rules": defaults, "reference": defaults, "snapshot": None,
                         "error": "", "notice": "", "attempted": False}
    return session[name]


def load_rules_from_reference(rows: list[dict], metadata: Metadata, client: Any) -> ExclusionSnapshot:
    """Reuse the current export instead of asking the operator for a second file."""
    definition = metadata.fields.get(EXCLUSION_FIELD)
    if definition is None:
        return ExclusionSnapshot()
    if definition["type"] != "notes" or not definition["form"]:
        raise ValueError("The saved-exclusions field must be a Notes Box on an internal, non-repeating instrument.")
    metadata.encode(EXCLUSION_FIELD, "{}")
    repeating = client.call("repeatingFormsEvents")
    if not isinstance(repeating, list):
        raise ValueError("Could not verify the saved-exclusions instrument.")
    if any(get(row, "form_name") == definition["form"] for row in repeating):
        raise ValueError("The saved-exclusions instrument must not repeat.")
    return parse_exclusion_snapshot(rows, form=definition["form"])


def automatic_reference(session: Any, api_url: str, token: str, *,
                        refresh: bool = False, client: Any = None,
                        now: datetime | None = None, max_age_seconds: int = 600) -> dict:
    """Session-only cache. Calls READ endpoints, never imports or changes records.

    A failed read is not promoted to a valid empty project. Reminder-only operation
    may still use the four exports; scores/sync are withheld when saved rules cannot
    be verified. Concurrent/unsaved exclusion edits are not overwritten.
    """
    if not token:
        return {"rows": [], "metadata": None, "error": "", "rules_error": "",
                "connected": False, "when": "", "connection": ""}
    now = now or datetime.now(ZoneInfo("America/New_York"))
    connection = digest([api_url, token])
    cached = session.get("simple_reference")
    if not refresh and cached and cached["connection"] == connection:
        age = now.timestamp() - cached.get("checked_at", 0)
        if 0 <= age < max_age_seconds:
            return cached
    state = ensure_exclusion_state(session, api_url, token)
    value = {"rows": [], "metadata": None, "connected": False,
             "when": now.isoformat(timespec="seconds"), "checked_at": now.timestamp(),
             "connection": connection, "error": "", "rules_error": ""}
    try:
        client = client or RedcapClient(api_url, token)
        rows, metadata = client.read()
        if not rows or not metadata.fields:
            raise ValueError("The connected project returned no records or field definitions.")
        value.update(rows=rows, metadata=metadata, connected=True)
        try:
            stored = load_rules_from_reference(rows, metadata, client)
            dirty = changed_rule_students(state["reference"], state["rules"])
            if dirty:
                # Leave the old baseline in place: ExclusionStore.save checks it
                # against current data rather than silently hiding conflicts.
                if state["error"]:
                    raise ValueError("Resolve the saved-exclusion error before using the session's edits.")
            else:
                loaded = overlay_saved_rules(load_private_rules(), stored.stored)
                state.update(snapshot=stored, rules=loaded, reference=loaded,
                             error="", attempted=True)
        except Exception as exc:
            value["rules_error"] = str(exc).replace(token, "[REDACTED]")
            state.update(error=value["rules_error"], attempted=True)
    except Exception as exc:
        value["error"] = str(exc).replace(token, "[REDACTED]")
        value["rules_error"] = "Saved exclusions could not be checked because REDCap was unavailable."
        state.update(error=value["rules_error"], attempted=True)
    session["simple_reference"] = value
    return value


def daily_readiness(run: Run) -> dict[str, bool]:
    valid = bool(run.roster) and not run.messages.blocked
    checks = valid and all(run.coverage["checklist"].get(s["start_date"], False) for s in run.roster)
    assessments = valid and all(
        run.coverage["matches"].get(s["start_date"], False)
        and run.coverage["oasis"].get(s["start_date"], False) for s in run.roster)
    return {REMINDER_NAMES[0]: checks, REMINDER_NAMES[1]: assessments,
            REMINDER_NAMES[2]: assessments}


def reminder_only_files(run: Run) -> dict[str, bytes]:
    """Only the three mailing files; never bundle grades or raw evaluation text."""
    reports = output_files(run)
    return {name: reports[name] for name in REMINDER_NAMES}


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


def portfolio_field_review(metadata: Metadata | None) -> list[dict]:
    """Describe, do not invent, formulas hidden in the supplied portfolio PDF."""
    review = []
    for name in PORTFOLIO_FIELDS:
        d = metadata.fields.get(name) if metadata else None
        calculated = bool(d and (d["type"] == "calc" or "@CALC" in d["annotation"].upper()))
        review.append({"field": name, "present_in_connected_project": bool(d),
                       "form": d["form"] if d else "", "type": d["type"] if d else "",
                       "calculated": calculated,
                       "handling": "Source-derived update subject to preview" if name in {"exclude", "submitted_ce"}
                       else "Leave existing REDCap calculation/manual value unchanged"})
    return review


def safe_daily_plan(run: Run, rows: list[dict], metadata: Metadata | None) -> SyncPlan:
    """No extra tracking instrument is required; use installed fields only."""
    if metadata is None:
        plan = SyncPlan()
        plan.errors.append("REDCap has not been connected. Reminder downloads do not require this step.")
        return plan
    if not all(daily_readiness(run).values()):
        plan = SyncPlan()
        plan.errors.append("The source files do not cover the same selected rotation. Reminder processing and REDCap updating are separate; correct the sources before updating REDCap.")
        return plan
    installed_tracking = bool(set(TRACKING_FIELDS) & set(metadata.fields))
    return plan_sync(run, rows, metadata, replace_conflicts=False, allow_clears=False,
                     include_tracking=installed_tracking)


def prepare_routine_run(source_bytes: list[bytes], snapshot: list[dict], settings: Settings) -> Run:
    if len(source_bytes) != 4 or not all(source_bytes):
        raise ValueError("Upload the rotation schedule, checklist, preceptor matches, and OASIS evaluation export.")
    messages = Messages()
    labels = ("Rotation schedule", "Checklist", "Preceptor matches", "OASIS ME")
    raw = [read_csv_bytes(data, name, messages) for data, name in zip(source_bytes, labels)]
    return process(*raw, snapshot=snapshot, settings=settings, log=messages)


def app_secret(st: Any, name: str, fallback: str = "") -> str:
    try:
        return text(st.secrets.get(name, os.getenv(name, fallback)))
    except (FileNotFoundError, KeyError):
        return os.getenv(name, fallback)


def routine_settings(options: dict, filenames: list[str], rules: list[dict]) -> Settings:
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    as_of = options.get("as_of") or today
    return Settings(as_of=as_of,
                    data_through=options.get("data_through") or inferred_export_date(filenames[3] if len(filenames) > 3 else "", as_of),
                    targets={k: int(options.get(k + "_target", v)) for k, v in {"cas": 8, "hp": 2, "handoff": 1}.items()},
                    fallback_rotation_days=int(options.get("rotation_days", 26)),
                    grace_days=int(options.get("grace_days", 0)), cohort=options.get("cohort", "all"),
                    confirm_coverage=bool(options.get("confirm_coverage", False)),
                    exclusions=normalize_rules(rules),
                    survey_urls={k: options.get(k + "_url", SURVEYS[k]) for k in KINDS},
                    legacy_partial_links=bool(options.get("legacy_partial_links", False)))


def routine_fingerprint(source_bytes: list[bytes], filenames: list[str], settings: Settings,
                        backup_bytes: bytes, connection: str) -> str:
    return digest([[hashlib.sha256(b).hexdigest() for b in source_bytes], filenames,
                   asdict(settings), hashlib.sha256(backup_bytes).hexdigest(), connection])


def render_director_tools(st: Any, api_url: str, token: str, oasis_file: Any) -> None:
    st.subheader("Director tools")
    st.caption("These controls are not part of the routine reminder workflow. Do not enter credentials here.")
    choices = st.radio("Director workspace", ["Exclusions", "Processing options", "Connection status"], horizontal=True, key="director_workspace")
    if choices == "Exclusions":
        render_exclusion_manager(st, api_url, token, oasis_file)
        return
    if choices == "Processing options":
        opts = st.session_state.get("simple_options", {})
        today = datetime.now(ZoneInfo("America/New_York")).date()
        with st.form("director_processing_options"):
            use_today = st.checkbox("Use today's date automatically", value=not bool(opts.get("as_of")))
            as_of = st.date_input("Review date", value=dt(opts.get("as_of")).date() if opts.get("as_of") else today)
            auto_date = st.checkbox("Read the export date from the OASIS filename when available", value=not bool(opts.get("data_through")))
            data_through = st.date_input("Export coverage date override", value=dt(opts.get("data_through")).date() if opts.get("data_through") else as_of)
            cohort = st.selectbox("Students to include", ["All students in the uploaded schedule", "Only students currently on rotation"], index=1 if opts.get("cohort") == "active" else 0)
            counts = {}
            for k, default in {"cas": 8, "hp": 2, "handoff": 1}.items():
                counts[k + "_target"] = int(st.number_input(LABELS[k] + " requirement", min_value=1, max_value=50, value=int(opts.get(k + "_target", default))))
            days = int(st.number_input("Rotation length when end date is absent", 1, 366, int(opts.get("rotation_days", 26))))
            grace = int(st.number_input("Days to wait after a preceptor evaluation period ends", 0, 60, int(opts.get("grace_days", 0))))
            coverage_ok = st.checkbox("I verified that a rotation with no exported rows genuinely has zero entries", value=bool(opts.get("confirm_coverage", False)))
            st.caption("Normally leave this off. Do not use it to override exports from different rotations.")
            urls = {k + "_url": st.text_input(LABELS[k] + " form URL", value=opts.get(k + "_url", SURVEYS[k])) for k in KINDS}
            legacy = st.checkbox("Include old partial-form links that prefill ratings", value=bool(opts.get("legacy_partial_links", False)))
            if st.form_submit_button("Save processing options"):
                if not auto_date and data_through > as_of:
                    st.error("The export date cannot be later than the review date.")
                else:
                    st.session_state.simple_options = {**counts, **urls, "as_of": "" if use_today else as_of.isoformat(), "data_through": "" if auto_date else data_through.isoformat(), "cohort": "active" if cohort.startswith("Only") else "all", "rotation_days": days, "grace_days": grace, "confirm_coverage": coverage_ok, "legacy_partial_links": legacy}
                    invalidate_rule_results(st)
                    st.rerun()
        if st.button("Restore ordinary reminder settings"):
            st.session_state.simple_options = {}
            invalidate_rule_results(st)
            st.rerun()
    else:
        st.write("REDCap credentials are configured once in Streamlit Settings → Secrets. Routine users only upload their four files.")
        st.write("Saved connection configured." if token else "No saved REDCap token. Reminders still work when the four files identify each student.")
        ref = st.session_state.get("simple_reference", {})
        if ref.get("when"):
            st.caption("Last connection check: " + ref["when"])
        for name in ("error", "rules_error"):
            if ref.get(name):
                st.warning(ref[name])
        if st.button("Check connection now", disabled=not bool(token)):
            with st.spinner("Reading the existing project; no changes are being made..."):
                ref = automatic_reference(st.session_state, api_url, token, refresh=True)
            if ref["connected"] and not ref["rules_error"]:
                st.success("Connection works. The reference export will load automatically when reminder files are created.")
            else:
                st.warning(ref["error"] or ref["rules_error"])
        backup = st.file_uploader("Optional backup REDCap export", type="csv", key="director_snapshot")
        if backup:
            st.session_state.backup_reference_bytes = backup.getvalue()
            st.caption("Backup loaded for name matching when the connection is unavailable; not used instead of a fresh read before an upload.")
        if st.button("Remove backup reference"):
            st.session_state.backup_reference_bytes = b""
            st.rerun()
        st.caption("The review-form PDF identifies the fields displayed to students. It is not a current student-data export and does not expose the underlying calculation formulas.")
        if ref.get("metadata"):
            st.dataframe(portfolio_field_review(ref["metadata"]), hide_index=True, use_container_width=True)


# ---------------------------------------------------------------------------
# Manual REDCap imports: no API, no network, and no external write operations.
# Reuse the existing reconciliation logic; never invent repeat-instance numbers
# without a complete current reference. A download is NOT an import receipt.
# ---------------------------------------------------------------------------
MANUAL_REFERENCE_COLUMNS = (
    *REPEAT, "start_date", "evaluator", "evaluator_email", "evaluation",
    "submit_date", "epa_evaluator", "epa_evaluator_email", "epa_evaluation",
    "epa_submit_date", "item", "time_entered", "faculty_name", "faculty_email",
    "manual_evaluations", "eval_period_start_date", "eval_period_end_date",
)
MANUAL_IMPORT_HELP = """MANUAL REDCAP IMPORT — NO API REQUIRED

Use this file in the SAME REDCap project from which the reference was exported.
It is a DATA import, not a Data Dictionary or a project-structure replacement.
The app has not imported, emailed, or otherwise transmitted this file.

1. In REDCap, open Applications > Data Import Tool.
2. Choose real-time import and select the generated redcap_import.csv.
3. Display the data comparison table: YES.
4. Keep the existing record names/IDs; do NOT auto-number or rename records.
5. Overwrite data with blank values: NO. This is essential: blank CSV cells are
   omissions, NOT instructions to delete existing values.
6. Use comma-separated CSV, records in rows, and YMD dates (YYYY-MM-DD).
7. Review REDCap's validation and comparison table before clicking Import Data.
   Stop if it shows unexpected new student records, changed existing source
   values, wrong repeating instances, or field/code errors. Ask the director.
8. After importing, check the affected records. Download a NEW full raw REDCap
   export before preparing another batch. Do not reuse an old reference after
   anyone has edited or imported data into the project.

No concurrent edits/imports should occur between taking the reference export and
finishing this import. Offline files cannot check for changes on the server.
Upload the CSV directly; do not open and resave it in Excel (IDs, dates, and
free-text responses must retain their original content).

Existing manual grading exclusions and the automatic lowest-evaluation drop are
applied by the app. Manually excluded submissions are not newly imported, as in
the original scripts; submissions already in REDCap are NOT deleted. Excluded
submissions still count as received for reminders. Existing REDCap calculations,
NBME values, final grades, and unrelated manual fields are left to REDCap.
Without a Data Dictionary, choice codes/field types cannot be fully checked by
the app. Resolve all REDCap validation errors; never bypass the comparison step.
Saved-exclusion configuration is not changed by this data CSV. Use the exclusion
manager's JSON backup to retain session-only changes when API saving is unavailable.

Reference documentation (University of Colorado REDCap Help Center):
https://redcapucdenver.zendesk.com/hc/en-us/articles/31248111520276-Data-Import-Tool
"""


@dataclass
class ManualImport:
    plan: SyncPlan = field(default_factory=SyncPlan)
    run: Run | None = None
    metadata_checked: bool = False
    reference_fingerprint: str = ""


def validate_manual_reference(rows: list[dict]) -> list[str]:
    """Validate recognizable structure, not unprovable completeness/freshness."""
    if not rows:
        return ["Upload a current full REDCap data export before preparing the import file."]
    columns = {k for row in rows for k in row}
    missing = set(MANUAL_REFERENCE_COLUMNS) - columns
    if missing:
        return ["This looks like a partial report, not a full-project export. Export all "
                "records/fields with raw variable names and all repeating instruments. "
                "Missing columns: " + ", ".join(sorted(missing))]
    if any(get(row, "redcap_event_name") for row in rows):
        return ["This app uses your non-longitudinal project. Event-based exports need an explicit mapping."]
    parent_ids = []
    for row in rows:
        if not get(row, "record_id"):
            return ["The reference contains a blank record ID; no import file was prepared."]
        if not get(row, "redcap_repeat_instrument"):
            if get(row, "redcap_repeat_instance"):
                return ["The reference has a parent row with a repeating instance; check the export."]
            parent_ids.append(norm(get(row, "record_id")))
    if not parent_ids:
        return ["Include the main student records as well as repeating records in the REDCap export."]
    if len(parent_ids) != len(set(parent_ids)):
        return ["The reference has duplicate main student records. Correct the export before importing."]
    return []


def manual_exclusion_rules(rows: list[dict], current: list[dict],
                           baseline: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """Honor saved rules; preserve nonconflicting unsaved director edits.

    Exported saved rules replace their student's defaults exactly as in the live
    workflow. A conflict is not silently decided in favor of stale session data.
    This function does not save rules or mutate any session/reference object.
    """
    current = normalize_rules(current)
    if not any(EXCLUSION_FIELD in row for row in rows):
        return current, ["The export has no saved-exclusion configuration field. The import uses "
                         "the exclusions currently loaded in the app. Retain a JSON backup of any new rules."]
    stored = parse_exclusion_snapshot(rows)
    base = normalize_rules(baseline if baseline is not None else load_private_rules())
    latest = overlay_saved_rules(load_private_rules(), stored.stored)
    dirty = changed_rule_students(base, current)
    old_by, new_by, current_by = map(rules_by_student, (base, latest, current))
    # Limit conflict checking to students actually represented in this export.
    dirty &= set(stored.parents)
    for rid in sorted(dirty):
        if (old_by.get(rid, []) != new_by.get(rid, [])
                and current_by.get(rid, []) != new_by.get(rid, [])):
            raise ValueError("Saved exclusions and unsaved edits both changed for " + rid
                             + ". Reconcile these in Director tools before preparing the import.")
    parent_ids = set(stored.parents)
    combined = [r for r in latest if r["record_id"] not in dirty and r["record_id"] in parent_ids]
    combined += [r for r in current if r["record_id"] in dirty or r["record_id"] not in parent_ids]
    return normalize_rules(combined), []


def prepare_manual_import(source_bytes: list[bytes], reference_rows: list[dict],
                          settings: Settings, metadata: Metadata | None = None,
                          *, baseline_rules: list[dict] | None = None) -> ManualImport:
    """Prepare only a local CSV plan. No API token/client/network is used here."""
    result = ManualImport(metadata_checked=metadata is not None)
    result.plan.errors = validate_manual_reference(reference_rows)
    if result.plan.errors:
        return result
    try:
        rules, rule_warnings = manual_exclusion_rules(reference_rows, settings.exclusions, baseline_rules)
        opts = {**asdict(settings), "exclusions": rules}
        run = prepare_routine_run(source_bytes, reference_rows, Settings(**opts))
        result.run = run
        if not all(daily_readiness(run).values()):
            result.plan.errors.append("The source files do not cover the same selected rotation, "
                                      "or student matching needs review. Correct those checks first.")
            return result
        ids = {r["record_id"] for r in run.roster}
        # Without a dictionary a coded assessment identity cannot be matched.
        # Do not append a supposed 'new' evaluation over an unrecognized old one.
        if metadata is None:
            for row in reference_rows:
                if norm(get(row, "record_id")) not in ids:
                    continue
                inst = get(row, "redcap_repeat_instrument")
                field_name = {"oasis_eval": "evaluation", "epa": "epa_evaluation",
                              "preceptor_matching": "manual_evaluations"}.get(inst)
                if field_name and get(row, field_name) and not kind_of(get(row, field_name)):
                    raise ValueError("The reference uses coded or unrecognized assessment names. "
                                     "Add the current REDCap Data Dictionary using Additional field checks.")
                if inst == "checklist_entry" and number(get(row, "item")) is not None:
                    raise ValueError("The reference uses coded checklist items. Add the REDCap "
                                     "Data Dictionary using Additional field checks.")
        # No new tracking instrument is needed. Preserve the routine update's
        # optional existing tracking fields only when their metadata is known.
        include_tracking = bool(metadata and set(TRACKING_FIELDS) & set(metadata.fields))
        result.plan = plan_sync(run, reference_rows, metadata, replace_conflicts=False,
                                allow_clears=False, include_tracking=include_tracking)
        result.reference_fingerprint = result.plan.snapshot_fingerprint
        result.plan.warnings.extend(rule_warnings)
        if metadata is None:
            result.plan.warnings = [w for w in result.plan.warnings if not w.startswith("Snapshot-only plan:")]
            result.plan.warnings.append("Field types and choice codes are not fully validated without a Data Dictionary. "
                                        "REDCap's Data Import Tool must validate this CSV before you approve it.")
        result.plan.warnings = list(dict.fromkeys(result.plan.warnings))
    except (ValueError, TypeError, KeyError) as exc:
        result.plan.errors.append(str(exc))
    return result


def manual_import_csv(plan: SyncPlan) -> bytes:
    """Sparse update CSV with fixed existing IDs and explicit integer instances.

    Blank cells mean 'leave unchanged'; import with blank-overwrite set to NO.
    Preserve clinical free text verbatim internally: never use flow=True here.
    """
    if plan.errors:
        raise ValueError("Resolve import checks before downloading a REDCap data file.")
    if plan.clear_records:
        raise ValueError("Blank-clearing instructions cannot be included in this import file.")
    if not plan.records:
        raise ValueError("There are no new or changed values to import.")
    seen = set()
    for row in plan.records:
        rid, inst, repeat = (text(row.get(k)) for k in REPEAT)
        if not rid or (inst and (not normalized_repeat(repeat) or normalized_repeat(repeat) != repeat)) or (not inst and repeat):
            raise ValueError("Invalid record or repeating-instance identity in the proposed import.")
        identity = (rid, inst, repeat)
        if identity in seen:
            raise ValueError("Duplicate target identities in the proposed import.")
        seen.add(identity)
        if any(k not in REPEAT and not text(v) for k, v in row.items()):
            raise ValueError("The proposed import contains an explicit blank update; review it with the director.")
    columns = list(REPEAT) + sorted({k for row in plan.records for k in row} - set(REPEAT))
    return csv_bytes(plan.records, columns=columns, flow=False)


def render_manual_redcap(st: Any, result: dict, source_bytes: list[bytes],
                         settings: Settings, fingerprint: str,
                         api_url: str = "", token: str = "") -> None:
    """A self-contained no-API fallback, including after an API-read failure."""
    with st.expander("Download REDCap import file — no API required", expanded=False):
        st.write("Create one CSV to upload yourself in REDCap's Data Import Tool. "
                 "This does not change your Power Automate files or send data to REDCap.")
        upload = st.file_uploader("Current full REDCap export (CSV)", type="csv", key="manual_reference_upload",
                                  help="Use a fresh export of all data, with raw variable names/values and all repeating instances. The review-form PDF and rotation-only report are not data exports.")
        backup = st.session_state.get("backup_reference_bytes", b"")
        ref = result.get("reference", {})
        reference_bytes = upload.getvalue() if upload is not None else backup
        reference_rows = []
        reference_error = ""
        reference_tag = hashlib.sha256(reference_bytes).hexdigest() if reference_bytes else digest([ref.get("connection"), ref.get("when"), ref.get("checked_at")])
        try:
            if reference_bytes:
                cache = st.session_state.get("manual_reference_cache", {})
                if cache.get("tag") == reference_tag:
                    reference_rows = cache["rows"]
                else:
                    reference_rows = read_csv_bytes(reference_bytes, "REDCap reference", Messages())
                    st.session_state.manual_reference_cache = {"tag": reference_tag, "rows": reference_rows}
                st.caption("Using the uploaded reference export. No API is used to prepare this file.")
            elif ref.get("connected") and ref.get("rows"):
                reference_rows = ref["rows"]
                st.caption("Using the reference already read from REDCap when you created the reminders. "
                           "No further API call or API upload is needed for this download.")
            else:
                st.info("Upload a fresh full REDCap export here. It is needed only for the REDCap import file, "
                        "so existing evaluations and repeating-instance numbers can be matched instead of duplicated.")
        except ValueError as exc:
            reference_error = str(exc)
            st.warning(reference_error)
        # Do not silently apply cached metadata from another reference/project.
        metadata = ref.get("metadata") if not reference_bytes and ref.get("connected") else None
        dictionary_bytes = b""
        if st.checkbox("Additional field checks — optional Data Dictionary", value=False, key="manual_dictionary_checks"):
            dd = st.file_uploader("REDCap Data Dictionary (CSV)", type="csv", key="manual_dictionary_upload")
            if dd is not None:
                dictionary_bytes = dd.getvalue()
                try:
                    metadata = Metadata(read_csv_bytes(dictionary_bytes, "Data Dictionary", Messages()))
                    if not metadata.fields:
                        raise ValueError("This file does not contain REDCap field definitions.")
                except ValueError as exc:
                    reference_error = str(exc)
                    st.warning(reference_error)
        confirmed = st.checkbox("This is a current, complete export from the destination project; "
                                "no records have changed since it was exported.", key="manual_reference_confirmed")
        state = ensure_exclusion_state(st.session_state, api_url, token)
        local_key = digest([fingerprint, reference_tag, metadata.fields if metadata else {}, hashlib.sha256(dictionary_bytes).hexdigest(),
                            state["reference"], settings.exclusions])
        if st.button("Prepare REDCap import file", key="manual_prepare", disabled=not reference_rows or not confirmed or bool(reference_error)):
            manual = prepare_manual_import(source_bytes, reference_rows, settings, metadata,
                                           baseline_rules=state["reference"])
            st.session_state.manual_import = {"key": local_key, "value": manual}
        saved = st.session_state.get("manual_import")
        if saved and saved.get("key") != local_key:
            st.info("The reference, files, or exclusions changed. Prepare a new REDCap import file.")
            saved = None
        if saved:
            manual = saved["value"]
            plan = manual.plan
            if plan.errors:
                st.warning("No import CSV is available until these checks are resolved. Reminder downloads are unaffected.")
                for error in plan.errors:
                    st.write(error)
            else:
                summary = Counter(r.get("redcap_repeat_instrument") or "student summary" for r in plan.records)
                st.write(f"{len(plan.records)} rows contain new or changed values. Existing matching entries retain their instance numbers.")
                if summary:
                    st.dataframe([{"Record type": k, "Rows in file": v} for k, v in summary.items()], hide_index=True, use_container_width=True)
                conflicts = sum(r["action"] == "Conflict — preserved" for r in plan.changes)
                if conflicts:
                    st.warning(f"{conflicts} existing source values differ. They are omitted from the update and left unchanged; the director should review them.")
                for message in plan.warnings:
                    st.caption(message)
                if not manual.metadata_checked:
                    st.info("This CSV still needs REDCap's field/code validation. Review its comparison table before approving the import.")
                st.warning("In REDCap: keep the existing record IDs, set Overwrite data with blank values to NO, "
                           "and review the comparison table. Get a new reference export after any intervening import or edit.")
                if plan.records:
                    try:
                        payload = manual_import_csv(plan)
                        st.download_button("Download REDCap import CSV", payload, "redcap_import.csv", "text/csv",
                                           key="manual_download", disabled=not confirmed or bool(reference_error))
                    except ValueError as exc:
                        st.warning(str(exc))
                else:
                    st.success("No new values to import from these files. No empty import file is needed.")
                st.download_button("Download import instructions", MANUAL_IMPORT_HELP.encode("utf-8"),
                                   "redcap_import_instructions.txt", "text/plain", key="manual_instructions")
                if st.checkbox("Show import details for the director", key="manual_show_details"):
                    st.dataframe(plan.changes, hide_index=True, use_container_width=True)
                    st.download_button("Download import change report", csv_bytes(plan.changes),
                                       "redcap_manual_change_report.csv", "text/csv", key="manual_changes")
        st.caption("Downloading is not importing. No API connection, API token, or new tracking instrument is required for this manual path. "
                   "Existing REDCap final-grade calculations and unrelated manual values are not replaced. "
                   "New session-only exclusion rules still need a JSON backup when API saving is unavailable.")


def render_routine_redcap(st: Any, result: dict, api_url: str, token: str,
                          source_bytes: list[bytes], settings: Settings,
                          fingerprint: str) -> None:
    if not token:
        return
    with st.expander("Update REDCap — optional", expanded=False):
        st.write("Reminder files are ready independently of this step. Use this section only to update the existing student records.")
        st.caption("The app reads the current project automatically. It does not ask you to export a database or upload a Data Dictionary.")
        if st.button("Review REDCap update", disabled=not all(daily_readiness(result["run"]).values()), key="simple_prepare_redcap"):
            st.session_state.pop("sync_plan", None)
            try:
                with st.spinner("Reading REDCap and checking the proposed update..."):
                    ref = automatic_reference(st.session_state, api_url, token, refresh=True)
                    if not ref["connected"]:
                        raise ValueError("REDCap could not be reached. Your reminder files are unaffected. Ask the director to check the saved connection.")
                    if ref["rules_error"]:
                        raise ValueError("Saved exclusions could not be verified. Your reminder files are unaffected; the director needs to review the exclusion settings.")
                    state = ensure_exclusion_state(st.session_state, api_url, token)
                    if digest(normalize_rules(state["rules"])) != digest(settings.exclusions):
                        raise ValueError("Saved exclusions changed. Click Create reminder files again before preparing the REDCap update.")
                    fresh_run = prepare_routine_run(source_bytes, ref["rows"], settings)
                    plan = safe_daily_plan(fresh_run, ref["rows"], ref["metadata"])
                    st.session_state.sync_plan = {"plan": plan, "run": fresh_run, "key": fingerprint,
                                                  "connection": digest([api_url, token])}
            except Exception as exc:
                st.warning(str(exc).replace(token, "[REDACTED]"))
        saved = st.session_state.get("sync_plan")
        if saved and (saved.get("key") != fingerprint or saved.get("connection") != digest([api_url, token])):
            st.info("The inputs changed. Review the REDCap update again.")
            saved = None
        if saved:
            plan = saved["plan"]
            if plan.errors:
                st.warning("This REDCap update needs the director's review. Reminder downloads are not affected.")
                if st.checkbox("Show details for the director", key="simple_show_sync_errors"):
                    for error in plan.errors:
                        st.write(error)
            else:
                summary = Counter(r.get("redcap_repeat_instrument") or "student summary" for r in plan.records)
                st.write(f"{len(plan.records)} records to add or update. Existing matching records will not be duplicated.")
                if summary:
                    st.dataframe([{"Record type": k, "To update": v} for k, v in summary.items()], hide_index=True, use_container_width=True)
                conflicts = sum(c["action"] == "Conflict — preserved" for c in plan.changes)
                if conflicts:
                    st.warning(f"{conflicts} existing values differ from the source. They will be left unchanged for the director to review.")
                if any("absent from" in w for w in plan.warnings):
                    st.warning("REDCap contains evaluations absent from this OASIS file. Score/exclusion summaries are withheld; use a complete OASIS export for grading.")
                if any("clearing" in w for w in plan.warnings):
                    st.warning("An old summary value would need to be cleared. This update preserves it; the director should review the details.")
                if st.checkbox("Show proposed field changes", key="simple_show_sync_changes"):
                    st.dataframe(plan.changes, hide_index=True, use_container_width=True)
                    for message in plan.warnings:
                        st.caption(message)
                    st.download_button("Download change report", csv_bytes(plan.changes), "redcap_change_audit.csv", "text/csv", key="simple_change_report")
                approved = st.checkbox("I approve this update to the selected students' REDCap records", key="approve_simple_" + digest([fingerprint, plan.changes])[:12])
                if st.button("Confirm update to REDCap", disabled=not approved or not bool(plan.records), key="simple_confirm_redcap"):
                    try:
                        with st.spinner("Updating REDCap and verifying the saved values..."):
                            receipts = RedcapClient(api_url, token).upload(plan, saved["run"])
                        st.session_state.upload_receipts = receipts
                        st.session_state.pop("simple_reference", None)
                        st.success("REDCap update saved and verified.")
                    except Exception as exc:
                        st.warning(str(exc).replace(token, "[REDACTED]"))
                        st.warning("Some records may already have been saved. Review a fresh REDCap update before trying again.")
                        st.session_state.pop("simple_reference", None)
                    finally:
                        st.session_state.pop("sync_plan", None)
        if st.session_state.get("upload_receipts"):
            st.download_button("Download update receipt", csv_bytes(st.session_state.upload_receipts), "redcap_import_receipt.csv", "text/csv", key="simple_receipt")
        st.caption("The existing evaluation, H&P/handoff, checklist, and matching instruments are used. REDCap calculations, final grades, NBME, and unrelated manually entered fields are not replaced. No new tracking instrument is required.")


def main() -> None:
    import hmac
    import streamlit as st

    st.set_page_config(page_title="Pediatric Clerkship Reminders", page_icon="📋", layout="wide")
    password = app_secret(st, "APP_PASSWORD")
    trusted = norm(app_secret(st, "TRUSTED_HOST_AUTH", "false")) == "true"
    if not password and not trusted:
        st.title("Pediatric Clerkship Reminders")
        st.info("The director needs to finish the one-time app sign-in setup in Streamlit Secrets.")
        st.stop()
    if password and (len(password) < 16 or password.startswith("CHANGE_ME")):
        st.error("Set a unique APP_PASSWORD of at least 16 characters in Streamlit Secrets.")
        st.stop()
    auth_key = digest(password) if password else "trusted-host"
    if password and st.session_state.get("authenticated") != auth_key:
        st.title("Pediatric Clerkship Reminders")
        with st.form("sign_in"):
            entered = st.text_input("App password", type="password")
            login = st.form_submit_button("Sign in")
        if login:
            if hmac.compare_digest(entered.encode("utf-8"), password.encode("utf-8")):
                st.session_state.authenticated = auth_key
                st.rerun()
            else:
                st.error("Incorrect password.")
        st.stop()

    api_url = app_secret(st, "REDCAP_API_URL", API_URL)
    token = app_secret(st, "REDCAP_API_TOKEN")
    connection = digest([api_url, token]) if token else ""
    with st.sidebar:
        st.caption("Clerkship reminder tools")
        if st.button("Start over / clear uploaded files", key="simple_reset"):
            keep = {k: v for k, v in st.session_state.items() if k in {"authenticated", "simple_options"} or k.startswith("exclusions_")}
            st.session_state.clear()
            st.session_state.update(keep)
            st.rerun()
        if password and st.button("Sign out", key="simple_signout"):
            st.session_state.clear()
            st.rerun()
        director = st.checkbox("Show director tools", value=False, key="show_director_tools")
        st.caption("v" + VERSION)

    st.title("Pediatric Clerkship Reminders")
    st.write("Upload the four files, create the reminders, then download the files for Power Automate.")
    st.caption("Use exports for the same rotation. This app creates files; it does not send email.")
    st.subheader("1 · Upload files")
    left, right = st.columns(2)
    with left:
        schedule_file = st.file_uploader("Rotation schedule", type="csv", key="schedule_upload")
        checklist_file = st.file_uploader("Updated checklist", type="csv", key="checklist_upload")
    with right:
        match_file = st.file_uploader("Preceptor match file", type="csv", key="match_upload")
        oasis_file = st.file_uploader("OASIS ME evaluation export", type="csv", key="oasis_upload")
    uploads = [schedule_file, checklist_file, match_file, oasis_file]
    source_bytes = [f.getvalue() if f is not None else b"" for f in uploads]
    filenames = [f.name if f is not None else "" for f in uploads]
    if not token:
        with st.expander("Optional reference file — only needed when a student's details are missing", expanded=False):
            st.caption("A full REDCap export, such as the 0959 file, can supply missing student IDs or emails. Your director can connect REDCap once so this loads automatically.")
            backup = st.file_uploader("REDCap reference export", type="csv", key="simple_backup")
            st.session_state.backup_reference_bytes = backup.getvalue() if backup is not None else b""
    backup_bytes = st.session_state.get("backup_reference_bytes", b"")
    state = ensure_exclusion_state(st.session_state, api_url, token)
    settings = routine_settings(st.session_state.get("simple_options", {}), filenames, state["rules"])
    fingerprint = routine_fingerprint(source_bytes, filenames, settings, backup_bytes, connection)

    if st.button("Create reminder files", type="primary", disabled=not all(source_bytes), key="simple_create"):
        st.session_state.pop("result", None)
        st.session_state.pop("sync_plan", None)
        st.session_state.pop("upload_receipts", None)
        st.session_state.pop("manual_import", None)
        try:
            with st.spinner("Checking the files and preparing reminders..."):
                ref = automatic_reference(st.session_state, api_url, token, refresh=True)
                reference_rows = ref["rows"]
                if not ref["connected"] and backup_bytes:
                    reference_rows = read_csv_bytes(backup_bytes, "Reference export", Messages())
                state = ensure_exclusion_state(st.session_state, api_url, token)
                # An uploaded backup can carry saved rules offline; it is not a
                # replacement for a fresh rule check when a connection is configured.
                if not token and reference_rows and any(EXCLUSION_FIELD in r for r in reference_rows):
                    if not changed_rule_students(state["reference"], state["rules"]):
                        try:
                            saved_rules = parse_exclusion_snapshot(reference_rows)
                            state["rules"] = overlay_saved_rules(load_private_rules(), saved_rules.stored)
                            state["reference"] = state["rules"]
                            state["error"] = ""
                        except ValueError as exc:
                            ref["rules_error"] = str(exc)
                            state["error"] = str(exc)
                settings = routine_settings(st.session_state.get("simple_options", {}), filenames, state["rules"])
                fingerprint = routine_fingerprint(source_bytes, filenames, settings, backup_bytes, connection)
                run = prepare_routine_run(source_bytes, reference_rows, settings)
                st.session_state.result = {"run": run, "files": output_files(run), "fingerprint": fingerprint,
                                           "reference": ref, "rules_verified": not bool(ref["rules_error"]) and (not bool(token) or bool(ref["connected"]))}
        except Exception as exc:
            message = str(exc).replace(token, "[REDACTED]") if token else str(exc)
            st.error("The files could not be processed. Check that each upload is the correct CSV export.")
            with st.expander("What needs attention", expanded=True):
                st.write(message)

    result = st.session_state.get("result")
    current = bool(result and result.get("fingerprint") == fingerprint)
    if result and not current:
        st.info("A file or setting changed. Click Create reminder files again to update the downloads.")
    if current:
        run = result["run"]
        readiness = daily_readiness(run)
        if result["reference"].get("error"):
            st.info("REDCap is unavailable. Reminder files are being built from the uploads; automatic REDCap updating and verified scoring are unavailable until the connection is restored.")
        elif not result["rules_verified"]:
            st.info("Saved grading exclusions need the director's review. Reminder matching is unaffected, but score downloads and REDCap updating are withheld.")
        if run.messages.blocked:
            st.error("Some student details could not be matched safely. Review the items below; no reminders will be released until they are corrected.")
        elif not all(readiness.values()):
            st.warning("One or more files do not cover the rotation in your schedule. The affected reminder downloads are withheld—not treated as zero completed work.")
        else:
            st.success(f"Reminder files prepared for {len(run.roster)} students.")
        if settings.data_through < settings.as_of:
            st.warning(f"This OASIS export is dated {settings.data_through}. Assessments submitted after that export may be missing. Use a fresh export before sending reminders.")
        missing_emails = sum(not s["email"] for s in run.roster)
        if missing_emails:
            st.warning(f"{missing_emails} students have no uniquely matched email address and are omitted from the mailing files. Their status is retained in the review table.")
        missing_preceptor_emails = sum(r["status"] == "Pending" and not r["faculty_email"] for r in run.reports["match_audit"])
        if missing_preceptor_emails:
            st.warning(f"{missing_preceptor_emails} pending preceptor requests have no email address and are omitted from the mailing file. Ask the director to review the matching details.")
        st.subheader("2 · Download reminder files")
        counts = [sum(r["status"] == "Needs review" and bool(r["email"]) for r in run.reports["checklist_review"]),
                  sum(r["reminder_needed"] == "Yes" and bool(r["email"]) for r in run.reports["student_review"]),
                  len(run.reports["preceptor_reminders"])]
        titles = ["Student encounter-log reminders", "Student evaluation, H&P and handoff reminders", "Preceptor reminders"]
        for col, name, title, count in zip(st.columns(3), REMINDER_NAMES, titles, counts):
            with col:
                st.markdown("**" + title + "**")
                st.caption(f"{count} reminder rows" if readiness[name] else "Needs matching source files")
                st.download_button("Download CSV", result["files"][name], name, "text/csv", key="daily_" + name, disabled=not readiness[name])
        st.download_button("Download all three reminder files", zipped(reminder_only_files(run)), "power_automate_reminders.zip", "application/zip", disabled=not all(readiness.values()), key="simple_all_reminders")
        st.caption("A file with zero rows has no sendable reminders; check any missing-email or matching warnings. Preceptor rows combine missing assessment types; duplicate matches never create extra rows.")
        with st.expander("Check students and preview the reminders", expanded=run.messages.blocked):
            st.dataframe(run.roster, hide_index=True, use_container_width=True)
            for name, report in (("Encounter status", "checklist_review"), ("Student assessment status", "student_review"), ("Preceptor reminders", "preceptor_reminders")):
                st.markdown("**" + name + "**")
                st.dataframe(run.reports[report], hide_index=True, use_container_width=True)
            with st.container():
                st.markdown("**File checks and matching details**")
                st.dataframe(run.messages.rows, hide_index=True, use_container_width=True)
                st.dataframe(run.source_stats, hide_index=True, use_container_width=True)
            st.download_button("Download file-check report", result["files"]["validation_report.csv"], "validation_report.csv", "text/csv", key="simple_validation")
        render_manual_redcap(st, result, source_bytes, settings, fingerprint, api_url, token)
        if result["rules_verified"]:
            render_routine_redcap(st, result, api_url, token, source_bytes, settings, fingerprint)
    elif not result:
        st.caption("No full-database export or Data Dictionary is required just to create reminders when the students can be identified from the four files. REDCap reads happen automatically when your director has configured the connection.")

    if director:
        st.divider()
        render_director_tools(st, api_url, token, oasis_file)
        # Re-read because editing exclusions can invalidate the former results.
        result = st.session_state.get("result")
        if result and result.get("fingerprint") == fingerprint:
            run = result["run"]
            with st.expander("Clinical scores and evaluation audit"):
                if result["rules_verified"] and not run.messages.blocked:
                    st.dataframe(run.reports["scores"], hide_index=True, use_container_width=True)
                    st.download_button("Download clinical scores", result["files"]["clinical_scores.csv"], "clinical_scores.csv", "text/csv", key="simple_scores")
                    st.download_button("Download evaluation audit", result["files"]["evaluation_audit.csv"], "evaluation_audit.csv", "text/csv", key="simple_audit")
                else:
                    st.warning("Restore the REDCap/rule connection or correct the inputs before using clinical scores.")
            with st.expander("Files for older, separate Power Automate preceptor flows"):
                st.caption("Use this package only with the older separate CAS and observed-H&P flows. Do not also send the combined preceptor file for the same run. The normal three-file package keeps the combined layout from version 1.1.")
                st.download_button("Download legacy-flow reminder files", zipped(legacy_power_automate_files(run)), "legacy_power_automate_reminders.zip", "application/zip", disabled=not all(daily_readiness(run).values()), key="simple_legacy")
                st.caption("The legacy package contains the original clinical and observed-H&P preceptor CSV layouts. It does not include preceptor handoff reminders; those remain in the combined file and student requirement reminders.")


if __name__ == "__main__":
    main()
