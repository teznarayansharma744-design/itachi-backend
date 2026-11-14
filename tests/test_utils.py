import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils import make_token, new_key, parse_caption, parse_lecture_line, parse_range, read_token  # noqa: E402


def test_key_format():
    k = new_key()
    assert len(k) == 19 and k.count("-") == 3


def test_token_roundtrip_and_tamper():
    t = make_token("s3cret", "AAAA-BBBB-CCCC-DDDD", "DEV1-2222-3333", 1)
    data = read_token("s3cret", t)
    assert data["k"] == "AAAA-BBBB-CCCC-DDDD" and data["d"] == "DEV1-2222-3333"
    assert read_token("wrong", t) is None
    assert read_token("s3cret", t[:-2] + "00") is None
    assert read_token("s3cret", "garbage") is None


def test_admin_token():
    from app.utils import make_admin_token

    t = make_admin_token("s3cret", 1)
    data = read_token("s3cret", t)
    assert data["r"] == "admin" and "k" not in data
    assert read_token("other", t) is None
    # a student token must never look like an admin token
    assert read_token("s3cret", make_token("s3cret", "K", "D", 1)).get("r") != "admin"


def test_token_expiry():
    t = make_token("s", "K", "D", -1)
    assert read_token("s", t) is None


def test_range():
    assert parse_range(None, 1000) == (0, 999, False)
    assert parse_range("bytes=0-99", 1000) == (0, 99, True)
    assert parse_range("bytes=500-", 1000) == (500, 999, True)
    assert parse_range("bytes=-100", 1000) == (900, 999, True)
    assert parse_range("bytes=900-5000", 1000) == (900, 999, True)
    for bad in ("bytes=1000-", "bytes=50-10", "bytes=-0", "bytes=abc-"):
        try:
            parse_range(bad, 1000)
            assert False, bad
        except ValueError:
            pass


def test_caption():
    assert parse_caption("12 | Newton's Laws") == (12, "Newton's Laws", False)
    assert parse_caption("12 | Newton's Laws | free") == (12, "Newton's Laws", True)
    assert parse_caption("12 | Title | FREE")[2] is True
    assert parse_caption("hello") is None
    assert parse_caption("x | Title") is None
    assert parse_caption("12 |  ") is None
    assert parse_caption(None) is None


def test_lecture_line():
    r = parse_lecture_line("1 | Lecture 1 | https://a.com/v.mp4")
    assert r == {"unit_id": 1, "title": "Lecture 1", "video_url": "https://a.com/v.mp4", "pdf_url": None, "free": False}
    r = parse_lecture_line("2 | L2 | https://a.com/v.m3u8 | https://a.com/n.pdf | free")
    assert r["pdf_url"] == "https://a.com/n.pdf" and r["free"] is True
    r = parse_lecture_line("2 | L2 | https://a.com/v.mp4 | FREE | https://a.com/n.pdf")
    assert r["pdf_url"] and r["free"]
    r = parse_lecture_line("3 | Formula Sheet | - | https://a.com/f.pdf")
    assert r["video_url"] is None and r["pdf_url"] == "https://a.com/f.pdf"
    for bad in ("x | T | https://a.com", "1 |  | https://a.com", "1 | T", "1 | T | notalink",
                "1 | T | https://a.com | what", "1 | T | https://a .com/v", None):
        assert parse_lecture_line(bad) is None, bad


def test_link_token_expiry_and_tamper():
    from app.utils import make_link_token
    t = make_link_token("s3cret", 7, "KEY1", 60)
    d = read_token("s3cret", t)
    assert d["l"] == 7 and d["k"] == "KEY1"
    assert read_token("other", t) is None
    assert read_token("s3cret", make_link_token("s3cret", 7, "K", -5)) is None
