"""EU AI Act guardrail: priority prefix + first-message disclosure gating."""

from app.services.compliance import apply_compliance, COMPLIANCE_PREFIX, DISCLOSURE_HINT

OWNER = 'Du bist der Assistent von Muster GmbH. Sage, dass du Anna heisst.'
# A phrase unique to the disclosure hint (not present in the always-on prefix).
_DISC_MARKER = 'nur für diese erste Antwort'


def test_prefix_is_prepended_first():
    r = apply_compliance(OWNER, is_new_conversation=False)
    assert r.startswith(COMPLIANCE_PREFIX)


def test_owner_prompt_is_preserved_after_prefix():
    r = apply_compliance(OWNER, is_new_conversation=False)
    assert OWNER in r
    assert r.index('VORRANG') < r.index('Muster GmbH')


def test_disclosure_present_on_new_conversation():
    r = apply_compliance(OWNER, is_new_conversation=True)
    assert _DISC_MARKER in r
    assert DISCLOSURE_HINT in r


def test_disclosure_absent_on_returning_conversation():
    r = apply_compliance(OWNER, is_new_conversation=False)
    assert _DISC_MARKER not in r


def test_empty_owner_prompt_still_gets_prefix():
    r = apply_compliance('', is_new_conversation=False)
    assert r.startswith(COMPLIANCE_PREFIX)
