"""Grounding via a plain-language description - fastest, no repo access
needed, weakest evidence (plan.md §02). Everything from this source is
inherently `inferred` (technically `user`-sourced but unverified against
code), since nothing here is checked against real code.
"""

from __future__ import annotations

from ..models import Claim, Confidence, Evidence


def ground(description: str) -> list[Claim]:
    """Wrap the user's own description as a single unverified claim per
    paragraph. A real implementation would still run this through an LLM to
    extract structured claims; this stub keeps the boundary explicit: nothing
    from Describe-it is ever `confirmed` by construction, since there's
    nothing to check it against.
    """
    paragraphs = [p.strip() for p in description.split("\n\n") if p.strip()]
    return [
        Claim(
            text=p,
            confidence=Confidence.INFERRED,
            cites=[Evidence(source="user", note="Describe-it mode - not verified against code")],
        )
        for p in paragraphs
    ]
