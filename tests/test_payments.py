import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.payments import paid_payment_id, parse_plans, shop_secret, verify_razorpay_signature  # noqa: E402


def test_parse_plans():
    p = parse_plans("1 Month:30:299, 1 Year:365:2499,bad,x:y:z,Zero:0:5")
    assert [(x["name"], x["days"], x["price"]) for x in p] == [("1 Month", 30, 299), ("1 Year", 365, 2499)]
    assert p[0]["id"] == "p0" and p[1]["id"] == "p1"


def test_signature():
    body = b'{"event":"payment_link.paid"}'
    sig = hmac.new(b"whsec", body, hashlib.sha256).hexdigest()
    assert verify_razorpay_signature("whsec", body, sig)
    assert not verify_razorpay_signature("whsec", body + b" ", sig)
    assert not verify_razorpay_signature("other", body, sig)
    assert not verify_razorpay_signature("", body, sig)
    assert not verify_razorpay_signature("whsec", body, None)


def test_paid_payment_id():
    assert paid_payment_id({"status": "created"}) is None
    assert paid_payment_id({"status": "paid", "payments": [{"payment_id": "pay_1", "status": "captured"}]}) == "pay_1"
    assert paid_payment_id({"status": "paid", "payments": [{"payment_id": "pay_2", "status": "failed"}]}) is None


def test_shop_secret_stable():
    assert shop_secret("a") == shop_secret("a") and shop_secret("a") != shop_secret("b")
