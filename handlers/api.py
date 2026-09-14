"""API Gateway entry point: Mangum adapter over the FastAPI app."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from mangum import Mangum  # noqa: E402

from trustsight.api.main import app  # noqa: E402

handler = Mangum(app, lifespan="off")
