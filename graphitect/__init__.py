"""graphitect - graph(ify) + (arch)itect.

Orchestrates Graphify (structure) and archify (diagrams) to produce a
sourced, confidence-tagged design doc alongside the architecture diagram.
See plan.md in the project root for the full design.
"""

__version__ = "0.2.0"

from .models import (
    Claim,
    Confidence,
    DesignDocSection,
    Evidence,
    GroundedUnderstanding,
    QuestionOption,
    RationaleQuestion,
)

__all__ = [
    "Claim",
    "Confidence",
    "DesignDocSection",
    "Evidence",
    "GroundedUnderstanding",
    "QuestionOption",
    "RationaleQuestion",
]
