"""archify's real architecture-mode IR - checked directly against
archify/references/authoring-contract.md and archify/renderers/architecture/grid.mjs
(not inferred from its README). See plan.md section 04's "Correction" note.

Deliberately narrower than archify's full schema: this covers what
graphitect.deliver.archify_adapter needs to emit (grid-mode architecture
diagrams), not every mode/field archify's authoring contract supports.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


def _drop_empty_lists(value):
    if isinstance(value, dict):
        return {k: _drop_empty_lists(v) for k, v in value.items() if not (isinstance(v, list) and not v)}
    if isinstance(value, list):
        return [_drop_empty_lists(v) for v in value]
    return value

ComponentType = Literal[
    "frontend", "backend", "database", "cloud", "security", "messagebus", "external"
]
ConnectionVariant = Literal["default", "emphasis", "security", "dashed"]
ConnectionSide = Literal["left", "right", "top", "bottom"]
ConnectionRoute = Literal["auto", "straight", "orthogonal-h", "orthogonal-v"]


class GridLayout(BaseModel):
    mode: Literal["grid"] = "grid"
    cols: int = 4
    # archify's own DEFAULT_GRID (renderers/architecture/grid.mjs) is
    # {cellW: 130, gapX: 30, gapY: 40} - fine for short labels, but confirmed
    # live to produce "label wider than component" and label/connection
    # overlap failures on a real graph with longer labels ("Schema
    # Discoverer Agent") and vertically-adjacent connected components.
    # graphitect widens these based on actual label length instead of
    # leaving archify's short-label default in place - see
    # archify_adapter.to_architecture_ir.
    cell_w: int | None = Field(default=None, alias="cellW")
    cell_h: int | None = Field(default=None, alias="cellH")
    gap_x: int | None = Field(default=None, alias="gapX")
    gap_y: int | None = Field(default=None, alias="gapY")

    model_config = {"populate_by_name": True}


class Component(BaseModel):
    id: str
    type: ComponentType
    label: str
    sublabel: str | None = None
    row: int | None = None
    col: int | None = None
    pos: tuple[int, int] | None = None  # explicit pixel pos wins over row/col
    size: tuple[int, int] | None = None
    tag: str | None = None
    sources: list[dict] = Field(default_factory=list)  # repo-evidence links, see authoring-contract.md


class Boundary(BaseModel):
    kind: str  # e.g. "region", "security-group"
    label: str
    wraps: list[str]  # component ids


class Connection(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: str
    label: str | None = None
    variant: ConnectionVariant = "default"
    # Routing/label-position overrides - unused by archify_adapter's own
    # deterministic layout (it never sets these), but archify_repair's LLM
    # loop writes them to resolve real validator diagnostics like
    # clean-flow/edge-through-node and layout/constraint (label overlaps
    # component) - see archify/references/authoring-contract.md and
    # archify/schemas/architecture.schema.json's Connection definition,
    # confirmed against the real schema rather than guessed.
    from_side: ConnectionSide | None = Field(default=None, alias="fromSide")
    to_side: ConnectionSide | None = Field(default=None, alias="toSide")
    route: ConnectionRoute | None = None
    via: list[tuple[float, float]] | None = None
    label_at: tuple[float, float] | None = Field(default=None, alias="labelAt")
    label_dx: float | None = Field(default=None, alias="labelDx")
    label_dy: float | None = Field(default=None, alias="labelDy")
    label_segment: int | None = Field(default=None, alias="labelSegment")

    model_config = {"populate_by_name": True}


class Card(BaseModel):
    dot: str
    title: str
    items: list[str]


class GuidedView(BaseModel):
    """One evidence-grounded chapter in Archify's guided-story rail."""

    id: str
    label: str
    focus: list[str]
    note: str | None = None


class Meta(BaseModel):
    title: str
    # Architecture's schema supports an explicit [width, height] canvas.
    # Graphitect uses it when a safe detour lane extends past the rightmost
    # component; otherwise Archify's automatic component-only bounds clip
    # the valid connection geometry.
    view_box: tuple[int, int] | None = Field(default=None, alias="viewBox")
    # "draft" was never a real archify value - confirmed against its own
    # --help output (`--quality standard|showcase`) - and defaulting to
    # "showcase" made every real multi-node graph fail archify's strict
    # layout validator (labels wider than boxes, overlapping connections,
    # crossing corridors) since graphitect's own grid layout is a simple
    # heuristic, not hand-tuned for showcase-grade spacing rules. "standard"
    # is the honest default until the layout heuristic itself improves
    # (wider boxes for long labels, real connection routing) rather than
    # just asking archify to be less strict about a still-rough auto-layout.
    quality_profile: Literal["standard", "showcase"] = "standard"
    # Story uses Archify's own focused chapter rail and optional trace
    # motion. Overview and Full rollup stay still until the reader asks.
    animation: Literal["none", "trace"] = "none"
    visual_preset: Literal["classic", "signal-flow", "blueprint", "editorial"] | None = None
    views: list[GuidedView] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class ArchitectureIR(BaseModel):
    """A complete document for `archify validate` / `archify deliver`."""

    schema_version: int = 1
    diagram_type: Literal["architecture"] = "architecture"
    meta: Meta
    layout: GridLayout = Field(default_factory=GridLayout)
    components: list[Component]
    boundaries: list[Boundary] = Field(default_factory=list)
    connections: list[Connection]
    cards: list[Card] = Field(default_factory=list)

    def model_dump_archify(self) -> dict:
        """Serialize with `from`/`to` un-aliased back to archify's own field
        names, and with empty-list fields dropped entirely rather than
        emitted as `[]`.

        Confirmed live (12 Sep 2026) against the real `archify validate`:
        archify's schema requires `minItems: 1` on array fields like a
        component's `sources` whenever the key is present at all - pydantic's
        own `default_factory=list` means every Component always carries
        `sources: []` unless explicitly stripped, which fails validation with
        exactly the field populated by default and never actually used.
        """
        return _drop_empty_lists(self.model_dump(by_alias=True, exclude_none=True))
