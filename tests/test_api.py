from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
import pytest

SAMPLE_DIR = Path(__file__).resolve().parents[1] / "samples"


def test_page_and_health(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "频带能量".encode() in resp.data
    assert client.get("/api/health").get_json()["status"] == "ok"
    cfg = client.get("/api/config").get_json()
    assert cfg["db_path"].endswith("test_vibration.db")


def test_upload_json_csv_and_query(client):
    t = np.arange(300) / 256
    x = 2.0 * np.sin(2 * np.pi * 20 * t)
    csv_text = "\n".join(f"{v:.10f}" for v in x)

    resp = client.post("/api/segments", json={
        "start_sample": 0, "name": "t", "csv_text": csv_text,
    })
    assert resp.status_code == 201, resp.data
    body = resp.get_json()
    assert body["reindex"]["stored_indices"] == [0]

    r = client.get("/api/energy?start_sample=0&end_sample=255&f_low=15&f_high=25")
    data = r.get_json()
    assert data["status"] == "ok"
    assert data["window_count"] == 1
    assert data["avg_band_ms"] == pytest.approx(2.0, abs=1e-8)


def test_upload_multipart_file(client):
    data = {
        "start_sample": "0",
        "name": "mp",
        "file": (io.BytesIO(b"0.1\n0.2\n0.3\n"), "one.csv"),
    }
    resp = client.post("/api/segments", data=data, content_type="multipart/form-data")
    assert resp.status_code == 201
    assert resp.get_json()["segment"]["n_samples"] == 3


def test_header_line_is_tolerated(client):
    resp = client.post("/api/segments", json={
        "start_sample": 0,
        "csv_text": "vibration\n" + "\n".join(["0.1"] * 256),
    })
    assert resp.status_code == 201
    assert resp.get_json()["segment"]["n_samples"] == 256


def test_bad_csv_rejected_without_state_change(client):
    resp = client.post("/api/segments", json={
        "start_sample": 0, "csv_text": "0.1\nnot-a-number\n0.3",
    })
    assert resp.status_code == 400
    ov = client.get("/api/overview").get_json()
    assert ov["segments"] == []


def test_missing_start_sample(client):
    resp = client.post("/api/segments", json={"csv_text": "0.1\n0.2"})
    assert resp.status_code == 400
    assert "start_sample" in resp.get_json()["error"]


def test_overlap_api_conflict(client):
    client.post("/api/segments", json={"start_sample": 0, "csv_text": "\n".join(["0.1"] * 300)})
    resp = client.post("/api/segments", json={"start_sample": 100, "csv_text": "\n".join(["0.2"] * 50)})
    assert resp.status_code == 409
    assert "重叠" in resp.get_json()["error"]


def test_adjacent_uploads_ok(client):
    r1 = client.post("/api/segments", json={"start_sample": 0, "csv_text": "\n".join(["0.1"] * 256)})
    assert r1.status_code == 201
    r2 = client.post("/api/segments", json={"start_sample": 256, "csv_text": "\n".join(["0.1"] * 128)})
    assert r2.status_code == 201


def test_demo_load_and_energy_api(client):
    resp = client.post("/api/demo/load", json={"reset": True})
    assert resp.status_code == 201, resp.data
    assert len(resp.get_json()["segments"]) == 3

    r = client.get("/api/energy?start_sample=0&end_sample=1023&f_low=15&f_high=25")
    d = r.get_json()
    assert d["status"] == "ok"
    assert d["window_count"] == 5
    assert d["avg_band_ms"] == pytest.approx(2.0, abs=1e-8)

    miss = client.get("/api/energy?start_sample=512&end_sample=895&f_low=0&f_high=128").get_json()
    assert miss["status"] == "missing"
    assert miss["avg_band_ms"] is None

    # 无 reset 再次载入应 409
    again = client.post("/api/demo/load", json={})
    assert again.status_code == 409


def test_demo_files_endpoint_and_raw(client):
    files = client.get("/api/demo/files").get_json()["files"]
    assert len(files) == 3 and all(f["exists"] for f in files)
    raw = client.get("/api/demo/raw/demo_seg1_start0.csv")
    assert raw.status_code == 200
    assert b"single-column samples" in raw.data


def test_replace_and_delete_api(client):
    client.post("/api/demo/load", json={"reset": True})

    new_csv = "\n".join(["0.05"] * 350)
    resp = client.post(
        "/api/segments/2/replace",
        data=json.dumps({"csv_text": new_csv}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.data
    body = resp.get_json()
    assert body["segment"]["n_samples"] == 350
    assert body["reindex"]["window_indices"] == [1, 2, 3, 4, 5]

    # 按起始样点换报也可用
    resp2 = client.put(
        "/api/segments/by-start/700/replace",
        json={"csv_text": "\n".join(["0.02"] * 324)},
    )
    assert resp2.status_code == 200

    # 不存在的段 -> 404
    resp3 = client.post("/api/segments/999/replace", json={"csv_text": "0.1"})
    assert resp3.status_code == 404

    # 撤段
    d = client.delete("/api/segments/1")
    assert d.status_code == 200
    ov = client.get("/api/overview").get_json()
    assert {s["id"] for s in ov["segments"]} == {2, 3}


def test_create_endpoint_replace_mode_by_start(client):
    client.post("/api/demo/load", json={"reset": True})
    # 以起始样点定位、通过同一个 POST 上传入口整段换报
    resp = client.post("/api/segments", json={
        "start_sample": 0,
        "replace": True,
        "csv_text": "\n".join(["0.3"] * 300),
    })
    assert resp.status_code == 200, resp.data
    assert resp.get_json()["segment"]["start_sample"] == 0
    # 不带 replace 时同起点应按重叠拒收
    again = client.post("/api/segments", json={
        "start_sample": 0, "csv_text": "\n".join(["0.3"] * 10),
    })
    assert again.status_code == 409


def test_clear_all_api(client):
    client.post("/api/demo/load", json={"reset": True})
    assert client.delete("/api/segments").status_code == 200
    ov = client.get("/api/overview").get_json()
    assert ov["segments"] == []
    assert ov["valid_window_starts"] == []


def test_energy_validation_errors(client):
    assert client.get("/api/energy?start_sample=10&end_sample=1").status_code == 400
    assert client.get("/api/energy?start_sample=x&end_sample=1").status_code == 400
    assert client.get("/api/energy?start_sample=0").status_code == 400
    assert client.get("/api/energy?start_sample=0&end_sample=10&f_low=90&f_high=10").status_code == 400
