"""The client's Excel layouts (Online / Medsos): same columns, order, header style and value formats as their files."""
import os
import sys
from datetime import datetime, timedelta, timezone

import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from crawler import templates as T  # noqa: E402

WIB = timezone(timedelta(hours=7))
ONLINE_HEAD = ["DATE", "TIME", "TITLE", "URL", "IMAGE", "MEDIA", "INFLUENCER", "PERSON", "AUTHOR", "SUMMARY", "CONTENT",
               "SENTIMENT"]
MEDSOS_HEAD = ["No", "Sentiment", "Username", "User ID", "Follower", "Text", "Date", "Time", "Likes", "Reply", "Shares",
               "Url", "Emotion", "User Image", "Platform", "Sub Type", "Views Count", "Gender", "Age Category",
               "Content Location", "Religion", "Ethnic", "Phsychography", "Parent ID", "Post ID", "User ID Parent"]


def ts(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=WIB).timestamp()


def test_online_layout(tmp_path):
    rows = [
        {"url": "https://www.mediasulsel.com/sering-mati-lampu", "title": "Sering Mati Lampu, 10 Rumah Ludes", "ts": ts(2026, 11, 9, 0, 7),
         "published": "2026-11-09", "published_full": "2026-11-09 00:07", "author": "Ambang Ardi",
         "image": "https://www.mediasulsel.com/img.jpg", "description": "Pemadaman listrik yang kerap terjadi...",
         "text": "Pemadaman listrik yang kerap terjadi di Kabupaten.\n\nWarga <kecewa> & mengadu."},
        {"url": "https://mataram.antaranews.com/berita/581071/x", "title": "Forum KSSK NTB", "ts": ts(2026, 11, 9, 1, 4, 33),
         "published": "2026-11-09"},                                          # feed only: exact time, no article
        {"url": "https://www.example.id/a", "title": "Only a date", "published": "2026-11-08"},     # Google Search card
    ]
    x, c = tmp_path / "o.xlsx", tmp_path / "o.csv"
    T.write("online", str(x), str(c), rows, WIB)
    wb = openpyxl.load_workbook(x)
    ws = wb.active
    assert wb.sheetnames == ["Sheet1"] and [v.value for v in ws[1]] == ONLINE_HEAD
    assert ws["A1"].font.b and ws["A1"].fill.fgColor.rgb == "FF15677B"
    r2 = [v.value for v in ws[2]]
    assert r2[:6] == ["09-Nov-2026", "00:07:00", "Sering Mati Lampu, 10 Rumah Ludes", rows[0]["url"],
                      "https://www.mediasulsel.com/img.jpg", "mediasulsel.com"]
    assert r2[8] == "Ambang Ardi" and r2[9].startswith("Pemadaman listrik yang kerap")
    assert r2[10] == "<p>Pemadaman listrik yang kerap terjadi di Kabupaten.</p><p>Warga &lt;kecewa&gt; &amp; mengadu.</p>"
    assert r2[6] is None and r2[7] is None and r2[11] is None             # INFLUENCER / PERSON / SENTIMENT: no AI
    r3 = [v.value for v in ws[3]]
    assert r3[:2] == ["09-Nov-2026", "01:04:33"] and r3[5] == "mataram.antaranews.com"
    r4 = [v.value for v in ws[4]]
    assert r4[:2] == ["08-Nov-2026", None]
    assert open(c, encoding="utf-8-sig").readline().strip() == ",".join(ONLINE_HEAD)


def test_medsos_layout(tmp_path):
    rows = [
        {"platform": "x", "author": "Hyubbyu", "url": "https://x.com/Hyubbyu/status/2119695338454053050",
         "ts": ts(2026, 11, 9, 14, 35, 25), "caption": "Bangke lahh mati listrik", "likes": 0, "comments": 0, "shares": 0,
         "views": 14, "user_image": "https://pbs.twimg.com/profile_images/1/a.jpg", "sub_type": "tweet"},
        {"platform": "instagram", "author": "pln_123", "url": "https://www.instagram.com/p/Dfgn_coE8gp/",
         "ts": ts(2026, 11, 9, 10, 0), "caption": "Listrik normal", "likes": 4321, "comments": 87, "views": 120000,
         "user_id": "6025224660", "sub_type": "carousel"},
        {"platform": "threads", "author": "altika1993", "url": "https://www.threads.com/@altika1993/post/DfgRyCvkqLJ",
         "ts": ts(2026, 11, 9, 9, 0), "text": "kok mati lagi", "user_id": "73052146488"},
        {"platform": "tiktok", "author": "kaltimpostnews", "url": "https://www.tiktok.com/@kaltimpostnews/photo/7705797219735129365",
         "ts": ts(2026, 11, 9, 8, 0), "followers": 5100, "user_id": "7370322159623865350"},
        {"platform": "youtube", "handle": "plnchannel", "url": "https://www.youtube.com/watch?v=ZpirBuN6RJM&pp=x",
         "date": "2026-11-08", "time": "19:30", "user_id": "UCPWckJMFKh248bE3WSEk3UQ"},
        {"platform": "instagram", "author": "a", "url": "https://www.instagram.com/reel/Dfgn_coE8gp/", "ts": ts(2026, 11, 9, 7, 0)},
        {"platform": "facebook", "author": "b", "url": "https://web.facebook.com/b/videos/1144895638031825", "ts": ts(2026, 11, 9, 6, 0)},
    ]
    x, c = tmp_path / "m.xlsx", tmp_path / "m.csv"
    T.write("medsos", str(x), str(c), rows, WIB)
    wb = openpyxl.load_workbook(x)
    ws = wb.active
    assert wb.sheetnames == ["Sheet1"] and [v.value for v in ws[1]] == MEDSOS_HEAD
    assert ws["A1"].fill.fgColor.rgb == "FF4472C4" and ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:Z8"
    col = {h: i for i, h in enumerate(MEDSOS_HEAD)}
    val = lambda r, h: ws.cell(row=r, column=col[h] + 1).value     # noqa: E731
    assert [val(2, h) for h in ("No", "Username", "Text", "Date", "Time", "Likes", "Reply", "Shares", "Views Count",
                                "Platform", "Sub Type", "Post ID")] == \
        [1, "Hyubbyu", "Bangke lahh mati listrik", "2026-11-09", "14:35:25", 0, 0, 0, 14, "twitter", "tweet",
         "2119695338454053050"]
    assert val(2, "User Image").startswith("https://pbs.twimg.com/") and val(2, "Follower") is None
    assert (val(3, "Post ID"), val(3, "Sub Type"), val(3, "User ID"), val(3, "User ID Parent"), val(3, "Likes")) == \
        ("4026393950747346985", "carousel", "6025224660", "6025224660", 4321)
    assert (val(4, "Post ID"), val(4, "Text"), val(4, "Platform")) == ("4026296272579371721_73052146488", "kok mati lagi", "threads")
    assert (val(5, "Post ID"), val(5, "Follower")) == ("7705797219735129365", 5100)
    assert (val(6, "Post ID"), val(6, "Username"), val(6, "Date"), val(6, "Time")) == ("ZpirBuN6RJM", "plnchannel", "2026-11-08", "19:30:00")
    assert val(7, "Sub Type") == "video" and val(8, "Sub Type") == "video" and val(8, "Post ID") == "1144895638031825"
    for h in ("Sentiment", "Emotion", "Gender", "Age Category", "Religion", "Ethnic", "Phsychography", "Parent ID"):
        assert val(2, h) is None


def test_file_names_follow_the_team_pattern(tmp_path):
    now = datetime.now(WIB)
    past = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    assert T.file_name("Online", past, WIB) == past.replace("-", "") + "_Online_Manual_24.00"
    today = T.file_name("Medsos", now.strftime("%Y-%m-%d"), WIB, "Auto")
    assert today.startswith(now.strftime("%Y%m%d") + "_Medsos_Auto_") and len(today.rsplit("_", 1)[1]) == 5
    (tmp_path / (past.replace("-", "") + "_Online_Manual_24.00.xlsx")).write_text("x")
    assert T.file_name("Online", past, WIB, folder=str(tmp_path)).endswith("_24.00_2")       # never overwrites


def test_exports_list_and_delivery_use_the_layout(tmp_path, monkeypatch):
    from crawler import delivery, server
    monkeypatch.setattr(server, "OUTPUT_DIR", str(tmp_path))
    for n in ("20261108_Online_Manual_24.00.xlsx", "news_links_x.xlsx", "random.xlsx"):
        (tmp_path / n).write_text("x")
    assert sorted(f["name"] for f in server.list_outputs()) == ["20261108_Online_Manual_24.00.xlsx", "news_links_x.xlsx"]

    class J:
        data = {"news": {"meta": {"template": "online"}, "links": [
            {"url": "https://www.detik.com/a", "title": "A", "ts": ts(2026, 11, 9, 5, 6, 7), "published": "2026-11-09"}]}}

        def state(self):
            return {"modes": {"news": {"count": 1, "files": ["20261109_Online_Auto_24.00.xlsx"],
                                       "period": {"start": "2026-11-09", "end": "2026-11-09", "tz": "WIB"}}}}
    body = delivery.payload(J(), "news")
    assert body["layout"] == "online" and body["columns"] == ONLINE_HEAD
    assert body["rows"][0]["DATE"] == "09-Nov-2026" and body["rows"][0]["TIME"] == "05:06:07" and body["rows"][0]["MEDIA"] == "detik.com"
