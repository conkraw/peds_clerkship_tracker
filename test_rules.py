"""Exclusion rules: synthetic records, no network dependencies."""
import json, unittest
import peds_clerkship_tracker as a
import test_core as core


def rule(**kw):
    return {"record_id":"s001", "student_name":"Alex Example", "evaluator":"Able, Jamie",
            "reason":"Approved correction for testing", "active":True, **kw}


def row(**kw):
    return {"record_id":"s001", "evaluator":"Able, Jamie", "evaluator_email":"jamie@example.invalid",
            "kind":"cas", "form_record":"f1", "rotation_start":core.START,
            "submit_date":"2026-09-05 10:00:00", **kw}


class RuleTests(unittest.TestCase):
    def test_each_original_pair_applies_only_to_that_student_and_preceptor(self):
        for r in a.normalize_rules(a.LEGACY_EXCLUSIONS):
            e=row(record_id=r["record_id"],evaluator=r["evaluator"])
            self.assertTrue(a.exclusion_matches(r,e))
            self.assertFalse(a.exclusion_matches(r,{**e,"record_id":"another_student"}))
            self.assertFalse(a.exclusion_matches(r,{**e,"evaluator":"Someone, Else"}))

    def test_normalized_name_credentials_match(self):
        self.assertTrue(a.exclusion_matches(rule(evaluator="Able, Jamie; MD"),row(evaluator="Jamie Able (MD)")))

    def test_email_mismatch_prevents_name_only_match(self):
        self.assertFalse(a.exclusion_matches(rule(evaluator_email="other@example.invalid"),row()))

    def test_email_only_rule_matches(self):
        self.assertTrue(a.exclusion_matches(rule(evaluator="",evaluator_email="JAMIE@example.invalid"),row()))
        self.assertFalse(a.exclusion_matches(rule(evaluator="",evaluator_email="jamie@example.invalid"),row(evaluator_email="")))

    def test_blank_source_email_falls_back_to_name(self):
        self.assertTrue(a.exclusion_matches(rule(evaluator_email="jamie@example.invalid"),row(evaluator_email="")))

    def test_form_scope(self):
        r=rule(form_record="f1")
        self.assertTrue(a.exclusion_matches(r,row()))
        self.assertFalse(a.exclusion_matches(r,row(form_record="f2")))
        self.assertFalse(a.exclusion_matches(r,row(form_record="")))

    def test_rotation_scope(self):
        self.assertTrue(a.exclusion_matches(rule(rotation_start=core.START),row()))
        self.assertFalse(a.exclusion_matches(rule(rotation_start="2026-07-06"),row()))

    def test_submission_scope(self):
        self.assertTrue(a.exclusion_matches(rule(submit_date="09/05/2026 10:00:00"),row()))
        self.assertFalse(a.exclusion_matches(rule(submit_date="2026-09-05 11:00:00"),row()))

    def test_no_hp_or_handoff_exclusion(self):
        for kind in ("hp","handoff"):
            self.assertFalse(a.exclusion_matches(rule(),row(kind=kind)))

    def test_inactive_does_not_match(self):
        for value in (False,"false","0","no"):
            self.assertFalse(a.exclusion_matches(rule(active=value),row()))

    def test_invalid_rules_fail_closed(self):
        for x in ({},[None],[{}],[rule(record_id="")],[rule(record_id="*")],
                  [rule(record_id="all students")],[rule(evaluator="")],
                  [rule(rotation_start="bad")],[rule(submit_date="bad")],
                  [rule(active="maybe")], {"version":2,"rules":[rule()]}):
            with self.subTest(x=x), self.assertRaises(ValueError):
                a.normalize_rules(x)

    def test_backup_round_trip(self):
        r=a.normalize_rules([rule()])
        self.assertEqual(a.normalize_rules(json.loads(json.dumps({"version":1,"rules":r}))),r)

    def test_same_name_different_emails_have_distinct_rules(self):
        rules=a.normalize_rules([rule(evaluator_email="one@example.invalid"),rule(evaluator_email="two@example.invalid")])
        self.assertEqual(len(rules),2)
        self.assertNotEqual(rules[0]["rule_id"],rules[1]["rule_id"])

    def test_same_identity_conflicting_active_in_backup_rejected(self):
        with self.assertRaises(ValueError):
            a.normalize_rules([rule(),rule(active=False)])

    def test_same_rule_deduplicated(self):
        self.assertEqual(len(a.normalize_rules([rule(),rule()])),1)

    def test_mutation_does_not_modify_defaults(self):
        r=a.normalize_rules(a.LEGACY_EXCLUSIONS); r[0]["active"]=False
        self.assertTrue(a.normalize_rules(a.LEGACY_EXCLUSIONS)[0]["active"])


