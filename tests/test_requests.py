from permissions_ui.github import Comment, PullRequest
from permissions_ui.policy import Need
from permissions_ui.requests import parse, record, records

META = {"version": 1, "kind": "grant", "person": "ada.lovelace@example.com", "items": [{"resource": "grafana-viewer"}], "reason": "dashboards -- please", "requested_by": "ada.lovelace@example.com", "expires": None}


def pull(*comments: Comment, state: str = "open", merged: bool = False) -> PullRequest:
    return PullRequest(1, "t", "text\n" + record("request", META), state, merged, ("access-request",), "access/ada-1", "abc", "u", "bot", "c", "u", comments=comments)


def approval(author: str, upn: str) -> Comment:
    return Comment(1, author, "Approved\n" + record("approval", {"upn": upn, "name": upn, "resources": ["grafana-viewer"], "at": "now"}), "now")


def test_a_record_survives_text_that_would_end_an_html_comment():
    text = record("request", META)
    assert "--" not in text[4:-3]
    assert records(text, "request") == [META]


def test_only_the_apps_own_comments_count_as_approvals():
    need = [Need("grafana-viewer", ("team:platform",), 1)]
    forged = parse(pull(approval("mallory", "grace.hopper@example.com")), "permissions-bot", need)
    assert forged.state == "pending" and forged.approvals == ()
    real = parse(pull(approval("permissions-bot", "grace.hopper@example.com")), "permissions-bot", need)
    assert real.state == "approved" and real.approvals[0].upn == "grace.hopper@example.com"


def test_states_follow_the_pull_request():
    need = [Need("grafana-viewer", ("team:platform",), 1)]
    assert parse(pull(merged=True, state="closed"), "permissions-bot", need).state == "granted"
    declined = Comment(2, "permissions-bot", record("decline", {"upn": "g@example.com", "reason": "no", "withdrawn": False}), "now")
    assert parse(pull(declined, state="closed"), "permissions-bot", need).state == "declined"
    assert parse(pull(state="closed"), "permissions-bot", need).state == "withdrawn"


def test_a_pull_request_without_a_record_is_not_a_request():
    other = PullRequest(2, "t", "just a change", "open", False, (), "b", "s", "u", "a", "c", "u")
    assert parse(other, "permissions-bot", []) is None
