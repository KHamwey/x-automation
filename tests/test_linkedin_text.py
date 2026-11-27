"""Commentary escaping and LinkedIn post shaping."""

from x_automation.linkedin.commentary import escape_little
from x_automation.linkedin.generate import compose_post, plain_summary


def test_escape_reserved_characters_and_keep_hashtags():
    text = (
        "Cost (approx) is in the report.\n\n"
        "https://example.com/fed_ramp\n\n"
        "What stands out?\n\n"
        "#FedRAMP #GovCloud"
    )
    escaped = escape_little(text)
    assert r"\(approx\)" in escaped
    assert r"fed\_ramp" in escaped
    assert "#FedRAMP" in escaped
    assert r"\#FedRAMP" not in escaped
    assert r"\@" not in escaped
    assert escape_little("ping @ada") == r"ping \@ada"


def test_compose_adds_link_question_and_hashtags():
    post = compose_post(
        body="Agencies can reuse an authorization.\n\nThe boundary is the part that slips.",
        link="https://example.com/fedramp",
        include_url=True,
        hashtags=["#FedRAMP", "#GovCloud", "#FederalIT", "#Compliance", "#Extra"],
        char_budget=1600,
    )
    assert post.startswith("Agencies can reuse an authorization.")
    assert "https://example.com/fedramp" in post
    assert "What stands out to you?" in post
    assert "#FedRAMP" in post
    assert "#Extra" not in post
    assert post.rstrip().endswith("#Compliance") or "#Compliance" in post.splitlines()[-1]


def test_compose_keeps_model_question_and_strips_html_summary():
    summary = plain_summary("<p>FedRAMP&nbsp;authorization <b>boundary</b></p>")
    assert "<" not in summary
    assert "boundary" in summary
    post = compose_post(
        body="Hook\n\nDetail\n\nHow is your team handling this?\n\n#FedRAMP",
        link="https://example.com/a",
        include_url=True,
        hashtags=["#GovCloud"],
        char_budget=1600,
    )
    assert "How is your team handling this?" in post
    assert "What stands out to you?" not in post
    assert post.endswith("#FedRAMP")
    assert "https://example.com/a" in post


def test_compose_trims_to_budget_but_keeps_link_and_question():
    body = "Hook\n\n" + ("word " * 400)
    post = compose_post(
        body=body,
        link="https://example.com/story",
        include_url=True,
        hashtags=["#FedRAMP"],
        char_budget=280,
    )
    assert len(post) <= 280
    assert "https://example.com/story" in post
    assert "?" in post
    assert "#FedRAMP" in post
