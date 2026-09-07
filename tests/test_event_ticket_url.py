"""Unit coverage for app.services.event_ticket_url.

plans/260906_events-venue-bairro-and-ticket-url.md §3 (defect C2). The nine
rules pinned there are the CROSS-REPO contract vibes_bot and the mobile app
plan against, so every rule gets its own row here, including the two
counter-examples review finding F30 produced: `evenyx.com:8080/lote2` must
be QUALIFIED (revision 1's scheme regex rejected it because `.` was inside
the scheme character class) and `evenyx.com:lote2` must be REJECTED (so the
port branch that fixes the first case cannot become a hole).

The literal production value at the time of writing is `"evenyx.com"` — the
only `ticket_url` in the whole live corpus — and it is asserted by name.
"""
from __future__ import annotations

import pytest

from app.services.event_ticket_url import (
    MAX_TICKET_URL_LENGTH,
    OUTCOME_ABSENT,
    OUTCOME_PASSTHROUGH,
    OUTCOME_REJECTED,
    OUTCOME_SCHEME_ADDED,
    classify_ticket_url,
    normalize_ticket_url,
)

# (stored value, normalised value, outcome label)
_CASES = [
    # ── rule 8: a scheme-less host is qualified with https ───────────────
    ("evenyx.com", "https://evenyx.com", OUTCOME_SCHEME_ADDED),  # the live value
    ("www.sympla.com.br", "https://www.sympla.com.br", OUTCOME_SCHEME_ADDED),
    (
        "evenyx.com/e/42?ref=ig#lote2",
        "https://evenyx.com/e/42?ref=ig#lote2",
        OUTCOME_SCHEME_ADDED,
    ),
    ("sympla.com.br/evento/123", "https://sympla.com.br/evento/123", OUTCOME_SCHEME_ADDED),
    # F30: host:port survives, and the port is preserved in the output.
    ("evenyx.com:8080/lote2", "https://evenyx.com:8080/lote2", OUTCOME_SCHEME_ADDED),
    ("evenyx.com:8080", "https://evenyx.com:8080", OUTCOME_SCHEME_ADDED),
    # F30's sibling: a non-numeric pseudo-port is not a port, so the whole
    # value is prose, not a link.
    ("evenyx.com:lote2", None, OUTCOME_REJECTED),
    ("evenyx.com:99999999", None, OUTCOME_REJECTED),  # 8 digits is not a port
    # ── the port must be a REAL TCP port, not merely 1-5 digits ──────────
    # `_PORT_RE = ^[0-9]{1,5}$` counted digits only, so `:80808` and
    # `:99999` were qualified into `https://evenyx.com:80808/x` — a link no
    # client can open — and counted `scheme_added`, i.e. a success. Found by
    # vibes_bot's serve-time twin; fixed HERE because cs-server is the
    # canonical writer of the projection.
    ("evenyx.com:80808/x", None, OUTCOME_REJECTED),
    ("evenyx.com:99999/x", None, OUTCOME_REJECTED),
    ("evenyx.com:65536/x", None, OUTCOME_REJECTED),   # one past the bound
    # ...and the bound itself, plus every legitimate port, still passes.
    ("evenyx.com:65535/x", "https://evenyx.com:65535/x", OUTCOME_SCHEME_ADDED),
    ("evenyx.com:8080/lote2#a", "https://evenyx.com:8080/lote2#a", OUTCOME_SCHEME_ADDED),
    # Edge values, pinned because vibes_bot pins them identically: `:0` and a
    # leading-zero port are accepted (the rule is a numeric bound, not a
    # reachability check), an EMPTY port suffix is not a port at all.
    ("evenyx.com:0/x", "https://evenyx.com:0/x", OUTCOME_SCHEME_ADDED),
    ("evenyx.com:0", "https://evenyx.com:0", OUTCOME_SCHEME_ADDED),
    ("evenyx.com:0080/x", "https://evenyx.com:0080/x", OUTCOME_SCHEME_ADDED),
    ("evenyx.com:", None, OUTCOME_REJECTED),
    ("evenyx.com:/x", None, OUTCOME_REJECTED),
    # rule 7 (protocol-relative) and rule 5 (already absolute) share
    # `_authority_is_valid`, so both inherit the bound.
    ("//evenyx.com:80808/x", None, OUTCOME_REJECTED),
    ("https://evenyx.com:80808/x", None, OUTCOME_REJECTED),
    ("https://evenyx.com:65536/x", None, OUTCOME_REJECTED),
    ("https://evenyx.com:/x", None, OUTCOME_REJECTED),
    (
        "https://evenyx.com:65535/x",
        "https://evenyx.com:65535/x",
        OUTCOME_PASSTHROUGH,
    ),
    # ── rule 7: protocol-relative ────────────────────────────────────────
    ("//evenyx.com/e/42", "https://evenyx.com/e/42", OUTCOME_SCHEME_ADDED),
    # ── rule 5: already absolute, returned untouched ─────────────────────
    (
        "https://sympla.com.br/evento/123",
        "https://sympla.com.br/evento/123",
        OUTCOME_PASSTHROUGH,
    ),
    # NEVER upgraded to https — the stored scheme is the stored scheme.
    ("http://ingressos.example.com/x", "http://ingressos.example.com/x", OUTCOME_PASSTHROUGH),
    ("HTTPS://SYMPLA.COM.BR/X", "HTTPS://SYMPLA.COM.BR/X", OUTCOME_PASSTHROUGH),
    ("HtTp://sympla.com.br", "HtTp://sympla.com.br", OUTCOME_PASSTHROUGH),
    # ── rule 5's authority must still be a real host ─────────────────────
    # A scheme with nothing behind it is not "an absolute http/https URL".
    # It used to pass through as a live ticket_url AND be counted a success,
    # so the dead button was invisible to the `rejected` counter.
    ("https://", None, OUTCOME_REJECTED),
    ("https:///", None, OUTCOME_REJECTED),
    ("https://.", None, OUTCOME_REJECTED),          # rule 2 strips the "." first
    ("http://", None, OUTCOME_REJECTED),
    ("https://?ref=ig", None, OUTCOME_REJECTED),
    ("https://#lote2", None, OUTCOME_REJECTED),
    ("https://localhost:8080/x", None, OUTCOME_REJECTED),   # no dot
    ("https://192.168.0.1/x", None, OUTCOME_REJECTED),      # numeric last label
    ("https://evenyx.com:lote2/x", None, OUTCOME_REJECTED),  # not a port
    # ...while every real absolute URL still comes back byte-for-byte.
    (
        "https://evenyx.com/e/42?ref=ig#lote2",
        "https://evenyx.com/e/42?ref=ig#lote2",
        OUTCOME_PASSTHROUGH,
    ),
    ("https://evenyx.com:8080/lote2", "https://evenyx.com:8080/lote2", OUTCOME_PASSTHROUGH),
    # ── rule 6: any other scheme is not something the app should open ────
    ("mailto:vendas@casa.com", None, OUTCOME_REJECTED),
    ("tel:+5581999999999", None, OUTCOME_REJECTED),
    ("whatsapp://send?phone=5581", None, OUTCOME_REJECTED),
    ("javascript:alert(1)", None, OUTCOME_REJECTED),
    ("data:text/html;base64,AAAA", None, OUTCOME_REJECTED),
    # ── rule 8's host validation ─────────────────────────────────────────
    ("vendas@casa.com", None, OUTCOME_REJECTED),        # userinfo -> email, not a host
    ("ingressos", None, OUTCOME_REJECTED),              # no dot at all
    ("R$50", None, OUTCOME_REJECTED),
    ("evenyx.c", None, OUTCOME_REJECTED),               # 1-character TLD
    ("evenyx.123", None, OUTCOME_REJECTED),             # numeric TLD
    ("192.168.0.1/x", None, OUTCOME_REJECTED),          # bare IP: numeric last label
    ("evenyx..com", None, OUTCOME_REJECTED),            # empty label
    # ── rules 2 and 4: caption punctuation and prose ─────────────────────
    ("evenyx.com.", "https://evenyx.com", OUTCOME_SCHEME_ADDED),
    ("evenyx.com!", "https://evenyx.com", OUTCOME_SCHEME_ADDED),
    ("  https://sympla.com.br/x  ", "https://sympla.com.br/x", OUTCOME_PASSTHROUGH),
    ("ingressos na portaria", None, OUTCOME_REJECTED),
    ("R$ 50 na hora", None, OUTCOME_REJECTED),
    ("evenyx.com /lote2", None, OUTCOME_REJECTED),      # internal whitespace
    # ── rule 1 and the empty cases ───────────────────────────────────────
    (None, None, OUTCOME_ABSENT),
    ("", None, OUTCOME_ABSENT),
    ("   ", None, OUTCOME_ABSENT),
    ("...", None, OUTCOME_ABSENT),  # trimmed to nothing
    (12345, None, OUTCOME_ABSENT),
    (["evenyx.com"], None, OUTCOME_ABSENT),
]


@pytest.mark.parametrize("stored,expected,outcome", _CASES)
def test_classify_ticket_url(stored, expected, outcome):
    assert classify_ticket_url(stored) == (expected, outcome)


@pytest.mark.parametrize("stored,expected,_outcome", _CASES)
def test_normalize_ticket_url_matches_the_classifier(stored, expected, _outcome):
    """`normalize_ticket_url` is the plan's named export; it must never
    disagree with the classifier the projector's counter reads."""
    assert normalize_ticket_url(stored) == expected


def test_an_over_length_value_is_rejected():
    long_path = "evenyx.com/" + ("a" * MAX_TICKET_URL_LENGTH)
    assert len(long_path) > MAX_TICKET_URL_LENGTH
    assert classify_ticket_url(long_path) == (None, OUTCOME_REJECTED)


def test_a_value_exactly_at_the_length_limit_is_accepted():
    """The bound is inclusive, so a link at the limit is not silently lost."""
    padding = MAX_TICKET_URL_LENGTH - len("https://evenyx.com/")
    value = "https://evenyx.com/" + ("a" * padding)
    assert len(value) == MAX_TICKET_URL_LENGTH
    assert classify_ticket_url(value) == (value, OUTCOME_PASSTHROUGH)


def test_normalisation_is_idempotent():
    """Re-normalising the projected value must be a no-op — the projector
    rebuilds every payload on every 2-minute cycle, so a non-idempotent
    rule would rewrite the same row forever."""
    once = normalize_ticket_url("evenyx.com")
    assert once == "https://evenyx.com"
    assert normalize_ticket_url(once) == once


def test_it_never_raises_on_anything():
    """The projector calls this inside its per-event try/except, but a
    pathological value must not need that safety net."""
    for value in (object(), {"url": "x"}, b"evenyx.com", float("nan"), True):
        assert normalize_ticket_url(value) is None


def test_rule_5_never_rewrites_a_url_it_accepts():
    """The authority check added for the bare-scheme hole must VALIDATE
    without normalising: query, fragment, port, case and trailing slash all
    have to survive untouched, or `passthrough` stops meaning passthrough."""
    for value in (
        "https://evenyx.com/e/42?ref=ig#lote2",
        "https://evenyx.com:8080/lote2?a=1&b=2#x",
        "HTTPS://SYMPLA.COM.BR/Evento/123?Q=1#F",
        "http://ingressos.example.com/",
        "https://www.sympla.com.br",
    ):
        normalised, outcome = classify_ticket_url(value)
        assert outcome == OUTCOME_PASSTHROUGH, (value, outcome)
        assert normalised == value, (value, normalised)


def test_a_dead_scheme_is_counted_rejected_not_passthrough():
    """The counter is the operator's only signal here. Returning None while
    still counting `passthrough` would hide the defect just as effectively
    as returning the dead value did."""
    for value in ("https://", "https:///", "https://.", "http://"):
        normalised, outcome = classify_ticket_url(value)
        assert normalised is None, value
        assert outcome == OUTCOME_REJECTED, (value, outcome)


def test_an_impossible_port_is_counted_rejected_not_qualified():
    """The counter is the only signal that would ever reveal this class of
    defect, so labelling matters as much as the value. `:80808` used to come
    back as `https://evenyx.com:80808/x` under `scheme_added` — a dead ticket
    button recorded as a SUCCESS. It must now be None AND `rejected`; None
    under `scheme_added` or `passthrough` would hide the drift just as
    effectively."""
    for value in (
        "evenyx.com:80808/x",
        "evenyx.com:99999/x",
        "evenyx.com:65536/x",
        "//evenyx.com:80808/x",
        "https://evenyx.com:80808/x",
        "https://evenyx.com:65536/x",
    ):
        normalised, outcome = classify_ticket_url(value)
        assert normalised is None, (value, normalised)
        assert outcome == OUTCOME_REJECTED, (value, outcome)


# The cross-repo contract table. See the docstring on the test below: this
# is vibes_bot's OUTPUT, captured and pinned, not a live import.
_CROSS_REPO_CONTRACT = [
    ('evenyx.com', 'https://evenyx.com', OUTCOME_SCHEME_ADDED),
    ('www.sympla.com.br', 'https://www.sympla.com.br', OUTCOME_SCHEME_ADDED),
    ('evenyx.com/e/42?ref=ig#lote2', 'https://evenyx.com/e/42?ref=ig#lote2', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:8080/lote2', 'https://evenyx.com:8080/lote2', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:8080', 'https://evenyx.com:8080', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:65535/x', 'https://evenyx.com:65535/x', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:65536/x', None, OUTCOME_REJECTED),
    ('evenyx.com:80808/x', None, OUTCOME_REJECTED),
    ('evenyx.com:99999/x', None, OUTCOME_REJECTED),
    ('evenyx.com:99999999', None, OUTCOME_REJECTED),
    ('evenyx.com:0/x', 'https://evenyx.com:0/x', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:0', 'https://evenyx.com:0', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:0080/x', 'https://evenyx.com:0080/x', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:00000/x', 'https://evenyx.com:00000/x', OUTCOME_SCHEME_ADDED),
    ('evenyx.com:', None, OUTCOME_REJECTED),
    ('evenyx.com:/x', None, OUTCOME_REJECTED),
    ('evenyx.com:lote2', None, OUTCOME_REJECTED),
    ('evenyx.com:-1/x', None, OUTCOME_REJECTED),
    ('evenyx.com: 80/x', None, OUTCOME_REJECTED),
    ('https://evenyx.com:80808/x', None, OUTCOME_REJECTED),
    ('https://evenyx.com:65535/x', 'https://evenyx.com:65535/x', OUTCOME_PASSTHROUGH),
    ('https://evenyx.com:65536/x', None, OUTCOME_REJECTED),
    ('https://evenyx.com:0/x', 'https://evenyx.com:0/x', OUTCOME_PASSTHROUGH),
    ('https://evenyx.com:0080/x', 'https://evenyx.com:0080/x', OUTCOME_PASSTHROUGH),
    ('https://evenyx.com:/x', None, OUTCOME_REJECTED),
    ('https://evenyx.com:8080/lote2', 'https://evenyx.com:8080/lote2', OUTCOME_PASSTHROUGH),
    ('//evenyx.com/e/42', 'https://evenyx.com/e/42', OUTCOME_SCHEME_ADDED),
    ('//evenyx.com:80808/x', None, OUTCOME_REJECTED),
    ('https://sympla.com.br/evento/123', 'https://sympla.com.br/evento/123', OUTCOME_PASSTHROUGH),
    ('http://ingressos.example.com/x', 'http://ingressos.example.com/x', OUTCOME_PASSTHROUGH),
    ('HTTPS://SYMPLA.COM.BR/X', 'HTTPS://SYMPLA.COM.BR/X', OUTCOME_PASSTHROUGH),
    ('https://', None, OUTCOME_REJECTED),
    ('https:///', None, OUTCOME_REJECTED),
    ('https://.', None, OUTCOME_REJECTED),
    ('http://', None, OUTCOME_REJECTED),
    ('https://?ref=ig', None, OUTCOME_REJECTED),
    ('https://#lote2', None, OUTCOME_REJECTED),
    ('https://localhost:8080/x', None, OUTCOME_REJECTED),
    ('https://192.168.0.1/x', None, OUTCOME_REJECTED),
    ('mailto:vendas@casa.com', None, OUTCOME_REJECTED),
    ('tel:+5581999999999', None, OUTCOME_REJECTED),
    ('whatsapp://send?phone=5581', None, OUTCOME_REJECTED),
    ('javascript:alert(1)', None, OUTCOME_REJECTED),
    ('data:text/html;base64,AAAA', None, OUTCOME_REJECTED),
    ('vendas@casa.com', None, OUTCOME_REJECTED),
    ('ingressos', None, OUTCOME_REJECTED),
    ('R$50', None, OUTCOME_REJECTED),
    ('evenyx.c', None, OUTCOME_REJECTED),
    ('evenyx.123', None, OUTCOME_REJECTED),
    ('192.168.0.1/x', None, OUTCOME_REJECTED),
    ('evenyx..com', None, OUTCOME_REJECTED),
    ('evenyx.com.', 'https://evenyx.com', OUTCOME_SCHEME_ADDED),
    ('evenyx.com!', 'https://evenyx.com', OUTCOME_SCHEME_ADDED),
    ('  https://sympla.com.br/x  ', 'https://sympla.com.br/x', OUTCOME_PASSTHROUGH),
    ('ingressos na portaria', None, OUTCOME_REJECTED),
    ('evenyx.com /lote2', None, OUTCOME_REJECTED),
    (None, None, OUTCOME_ABSENT),
    ('', None, OUTCOME_ABSENT),
    ('   ', None, OUTCOME_ABSENT),
    ('...', None, OUTCOME_ABSENT),
    (12345, None, OUTCOME_ABSENT),
    (['evenyx.com'], None, OUTCOME_ABSENT),
]


@pytest.mark.parametrize("stored,expected,outcome", _CROSS_REPO_CONTRACT)
def test_the_two_repos_agree_input_for_input(stored, expected, outcome):
    """cs-server and vibes_bot must classify the same value the same way.

    HONEST ABOUT WHAT THIS IS: it does NOT import vibes_bot. The two repos
    are separate git repositories with separate virtualenvs; vibes_bot is
    not on this repo's import path in any developer checkout or in CI, and
    faking the cross-import (a sys.path poke at a sibling directory) would
    pass locally on one machine and be meaningless everywhere else.

    So the expected column below was CAPTURED by running vibes_bot's
    `app/services/event_ticket_url.py` (branch `fix/events-ui-api-hardening`,
    plan `plans/260906_events-ui-api-hardening.md`, sha256
    5ad397dad358202732cb48fa788549e063e5bac43bf51b73561aca96df003c57) over
    this exact input list, and pinned. It is a regression guard on OUR side
    of a contract, not a live equality proof: if cs-server drifts, this goes
    red; if vibes_bot drifts, only vibes_bot's own copy of the table does.
    That is why both repos keep the rules in a same-named, same-shaped
    module — the two files are meant to stay trivially diffable.

    At the time of pinning, all 62 rows agreed, plus 2400 generated
    `prefix + host + :port + path` permutations.
    """
    assert classify_ticket_url(stored) == (expected, outcome)
