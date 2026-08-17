from email.message import EmailMessage

from icloud_gateway.mail_tags import classify_message, merge_usage


def test_merge_usage_marks_plan_as_active_and_ban_together() -> None:
    assert merge_usage("", plan=True, banned=False) == "gpt 活跃"
    assert merge_usage("", plan=True, banned=True) == "gpt 封号"
    assert merge_usage("sg", plan=True, banned=False) == "gpt 活跃 sg"
    assert merge_usage("gpt 活跃", plan=True, banned=True) == "gpt 封号"


def test_classify_message_uses_decoded_subjects() -> None:
    plan = EmailMessage()
    plan["Subject"] = "ChatGPT - Your new plan"
    plan["From"] = "ChatGPT <noreply@tm.openai.com>"
    ban = EmailMessage()
    ban["Subject"] = "OpenAI - Access Deactivated"
    ban["From"] = "OpenAI <noreply@tm.openai.com>"
    other = EmailMessage()
    other["Subject"] = "Your ChatGPT code is 123456"
    other["From"] = "OpenAI <noreply@tm.openai.com>"

    assert classify_message(plan) == {"plan"}
    assert classify_message(ban) == {"ban"}
    assert classify_message(other) == set()
