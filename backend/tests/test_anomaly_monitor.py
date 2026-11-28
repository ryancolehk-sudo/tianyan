from types import SimpleNamespace

from app.market import anomaly_archive
from tests.test_ai_backend import login


def test_anomaly_reads_local_archives_and_respects_as_of(client, monkeypatch, tmp_path):
    monkeypatch.setattr(anomaly_archive, "settings", lambda: SimpleNamespace(
        app_mode="real", deployment_mode="local", artifact_directory=str(tmp_path)))
    path = anomaly_archive.directory()
    day = "2026-11-24"
    anomaly_archive.save(path, day + "_20261124T083100000000.json", {
        "day": day, "received_at": "2026-11-24T08:31:00+00:00",
        "predictions": [{"code":"999991","name":"合成样本","state":2}], "repeated": [], "history": [], "pool": [],
    })
    assert client.get("/api/v1/review-data/anomaly").status_code == 401
    login(client)
    result = client.get("/api/v1/review-data/anomaly", params={"trade_day": day})
    assert result.status_code == 200
    assert result.json()["data"]["predictions"][0]["name"] == "合成样本"
    earlier = client.get("/api/v1/review-data/anomaly", params={
        "trade_day": day, "as_of_time": "2026-11-24T08:30:00Z",
    })
    assert earlier.status_code == 200 and earlier.json()["data"] is None


def test_anomaly_fetch_disabled_by_default(monkeypatch):
    for config in (
        SimpleNamespace(app_mode="real", deployment_mode="local", data_collection_enabled=True, anomaly_collection_enabled=False),
        SimpleNamespace(app_mode="real", deployment_mode="cloud", data_collection_enabled=True, anomaly_collection_enabled=True),
    ):
        monkeypatch.setattr(anomaly_archive, "settings", lambda: config)
        monkeypatch.setattr(anomaly_archive, "_next_check", 0)
        monkeypatch.setattr(anomaly_archive, "engine", lambda: (_ for _ in ()).throw(AssertionError("collector ran")))
        anomaly_archive.schedule_anomaly()
