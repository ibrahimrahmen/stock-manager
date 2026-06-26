"""
SMS notifications via TunisieSMS (api.l2t.io).

Messages are Arabic, kept under 70 chars (1 SMS each). Each order-lifecycle
event fires its SMS once (dedup flags live on the Order model).

Environment variables:
  SMS_API_KEY        TunisieSMS API key (Outil > URL 2 SMS > "Votre Key")
  SMS_SENDER         Sender header, e.g. "Barats" (<=11 chars). Default "Barats".
  SMS_ENABLED        "1" to actually send; anything else = disabled (logged only).
  SMS_TEST_NUMBER    If set (e.g. "97159750"), SMS are ONLY sent to this number;
                     all other recipients are skipped. Use during testing.
"""
import os
import urllib.parse
import urllib.request

SMS_API_URL = "https://api.l2t.io/tn/v0/api/api.aspx"


def _normalize_mobile(phone):
    """Return an 8-digit Tunisian mobile (digits only), or '' if not valid.
    Strips spaces, +216, leading 216, etc."""
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    # Drop a leading country code 216 if present (216 + 8 digits = 11).
    if len(digits) == 11 and digits.startswith("216"):
        digits = digits[3:]
    if len(digits) == 8:
        return digits
    return ""


def send_sms(phone, message):
    """Send one SMS via TunisieSMS. Best-effort: returns (ok: bool, info: str).
    Never raises. Honors SMS_ENABLED and SMS_TEST_NUMBER gating."""
    mobile = _normalize_mobile(phone)
    if not mobile or not (message or "").strip():
        return (False, "no mobile or empty message")

    # Test gating: if a test number is configured, only send to it.
    test_number = _normalize_mobile(os.environ.get("SMS_TEST_NUMBER", ""))
    if test_number and mobile != test_number:
        return (False, f"skipped (test mode, not {test_number})")

    if os.environ.get("SMS_ENABLED", "") != "1":
        return (False, "SMS disabled (SMS_ENABLED != 1)")

    key = os.environ.get("SMS_API_KEY", "")
    sender = os.environ.get("SMS_SENDER", "Barats")
    if not key:
        return (False, "no SMS_API_KEY configured")

    # TunisieSMS expects the international format in 'mobile' (216XXXXXXXX).
    params = {
        "fct": "sms",
        "key": key,
        "mobile": "216" + mobile,
        "sms": message,
        "sender": sender,
    }
    url = SMS_API_URL + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", "ignore")
        ok = ("200" in body) or ("<status_msg>ok" in body.lower())
        return (ok, body[:200])
    except Exception as e:
        return (False, f"error: {e}")


# ---- Message builders (Arabic, 1 SMS each) -------------------------------

CALLBACK_NUMBER = "26200219"


def msg_created(when_today):
    """Order received. when_today=True -> 'today', else 'tomorrow' (غدوة)."""
    quand = "اليوم" if when_today else "غدوة"
    return f"Barats: وصلنا طلبك، باش نتواصلو معاك {quand} باش نأكدوه. شكرا"


def msg_injoignable():
    return (f"Barats: حاولنا نتصلو بيك وما نجمناش، يرجى الاتصال على "
            f"{CALLBACK_NUMBER}")


def msg_expedie(total):
    return f"Barats: طلبك تبعث، باش يوصلك غدوة وإلا بعد غدوة، المبلغ {total} د. شكرا"


def msg_en_cours(total, livreur_tel=""):
    if livreur_tel:
        return (f"Barats: طلبك في الطريق مع الليفرور {livreur_tel}، "
                f"المبلغ {total} د. شكرا")
    return f"Barats: طلبك في الطريق، المبلغ {total} د. شكرا"


def _fmt_total(order):
    """Format the order total as an integer DT amount (no decimals)."""
    try:
        return str(int(round(float(order.total or 0))))
    except Exception:
        return str(order.total or 0)
