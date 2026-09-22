import hashlib
import json

import pytest

from scripts import jev_report_audit as audit


SOURCE = "Policy rates stayed unchanged. Inflation slowed. The bank did not cut rates."


def summary(text="Policy rates stayed unchanged.", quote="Policy rates stayed unchanged."):
    return {"conclusion": {"text": text, "source_quote": quote}}


def answer(choice, probability=0.96, confidence=0.95):
    others = [key for key in audit.RELATIONS if key != choice]
    remainder = 1 - probability
    return {
        "type": "choice",
        "choice": choice,
        "confidence": confidence,
        "probabilities": {choice: probability, others[0]: remainder / 2, others[1]: remainder / 2},
    }


def response(choice="supports", **kwargs):
    return {
        "model": "jev-1.13.0",
        "answers": {"q0000": answer(choice, **kwargs)},
        "usage": {"input_tokens": 200, "output_tokens": 20},
    }


def call_jev(tmp_path, value, transport, environment=None):
    return audit.audit_summary(
        value,
        SOURCE,
        mode="jev",
        data_class="synthetic",
        environment={"TYPESAFE_API_KEY": "test-key"} if environment is None else environment,
        transport=transport,
        cache_dir=tmp_path,
    )


def test_code_checks_exact_quote_without_network():
    result = audit.audit_summary(summary(), SOURCE, transport=lambda *_: pytest.fail("network called"))
    pair = result["pairs"][0]
    assert pair["id"] == "/conclusion"
    assert pair["deterministic_status"] == "matched"
    assert pair["result"] == "exact_match"
    assert result["api"]["request_count"] == 0
    assert result["policy"] == {
        "role": "advisory_only",
        "parent_review_required": True,
        "automatic_acceptance": False,
        "trading_decision_delegated": False,
    }


@pytest.mark.parametrize(
    ("quote", "reason"),
    [("", "empty_source_quote"), ("Rates rose.", "source_quote_not_found")],
)
def test_empty_or_nonverbatim_quote_is_deterministic_error(quote, reason):
    result = audit.audit_summary(summary("A claim", quote), SOURCE)
    assert result["status"] == "changes_required"
    assert result["pairs"][0]["result"] == "deterministic_error"
    assert result["pairs"][0]["reason"] == reason


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_empty_or_whitespace_summary_text_is_deterministic_error(text):
    result = audit.audit_summary(summary(text, "Policy rates stayed unchanged."), SOURCE)
    assert result["status"] == "changes_required"
    assert result["pairs"][0]["result"] == "deterministic_error"
    assert result["pairs"][0]["reason"] == "empty_text"


def test_recursive_collection_only_takes_text_quote_objects():
    value = {
        "ignored": {"text": "no quote"},
        "items": [summary()["conclusion"], {"nested": {"text": "Inflation eased.", "source_quote": "Inflation slowed."}}],
    }
    result = audit.audit_summary(value, SOURCE)
    assert [pair["id"] for pair in result["pairs"]] == ["/items/0", "/items/1/nested"]


def test_code_and_local_modes_preserve_semantic_uncertainty(tmp_path):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    code = audit.audit_summary(value, SOURCE, mode="code", data_class="public")
    local = audit.audit_summary(value, SOURCE, mode="jev", data_class="local", cache_dir=tmp_path)
    assert code["pairs"][0]["result"] == "unassessed"
    assert code["api"]["reason"] == "local_mode"
    assert local["pairs"][0]["result"] == "unassessed"
    assert local["api"]["reason"] == "data_class_local_only"


def test_condition_missing_is_reported_insufficient(tmp_path):
    value = summary(
        "Policy rates stayed unchanged because inflation slowed.",
        "Policy rates stayed unchanged.",
    )
    result = call_jev(tmp_path, value, lambda *_: response("insufficient"))
    pair = result["pairs"][0]
    assert pair["result"] == "insufficient"
    assert pair["raw_judgement"] == "insufficient"
    assert pair["reason"] == "jev_advisory_parent_review_required"
    assert result["status"] == "review_required"
    assert result["scope"]["kind"] == "summary_vs_report"
    assert result["scope"]["source_fact_verification"] is False


def test_external_state_contains_only_text_and_quote(tmp_path):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    captured = {}

    def inspect(payload, *_):
        captured.update(json.loads(payload))
        return response("supports")

    call_jev(tmp_path, value, inspect)
    assert captured["state"] == {
        "pairs": [{"text": "Rates were held steady.", "source_quote": "Policy rates stayed unchanged."}]
    }
    assert set(captured) == {"model", "state", "questions"}


def test_negation_reversal_is_reported_contradicted(tmp_path):
    value = summary("The bank cut rates.", "The bank did not cut rates.")
    result = call_jev(tmp_path, value, lambda *_: response("contradicts"))
    assert result["pairs"][0]["result"] == "contradicts"
    assert result["status"] == "changes_required"


@pytest.mark.parametrize(
    "api_response,reason",
    [
        ({"model": "jev-1.13.0", "answers": {}, "usage": {"input_tokens": 20, "output_tokens": 2}}, "invalid_or_missing_answer"),
        ({"model": "jev-not-approved", "answers": {"q0000": answer("supports")}, "usage": {"input_tokens": 20, "output_tokens": 2}}, "unexpected_response_model"),
        ({"model": {"unexpected": "object"}, "answers": {"q0000": answer("supports")}, "usage": {"input_tokens": 20, "output_tokens": 2}}, "unexpected_response_model"),
        ({"model": "jev-1.13.0", "answers": {"q0000": answer("supports")}}, "malformed_response"),
    ],
)
def test_missing_answer_wrong_model_and_missing_usage_stay_unassessed(tmp_path, api_response, reason):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    result = call_jev(tmp_path, value, lambda *_: api_response)
    assert result["pairs"][0]["result"] == "unassessed"
    assert result["pairs"][0]["reason"] == reason
    assert result["status"] == "review_required"


@pytest.mark.parametrize("invalid_choice", [["supports"], {"choice": "supports"}])
def test_unhashable_answer_choice_stays_unassessed(tmp_path, invalid_choice):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    invalid_answer = {
        "type": "choice",
        "choice": invalid_choice,
        "confidence": 0.99,
        "probabilities": {"supports": 0.98, "contradicts": 0.01, "insufficient": 0.01},
    }
    api_response = {
        "model": "jev-1.13.0",
        "answers": {"q0000": invalid_answer},
        "usage": {"input_tokens": 20, "output_tokens": 2},
    }
    result = call_jev(tmp_path, value, lambda *_: api_response)
    assert result["pairs"][0]["result"] == "unassessed"
    assert result["pairs"][0]["reason"] == "invalid_or_missing_answer"
    assert result["api"]["status"] == "partial"


def test_low_confidence_or_low_choice_probability_is_uncertain(tmp_path):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    low_conf = call_jev(tmp_path / "a", value, lambda *_: response("supports", probability=0.96, confidence=0.7))
    low_prob = call_jev(tmp_path / "b", value, lambda *_: response("supports", probability=0.89, confidence=0.95))
    assert low_conf["pairs"][0]["reason"] == "low_confidence"
    assert low_prob["pairs"][0]["reason"] == "low_choice_probability"
    assert {low_conf["pairs"][0]["result"], low_prob["pairs"][0]["result"]} == {"uncertain"}


def test_api_failure_never_exposes_exception_text(tmp_path):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")

    def failed(*_):
        raise RuntimeError("Bearer secret-value from remote body")

    result = call_jev(tmp_path, value, failed)
    serialized = json.dumps(result)
    assert result["api"]["reason"] == "api_error"
    assert result["pairs"][0]["result"] == "unassessed"
    assert "secret-value" not in serialized


def test_shared_budget_exhaustion_prevents_transport(tmp_path):
    module = audit._load_jev_module()
    ledger = module.BudgetLedger(tmp_path, "0.01")
    while True:
        reservation, _ = ledger.reserve()
        if reservation is None:
            break
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    result = call_jev(tmp_path, value, lambda *_: pytest.fail("transport called"))
    assert result["api"]["reason"] == "budget_exhausted"
    assert result["api"]["request_count"] == 0
    assert result["pairs"][0]["result"] == "unassessed"


def test_secret_pattern_blocks_external_request(tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
    value = summary(f"Rates held; leaked {secret}", "Policy rates stayed unchanged.")
    result = call_jev(tmp_path, value, lambda *_: pytest.fail("transport called"))
    assert result["api"]["reason"] == "secret_pattern_detected"
    assert secret not in json.dumps(result["api"])
    assert result["pairs"][0]["result"] == "unassessed"


def test_disabled_and_missing_key_are_local_fallbacks(tmp_path):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    disabled = call_jev(tmp_path / "a", value, lambda *_: pytest.fail("transport called"), {"LAA_JEV_DISABLED": "1"})
    no_key = call_jev(tmp_path / "b", value, lambda *_: pytest.fail("transport called"), {})
    assert disabled["api"]["reason"] == "disabled"
    assert no_key["api"]["reason"] == "api_key_missing"


def test_missing_shared_dependency_is_local_fallback(tmp_path, monkeypatch):
    value = summary("Rates were held steady.", "Policy rates stayed unchanged.")
    monkeypatch.setattr(audit, "_load_jev_module", lambda: None)
    result = call_jev(tmp_path, value, lambda *_: pytest.fail("transport called"))
    assert result["api"]["reason"] == "shared_dependency_unavailable"
    assert result["pairs"][0]["result"] == "unassessed"


def test_non_string_pair_values_are_deterministic_error():
    result = audit.audit_summary({"text": ["claim"], "source_quote": 123}, SOURCE)
    assert result["pairs"][0]["reason"] == "pair_values_must_be_strings"
    assert result["status"] == "changes_required"


def test_overflow_is_not_silently_accepted():
    items = [
        {"text": f"Claim {index}", "source_quote": "Inflation slowed."}
        for index in range(audit.MAX_PAIRS + 1)
    ]
    result = audit.audit_summary({"items": items}, SOURCE, mode="code")
    assert result["api"]["overflow_count"] == 1
    assert result["pairs"][-1]["reason"] == "candidate_limit_exceeded"
    assert all(pair["result"] == "unassessed" for pair in result["pairs"])


def test_canonical_hashes_ignore_object_key_order():
    first = {"b": 2, "a": summary()}
    second = {"a": summary(), "b": 2}
    result1 = audit.audit_summary(first, SOURCE)
    result2 = audit.audit_summary(second, SOURCE)
    assert result1["summary_sha256"] == result2["summary_sha256"]
    expected = hashlib.sha256(json.dumps(SOURCE, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert result1["source_sha256"] == expected


def test_cli_writes_local_audit_without_api(tmp_path):
    summary_path = tmp_path / "summary.json"
    source_path = tmp_path / "source.md"
    output_path = tmp_path / "audit.json"
    summary_path.write_text(json.dumps(summary()), encoding="utf-8")
    source_path.write_text(SOURCE, encoding="utf-8")
    assert audit.main(["--summary", str(summary_path), "--source", str(source_path), "--output", str(output_path)]) == 0
    result = json.loads(output_path.read_text(encoding="utf-8"))
    assert result["api"]["status"] == "local_only"
    assert result["policy"]["automatic_acceptance"] is False
