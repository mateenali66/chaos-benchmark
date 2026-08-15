#!/usr/bin/env python3
"""
Offline tests for run-campaign.py's LLMStrategy (Component 3, ML
fault-selection study). Zero network, zero cluster, zero AWS: every test
drives LLMStrategy through a MockLLMClient implementing the same
`.invoke(prompt, temperature, max_tokens) -> response` shape as
llmclient.BedrockClient, so nothing here ever imports boto3 or touches
Bedrock.

Runnable directly (no pytest dependency assumed):
  python3 scripts/tests/test_llm_strategy.py
or via pytest if it happens to be installed:
  pytest scripts/tests/test_llm_strategy.py
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    # run-campaign.py does bare `import chaoslib` / `import llmclient`,
    # so scripts/ must be on sys.path before it's loaded below.
    sys.path.insert(0, str(SCRIPTS_DIR))


def _load_run_campaign():
    """run-campaign.py's filename has a hyphen and cannot be `import`-ed
    directly (same reason chaoslib.py's module docstring gives for why it
    exists), so load it by file path instead."""
    spec = importlib.util.spec_from_file_location("run_campaign", SCRIPTS_DIR / "run-campaign.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run_campaign = _load_run_campaign()


@dataclass
class FakeLLMResponse:
    """Mirrors llmclient.LLMResponse's shape (text/model_id/raw) without
    importing llmclient (which lazily needs boto3 only inside
    BedrockClient, so importing llmclient itself is fine too -- this class
    just avoids the dependency entirely for clarity)."""
    text: str
    model_id: str
    raw: dict


class MockLLMClient:
    """Zero-network stand-in for llmclient.BedrockClient. `responses` is a
    list where each entry is either a string (becomes the completion text)
    or an Exception instance (raised instead of returning, simulating a
    request/API failure). Each call to invoke() consumes the next entry; if
    invoke() is called more times than len(responses), the last entry
    repeats."""

    def __init__(self, responses, model_id="mock-model-id"):
        self.responses = list(responses)
        self.model_id = model_id
        self.calls = []

    def invoke(self, prompt, temperature, max_tokens):
        self.calls.append({"prompt": prompt, "temperature": temperature, "max_tokens": max_tokens})
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        item = self.responses[idx]
        if isinstance(item, Exception):
            raise item
        return FakeLLMResponse(text=item, model_id=self.model_id, raw={"mock": True, "call": len(self.calls)})


def valid_json(candidate_id: str, rationale: str = "test rationale") -> str:
    return json.dumps({"candidate_id": candidate_id, "rationale": rationale})


class LLMStrategyTestBase(unittest.TestCase):
    def setUp(self):
        self.fault_space = run_campaign.load_fault_space()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.campaign_dir = Path(self.tmpdir.name)

    def tearDown(self):
        self.tmpdir.cleanup()

    def make_strategy(self, client, fallback_seed=0):
        return run_campaign.LLMStrategy(
            arm="llm-claude",
            client=client,
            campaign_dir=self.campaign_dir,
            model_id="test-model-id",
            temperature=0.2,
            max_tokens=1024,
            fallback_seed=fallback_seed,
        )

    def read_transcript(self):
        path = self.campaign_dir / "llm-transcript.jsonl"
        if not path.exists():
            return []
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]


class TestValidSelection(LLMStrategyTestBase):
    def test_valid_first_attempt_is_accepted(self):
        target_id = self.fault_space[5].id
        client = MockLLMClient([valid_json(target_id, "picking this one")])
        strategy = self.make_strategy(client)

        selected = strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(selected.id, target_id)
        self.assertEqual(strategy.fallback_count, 0)
        self.assertEqual(len(client.calls), 1)

        transcript = self.read_transcript()
        self.assertEqual(len(transcript), 1)
        self.assertEqual(transcript[0]["validation_outcome"], "valid")
        self.assertEqual(transcript[0]["selected_candidate_id"], target_id)
        self.assertFalse(transcript[0]["fallback"])
        # model_id in the transcript comes from the response object (what
        # the API actually reported), not assumed from the strategy config.
        self.assertEqual(transcript[0]["model_id"], client.model_id)
        self.assertIn("prompt", transcript[0])
        self.assertIn(target_id, transcript[0]["prompt"])

    def test_valid_on_second_attempt_after_one_invalid(self):
        target_id = self.fault_space[10].id
        client = MockLLMClient([
            valid_json("NOT-A-REAL-ID"),
            valid_json(target_id),
        ])
        strategy = self.make_strategy(client)

        selected = strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(selected.id, target_id)
        self.assertEqual(strategy.fallback_count, 0)
        self.assertEqual(len(client.calls), 2)

        transcript = self.read_transcript()
        self.assertEqual(len(transcript), 2)
        self.assertTrue(transcript[0]["validation_outcome"].startswith("invalid:"))
        self.assertEqual(transcript[1]["validation_outcome"], "valid")
        # Re-prompt must restate the specific error so the model can correct itself.
        self.assertIn("NOT-A-REAL-ID", transcript[1]["prompt"])


class TestInvalidCandidateIdFallback(LLMStrategyTestBase):
    def test_unknown_candidate_id_falls_back_after_max_reprompts(self):
        client = MockLLMClient([valid_json("does-not-exist")] * 5)
        strategy = self.make_strategy(client, fallback_seed=7)

        selected = strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(len(client.calls), run_campaign.LLMStrategy.MAX_REPROMPTS)
        self.assertEqual(strategy.fallback_count, 1)
        self.assertIn(selected.id, {c.id for c in self.fault_space})

        transcript = self.read_transcript()
        self.assertEqual(len(transcript), run_campaign.LLMStrategy.MAX_REPROMPTS + 1)
        for rec in transcript[:-1]:
            self.assertTrue(rec["validation_outcome"].startswith("invalid:"))
            self.assertFalse(rec["fallback"])
        self.assertTrue(transcript[-1]["fallback"])
        self.assertEqual(transcript[-1]["selected_candidate_id"], selected.id)
        self.assertIn("fallback_after_3_invalid_attempts", transcript[-1]["validation_outcome"])


class TestAlreadyTriedRejected(LLMStrategyTestBase):
    def test_already_tried_candidate_is_rejected_and_falls_back(self):
        tried_candidate = self.fault_space[0]
        history = [run_campaign.RunResult(candidate=tried_candidate, weakness_signals={"any_violation": False})]
        # A valid, real candidate_id -- but one already in `history` -- must
        # still be rejected (not-yet-tried is a hard constraint, not just
        # fault-space membership).
        client = MockLLMClient([valid_json(tried_candidate.id)] * 5)
        strategy = self.make_strategy(client, fallback_seed=3)

        selected = strategy.select(history=history, fault_space=self.fault_space, k_remaining=9)

        self.assertNotEqual(selected.id, tried_candidate.id)
        self.assertEqual(strategy.fallback_count, 1)

        transcript = self.read_transcript()
        self.assertTrue(any("already tried" in rec["validation_outcome"] for rec in transcript[:-1]))


class TestMalformedJSON(LLMStrategyTestBase):
    def test_non_json_text_is_rejected_and_retried(self):
        client = MockLLMClient(["this is not json at all", "still not json"])
        strategy = self.make_strategy(client, fallback_seed=1)

        selected = strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(strategy.fallback_count, 1)
        self.assertIn(selected.id, {c.id for c in self.fault_space})
        transcript = self.read_transcript()
        self.assertTrue(all(
            "malformed_json" in rec["validation_outcome"] for rec in transcript[:-1]
        ))

    def test_markdown_fenced_json_is_accepted(self):
        target_id = self.fault_space[3].id
        fenced = "```json\n" + valid_json(target_id) + "\n```"
        client = MockLLMClient([fenced])
        strategy = self.make_strategy(client)

        selected = strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(selected.id, target_id)
        self.assertEqual(strategy.fallback_count, 0)

    def test_missing_candidate_id_key_is_rejected(self):
        client = MockLLMClient([json.dumps({"rationale": "no id here"})] * 5)
        strategy = self.make_strategy(client, fallback_seed=2)

        strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(strategy.fallback_count, 1)
        transcript = self.read_transcript()
        self.assertTrue(all(
            "missing or non-string" in rec["validation_outcome"] for rec in transcript[:-1]
        ))


class TestRequestError(LLMStrategyTestBase):
    def test_client_exception_is_treated_as_invalid_and_falls_back(self):
        client = MockLLMClient([RuntimeError("throttled"), RuntimeError("throttled"), RuntimeError("throttled")])
        strategy = self.make_strategy(client, fallback_seed=4)

        selected = strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(strategy.fallback_count, 1)
        self.assertIn(selected.id, {c.id for c in self.fault_space})
        transcript = self.read_transcript()
        self.assertTrue(all(
            "request_error" in rec["validation_outcome"] for rec in transcript[:-1]
        ))


class TestTranscriptWriting(LLMStrategyTestBase):
    def test_transcript_file_created_under_campaign_dir(self):
        target_id = self.fault_space[1].id
        client = MockLLMClient([valid_json(target_id)])
        strategy = self.make_strategy(client)
        strategy.select(history=[], fault_space=self.fault_space, k_remaining=10)

        transcript_path = self.campaign_dir / "llm-transcript.jsonl"
        self.assertTrue(transcript_path.exists())
        lines = transcript_path.read_text().strip().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        for key in ("timestamp", "arm", "injection_number", "attempt", "prompt",
                    "raw_completion", "raw_response", "model_id",
                    "validation_outcome", "selected_candidate_id", "fallback"):
            self.assertIn(key, record)
        self.assertEqual(record["arm"], "llm-claude")

    def test_transcript_accumulates_across_multiple_injections(self):
        # Two injections in the same campaign_dir -> one shared transcript file.
        id1, id2 = self.fault_space[0].id, self.fault_space[1].id
        client1 = MockLLMClient([valid_json(id1)])
        strategy1 = self.make_strategy(client1)
        c1 = strategy1.select(history=[], fault_space=self.fault_space, k_remaining=10)

        history = [run_campaign.RunResult(candidate=c1, weakness_signals={"any_violation": True})]
        client2 = MockLLMClient([valid_json(id2)])
        strategy2 = self.make_strategy(client2)
        strategy2.select(history=history, fault_space=self.fault_space, k_remaining=9)

        transcript = self.read_transcript()
        self.assertEqual(len(transcript), 2)
        self.assertEqual(transcript[0]["injection_number"], 1)
        self.assertEqual(transcript[1]["injection_number"], 2)


class TestDeterministicFallback(LLMStrategyTestBase):
    def test_same_seed_gives_same_fallback_choice(self):
        client_a = MockLLMClient([valid_json("bad-id")] * 5)
        client_b = MockLLMClient([valid_json("bad-id")] * 5)
        strategy_a = self.make_strategy(client_a, fallback_seed=99)
        # Separate campaign_dir so the two transcripts don't interleave.
        with tempfile.TemporaryDirectory() as other_dir:
            strategy_b = run_campaign.LLMStrategy(
                arm="llm-claude", client=client_b, campaign_dir=Path(other_dir),
                model_id="test-model-id", fallback_seed=99,
            )
            selected_a = strategy_a.select(history=[], fault_space=self.fault_space, k_remaining=10)
            selected_b = strategy_b.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertEqual(selected_a.id, selected_b.id)

    def test_different_seed_can_give_different_fallback_choice(self):
        # Not guaranteed to differ for any two seeds in general, but this
        # pair is verified to diverge for the current fault-space.yaml
        # ordering; if fault-space.yaml's candidate order ever changes this
        # assertion may need a different seed pair, not a design change.
        client_a = MockLLMClient([valid_json("bad-id")] * 5)
        client_b = MockLLMClient([valid_json("bad-id")] * 5)
        strategy_a = self.make_strategy(client_a, fallback_seed=1)
        with tempfile.TemporaryDirectory() as other_dir:
            strategy_b = run_campaign.LLMStrategy(
                arm="llm-claude", client=client_b, campaign_dir=Path(other_dir),
                model_id="test-model-id", fallback_seed=2,
            )
            selected_a = strategy_a.select(history=[], fault_space=self.fault_space, k_remaining=10)
            selected_b = strategy_b.select(history=[], fault_space=self.fault_space, k_remaining=10)

        self.assertNotEqual(selected_a.id, selected_b.id)

    def test_fallback_reproducible_across_repeated_construction(self):
        results = []
        for _ in range(3):
            client = MockLLMClient([valid_json("bad-id")] * 5)
            strategy = self.make_strategy(client, fallback_seed=42)
            # Fresh campaign_dir each time so transcripts don't pile up
            # (the fallback RNG only depends on fallback_seed, not on any
            # prior transcript state).
            with tempfile.TemporaryDirectory() as d:
                strategy.campaign_dir = Path(d)
                strategy.transcript_path = Path(d) / "llm-transcript.jsonl"
                results.append(strategy.select(history=[], fault_space=self.fault_space, k_remaining=10).id)
        self.assertEqual(len(set(results)), 1)


class TestNotTriedFallbackNeverRepeats(LLMStrategyTestBase):
    def test_fallback_never_repeats_a_tried_candidate(self):
        tried = self.fault_space[:34]  # leave only 2 untried candidates
        history = [run_campaign.RunResult(candidate=c, weakness_signals={"any_violation": False}) for c in tried]
        client = MockLLMClient([valid_json(tried[0].id)] * 5)  # always an already-tried id
        strategy = self.make_strategy(client, fallback_seed=5)

        selected = strategy.select(history=history, fault_space=self.fault_space, k_remaining=2)

        tried_ids = {c.id for c in tried}
        self.assertNotIn(selected.id, tried_ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
