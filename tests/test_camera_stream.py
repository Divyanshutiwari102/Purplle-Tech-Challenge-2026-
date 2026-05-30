# PROMPT: "Tests for the simulated MJPEG camera stream and the /cameras
#          listing. Cover: GET /cameras returns a list, an MJPEG stream
#          starts with a multipart Content-Type header and the first
#          chunk is a JPEG."
#
# CHANGES MADE:
#   - Used max_frames=2 query param (added to the route for testability)
#     to bound the otherwise-infinite MJPEG generator. This is a clean
#     escape hatch that doesn't pollute production semantics — production
#     callers simply omit the parameter.
"""Tests for simulated YOLO camera MJPEG streaming."""
from __future__ import annotations


def test_list_cameras(client):
    body = client.get("/cameras").json()
    assert "cameras" in body
    assert isinstance(body["cameras"], list)
    assert len(body["cameras"]) >= 1


def test_camera_stream_returns_mjpeg(client):
    """First two frames should arrive in well-formed MJPEG framing."""
    r = client.get("/cameras/stream/CAM_1?fps=20&max_frames=2")
    assert r.status_code == 200
    ctype = r.headers.get("content-type", "")
    assert ctype.startswith("multipart/x-mixed-replace")
    body = r.content
    assert b"\xff\xd8\xff" in body, "expected JPEG SOI marker"
    assert b"--frame" in body, "expected MJPEG boundary"
    assert b"Content-Type: image/jpeg" in body


def test_unknown_camera_falls_back(client):
    """Unknown camera ids should not 500 — we fall back to a default."""
    r = client.get("/cameras/stream/CAM_999?fps=20&max_frames=1")
    assert r.status_code == 200
    assert b"\xff\xd8\xff" in r.content
