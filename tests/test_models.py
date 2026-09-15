from graphitect.models import (
    Claim,
    Confidence,
    DesignDocSection,
    Evidence,
    GroundedUnderstanding,
    QuestionOption,
    RationaleQuestion,
    Tradeoff,
)


def test_claim_round_trips_through_json():
    claim = Claim(
        text="chose FastAPI for async support",
        confidence=Confidence.CONFIRMED,
        cites=[Evidence(source="readme", note="README line 12")],
    )
    restored = Claim.model_validate_json(claim.model_dump_json())
    assert restored == claim


def test_prescriptive_claims_default_kind_is_descriptive():
    # kind defaults to "descriptive" - callers must opt in to "prescriptive"
    # explicitly, so a forgotten classification doesn't silently make a
    # recommendation look askable.
    claim = Claim(text="extend the verifier past a single LOC check", confidence=Confidence.INFERRED)
    assert claim.kind == "descriptive"


def test_rationale_question_first_option_is_the_guess_by_convention():
    q = RationaleQuestion(
        claim_id="choices::0",
        question="Why Typer + Rich for the CLI?",
        options=[
            QuestionOption(label="Type-hints + Rich dashboards", becomes_text="chosen for type hints"),
            QuestionOption(label="Just familiarity", becomes_text="no deeper reason"),
        ],
    )
    assert q.allow_free_text is True
    assert q.options[0].label == "Type-hints + Rich dashboards"


def test_grounded_understanding_defaults_to_empty_collections():
    gu = GroundedUnderstanding(diagram_kind="architecture")
    assert gu.nodes == []
    assert gu.edges == []
    assert gu.doc == []
    assert gu.pending_questions == []


def test_design_doc_section_holds_claims():
    section = DesignDocSection(
        heading="Technology choices",
        claims=[Claim(text="x", confidence=Confidence.INFERRED)],
    )
    assert len(section.claims) == 1


def test_design_doc_section_defaults_tradeoffs_to_empty():
    # Old synthesized understanding.json files (from before tradeoffs
    # existed) must still validate cleanly - no required field was added.
    section = DesignDocSection(heading="Technology choices & why")
    assert section.tradeoffs == []


def test_tradeoff_round_trips_through_json():
    tradeoff = Tradeoff(
        decision=Claim(
            text="Chose FastAPI over Django for async support",
            confidence=Confidence.CONFIRMED,
            cites=[Evidence(source="readme", note="README line 12")],
        ),
        alternatives_considered=["Django", "Flask"],
        pros=[Claim(text="Native async/await support", confidence=Confidence.CONFIRMED)],
        cons=[Claim(text="Smaller ecosystem than Django", confidence=Confidence.INFERRED)],
    )
    restored = Tradeoff.model_validate_json(tradeoff.model_dump_json())
    assert restored == tradeoff
    assert restored.decision.confidence == Confidence.CONFIRMED
    assert restored.pros[0].text == "Native async/await support"
