"""The FastAPI app boots, guards the webhook route, and ACKs before predicting.

Railway's healthcheck hits GET /; an unsigned POST must be rejected with 401
before anything downstream (predict, submission) runs. Signed deliveries ACK
200 and hand the model call off to a background task.
"""

from __future__ import annotations

import base64
import hmac
import inspect
import json
import time
from hashlib import sha256

from fastapi.testclient import TestClient

from app import app

SECRET = "whsec_" + base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")


def _signed(payload: dict) -> tuple[bytes, dict]:
    """Serialize and sign a payload the way the platform does."""
    raw_body = json.dumps(payload).encode()
    ts = int(time.time())
    key = base64.urlsafe_b64decode(SECRET[len("whsec_"):] + "==")
    signed = f"{payload['id']}.{ts}.".encode() + raw_body
    signature = "v1," + base64.b64encode(hmac.new(key, signed, sha256).digest()).decode()
    return raw_body, {
        "webhook-id": payload["id"],
        "webhook-timestamp": str(ts),
        "webhook-signature": signature,
    }


def _test_event(webhook_id: str = "evt_test_abc") -> dict:
    return {
        "id": webhook_id,
        "event_id": "test_abc",
        "event_type": "TEST",
        "focal_assets": [{"identifier_type": "TICKER", "identifier_value": "TEST"}],
        "prediction_deadline": "2099-01-01T00:00:00+00:00",
    }


def _configured(monkeypatch) -> None:
    monkeypatch.setenv("EM_API_KEY", "test-key")
    monkeypatch.setenv("EM_WEBHOOK_SECRET", SECRET)


def test_health_ok() -> None:
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_unsigned_post_is_rejected(monkeypatch) -> None:
    _configured(monkeypatch)
    client = TestClient(app)
    for path in ("/", "/competition/webhook"):
        resp = client.post(path, content=b"{}")
        assert resp.status_code == 401


def test_prediction_work_is_sync_so_it_runs_off_the_event_loop() -> None:
    """Regression guard for the blocking-in-the-event-loop bug.

    FastAPI runs *sync* background tasks in a worker thread and *async* ones on
    the event loop. `_predict_and_submit` makes blocking httpx and OpenAI calls,
    so turning it into `async def` would stall every concurrent delivery.
    """
    import app as app_module

    assert not inspect.iscoroutinefunction(app_module._predict_and_submit)


def test_signed_test_event_submits_neutral_prediction(monkeypatch) -> None:
    """A TEST delivery round-trips: ACK 200, then submit a 0.5 prediction for
    the synthetic event so the portal test can verify the submit path."""
    _configured(monkeypatch)
    raw_body, headers = _signed(_test_event())

    submitted: list[dict] = []

    def fake_submit(*, event_id, predictions, config):
        submitted.append({"event_id": event_id, "predictions": predictions})
        return {"accepted": True}

    import app as app_module

    monkeypatch.setattr(app_module, "submit_predictions", fake_submit)
    app_module._seen_webhooks.clear()

    client = TestClient(app)
    resp = client.post("/", content=raw_body, headers=headers)
    assert resp.status_code == 200
    assert submitted == [
        {
            "event_id": "test_abc",
            "predictions": [{"identifier_value": "TEST", "predicted_percentile": 0.5}],
        }
    ]
    # A completed submission is remembered permanently.
    assert app_module._seen_webhooks["evt_test_abc"] == "done"


def test_submit_failure_still_acks_and_releases_the_claim(monkeypatch) -> None:
    """The delivery succeeded even if the prediction didn't. Releasing the claim
    is what lets a redelivery of the same Webhook-Id try again."""
    _configured(monkeypatch)
    raw_body, headers = _signed(_test_event())

    import app as app_module

    def broken_submit(**kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(app_module, "submit_predictions", broken_submit)
    app_module._seen_webhooks.clear()

    client = TestClient(app)
    resp = client.post("/", content=raw_body, headers=headers)
    assert resp.status_code == 200
    assert "evt_test_abc" not in app_module._seen_webhooks


def test_duplicate_after_success_is_skipped(monkeypatch) -> None:
    _configured(monkeypatch)
    raw_body, headers = _signed(_test_event())

    calls: list[str] = []

    def counting_submit(*, event_id, predictions, config):
        calls.append(event_id)
        return {"accepted": True}

    import app as app_module

    monkeypatch.setattr(app_module, "submit_predictions", counting_submit)
    app_module._seen_webhooks.clear()

    client = TestClient(app)
    assert client.post("/", content=raw_body, headers=headers).status_code == 200
    assert client.post("/", content=raw_body, headers=headers).status_code == 200
    assert calls == ["test_abc"]  # the redelivery did no work


def test_duplicate_while_in_flight_is_skipped(monkeypatch) -> None:
    """The window this closes: under ACK-first the job runs for minutes, and a
    duplicate arriving mid-flight would otherwise start a second model call."""
    _configured(monkeypatch)
    raw_body, headers = _signed(_test_event())

    import app as app_module

    app_module._seen_webhooks.clear()
    app_module._seen_webhooks["evt_test_abc"] = "in_flight"

    def must_not_run(**kwargs):
        raise AssertionError("duplicate started a second prediction job")

    monkeypatch.setattr(app_module, "submit_predictions", must_not_run)

    client = TestClient(app)
    assert client.post("/", content=raw_body, headers=headers).status_code == 200


def test_released_claim_lets_a_redelivery_retry(monkeypatch) -> None:
    _configured(monkeypatch)
    raw_body, headers = _signed(_test_event())

    import app as app_module

    attempts: list[int] = []

    def fail_then_succeed(*, event_id, predictions, config):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return {"accepted": True}

    monkeypatch.setattr(app_module, "submit_predictions", fail_then_succeed)
    app_module._seen_webhooks.clear()

    client = TestClient(app)
    assert client.post("/", content=raw_body, headers=headers).status_code == 200
    assert client.post("/", content=raw_body, headers=headers).status_code == 200
    assert len(attempts) == 2
    assert app_module._seen_webhooks["evt_test_abc"] == "done"
