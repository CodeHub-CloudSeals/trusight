"""Lambda entry points. Each handler is idempotent and keyed by
(run_id, step_no, input_hash) so Step Functions may retry safely."""
