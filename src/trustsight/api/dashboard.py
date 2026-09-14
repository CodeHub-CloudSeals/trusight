"""Local, dependency-free presenter workspace backed by the actual API."""
from pathlib import Path

DASHBOARD_HTML = (Path(__file__).parent / "static" / "index.html").read_text()
