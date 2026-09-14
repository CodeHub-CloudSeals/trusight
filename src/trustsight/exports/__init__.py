"""Client-facing output pack.

An estimator does not accept a JSON payload as a deliverable. What leaves
this system has to look like the bar list they already work from, carry the
same columns, and state plainly what is released and what is not.

Three principles hold across every format here:

* **The schedule is never silently complete.** Unreleased lines still
  appear, marked, because a schedule that quietly drops its unresolved
  items is exactly the document this product exists to prevent.
* **The data basis is on the page.** A sample walkthrough and a real
  estimate must not be distinguishable only by who remembers which was run.
* **Nothing is recalculated here.** These writers format what the engine
  produced. A number that differs between the screen and the PDF is a
  defect, so there is only one source for it.
"""
from .schedule import (  # noqa: F401
    ExportUnavailable,
    bbs_rows,
    exceptions_csv,
    exceptions_pdf,
    schedule_csv,
    schedule_pdf,
    schedule_xlsx,
    weight_summary,
    xlsx_available,
)
