"""Kiểm tra offline ảnh Postiz: id → {id, path} và upload không cần httpx.

Chạy: ~/.claude/company/.venv/bin/python ~/.claude/company/test_postiz_media.py
"""
import io
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.modules["httpx"] = None  # giả lập venv thiếu httpx
sys.path.insert(0, str(Path(__file__).parent))
tmp = Path(tempfile.mkdtemp())
os.environ["POSTIZ_MEDIA_CACHE"] = str(tmp / "cache.json")
os.environ["POSTIZ_API_KEY"] = "postiz-api-key-placeholder"  # publish trả payload_preview, không gọi mạng
import tools  # noqa: E402

# upload: multipart qua urllib, ghi path vào cache
img = tmp / "a.png"
img.write_bytes(b"\x89PNG fake")
seen = {}


def fake_urlopen(req, timeout=None):
    seen["body"], seen["ctype"] = req.data, req.headers["Content-type"]
    return io.BytesIO(json.dumps({"id": "m1", "path": "https://cdn/m1.png"}).encode())


os.environ["POSTIZ_API_KEY"] = "test-key"
urllib.request.urlopen = fake_urlopen
r = tools.upload_postiz_asset(str(img))
assert r["status"] == "success" and r["path"] == "https://cdn/m1.png", r
assert b'filename="a.png"' in seen["body"] and b"\x89PNG fake" in seen["body"]
assert seen["ctype"].startswith("multipart/form-data; boundary=")
assert json.loads((tmp / "cache.json").read_text()) == {"m1": "https://cdn/m1.png"}

# _media_objs: id trần tra cache, dict giữ nguyên, thiếu path thì báo lỗi
assert tools._media_objs(["m1"]) == [{"id": "m1", "path": "https://cdn/m1.png"}]
assert tools._media_objs([{"id": "x", "path": "p"}]) == [{"id": "x", "path": "p"}]
assert tools._media_objs(None) == [] and tools._media_objs([]) == []
for bad in (["unknown"], [{"id": "x"}]):
    try:
        tools._media_objs(bad)
        raise AssertionError(f"phải lỗi: {bad}")
    except ValueError:
        pass

# publish: payload gửi đủ {id, path}; id không có path thì không tạo bài
os.environ["POSTIZ_API_KEY"] = "postiz-api-key-placeholder"
r = tools.publish_to_postiz("ch", "hi", "2026-11-01T08:00:00+07:00", ["m1"], "schedule")
assert r["payload_preview"]["posts"][0]["value"][0]["image"] == [{"id": "m1", "path": "https://cdn/m1.png"}], r
assert tools.publish_to_postiz("ch", "hi", "2026-11-01T08:00:00+07:00", ["unknown"])["status"] == "error"
print("OK test_postiz_media")
