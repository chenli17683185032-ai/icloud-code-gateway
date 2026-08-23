from email.message import EmailMessage

from icloud_gateway.mail_tags import classify_message, merge_usage


def test_merge_usage_marks_plan_as_active_and_ban_together() -> None:
    assert merge_usage("", plan=True, banned=False) == "gpt 活跃"
    assert merge_usage("", plan=True, banned=True) == "gpt 封号"
    assert merge_usage("sg", plan=True, banned=False) == "gpt 活跃 sg"
    assert merge_usage("gpt 活跃", plan=True, banned=True) == "gpt 封号"


def test_registered_account_is_marked_used_even_without_a_subscription() -> None:
    assert merge_usage("", plan=False, banned=False, used=True) == "gpt 已使用"
    # A subscription or a ban is more specific than "registered", and the three
    # states must never stack on one alias.
    assert merge_usage("", plan=True, banned=False, used=True) == "gpt 活跃"
    assert merge_usage("", plan=False, banned=True, used=True) == "gpt 封号"
    assert merge_usage("gpt 已使用", plan=True, banned=False, used=True) == "gpt 活跃"
    assert merge_usage("gpt 活跃", plan=False, banned=False, used=True) == "gpt 已使用"
    # Custom notes survive.
    assert merge_usage("sg", plan=False, banned=False, used=True) == "gpt 已使用 sg"


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
