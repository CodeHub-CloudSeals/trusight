"""Evidence chain: append-only, tamper-evident, release-gating."""
from trustsight.evidence.fabric import ClaimType, EvidenceChain


def _full_chain(rulebook_approved: bool = True) -> EvidenceChain:
    c = EvidenceChain("run-test")
    c.append(claim_type=ClaimType.EXTRACTION, subject="P1", value=1)
    c.append(claim_type=ClaimType.INTERPRETATION, subject="P1", value=1)
    c.append(claim_type=ClaimType.RULE_APPLICATION, subject="P1", value=1,
             rulebook_approved=rulebook_approved)
    c.append(claim_type=ClaimType.CALCULATION, subject="P1", value=1)
    return c


def test_chain_verifies():
    ok, msg = _full_chain().verify()
    assert ok, msg


def test_tampering_is_detected():
    c = _full_chain()
    c.records[1].value = 999
    ok, _ = c.verify()
    assert not ok


def test_incomplete_chain_blocks_release():
    c = EvidenceChain("run-test")
    c.append(claim_type=ClaimType.EXTRACTION, subject="P1", value=1)
    releasable, missing = c.is_releasable("P1")
    assert not releasable
    assert "calculation" in missing


def test_complete_chain_allows_release():
    releasable, missing = _full_chain().is_releasable("P1")
    assert releasable and not missing


def test_complete_chain_on_unapproved_rulebook_does_not_release():
    """A complete chain is necessary but not sufficient: an unapproved
    rulebook holds the item at REVIEW rather than releasing it."""
    releasable, missing = _full_chain(rulebook_approved=False).is_releasable("P1")
    assert not releasable
    assert not missing, "the chain is complete; the block is policy, not evidence"


def test_correction_supersedes_rather_than_edits():
    c = _full_chain()
    original = c.records[0].record_id
    c.correct(original, subject="P1", value=2)
    assert c.records[-1].supersedes == original
    assert len(c.records) == 5          # nothing was removed
    assert c.verify()[0]
