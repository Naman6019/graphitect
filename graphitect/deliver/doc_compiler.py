"""Renders a GroundedUnderstanding to Markdown (secondary export) or a
designed HTML page (primary export, plan.md §05) - real tables for tech
choices/tradeoffs, a dynamically computed confirmed/inferred bar chart (not
hand-tuned per doc, unlike the earlier hand-authored artifacts this borrows
its visual language from), and an inline diagram sharing node IDs with the
surrounding prose via a hover-highlight script.
"""

from __future__ import annotations

import html as html_lib
import json
import re

from ..models import Claim, Confidence, DesignDocSection, GroundedUnderstanding, Tradeoff

# One-line explanations of what each canonical section (llm_backend's
# _CANONICAL_SECTIONS) is actually for - Naman's feedback (12 Sep 2026):
# "Limitations & future work... I don't understand what the section is."
# A bare heading over a couple of terse claims doesn't tell an unfamiliar
# reader what kind of content to expect there or why it's separate from,
# say, "Tradeoffs" - especially true for this one, since "future work" can
# read as a roadmap commitment rather than what it actually is here: this
# tool's own inferred suggestions, not something the author has committed
# to. Not derived from the heading text at runtime - a fixed, hand-written
# subtitle per canonical heading, shown regardless of what the LLM actually
# wrote for that section. A non-canonical (hand-authored) heading simply
# gets no subtitle rather than a guessed one.
_SECTION_DESCRIPTIONS: dict[str, str] = {
    "Overview": "What this system is and what problem it solves.",
    "Components & responsibilities": "The system's major parts and what each one actually does.",
    "Technology choices & why": "Specific tools and frameworks chosen, and the reasoning behind each.",
    "Tradeoffs & alternatives considered": "Decisions weighed against real alternatives, with their pros and cons.",
    "Key workflows": "How the system behaves end-to-end for its main use cases.",
    "Limitations & future work": (
        "Known gaps or weak points in the current implementation, and what graphitect "
        "suggests addressing next - its own inferred recommendations, not commitments the author has made."
    ),
}


def _all_claims(section: DesignDocSection) -> list[Claim]:
    """A section's flat claims plus every tradeoff's decision/pros/cons -
    used everywhere a confirmed/inferred count needs to reflect everything
    actually shown for a section, not just the flat list. Without this, the
    stat line and bar chart would silently undercount once tradeoffs
    (12 Sep 2026) added a second place claims live.
    """
    claims = list(section.claims)
    for tradeoff in section.tradeoffs:
        claims.append(tradeoff.decision)
        claims.extend(tradeoff.pros)
        claims.extend(tradeoff.cons)
    return claims


def _cite_bits(claim: Claim) -> list[str]:
    """The raw (unescaped, unwrapped) citation strings for a claim - shared
    by both the Markdown and HTML renderers so the "collapse a redundant
    user citation" rule (below) can't drift between the two formats.
    """
    bits = []
    for c in claim.cites:
        # A user answer's note is often verbatim the claim text itself
        # (apply_answer sets both from the same free-text response) -
        # repeating it in the citation adds nothing, so just say "user" then.
        if c.source == "user" and c.note and c.note.strip() == claim.text.strip():
            bits.append("user")
        else:
            bits.append(c.note or c.file or c.source)
    return [b for b in bits if b]


def _md_cite_str(claim: Claim) -> str:
    bits = _cite_bits(claim)
    return f" ({'; '.join(bits)})" if bits else ""


def _md_claim_line(claim: Claim, *, prefix: str = "- ") -> str:
    return f"{prefix}{claim.text} `{claim.confidence.value}`{_md_cite_str(claim)}"


def _md_tradeoff_block(tradeoff: Tradeoff) -> list[str]:
    decision = tradeoff.decision
    lines = [f"**{decision.text}** `{decision.confidence.value}`{_md_cite_str(decision)}"]
    if tradeoff.alternatives_considered:
        lines.append("")
        lines.append(f"*Alternatives considered: {', '.join(tradeoff.alternatives_considered)}*")
    lines.append("")
    pros_cell = "<br>".join(_md_claim_line(c, prefix="") for c in tradeoff.pros) or "—"
    cons_cell = "<br>".join(_md_claim_line(c, prefix="") for c in tradeoff.cons) or "—"
    lines.append("| Pros | Cons |")
    lines.append("|---|---|")
    lines.append(f"| {pros_cell} | {cons_cell} |")
    lines.append("")
    return lines


def to_markdown(understanding: GroundedUnderstanding, title: str) -> str:
    lines = [f"# {title}", ""]
    confirmed_count = 0
    inferred_count = 0

    for section in understanding.doc:
        lines.append(f"## {section.heading}")
        description = _SECTION_DESCRIPTIONS.get(section.heading)
        if description:
            lines.append(f"*{description}*")
        lines.append("")
        for claim in section.claims:
            lines.append(_md_claim_line(claim))
        lines.append("")

        for tradeoff in section.tradeoffs:
            lines.extend(_md_tradeoff_block(tradeoff))

        for claim in _all_claims(section):
            if claim.confidence == Confidence.CONFIRMED:
                confirmed_count += 1
            else:
                inferred_count += 1

    total = confirmed_count + inferred_count
    if total:
        lines.append("---")
        lines.append(
            f"**{confirmed_count} of {total}** claims confirmed against code, README, "
            f"git log, docs, or a direct answer. The rest are labeled reasoned judgment, not fact."
        )

    if understanding.pending_questions:
        lines.append("")
        lines.append(
            f"**{len(understanding.pending_questions)} question(s) pending** - "
            "answer them and re-run with `--answers` to upgrade the affected claims."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML export
# ---------------------------------------------------------------------------

_CSS = """\
:root{
  --paper:#f6f2e8; --surface:#fffcf4; --ink:#201d17; --ink-muted:#6d6656; --ink-faint:#948c76;
  --line:#ddd4bf; --confirmed:#1f6f5a; --confirmed-soft:#e1efe9; --inferred:#8a611a;
  --inferred-soft:#f3ead7; --gap:#a53f27; --gap-soft:#f5e2da; --focus:#1f6f5a;
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --paper:#16140f; --surface:#1d1a14; --ink:#ede7d8; --ink-muted:#a39a84; --ink-faint:#786f5b;
    --line:#3a3527; --confirmed:#5fbf9c; --confirmed-soft:#17332a; --inferred:#dba94e;
    --inferred-soft:#362c16; --gap:#e2795a; --gap-soft:#3a2019; --focus:#5fbf9c;
  }
}
:root[data-theme="dark"]{
  --paper:#16140f; --surface:#1d1a14; --ink:#ede7d8; --ink-muted:#a39a84; --ink-faint:#786f5b;
  --line:#3a3527; --confirmed:#5fbf9c; --confirmed-soft:#17332a; --inferred:#dba94e;
  --inferred-soft:#362c16; --gap:#e2795a; --gap-soft:#3a2019; --focus:#5fbf9c;
}
*{box-sizing:border-box;}
body{background:var(--paper); color:var(--ink); font-family:'Segoe UI',Arial,sans-serif; line-height:1.62; margin:0;}
a{color:var(--confirmed);}
.page{width:min(2400px,calc(100vw - clamp(24px,6vw,160px))); margin:0 auto; padding-block:clamp(32px,4vh,56px) 100px;}
header,.doc-nav,.explanation-content,footer{max-width:760px; margin-inline:auto;}
h1,h2{font-family:Georgia,serif; font-weight:600; text-wrap:balance; color:var(--ink);}
h1{font-size:clamp(1.9rem,4.4vw,2.5rem); line-height:1.14; margin:8px 0 10px;}
h2{font-size:1.44rem; margin:0 0 6px;}
p{margin:0 0 14px; max-width:62ch;}
p.lede{font-size:1.06rem; color:var(--ink-muted); max-width:60ch;}
strong{color:var(--ink); font-weight:600;}
.doc-nav{display:flex; flex-wrap:wrap; gap:2px 18px; font-family:Consolas,monospace; font-size:10.5px; letter-spacing:.05em; text-transform:uppercase; color:var(--ink-faint); margin-bottom:44px;}
.doc-nav a{color:var(--ink-muted); text-decoration:none; border-bottom:1px solid transparent;}
.doc-nav a:hover{color:var(--confirmed); border-color:var(--confirmed);}
.report-actions{display:flex; align-items:center; flex-wrap:wrap; gap:9px 12px; margin-top:18px;}
.print-button{appearance:none; cursor:pointer; border:1px solid var(--confirmed); border-radius:5px; padding:7px 11px; color:var(--surface); background:var(--confirmed); font:600 11px Consolas,monospace; letter-spacing:.02em;}
.print-button:hover{filter:brightness(.94);}
.print-hint{font-size:12.5px; color:var(--ink-muted); margin:0;}
section{margin-top:52px; scroll-margin-top:16px;}
.kicker{font-family:Consolas,monospace; font-size:11px; letter-spacing:.08em; text-transform:uppercase; color:var(--ink-faint); margin-bottom:6px;}
.section-desc{font-size:13.5px; color:var(--ink-muted); font-style:italic; margin:-4px 0 16px; max-width:58ch;}
.explanation-intro{background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:14px 16px; margin:18px 0 24px;}
.explanation-intro p{font-size:14px; color:var(--ink-muted); margin:0;}
.node-refs{display:flex; flex-wrap:wrap; gap:6px; margin-bottom:14px;}
.node-ref{font-family:Consolas,monospace; font-size:10.5px; color:var(--ink-muted); background:var(--surface); border:1px solid var(--line); border-radius:3px; padding:2px 7px; cursor:default;}
.node-ref.active{color:var(--confirmed); border-color:var(--confirmed); background:var(--confirmed-soft);}
.ev{display:inline-flex; align-items:baseline; gap:5px; font-family:Consolas,monospace; font-size:10.5px; padding:1.5px 7px 2px; border-radius:3px; white-space:nowrap; margin:0 2px; border:1px solid transparent;}
.ev.confirmed{background:var(--confirmed-soft); color:var(--confirmed); border-color:color-mix(in srgb, var(--confirmed) 35%, transparent);}
.ev.inferred{background:var(--inferred-soft); color:var(--inferred); border-color:color-mix(in srgb, var(--inferred) 35%, transparent);}
.claim-list{list-style:none; margin:0 0 8px; padding:0; display:flex; flex-direction:column; gap:14px;}
.claim-list li{padding-left:0; border-top:1px solid var(--line); padding-top:14px;}
.claim-list li:first-child{border-top:none; padding-top:0;}
code.inline{font-family:Consolas,monospace; font-size:.86em; background:var(--surface); border:1px solid var(--line); border-radius:4px; padding:.08em .38em;}
.tbl-wrap{overflow-x:auto; margin:18px 0 8px; border:1px solid var(--line); border-radius:8px;}
table{width:100%; border-collapse:collapse; font-size:13.6px; min-width:480px;}
th,td{text-align:left; padding:10px 13px; border-bottom:1px solid var(--line); vertical-align:top;}
th{font-family:Consolas,monospace; font-size:10.5px; letter-spacing:.04em; text-transform:uppercase; color:var(--ink-faint); font-weight:500; background:var(--surface);}
tr:last-child td{border-bottom:none;}
.tradeoff{margin:22px 0 6px; padding-top:18px; border-top:1px dashed var(--line);}
.tradeoff-label{font:10.5px Consolas,monospace; color:var(--ink-faint); letter-spacing:.06em; text-transform:uppercase; margin:0 0 6px;}
.tradeoff-decision{font-weight:600; margin-bottom:6px;}
.tradeoff-alts{font-size:12.5px; color:var(--ink-muted); font-style:italic; margin-bottom:4px;}
.tradeoff-table td{width:50%;}
.tradeoff-table .claim-list{gap:10px;}
.tradeoff-table .claim-list li{border-top:none; padding-top:0;}
.tradeoff-none{color:var(--ink-faint); font-style:italic; list-style:none;}
.output-section{margin-top:52px; width:100%;}
.output-heading{max-width:760px; margin-inline:auto;}
.diagram-controls{display:flex; flex-wrap:wrap; gap:8px; margin:18px 0 0;}
.diagram-mode-button{appearance:none; cursor:pointer; font:11px Consolas,monospace; letter-spacing:.03em; color:var(--ink-muted); background:var(--surface); border:1px solid var(--line); border-radius:999px; padding:7px 11px;}
.diagram-mode-button:hover{color:var(--ink); border-color:var(--ink-muted);}
.diagram-mode-button[aria-selected="true"]{color:var(--surface); background:var(--confirmed); border-color:var(--confirmed);}
.diagram-mode-note{font-size:12.5px; color:var(--ink-muted); max-width:none; margin:10px 0 -4px;}
.diagram-mode[hidden]{display:none;}
.diagram-frame{background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:0; overflow:hidden; margin:18px 0; min-height:clamp(620px,74vh,820px); height:min(78vh,980px);}
.diagram-frame iframe{display:block; width:100%; height:100%; border:0; background:#07111d;}
.diagram-canvas{max-width:100%; overflow:auto; scrollbar-gutter:stable both-edges; margin:18px 0; border-radius:12px;}
.diagram-canvas .diagram-frame{margin:0;}
.diagram-frame--full{width:1520px; min-width:1520px;}
.diagram-frame .empty{padding:24px; text-align:center; color:var(--ink-faint); font-family:Consolas,monospace; font-size:12.5px;}
.print-diagram-wrap{display:none;}
.legend{display:flex; gap:20px; align-items:center; font-size:12.5px; color:var(--ink-muted); margin-bottom:14px; flex-wrap:wrap;}
.legend .sw{display:inline-flex; align-items:center; gap:6px;}
.legend .sw i{width:11px; height:11px; border-radius:2.5px; display:inline-block;}
.stat-line{font-family:Consolas,monospace; font-size:13px; color:var(--ink-muted); margin-top:10px;}
.stat-line b{color:var(--ink); font-weight:600;}
.callout{background:var(--surface); border:1px solid var(--line); border-left:3px solid var(--gap); border-radius:0 8px 8px 0; padding:15px 18px; font-size:14px; margin:22px 0;}
.callout .lbl-top{font-family:Consolas,monospace; font-size:10.5px; text-transform:uppercase; letter-spacing:.06em; color:var(--gap); display:block; margin-bottom:6px;}
footer{margin-top:64px; padding-top:20px; border-top:1px solid var(--line); font-size:12.5px; color:var(--ink-faint); font-family:Consolas,monospace;}
@media(max-width:800px){.page{width:calc(100vw - 32px);padding-block-start:28px}.diagram-frame{min-height:520px;height:72vh}.diagram-frame--full{width:1260px;min-width:1260px}}
@page{size:A4; margin:14mm;}
@media print{
  :root{color-scheme:light;}
  *{-webkit-print-color-adjust:exact; print-color-adjust:exact;}
  html,body{background:#fff!important; color:#201d17!important;}
  body{font-size:10pt; line-height:1.48; padding:0;}
  .page{max-width:none; padding:0;}
  header,.explanation-content,footer{max-width:none;}
  .doc-nav,.report-actions,.diagram-modes{display:none!important;}
  .output-section{margin-top:24px;}
  h1{font-size:25pt;}
  h2{font-size:16pt; break-after:avoid-page;}
  .kicker{font-size:8.5pt;}
  .print-diagram-wrap{display:block!important; margin:12px 0 0; break-inside:avoid-page;}
  .print-diagram-caption{color:#6d6656; font-size:9pt; margin:0 0 7px;}
  .print-diagram{border:1px solid #ddd4bf; border-radius:6px; overflow:hidden;}
  .tbl-wrap{overflow:visible;}
  table{min-width:0; font-size:9.1pt;}
  th,td{padding:7px 8px;}
  .tradeoff{break-inside:avoid-page;}
  .node-refs{display:none;}
  footer{margin-top:30px;}
}
"""

_HIGHLIGHT_SCRIPT = """\
(function(){
  function activeFrame(){
    return document.querySelector('.diagram-mode:not([hidden]) iframe') || document.getElementById('graphitect-diagram-frame');
  }
  function selectMode(mode){
    document.querySelectorAll('[data-diagram-mode]').forEach(function(panel){
      panel.hidden = panel.getAttribute('data-diagram-mode') !== mode;
    });
    document.querySelectorAll('[data-diagram-mode-button]').forEach(function(button){
      button.setAttribute('aria-selected', String(button.getAttribute('data-diagram-mode-button') === mode));
    });
    var note = document.querySelector('[data-diagram-mode-note]');
    if (note) note.textContent = mode === 'sequence'
      ? 'Each arrow is a direct extracted Graphify calls/invokes edge. This is not runtime request telemetry, timing data, or a captured trace.'
      : mode === 'story'
        ? 'Each chapter follows directly observed component relationships. Present story only changes the stage; enable Live and Play story when you are ready for the paced walkthrough.'
        : mode === 'full'
          ? 'All detected relationships between the rendered components. On large codebases, those components are Graphify community rollups; this pane scrolls horizontally when needed.'
          : 'A readable structural subset. Switch to a Sequence trace, guided Workflow story, or Full architecture rollup for more detail.';
  }
  document.querySelectorAll('[data-diagram-mode-button]').forEach(function(button){
    button.addEventListener('click', function(){ selectMode(button.getAttribute('data-diagram-mode-button')); });
  });
  function useStoryFrame(callback){
    var frame = document.getElementById('graphitect-story-diagram-frame');
    if (!frame) return;
    function run(){
      try { if (frame.contentDocument) callback(frame.contentDocument); }
      catch (_) { /* Never break the report if a browser blocks iframe access. */ }
    }
    if (frame.contentDocument && frame.contentDocument.readyState === 'complete') run();
    else frame.addEventListener('load', run, {once:true});
  }
  function chooseStoryView(doc, id){
    if (!id) return;
    Array.prototype.slice.call(doc.querySelectorAll('[data-guided-view-id]')).some(function(button){
      if (button.getAttribute('data-guided-view-id') !== id) return false;
      button.click();
      return true;
    });
  }
  function runStory(options){
    selectMode('story');
    useStoryFrame(function(doc){
      chooseStoryView(doc, options.view);
      var present = doc.getElementById('btn-present');
      if (options.present && present && doc.documentElement.getAttribute('data-present') !== 'true') present.click();
      var motion = doc.getElementById('btn-motion');
      if (options.play && motion && motion.getAttribute('aria-pressed') !== 'true') motion.click();
      var play = doc.getElementById('guided-view-play');
      if (options.play && play && play.getAttribute('aria-pressed') !== 'true') play.click();
    });
  }
  var presentStory = document.querySelector('[data-present-story]');
  if (presentStory) presentStory.addEventListener('click', function(){ runStory({present:true, play:false}); });
  var query = new URLSearchParams(window.location.search);
  var hash = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  if (document.getElementById('graphitect-story-diagram-frame') &&
      (query.get('present') === '1' || query.get('play') === '1' || hash.get('view'))) {
    runStory({present:query.get('present') === '1', play:query.get('play') === '1', view:hash.get('view')});
  }
  var printButton = document.querySelector('[data-print-report]');
  if (printButton) printButton.addEventListener('click', function(){ window.print(); });
  var printHost = document.getElementById('graphitect-print-diagram');
  var printTemplate = document.getElementById('graphitect-print-diagram-template');
  if (printHost && printTemplate) {
    var printRoot = printHost.attachShadow ? printHost.attachShadow({mode:'open'}) : printHost;
    printRoot.appendChild(printTemplate.content.cloneNode(true));
  }
  function hoverDetails(doc){
    var element = doc.getElementById('graphitect-hover-details');
    if (!element) return {};
    try { return JSON.parse(element.textContent || '{}'); }
    catch (_) { return {}; }
  }
  function installDiagramHovers(frame){
    function install(doc){
      if (!doc || doc.getElementById('graphitect-node-tooltip')) return;
      var details = hoverDetails(doc);
      var tooltip = doc.createElement('div');
      tooltip.id = 'graphitect-node-tooltip';
      tooltip.setAttribute('role', 'tooltip');
      tooltip.style.cssText = 'position:fixed;z-index:2147483647;display:none;max-width:320px;padding:10px 12px;border:1px solid #5eead4;border-radius:8px;background:#061827;color:#e6fffb;box-shadow:0 8px 28px rgba(0,0,0,.42);font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;pointer-events:none;';
      doc.body.appendChild(tooltip);
      function nodeLabel(id){
        var node = doc.querySelector('[data-node-id="' + CSS.escape(id) + '"]');
        return node ? (node.getAttribute('data-node-label') || id) : id;
      }
      function linksFor(id, direction){
        var selector = direction === 'incoming' ? '[data-edge-to="' : '[data-edge-from="';
        var attribute = direction === 'incoming' ? 'data-edge-from' : 'data-edge-to';
        return Array.prototype.slice.call(doc.querySelectorAll(selector + CSS.escape(id) + '"]'))
          .slice(0, 3)
          .map(function(edge){
            var relation = edge.getAttribute('data-edge-label') || 'relates to';
            return relation + ' ' + nodeLabel(edge.getAttribute(attribute) || 'component');
          });
      }
      function addLine(text, strong){
        var line = doc.createElement('div');
        line.textContent = text;
        if (strong) line.style.fontWeight = '700';
        tooltip.appendChild(line);
      }
      function show(node, event){
        var id = node.getAttribute('data-node-id') || '';
        var detail = details[id] || {};
        tooltip.replaceChildren();
        addLine(node.getAttribute('data-node-label') || id, true);
        if (detail.summary) addLine(detail.summary);
        if (detail.symbol) addLine('Code symbol: ' + detail.symbol);
        var source = detail.source || node.getAttribute('data-node-sublabel');
        if (source) addLine('Source: ' + source);
        var incoming = linksFor(id, 'incoming');
        var outgoing = linksFor(id, 'outgoing');
        if (incoming.length) addLine('Incoming: ' + incoming.join('; '));
        if (outgoing.length) addLine('Outgoing: ' + outgoing.join('; '));
        var point = event && typeof event.clientX === 'number'
          ? {x:event.clientX, y:event.clientY}
          : (function(){ var box = node.getBoundingClientRect(); return {x:box.left, y:box.bottom}; })();
        tooltip.style.left = Math.max(8, Math.min(point.x + 14, doc.defaultView.innerWidth - 332)) + 'px';
        tooltip.style.top = Math.max(8, Math.min(point.y + 14, doc.defaultView.innerHeight - 180)) + 'px';
        tooltip.style.display = 'block';
      }
      function hide(){ tooltip.style.display = 'none'; }
      Array.prototype.slice.call(doc.querySelectorAll('[data-node-id]')).forEach(function(node){
        node.addEventListener('pointerenter', function(event){ show(node, event); });
        node.addEventListener('pointermove', function(event){ show(node, event); });
        node.addEventListener('pointerleave', hide);
        node.addEventListener('focus', function(){ show(node, null); });
        node.addEventListener('blur', hide);
        node.addEventListener('keydown', function(event){ if (event.key === 'Escape') hide(); });
      });
    }
    try {
      if (frame.contentDocument && frame.contentDocument.readyState === 'complete') install(frame.contentDocument);
      else frame.addEventListener('load', function(){ install(frame.contentDocument); }, {once:true});
    } catch (_) { /* Browsers may block iframe access; the report still works. */ }
  }
  document.querySelectorAll('.diagram-mode iframe').forEach(installDiagramHovers);
  document.querySelectorAll('[data-node-ref]').forEach(function(el){
    var id = el.getAttribute('data-node-ref');
    function target(){
      var frame = activeFrame();
      try { return frame && frame.contentDocument.querySelector('[id="' + id + '"]'); }
      catch (_) { return null; }
    }
    el.addEventListener('mouseenter', function(){
      var node = target();
      if (!node) return;
      el.classList.add('active');
      node.style.filter = 'drop-shadow(0 0 8px #5fbf9c)';
    });
    el.addEventListener('mouseleave', function(){
      var node = target();
      el.classList.remove('active');
      if (node) node.style.removeProperty('filter');
    });
  });
})();
"""


def _embed_hover_details(viewer_html: str, details: dict[str, dict[str, str]] | None) -> str:
    """Attach report-only, escaped hover metadata to an Archify viewer copy."""
    if not details:
        return viewer_html
    payload = json.dumps(details, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    marker = f'<script id="graphitect-hover-details" type="application/json">{payload}</script>'
    if "</body>" in viewer_html.lower():
        return re.sub(r"</body>", marker + "</body>", viewer_html, count=1, flags=re.IGNORECASE)
    return viewer_html + marker


def _present_embedded_viewer(viewer_html: str) -> str:
    """Start a complete Archify viewer in its responsive presentation stage.

    The report still embeds the original viewer HTML in ``iframe.srcdoc``.
    Presentation Stage only gives that live viewer the iframe's viewport; its
    pan/zoom, theme, route, story, and export controls remain its own runtime.
    """

    def replace_html_tag(match: re.Match[str]) -> str:
        attrs = re.sub(
            r'\sdata-present(?=\s|=|$)(?:\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]+))?',
            "",
            match.group(1),
            flags=re.IGNORECASE,
        )
        return f'<html{attrs} data-present="true">'

    return re.sub(r"<html\b([^>]*)>", replace_html_tag, viewer_html, count=1, flags=re.IGNORECASE)


def _esc(text: str) -> str:
    return html_lib.escape(text, quote=True)


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def _cite_str(claim: Claim) -> str:
    bits = _cite_bits(claim)
    return " &middot; " + "; ".join(_esc(b) for b in bits) if bits else ""


def _render_section_description(heading: str) -> str:
    description = _SECTION_DESCRIPTIONS.get(heading)
    return f'<p class="section-desc">{_esc(description)}</p>' if description else ""


def _render_node_refs(section: DesignDocSection, node_id_remap: dict[str, str] | None = None) -> str:
    if not section.related_node_ids:
        return ""
    remap = node_id_remap or {}
    # The pill's visible text always stays the real raw node id - that's the
    # actual citation and is meaningful to a reader. data-node-ref is what
    # _HIGHLIGHT_SCRIPT looks up in the rendered diagram, though, and once a
    # large graph's diagram is aggregated into community boxes those raw ids
    # no longer exist as SVG element ids at all - the highlight silently
    # matched nothing. remap.get(nid, nid) points the lookup at whatever id
    # the node actually renders as (unchanged when no aggregation happened).
    pills = "".join(
        f'<span class="node-ref" data-node-ref="{_esc(remap.get(nid, nid))}">{_esc(nid)}</span>'
        for nid in section.related_node_ids
    )
    return f'<div class="node-refs">{pills}</div>'


def _render_claim_items(claims: list[Claim]) -> str:
    """The <li> markup shared by _render_claim_list and the tradeoff
    pros/cons table cells - a claim's text + confidence badge + citation.
    """
    items = []
    for claim in claims:
        tag = claim.confidence.value
        items.append(
            f'<li><span>{_esc(claim.text)}</span> '
            f'<span class="ev {tag}">{tag}</span>{_cite_str(claim)}</li>'
        )
    return "".join(items)


def _render_claim_list(claims: list[Claim]) -> str:
    return f'<ul class="claim-list">{_render_claim_items(claims)}</ul>'


def _render_tradeoffs(section: DesignDocSection) -> str:
    """A real pros/cons comparison per significant decision (Naman's
    request, 12 Sep 2026) - not just the flat claim list above. Every piece
    of text here is still a confidence-tagged, cited Claim (decision, and
    each pro/con) so a table row can't read as more certain than the rest
    of the doc allows.
    """
    if not section.tradeoffs:
        return ""
    blocks = []
    for tradeoff in section.tradeoffs:
        decision = tradeoff.decision
        tag = decision.confidence.value
        alts_html = ""
        if tradeoff.alternatives_considered:
            alt_text = ", ".join(_esc(a) for a in tradeoff.alternatives_considered)
            alts_html = f'<div class="tradeoff-alts">Alternatives considered: {alt_text}</div>'
        pros_html = _render_claim_items(tradeoff.pros) or '<li class="tradeoff-none">None noted</li>'
        cons_html = _render_claim_items(tradeoff.cons) or '<li class="tradeoff-none">None noted</li>'
        blocks.append(
            '<div class="tradeoff">'
            '<p class="tradeoff-label">Decision analysis</p>'
            f'<div class="tradeoff-decision"><span>{_esc(decision.text)}</span> '
            f'<span class="ev {tag}">{tag}</span>{_cite_str(decision)}</div>'
            f"{alts_html}"
            '<div class="tbl-wrap"><table class="tradeoff-table">'
            "<thead><tr><th>Pros</th><th>Cons</th></tr></thead>"
            f'<tbody><tr><td><ul class="claim-list">{pros_html}</ul></td>'
            f'<td><ul class="claim-list">{cons_html}</ul></td></tr></tbody>'
            "</table></div>"
            "</div>"
        )
    return "".join(blocks)


def _render_claim_table(claims: list[Claim]) -> str:
    rows = []
    for claim in claims:
        tag = claim.confidence.value
        rows.append(
            f"<tr><td>{_esc(claim.text)}</td>"
            f'<td><span class="ev {tag}">{tag}</span>{_cite_str(claim)}</td></tr>'
        )
    return (
        '<div class="tbl-wrap choice-table"><table><thead><tr><th>Choice &amp; reasoning</th><th>Evidence</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table></div>"
    )

# Sections with these headings render as a table (claim + confidence side by
# side reads better for a list of discrete choices); everything else renders
# as the claim-list bullet style - matches the visual convention of prior
# hand-authored graphitect docs (see the git-resume-agent design doc).
_TABLE_SECTIONS = {"Technology choices & why"}


def _render_bar_chart(understanding: GroundedUnderstanding) -> str:
    """A confirmed/inferred bar per section, computed from the actual claim
    data - not hand-tuned per document the way earlier prototype docs were.
    """
    rows = []
    for section in understanding.doc:
        all_claims = _all_claims(section)
        confirmed = sum(1 for c in all_claims if c.confidence == Confidence.CONFIRMED)
        inferred = sum(1 for c in all_claims if c.confidence == Confidence.INFERRED)
        if confirmed or inferred:
            rows.append((section.heading, confirmed, inferred))
    if not rows:
        return ""

    row_h, gap, bar_x, px_per_claim = 26, 14, 160, 34
    height = len(rows) * (row_h + gap) + gap
    max_total = max(c + i for _, c, i in rows) or 1
    width = bar_x + max_total * px_per_claim + 60

    svg_rows = []
    for idx, (heading, confirmed, inferred) in enumerate(rows):
        y = gap + idx * (row_h + gap)
        svg_rows.append(
            f'<text x="{bar_x - 10}" y="{y + row_h * 0.7:.0f}" text-anchor="end" '
            f'font-size="11.5" fill="var(--ink-muted)">{_esc(heading)}</text>'
        )
        x = bar_x
        if confirmed:
            w = confirmed * px_per_claim
            svg_rows.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{row_h}" rx="4" fill="var(--confirmed)"/>'
                f'<text x="{x + w/2:.0f}" y="{y + row_h*0.68:.0f}" text-anchor="middle" '
                f'font-size="10.5" fill="#fff" font-weight="600">{confirmed}</text>'
            )
            x += w
        if inferred:
            w = inferred * px_per_claim
            svg_rows.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{row_h}" rx="4" fill="var(--inferred)"/>'
                f'<text x="{x + w/2:.0f}" y="{y + row_h*0.68:.0f}" text-anchor="middle" '
                f'font-size="10.5" fill="var(--ink)" font-weight="600">{inferred}</text>'
            )

    return (
        f'<div class="diagram-frame" style="padding:16px;">'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="Confirmed versus inferred claim counts by section.">'
        f'<g font-family="Source Sans 3, sans-serif">{"".join(svg_rows)}</g></svg></div>'
    )


def _extract_diagram_assets(diagram_html_or_svg: str) -> tuple[str, str]:
    """Extract archify's `<svg>` plus whichever of its `<style>` blocks the
    SVG actually depends on, from a full rendered HTML page.

    Confirmed live (12 Sep 2026) that a bare svg-markup extraction isn't
    enough: archify's diagram styles its elements entirely through CSS
    classes (`c-backend`, `m-security`, etc.) defined in the page's own
    `<style>` blocks, not inline attributes - embedding the SVG alone
    rendered as an unstyled, visually broken pattern-fill mess instead of a
    real diagram. The style block(s) get returned separately (not spliced
    into the svg itself) because they must be mounted in a shadow root, not
    the light DOM - archify's stylesheet defines global `body {}` and
    `:root` rules that would otherwise leak out and collide with this page's
    own styling (also confirmed live, not theoretical).

    Only style blocks that actually reference a class used inside the
    extracted SVG are kept - archify's page also ships a large embedded
    webfont block that has nothing to do with rendering the diagram
    correctly and would otherwise roughly double the file size for no
    visual benefit.

    Falls back to (input, "") unchanged if it's already a bare SVG (starts
    with `<svg`, no surrounding page to pull styles from).
    """
    lower = diagram_html_or_svg.lower()
    start = lower.find("<svg")
    if start == -1:
        return diagram_html_or_svg, ""
    end = lower.rfind("</svg>")
    if end == -1:
        return diagram_html_or_svg, ""
    svg = diagram_html_or_svg[start : end + len("</svg>")]

    used_classes = set(re.findall(r'class="([^"]+)"', svg))
    class_tokens = {tok for group in used_classes for tok in group.split()}
    if not class_tokens:
        return svg, ""

    style_blocks = re.findall(r"<style[^>]*>(.*?)</style>", diagram_html_or_svg, re.DOTALL)
    relevant = [
        block for block in style_blocks if any(f".{tok}" in block for tok in class_tokens)
    ]
    combined = "\n".join(relevant)
    # :root only ever matches the real document root, never a shadow tree -
    # confirmed live: the theme CSS custom properties (--bg, --text, ...)
    # archify defines on :root silently resolved to nothing inside the
    # shadow root, rendering as a solid black box with invisible text
    # instead of the intended dark-themed diagram with visible labels.
    # :host is the shadow-DOM equivalent - rewriting makes those variables
    # actually resolve within the mounted tree.
    combined = re.sub(r":root\b", ":host", combined)
    return svg, combined


def to_html(
    understanding: GroundedUnderstanding,
    title: str,
    *,
    diagram_svg: str | None = None,
    story_diagram_svg: str | None = None,
    story_hover_details: dict[str, dict[str, str]] | None = None,
    sequence_diagram_svg: str | None = None,
    full_diagram_svg: str | None = None,
    story_chapter_count: int | None = None,
    sequence_message_count: int | None = None,
    overview_connection_count: int | None = None,
    full_connection_count: int | None = None,
    node_id_remap: dict[str, str] | None = None,
    explanation_available: bool = True,
    diagram_opted_out: bool = False,
) -> str:
    """Render one HTML report containing the complete Archify viewer.

    ``diagram_svg`` keeps its old public name for compatibility, but now
    accepts the complete Archify HTML artifact. ``iframe.srcdoc`` starts its
    responsive presentation stage while preserving themes, navigation, route
    tools, guided views, and exports without a second output file.
    """
    visible_doc = understanding.doc if explanation_available else []
    nav_items = [("Diagram", "interactive-diagram"), ("Explanation", "project-explanation")]
    nav_items.extend((s.heading, _slug(s.heading)) for s in visible_doc)
    nav_html = "".join(f'<a href="#{slug}">{_esc(h)}</a>' for h, slug in nav_items)

    sections_html = []
    confirmed_total = inferred_total = 0
    for section in visible_doc:
        for c in _all_claims(section):
            if c.confidence == Confidence.CONFIRMED:
                confirmed_total += 1
            else:
                inferred_total += 1
        body = (
            _render_claim_table(section.claims)
            if section.heading in _TABLE_SECTIONS
            else _render_claim_list(section.claims)
        )
        sections_html.append(
            f'<section id="{_slug(section.heading)}">'
            f'<h2>{_esc(section.heading)}</h2>'
            f"{_render_section_description(section.heading)}"
            f"{_render_node_refs(section, node_id_remap)}"
            f"{body}"
            f"{_render_tradeoffs(section)}"
            f"</section>"
        )

    if diagram_svg:
        viewer_srcdoc = html_lib.escape(_present_embedded_viewer(diagram_svg), quote=True)
        printable_svg, printable_styles = _extract_diagram_assets(diagram_svg)
        print_diagram_block = (
            '<div class="print-diagram-wrap">'
            '<p class="print-diagram-caption">Printable structural overview. Open the HTML report to explore the full graph.</p>'
            '<div class="print-diagram" id="graphitect-print-diagram"></div>'
            '<template id="graphitect-print-diagram-template">'
            '<style>:host{display:block;-webkit-print-color-adjust:exact;print-color-adjust:exact;}'
            ':host svg{display:block;width:100%!important;height:auto!important;}</style>'
            f'<style>{printable_styles}</style>{printable_svg}</template></div>'
        )
        if sequence_diagram_svg or story_diagram_svg or full_diagram_svg:
            overview_label = "Overview"
            if overview_connection_count is not None:
                overview_label += f" · {overview_connection_count} key links"
            full_label = "Full architecture rollup"
            if full_connection_count is not None:
                full_label += f" · {full_connection_count} relationships"
            sequence_label = "Sequence trace"
            if sequence_message_count is not None:
                sequence_label += f" · {sequence_message_count} direct calls"
            story_label = "Workflow story"
            if story_chapter_count is not None:
                story_label += f" · {story_chapter_count} chapters"

            # A direct static call chain is the clearest non-jumpy starting
            # point. Fall back to the workflow, then the structural overview.
            active_mode = (
                "sequence"
                if sequence_diagram_svg
                else "story"
                if story_diagram_svg
                else "overview"
            )

            def mode_button(mode: str, label: str) -> str:
                return (
                    f'<button type="button" class="diagram-mode-button" '
                    f'data-diagram-mode-button="{mode}" '
                    f'aria-selected="{str(mode == active_mode).lower()}">{_esc(label)}</button>'
                )

            def mode_panel(mode: str, frame_id: str, frame_title: str, srcdoc: str) -> str:
                hidden = " hidden" if mode != active_mode else ""
                return (
                    f'<div class="diagram-mode" data-diagram-mode="{mode}"{hidden}>'
                    '<div class="diagram-frame">'
                    f'<iframe id="{frame_id}" title="{frame_title}" srcdoc="{srcdoc}"></iframe>'
                    '</div></div>'
                )

            controls = []
            panels = []
            if sequence_diagram_svg:
                sequence_viewer_srcdoc = html_lib.escape(
                    _present_embedded_viewer(sequence_diagram_svg), quote=True
                )
                controls.append(mode_button("sequence", sequence_label))
                panels.append(
                    mode_panel(
                        "sequence",
                        "graphitect-sequence-diagram-frame",
                        "Evidence-gated Archify static call sequence",
                        sequence_viewer_srcdoc,
                    )
                )
            if story_diagram_svg:
                story_viewer_srcdoc = html_lib.escape(
                    _present_embedded_viewer(
                        _embed_hover_details(story_diagram_svg, story_hover_details)
                    ),
                    quote=True,
                )
                controls.append(mode_button("story", story_label))
                panels.append(
                    mode_panel(
                        "story",
                        "graphitect-story-diagram-frame",
                        "Guided Archify workflow story",
                        story_viewer_srcdoc,
                    )
                )
            controls.append(mode_button("overview", overview_label))
            panels.append(
                mode_panel(
                    "overview",
                    "graphitect-diagram-frame",
                    "Overview Archify system diagram",
                    viewer_srcdoc,
                )
            )
            if full_diagram_svg:
                full_viewer_srcdoc = html_lib.escape(
                    _present_embedded_viewer(full_diagram_svg), quote=True
                )
                controls.append(mode_button("full", full_label))
                full_hidden = " hidden" if active_mode != "full" else ""
                panels.append(
                    f'<div class="diagram-mode" data-diagram-mode="full"{full_hidden}>'
                    '<div class="diagram-canvas" aria-label="Scrollable full relationship graph">'
                    '<div class="diagram-frame diagram-frame--full">'
                    '<iframe id="graphitect-full-diagram-frame" title="Full Archify architecture rollup" '
                    f'srcdoc="{full_viewer_srcdoc}"></iframe></div></div></div>'
                )
            if story_diagram_svg:
                controls.append(
                    '<button type="button" class="diagram-mode-button" data-present-story>Present story</button>'
                )

            initial_note = (
                'Each arrow is a direct extracted Graphify calls/invokes edge. This is not runtime request telemetry, timing data, or a captured trace.'
                if active_mode == "sequence"
                else 'The workflow follows one direct code path in order. Present story only changes the stage; enable Live and Play story when you are ready for the paced walkthrough.'
                if active_mode == "story"
                else 'A readable structural subset. Switch modes to inspect the evidence-gated sequence, workflow, or full relationship rollup.'
            )
            diagram_block = (
                '<div class="diagram-modes">'
                '<div class="diagram-controls" role="tablist" aria-label="Diagram detail">'
                f'{"".join(controls)}</div>'
                '<p class="diagram-mode-note" data-diagram-mode-note aria-live="polite">'
                f'{initial_note}</p>{"".join(panels)}</div>'
            )
        else:
            diagram_block = (
                '<div class="diagram-frame">'
                '<iframe id="graphitect-diagram-frame" title="Interactive Archify system diagram" '
                f'srcdoc="{viewer_srcdoc}"></iframe>'
                '</div>'
            )
    else:
        print_diagram_block = ""
        empty_message = (
            "Diagram generation was explicitly opted out."
            if diagram_opted_out
            else "The bundled Archify renderer could not produce a diagram."
        )
        diagram_block = (
            '<div class="diagram-frame"><div class="empty">'
            f"{_esc(empty_message)}</div></div>"
        )

    total = confirmed_total + inferred_total
    stat_line = (
        f'<p class="stat-line"><b>{confirmed_total} of {total}</b> claims confirmed against '
        "code, README, git log, docs, or a direct answer. The rest are labeled reasoned "
        "judgment, not fact.</p>"
        if total
        else ""
    )
    pending_callout = (
        f'<div class="callout"><span class="lbl-top">Pending</span>'
        f"<p><b>{len(understanding.pending_questions)}</b> question(s) still unanswered - "
        "answer them and re-run with <code class=\"inline\">--answers</code> to upgrade the "
        "affected claims.</p></div>"
        if explanation_available and understanding.pending_questions
        else ""
    )

    if explanation_available:
        explanation_block = (
            '<div class="explanation-intro"><p>Each statement explains what the repository shows, why a choice matters here, and what it costs. '
            'Source-backed facts are marked confirmed; reasoned analysis is marked inferred.</p></div>'
            '<div class="legend">'
            '<span class="sw"><i style="background:var(--confirmed)"></i> confirmed</span>'
            '<span class="sw"><i style="background:var(--inferred)"></i> inferred</span>'
            '</div>'
            f"{''.join(sections_html)}{_render_bar_chart(understanding)}{stat_line}{pending_callout}"
        )
    else:
        explanation_block = (
            '<div class="callout"><span class="lbl-top">LLM required</span>'
            '<p>Project rationale, technology choices, trade-offs, and pros and cons were not '
            'generated because no LLM API key was available.</p></div>'
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(title)}</title>
<style>{_CSS}</style>
</head><body><div class="page">
<header><p class="kicker">graphitect</p><h1>{_esc(title)}</h1>
  <div class="report-actions"><button type="button" class="print-button" data-print-report>Print / save PDF</button>
  <p class="print-hint">Uses your browser's Save as PDF option.</p></div></header>
<nav class="doc-nav">{nav_html}</nav>
<section class="output-section" id="interactive-diagram">
  <div class="output-heading"><p class="kicker">Section 1</p><h2>Interactive system diagram</h2></div>
  {diagram_block}{print_diagram_block}
</section>
<section class="output-section explanation-content" id="project-explanation">
  <p class="kicker">Section 2</p><h2>Project explanation</h2>
  {explanation_block}
</section>
<footer>Generated by graphitect - graph(ify) + (arch)itect.</footer>
</div>
<script>{_HIGHLIGHT_SCRIPT}</script>
</body></html>
"""
