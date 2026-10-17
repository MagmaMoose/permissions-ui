from permissions_ui.github import Comment, PullRequest
from permissions_ui.policy import Need
from permissions_ui.requests import Records, parse

META = {"version": 1, "kind": "grant", "person": "ada.lovelace@example.com", "items": [{"resource": "grafana-viewer"}], "reason": "dashboards -- please", "requested_by": "ada.lovelace@example.com", "expires": None}
RECORDS = Records((b"r" * 48,))
BRANCH = "access/ada-1"
NEED = [Need("grafana-viewer", ("team:platform",), 1)]


def pull(*comments: Comment, state: str = "open", merged: bool = False, body: str = "", head: str = BRANCH) -> PullRequest:
    body = body or "text\n" + RECORDS.write("request", META, BRANCH)
    return PullRequest(1, "t", body, state, merged, ("access-request",), head, "abc", "u", "bot", "c", "u", comments=comments)


def approval(author: str, upn: str, *, records: Records = RECORDS, number: int = 1) -> Comment:
    return Comment(1, author, "Approved\n" + records.write("approval", {"upn": upn, "name": upn, "resources": ["grafana-viewer"], "at": "now"}, number), "now")


def test_a_record_survives_text_that_would_end_an_html_comment():
    text = RECORDS.write("request", META, BRANCH)
    assert "--" not in text[4:-3]
    assert RECORDS.read(text, "request", BRANCH) == [META]


def test_only_the_apps_own_comments_count_as_approvals():
    forged = parse(pull(approval("mallory", "grace.hopper@example.com")), "permissions-bot", RECORDS, NEED)
    assert forged.state == "pending" and forged.approvals == ()
    real = parse(pull(approval("permissions-bot", "grace.hopper@example.com")), "permissions-bot", RECORDS, NEED)
    assert real.state == "approved" and real.approvals[0].upn == "grace.hopper@example.com"


def test_an_approval_counts_only_signed_by_this_app_for_this_pull_request():
    unsigned = Comment(1, "permissions-bot", '<!-- permissions-ui:approval {"upn":"grace.hopper@example.com","resources":["grafana-viewer"]} -->', "now")
    other_key = approval("permissions-bot", "grace.hopper@example.com", records=Records((b"x" * 48,)))
    other_pull = approval("permissions-bot", "grace.hopper@example.com", number=2)
    for comment in (unsigned, other_key, other_pull):
        assert parse(pull(comment), "permissions-bot", RECORDS, NEED).state == "pending"


def test_a_request_changed_or_moved_to_another_branch_is_not_a_request():
    signed = RECORDS.write("request", META, BRANCH)
    assert parse(pull(body=signed.replace("ada.lovelace", "mallory")), "permissions-bot", RECORDS, NEED) is None
    assert parse(pull(head="access/mallory-1"), "permissions-bot", RECORDS, NEED) is None


def test_a_rotated_key_still_reads_what_the_old_one_signed():
    old, new = b"o" * 48, b"n" * 48
    before = Records((old,)).write("approval", {"upn": "g@example.com"}, 1)
    rotated = Records((new, old))
    assert rotated.read(before, "approval", 1) == [{"upn": "g@example.com"}]
    after = rotated.write("approval", {"upn": "g@example.com"}, 1)
    assert Records((new,)).read(after, "approval", 1) and not Records((old,)).read(after, "approval", 1)


def test_states_follow_the_pull_request():
    assert parse(pull(merged=True, state="closed"), "permissions-bot", RECORDS, NEED).state == "granted"
    declined = Comment(2, "permissions-bot", RECORDS.write("decline", {"upn": "g@example.com", "reason": "no", "withdrawn": False}, 1), "now")
    assert parse(pull(declined, state="closed"), "permissions-bot", RECORDS, NEED).state == "declined"
    assert parse(pull(state="closed"), "permissions-bot", RECORDS, NEED).state == "withdrawn"


def test_a_pull_request_without_a_record_is_not_a_request():
    other = PullRequest(2, "t", "just a change", "open", False, (), "b", "s", "u", "a", "c", "u")
    assert parse(other, "permissions-bot", RECORDS, []) is None
