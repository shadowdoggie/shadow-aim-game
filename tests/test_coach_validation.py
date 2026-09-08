"""Regression guards for honest progress, previous goals, and sensitivity reviews."""
import copy
import json
import unittest

from companion.coach import (CoachError, CodexCoach, EFFORT, MODEL, SCHEMA,
    SENSITIVITY_SCHEMA, evaluate_prior_goals, validate_recommendation,
    validate_sensitivity_review)


def recorded(identifier, accuracy=97.0149, acquisition=683.158, overshoot=17.7419):
    return {"record_id": identifier, "drill": "switching", "benchmark_key": "same",
        "quality": {"usable_for_coaching": True},
        "metrics": {name: {"value": value} for name, value in (
            ("accuracy_pct", accuracy), ("acquisition_ms", acquisition), ("overshoot_pct", overshoot))},
        "evidence": [{"id": identifier + ":shots-summary"}, {"id": identifier + ":overshoot-summary"}]}


def original_advice():
    return {"drill": "switching", "cue": "Slow your approach near the target, then click.",
        "goals": [{"metric": "accuracy_pct", "operator": "at_least", "value": 90.2},
            {"metric": "acquisition_ms", "operator": "at_most", "value": 746},
            {"metric": "overshoot_pct", "operator": "at_most", "value": 35}]}


def coaching_context():
    return {"comparison": {"previous_record_id": "baseline", "metrics": {
        "accuracy_pct": {"previous": 90.1639, "current": 97.0149, "delta": 6.851}}},
        "journal": [{"record_id": "baseline", "benchmark_key": "same", "result": original_advice()}],
        "training_context": {"kind": "retest", "source_coaching_record_id": "baseline"}}


def advice():
    return {"summary": "You achieved the previous target; try a slightly quicker switch.",
        "observation": "Accuracy rose from 90.2% to 97.0% while acquisition time fell.",
        "evidence_ids": ["current:shots-summary", "baseline:shots-summary"],
        "cue": "Start the next switch as soon as the hit confirms.", "drill": "switching",
        "parameters": {"duration_s": 45, "target_scale": 1, "speed_scale": 1},
        "success_criterion": "Keep accuracy above 90.2% and overshoots below 35%; try acquisition below 675 ms.",
        "confidence": "medium", "needs_more_data": False,
        "progress_summary": "Accuracy rose from 90.2% to 97.0%, and acquisition fell from 746 to 683 ms.",
        "decision": "progress", "decision_reason": "You met all three prior goals; only the speed target increases.",
        "focus_id": "switching_speed", "goals": [
            {"metric": "acquisition_ms", "operator": "at_most", "value": 675},
            {"metric": "accuracy_pct", "operator": "at_least", "value": 90.2},
            {"metric": "overshoot_pct", "operator": "at_most", "value": 35}],
        "goal_status": "met", "baseline_record_id": "baseline"}


class ProgressValidationTests(unittest.TestCase):
    def setUp(self):
        self.current = recorded("current")
        self.baseline = recorded("baseline", 90.1639, 745.717, 43.75)
        self.practice = recorded("practice", 96.61, 729, 11.11)
        self.history = [self.practice, self.baseline]
        self.context = coaching_context()

    def validate(self, value):
        return validate_recommendation(value, self.current, self.history, self.context)

    def test_achieved_baseline_goals_are_not_failed_by_practice_best(self):
        self.assertEqual(evaluate_prior_goals(self.current, original_advice())["status"], "met")
        result = self.validate(advice())
        self.assertEqual(result["baseline_record_id"], "baseline")
        self.assertEqual(result["goal_status"], "met")
        self.assertEqual(set(result), set(SCHEMA["required"]))

    def test_rejects_silent_switch_from_baseline_to_practice(self):
        value = advice()
        value["baseline_record_id"] = "practice"
        with self.assertRaisesRegex(CoachError, "different baseline"):
            self.validate(value)

    def test_requires_evidence_for_both_sides_of_claimed_progress(self):
        for ids in (["current:shots-summary"], ["baseline:shots-summary"],
                ["current:shots-summary", "practice:shots-summary"]):
            value = advice()
            value["evidence_ids"] = ids
            with self.subTest(ids=ids), self.assertRaisesRegex(CoachError, "both sides"):
                self.validate(value)

    def test_cannot_claim_previously_achieved_goals_failed(self):
        value = advice()
        value["goal_status"] = "not_met"
        with self.assertRaisesRegex(CoachError, "earlier goals"):
            self.validate(value)

    def test_progress_cannot_ratchet_every_achieved_target(self):
        value = advice()
        value["goals"][1]["value"] = 98
        value["goals"][2]["value"] = 15
        with self.assertRaisesRegex(CoachError, "several achieved targets"):
            self.validate(value)

    def test_explicit_source_advice_beats_newer_journal_entry(self):
        unrelated = original_advice()
        unrelated["goals"][0]["value"] = 99
        self.context["journal"].insert(0, {"record_id": "practice", "result": unrelated})
        self.validate(advice())

    def test_goal_evaluation_handles_missing_and_changed_benchmarks(self):
        self.assertEqual(evaluate_prior_goals(self.current, original_advice(), "different")["status"],
            "insufficient_data")
        missing = copy.deepcopy(self.current)
        missing["metrics"]["accuracy_pct"]["value"] = None
        self.assertEqual(evaluate_prior_goals(missing, original_advice())["status"], "insufficient_data")
        partial = recorded("partial", 80, 680, 20)
        self.assertEqual(evaluate_prior_goals(partial, original_advice())["status"], "partly_met")
        self.assertEqual(evaluate_prior_goals(recorded("bad", 80, 800, 50), original_advice())["status"],
            "not_met")

    def test_legacy_journal_never_fabricates_structured_prior_goal(self):
        del self.context["journal"][0]["result"]["goals"]
        value = advice()
        value["goal_status"] = "no_prior_goal"
        self.validate(value)

    def test_rejects_invalid_new_goals_and_decisions(self):
        mutations = [lambda v: v["goals"][0].update(value=float("nan")),
            lambda v: v["goals"][1].update(value=101),
            lambda v: v["goals"][0].update(value=True),
            lambda v: v["goals"][0].update(metric="grip_strength"),
            lambda v: v["goals"][0].update(metric="tracking_error_deg", value=2),
            lambda v: v.update(goals=[]), lambda v: v.update(decision="try_harder"),
            lambda v: v.update(progress_summary=""), lambda v: v.update(decision_reason="")]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(CoachError):
                value = advice()
                mutation(value)
                self.validate(value)

    def test_context_requires_progression_fields(self):
        value = advice()
        for field in ("progress_summary", "decision", "decision_reason", "focus_id", "goals", "goal_status",
                "baseline_record_id"):
            value.pop(field)
        with self.assertRaisesRegex(CoachError, "incomplete"):
            self.validate(value)

    def test_legacy_tracking_quality_does_not_hide_negligible_engagement(self):
        self.current["drill"] = "tracking"
        self.current["evidence"].append({"id": "current:aim-summary", "data": {"observed_s": .7}})
        value = advice()
        value["drill"] = "tracking"
        with self.assertRaisesRegex(CoachError, "overstated"):
            self.validate(value)


class CapturingCoach(CodexCoach):
    def __init__(self, result):
        super().__init__()
        self.result = result
        self.sent = []
        self._temporary = type("Directory", (), {"name": "/tmp", "cleanup": lambda _: None})()

    def connect(self, cancel=None):
        self.state = "ready"

    def _request(self, method, params, cancel=None, timeout=30):
        self.sent.append((method, params))
        if method == "thread/start":
            return {"model": MODEL, "reasoningEffort": EFFORT, "thread": {"id": "thread"}}
        if method == "turn/start":
            self._events.put({"method": "item/completed", "params": {"threadId": "thread",
                "item": {"type": "agentMessage", "text": json.dumps(self.result)}}})
            self._events.put({"method": "turn/completed", "params": {"threadId": "thread",
                "turn": {"id": "turn", "status": "completed"}}})
            return {"turn": {"id": "turn"}}
        return {}


class ContextProtocolTests(unittest.TestCase):
    def test_model_receives_authoritative_comparison_and_evaluated_prior_advice(self):
        coach = CapturingCoach(advice())
        result = coach.recommend(recorded("current"), [recorded("baseline")],
            coaching_context=coaching_context())
        request = json.loads(coach.sent[1][1]["input"][0]["text"])
        self.assertEqual(request["coaching_context"]["comparison"]["previous_record_id"], "baseline")
        self.assertEqual(request["previous_coaching"], original_advice())
        self.assertEqual(request["prior_goal_evaluation"]["status"], "met")
        self.assertEqual(result, advice())
        self.assertEqual(coach.sent[1][1]["model"], MODEL)
        self.assertEqual(coach.sent[1][1]["effort"], "high")


def sensitivity_analysis():
    return {"experiment_id": "experiment", "status": "provisional", "confidence": "medium",
        "allowed_candidate_ids": ["lower"], "strong_candidate_ids": ["lower"],
        "evidence": [{"id": "experiment:comparison", "summary": "Repeated candidate comparisons."}]}


def sensitivity_review():
    return {"summary": "The lower setting is worth a confirmation round.",
        "reason": "Your repeated trials improved accuracy without slower acquisition.",
        "recommended_candidate_id": "lower", "evidence_ids": ["experiment:comparison"],
        "confidence": "medium", "next_step": "Confirm once now and recheck next session."}


class SensitivityReviewTests(unittest.TestCase):
    def test_reuses_exact_model_protocol_with_own_schema(self):
        coach = CapturingCoach(sensitivity_review())
        self.assertEqual(coach.review_sensitivity(sensitivity_analysis()), sensitivity_review())
        thread, turn = coach.sent
        self.assertEqual(thread[1]["model"], MODEL)
        self.assertEqual(turn[1]["effort"], EFFORT)
        self.assertEqual(turn[1]["outputSchema"], SENSITIVITY_SCHEMA)
        self.assertIn("not a diagnosis or a universal optimum", thread[1]["baseInstructions"])

    def test_cannot_choose_untested_or_disallowed_sensitivity(self):
        value = sensitivity_review()
        value["recommended_candidate_id"] = "invented"
        with self.assertRaisesRegex(CoachError, "unsupported by your trials"):
            validate_sensitivity_review(value, sensitivity_analysis())

    def test_cannot_invent_evidence_or_overstate_inconclusive_test(self):
        value = sensitivity_review()
        value["evidence_ids"] = ["invented"]
        with self.assertRaisesRegex(CoachError, "not recorded"):
            validate_sensitivity_review(value, sensitivity_analysis())
        analysis = sensitivity_analysis()
        analysis["confidence"] = "low"
        with self.assertRaisesRegex(CoachError, "inconclusive"):
            validate_sensitivity_review(sensitivity_review(), analysis)
        value = sensitivity_review()
        value["confidence"] = "low"
        validate_sensitivity_review(value, analysis)

    def test_inconclusive_screen_allows_quality_tested_low_confidence_trial(self):
        analysis = sensitivity_analysis()
        analysis.update(status="inconclusive", confidence="low", strong_candidate_ids=[],
            allowed_candidate_ids=["current", "lower", "higher"], recommended_candidate_id="current")
        value = sensitivity_review()
        value["confidence"] = "low"
        result = validate_sensitivity_review(value, analysis)
        self.assertEqual(result["recommended_candidate_id"], "lower")

    def test_medium_confidence_requires_selected_candidate_in_strong_shortlist(self):
        analysis = sensitivity_analysis()
        analysis.update(allowed_candidate_ids=["current", "lower", "higher"], strong_candidate_ids=["higher"])
        with self.assertRaisesRegex(CoachError, "provisional sensitivity trial"):
            validate_sensitivity_review(sensitivity_review(), analysis)
        value = sensitivity_review()
        value["confidence"] = "low"
        validate_sensitivity_review(value, analysis)

    def test_observational_follow_up_cannot_raise_confidence_by_itself(self):
        analysis = sensitivity_analysis()
        analysis.update(confidence="low", strong_candidate_ids=[], follow_up={"candidate_id": "lower"})
        analysis["evidence"].append({"id": "experiment:follow-up", "summary": "Later lower round improved speed and accuracy."})
        value = sensitivity_review()
        value["evidence_ids"].append("experiment:follow-up")
        with self.assertRaisesRegex(CoachError, "inconclusive"):
            validate_sensitivity_review(value, analysis)
        value["confidence"] = "low"
        validate_sensitivity_review(value, analysis)

    def test_incomplete_trials_do_not_start_inference(self):
        coach = CapturingCoach(sensitivity_review())
        with self.assertRaisesRegex(CoachError, "Complete"):
            coach.review_sensitivity({})
        partial = sensitivity_analysis()
        partial["status"] = "incomplete"
        with self.assertRaisesRegex(CoachError, "Complete"):
            coach.review_sensitivity(partial)
        self.assertEqual(coach.sent, [])


if __name__ == "__main__":
    unittest.main()
