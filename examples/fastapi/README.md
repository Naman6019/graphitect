# FastAPI Architecture Teardown Demo

This directory contains the real-world architecture teardown and system topology extracted from the [`fastapi/fastapi`](https://github.com/fastapi/fastapi) repository using **Graphitect**.

## Artifacts Generated

1. **`fastapi-architecture-carousel.pdf`**:
   - 7-slide visual architecture teardown designed for LinkedIn document carousel sharing (4:5 aspect ratio, mobile-optimized dark mode).
   - Covers: Core ASGI inheritance (`FastAPI(Starlette)`), decoupled routing (`APIRouter` / `APIRoute`), declarative DI DAG resolution (`Depends` / `Dependant`), OpenAPI 3.1.0 schema generation, and architectural tradeoffs.

2. **`fastapi-architecture-report.pdf`**:
   - Printable 4-page technical architecture report with verified code-level citations and grounded findings.

3. **`fastapi-architecture.html`**:
   - Interactive standalone HTML report with Cytoscape/WebGL dependency graph exploration (8,699 AST nodes, 16,066 edges, 818 communities) and verified design doc.

## How to Reproduce

```bash
# 1. Clone any target repository
git clone --depth 1 https://github.com/fastapi/fastapi.git

# 2. Extract deterministic AST structure
graphitect agent prepare ./fastapi -o .graphitect-agent

# 3. Compile report with verified narrative
graphitect agent compile .graphitect-agent --title "FastAPI Architecture" -o fastapi-architecture.html
```
