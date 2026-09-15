from graphitect.deliver.doc_compiler import _extract_diagram_assets, to_html, to_markdown
from graphitect.models import (
    Claim,
    Confidence,
    DesignDocSection,
    Evidence,
    GroundedUnderstanding,
    Tradeoff,
)


def _understanding() -> GroundedUnderstanding:
    return GroundedUnderstanding(
        diagram_kind="architecture",
        nodes=[{"id": "api", "label": "API"}],
        edges=[],
        doc=[
            DesignDocSection(
                heading="Overview",
                claims=[
                    Claim(
                        text="A small API service.",
                        confidence=Confidence.CONFIRMED,
                        cites=[Evidence(source="code", note="main.py:1")],
                    )
                ],
                related_node_ids=["api"],
            ),
            DesignDocSection(
                heading="Technology choices & why",
                claims=[
                    Claim(text="Chose FastAPI", confidence=Confidence.INFERRED),
                    Claim(text="Chose Postgres", confidence=Confidence.CONFIRMED, cites=[Evidence(source="readme")]),
                ],
            ),
        ],
    )


def _understanding_with_tradeoff() -> GroundedUnderstanding:
    gu = _understanding()
    gu.doc[1].tradeoffs = [
        Tradeoff(
            decision=Claim(
                text="Chose FastAPI over Django for async support",
                confidence=Confidence.CONFIRMED,
                cites=[Evidence(source="readme", note="README line 12")],
            ),
            alternatives_considered=["Django", "Flask"],
            pros=[Claim(text="Native async/await support", confidence=Confidence.CONFIRMED)],
            cons=[Claim(text="Smaller ecosystem than Django", confidence=Confidence.INFERRED)],
        )
    ]
    return gu


def test_to_html_renders_a_tradeoff_pros_cons_table():
    out = to_html(_understanding_with_tradeoff(), "Test")
    assert 'class="tradeoff-table"' in out
    assert "Chose FastAPI over Django for async support" in out
    assert "Native async/await support" in out
    assert "Smaller ecosystem than Django" in out
    assert "Alternatives considered: Django, Flask" in out


def test_to_html_tradeoff_claims_carry_confidence_badges():
    out = to_html(_understanding_with_tradeoff(), "Test")
    # every claim in the table - decision, pro, con - must still show its
    # own confidence tag, same as the flat claim list/table above it.
    assert out.count('class="ev confirmed"') >= 2  # the tech-choices claim + the decision + the pro
    assert 'class="ev inferred"' in out


def test_to_html_omits_tradeoffs_block_when_a_section_has_none():
    out = to_html(_understanding(), "Test")
    assert 'class="tradeoff"' not in out


def test_to_html_stat_line_counts_tradeoff_claims_too():
    without = to_html(_understanding(), "Test")
    with_tradeoff = to_html(_understanding_with_tradeoff(), "Test")
    # Adding a tradeoff (1 decision + 1 pro + 1 con = 3 more claims) must
    # move the "N of M claims confirmed" stat, not leave it stuck counting
    # only the flat claims list. Baseline is 3 flat claims total.
    assert "of 3</b>" in without
    assert "of 6</b>" in with_tradeoff


def test_to_markdown_renders_a_tradeoff_pros_cons_table():
    out = to_markdown(_understanding_with_tradeoff(), "Test")
    assert "| Pros | Cons |" in out
    assert "Native async/await support" in out
    assert "Smaller ecosystem than Django" in out
    assert "Alternatives considered: Django, Flask" in out


def test_to_html_shows_a_description_under_each_canonical_section_heading():
    # Naman's feedback (12 Sep 2026): "Limitations & future work... I don't
    # understand what the section is" - a bare heading over a couple of
    # terse claims doesn't tell an unfamiliar reader what to expect there.
    out = to_html(_understanding(), "Test")
    assert 'class="section-desc"' in out
    assert "What this system is and what problem it solves" in out
    assert "Specific tools and frameworks chosen" in out


def test_to_html_omits_description_for_a_non_canonical_heading():
    gu = GroundedUnderstanding(
        diagram_kind="architecture",
        doc=[DesignDocSection(heading="Something I made up", claims=[])],
    )
    out = to_html(gu, "Test")
    assert '<p class="section-desc">' not in out  # the CSS rule itself is always present, unused or not


def test_to_markdown_includes_a_description_under_each_canonical_section_heading():
    out = to_markdown(_understanding(), "Test")
    assert "What this system is and what problem it solves" in out
    assert "Specific tools and frameworks chosen" in out


def test_to_html_includes_title_and_all_section_headings():
    out = to_html(_understanding(), "My System")
    assert "<title>My System</title>" in out
    assert "Overview" in out
    assert "Technology choices &amp; why" in out


def test_to_html_escapes_claim_text():
    understanding = _understanding()
    understanding.doc[0].claims[0].text = "<script>alert(1)</script>"
    out = to_html(understanding, "Test")
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_to_html_renders_confirmed_and_inferred_tags():
    out = to_html(_understanding(), "Test")
    assert 'class="ev confirmed"' in out
    assert 'class="ev inferred"' in out


def test_to_html_renders_technology_choices_as_a_table():
    out = to_html(_understanding(), "Test")
    # "Technology choices & why" is a _TABLE_SECTIONS entry
    assert "<table>" in out
    assert "Choice &amp; reasoning" in out


def test_to_html_has_a_print_to_pdf_path_with_a_static_diagram_overview():
    viewer = (
        '<html><style>:root { --bg: #07111d; } .component { fill: var(--bg); }</style>'
        '<svg id="overview"><rect class="component"/></svg></html>'
    )
    out = to_html(_understanding(), "Test", diagram_svg=viewer)
    assert 'data-print-report' in out
    assert "Print / save PDF" in out
    assert "@media print" in out
    assert 'id="graphitect-print-diagram"' in out
    assert 'id="graphitect-print-diagram-template"' in out
    assert ":host" in out  # Archify's root variables are scoped to the print shadow tree.


def test_to_html_renders_node_refs_for_sections_that_have_them():
    out = to_html(_understanding(), "Test")
    assert 'data-node-ref="api"' in out


def test_to_html_node_ref_uses_remapped_id_for_highlighting_but_keeps_real_id_visible():
    # Regression test for a known gap: once a large graph's diagram gets
    # aggregated into community boxes (archify_adapter.node_id_remap), a raw
    # node id like "api" no longer exists as an SVG element id at all - the
    # pill's hover-highlight (_HIGHLIGHT_SCRIPT, matches on data-node-ref)
    # silently found nothing. The pill must still look up the id the diagram
    # actually rendered (data-node-ref="c0"), while still showing the real
    # raw id as its visible text - that's the actual citation, and "c0"
    # means nothing to a reader.
    out = to_html(_understanding(), "Test", node_id_remap={"api": "c0"})
    assert 'data-node-ref="c0"' in out
    assert ">api<" in out  # visible pill text is still the real citation
    assert 'data-node-ref="api"' not in out


def test_to_html_without_diagram_shows_placeholder_not_empty_frame():
    out = to_html(_understanding(), "Test", diagram_svg=None)
    assert "bundled Archify renderer could not produce" in out


def test_to_html_with_diagram_embeds_the_complete_viewer_in_srcdoc():
    viewer = '<html><script>window.viewerReady=true</script><svg id="test-diagram"></svg></html>'
    out = to_html(_understanding(), "Test", diagram_svg=viewer)
    assert 'id="graphitect-diagram-frame"' in out
    assert "window.viewerReady=true" in out
    assert "&lt;html&gt;" in out


def test_to_html_with_two_diagrams_adds_overview_and_scrollable_full_graph_modes():
    overview = '<html><svg id="overview"></svg></html>'
    full = '<html><svg id="full"></svg></html>'
    out = to_html(
        _understanding(),
        "Test",
        diagram_svg=overview,
        full_diagram_svg=full,
        overview_connection_count=18,
        full_connection_count=45,
    )
    assert 'data-diagram-mode-button="overview"' in out
    assert 'data-diagram-mode-button="full"' in out
    assert "Overview · 18 key links" in out
    assert "Full architecture rollup · 45 relationships" in out
    assert 'id="graphitect-full-diagram-frame"' in out
    assert 'data-diagram-mode="full" hidden' in out
    assert 'class="diagram-canvas"' in out
    assert ".diagram-frame--full{width:1520px; min-width:1520px;}" in out
    assert "All detected relationships between the rendered components" in out


def test_to_html_with_story_adds_guided_default_and_presentation_controls():
    story = '<html><svg id="story"></svg></html>'
    overview = '<html><svg id="overview"></svg></html>'
    full = '<html><svg id="full"></svg></html>'
    out = to_html(
        _understanding(),
        "Test",
        diagram_svg=overview,
        story_diagram_svg=story,
        full_diagram_svg=full,
        story_chapter_count=3,
    )

    assert 'data-diagram-mode-button="story"' in out
    assert "Workflow story · 3 chapters" in out
    assert 'id="graphitect-story-diagram-frame"' in out
    assert 'data-diagram-mode="story">' in out
    assert 'data-present-story' in out
    assert "runStory({present:true, play:false})" in out
    assert "query.get('present') === '1'" in out
    assert "width:min(2400px,calc(100vw - clamp(24px,6vw,160px)))" in out


def test_to_html_adds_escaped_per_block_hover_details_to_the_embedded_story_only():
    story = '<html><body><svg><g data-node-id="step-1" data-node-label="Provider"></g></svg></body></html>'
    overview = '<html><body><svg id="overview"></svg></body></html>'
    out = to_html(
        _understanding(),
        "Test",
        diagram_svg=overview,
        story_diagram_svg=story,
        story_chapter_count=1,
        story_hover_details={
            "step-1": {
                "symbol": "function_ollama_chat",
                "source": "backend/app/services/chat_service.py",
                "summary": "Code-level LLM provider adapter.",
            }
        },
    )

    assert "installDiagramHovers" in out
    assert "graphitect-hover-details" in out
    assert "function_ollama_chat" in out
    assert "Code symbol:" in out
    assert "Incoming:" in out


def test_to_html_with_evidence_gated_sequence_makes_it_the_default_mode():
    sequence = '<html><svg id="sequence"></svg></html>'
    story = '<html><svg id="story"></svg></html>'
    overview = '<html><svg id="overview"></svg></html>'
    out = to_html(
        _understanding(),
        "Test",
        diagram_svg=overview,
        sequence_diagram_svg=sequence,
        story_diagram_svg=story,
        sequence_message_count=3,
    )

    assert 'data-diagram-mode-button="sequence"' in out
    assert "Sequence trace · 3 direct calls" in out
    assert 'id="graphitect-sequence-diagram-frame"' in out
    assert 'data-diagram-mode="sequence">' in out
    assert 'data-diagram-mode="story" hidden' in out
    assert "direct extracted Graphify calls/invokes edge" in out
    assert "not runtime request telemetry" in out


def test_to_html_keeps_story_when_full_rollup_is_unavailable():
    story = '<html><svg id="story"></svg></html>'
    overview = '<html><svg id="overview"></svg></html>'
    out = to_html(
        _understanding(),
        "Test",
        diagram_svg=overview,
        story_diagram_svg=story,
        story_chapter_count=1,
    )

    assert 'id="graphitect-story-diagram-frame"' in out
    assert 'data-diagram-mode-button="full"' not in out
    assert 'data-diagram-mode="full"' not in out


def test_to_html_has_two_explicit_output_sections():
    out = to_html(_understanding(), "Test")
    assert "Section 1" in out
    assert "Interactive system diagram" in out
    assert "Section 2" in out
    assert "Project explanation" in out


def test_to_html_without_llm_hides_structure_only_claims_and_explains_requirement():
    out = to_html(_understanding(), "Test", explanation_available=False)
    assert "LLM required" in out
    assert "Technology choices &amp; why" not in out


def test_to_html_diagram_opt_out_is_explicit():
    out = to_html(_understanding(), "Test", diagram_opted_out=True)
    assert "Diagram generation was explicitly opted out" in out


def test_to_html_has_no_remote_font_dependency():
    out = to_html(_understanding(), "Test")
    assert "fonts.googleapis.com" not in out


def test_to_html_stat_line_reflects_real_claim_counts():
    out = to_html(_understanding(), "Test")
    # 2 confirmed (Overview + Postgres), 1 inferred (FastAPI) = 2 of 3
    assert "<b>2 of 3</b>" in out


def test_to_html_shows_pending_questions_callout():
    from graphitect.models import QuestionOption, RationaleQuestion

    understanding = _understanding()
    understanding.pending_questions = [
        RationaleQuestion(
            claim_id="x::0", question="Why?", options=[QuestionOption(label="guess", becomes_text="guess")]
        )
    ]
    out = to_html(understanding, "Test")
    assert "<b>1</b> question(s) still unanswered" in out


def test_extract_diagram_assets_pulls_svg_block_out_of_a_full_html_page():
    full_page = "<html><head></head><body><svg id=\"foo\"><rect/></svg></body></html>"
    svg, styles = _extract_diagram_assets(full_page)
    assert svg == '<svg id="foo"><rect/></svg>'
    assert styles == ""  # no class attributes used, nothing to pull in


def test_extract_diagram_assets_passes_through_bare_svg_unchanged():
    bare = '<svg id="foo"><rect/></svg>'
    svg, styles = _extract_diagram_assets(bare)
    assert svg == bare
    assert styles == ""


def test_extract_diagram_assets_falls_back_when_no_svg_found():
    svg, styles = _extract_diagram_assets("<html>no diagram here</html>")
    assert svg == "<html>no diagram here</html>"
    assert styles == ""


def test_extract_diagram_assets_pulls_in_only_style_blocks_the_svg_actually_uses():
    # Regression test for a real live bug: archify's diagram styles its
    # elements entirely via CSS classes (c-backend, m-security, etc.)
    # defined in separate <style> blocks, not inline attributes - extracting
    # bare SVG markup alone rendered as an unstyled, visually broken mess.
    # Confirmed live against a real archify-rendered page (12 Sep 2026).
    full_page = """
    <html><head>
    <style>.unrelated-font-block { font-family: 'Unused Embedded Font'; }</style>
    <style>.c-backend { fill: #4faf98; } body { background: red; }</style>
    </head><body><svg id="diagram"><rect class="c-backend"/></svg></body></html>
    """
    svg, styles = _extract_diagram_assets(full_page)
    assert "c-backend" in svg
    assert ".c-backend" in styles
    assert "Unused Embedded Font" not in styles  # irrelevant block excluded
    assert "background: red" in styles  # kept regardless of body{} - isolation is the mount's job, not extraction's


def test_extract_diagram_assets_rewrites_root_to_host_for_shadow_dom():
    # Regression test for a real live bug: :root only ever matches the real
    # document root, never a shadow tree, so archify's theme CSS custom
    # properties (--bg, --text, ...) defined via :root silently resolved to
    # nothing once mounted in a shadow root - rendering as a solid black box
    # with invisible text instead of the intended themed diagram. Confirmed
    # live against a real archify-rendered page (12 Sep 2026).
    full_page = """
    <html><head>
    <style>:root, [data-theme="dark"] { --bg: #020617; } .c-backend { fill: var(--bg); }</style>
    </head><body><svg id="diagram"><rect class="c-backend"/></svg></body></html>
    """
    svg, styles = _extract_diagram_assets(full_page)
    assert ":root" not in styles
    assert ":host" in styles


def _understanding_with_redundant_user_citation() -> GroundedUnderstanding:
    # Mirrors what apply_answer() actually produces: a free-text answer
    # becomes both the claim text and the citation note verbatim.
    text = "Async I/O for the DB calls, benchmarked against Flask+gevent"
    return GroundedUnderstanding(
        diagram_kind="architecture",
        doc=[
            DesignDocSection(
                heading="Technology choices & why",
                claims=[Claim(text=text, confidence=Confidence.CONFIRMED, cites=[Evidence(source="user", note=text)])],
            )
        ],
    )


def test_to_html_collapses_redundant_user_citation_to_just_user():
    out = to_html(_understanding_with_redundant_user_citation(), "Test")
    assert "&middot; user<" in out
    # the full claim text must not appear twice on the same line
    assert out.count("Async I/O for the DB calls") == 1


def test_to_markdown_collapses_redundant_user_citation_to_just_user():
    out = to_markdown(_understanding_with_redundant_user_citation(), "Test")
    assert "(user)" in out
    assert out.count("Async I/O for the DB calls") == 1
