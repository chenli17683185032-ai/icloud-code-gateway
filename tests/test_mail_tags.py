from email.message import EmailMessage

from icloud_gateway.mail_tags import apply_usage_hits, classify_message, merge_usage


def test_merge_usage_marks_plan_as_active_and_ban_together() -> None:
    assert merge_usage("", plan=True, banned=False) == "gpt 活跃"
    assert merge_usage("", plan=True, banned=True) == "gpt 封号"
    assert merge_usage("sg", plan=True, banned=False) == "gpt 活跃 sg"
    assert merge_usage("gpt 活跃", plan=True, banned=True) == "gpt 封号"


def test_registered_account_is_marked_used_even_without_a_subscription() -> None:
    assert merge_usage("", plan=False, banned=False, used=True) == "已使用"
    # A leftover GPT tag from the old rule is stripped when there is no Plan.
    assert merge_usage("gpt 已使用", plan=False, banned=False, used=True) == "已使用"
    # A subscription or a ban is more specific than "registered", and the three
    # states must never stack on one alias. GPT stays only when a Plan exists.
    assert merge_usage("", plan=True, banned=False, used=True) == "gpt 活跃"
    assert merge_usage("", plan=False, banned=True, used=True) == "封号"
    assert merge_usage("gpt 已使用", plan=True, banned=False, used=True) == "gpt 活跃"
    assert merge_usage("gpt 活跃", plan=False, banned=False, used=True) == "已使用"
    # Custom notes survive.
    assert merge_usage("sg", plan=False, banned=False, used=True) == "已使用 sg"


def test_apply_usage_hits_tags_gpt_only_for_plan_accounts() -> None:
    written: dict[str, str] = {}
    aliases = [
        {"id": "used", "email": "used@icloud.com", "usage_label": ""},
        {"id": "plan", "email": "plan@icloud.com", "usage_label": ""},
        {"id": "legacy", "email": "legacy@icloud.com", "usage_label": "gpt 已使用"},
    ]
    stats = apply_usage_hits(
        aliases,
        {
            "used@icloud.com": {"used"},
            "plan@icloud.com": {"plan", "used"},
            "legacy@icloud.com": {"used"},
        },
        written.__setitem__,
    )
    assert written == {
        "used": "已使用",
        "plan": "gpt 活跃",
        "legacy": "已使用",
    }
    assert stats["gpt_used"] == 2
    assert stats["gpt_active"] == 1


def test_classify_message_uses_decoded_subjects() -> None:
    plan = EmailMessage()
    plan["Subject"] = "ChatGPT - Your new plan"
    plan["From"] = "ChatGPT <noreply@tm.openai.com>"
    ban = EmailMessage()
    ban["Subject"] = "OpenAI - Access Deactivated"
    ban["From"] = "OpenAI <noreply@tm.openai.com>"
    code = EmailMessage()
    code["Subject"] = "Your ChatGPT code is 123456"
    code["From"] = "OpenAI <noreply@tm.openai.com>"
    unrelated = EmailMessage()
    unrelated["Subject"] = "Your receipt"
    unrelated["From"] = "Billing <billing@example.com>"

    assert classify_message(plan) == {"plan", "used"}
    assert classify_message(ban) == {"ban", "used"}
    # Any OpenAI mail proves the alias was registered, whatever the subject.
    assert classify_message(code) == {"used"}
    assert classify_message(unrelated) == set()
