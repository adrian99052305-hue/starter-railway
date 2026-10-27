"""Railway deployment for the Explaining Markets starter.

This is plumbing — you shouldn't need to edit it. It defines a small FastAPI app
that Railway runs as a persistent, public web service:

    GET  /    health check (Railway's healthcheckPath)
    POST /    receive a signed event, verify, ACK, then predict and submit
              (POST /competition/webhook is kept as an alias of the same handler)

The webhook is served at the root path on purpose: the URL `railway domain`
prints *is* your webhook URL — paste it into the portal as-is, nothing to append.

Deploy:    railway up
Dev/local: uv run uvicorn app:app --reload

The webhook handler ACKs first, then predicts. It verifies the signature, returns
200, and runs your `predict()` from predict.py plus the submission in the
background. Two clocks:

  * 20 seconds to ACK the delivery. Miss it and the platform retries; repeated
    failures disable your webhook.
  * 5 minutes from that ACK to submit your prediction.

Predicting before the ACK spends the 5-minute budget inside the 20-second one.

Deliveries are deduped on the `Webhook-Id` header (the server retries on
4xx/5xx/timeout, so the same event can arrive more than once).
"""

import os
import threading

from dotenv import load_dotenv

load_dotenv()  # local runs read .env; on Railway, variables come from the service

from fastapi import BackgroundTasks, FastAPI, Request, Response

from explaining_markets import WebhookVerificationError, verify_webhook
from explaining_markets.client import submit_predictions
from explaining_markets.config import Config
from explaining_markets.event_utils import is_test, log_deadline, neutral_predictions
from predict import predict

app = FastAPI(title="Explaining Markets starter")

# Idempotency guard keyed on the Webhook-Id header, with three states:
#
#   "in_flight"   a job is running right now — skip duplicates so you never pay
#                 for the same model call twice
#   "done"        the API accepted a prediction — skip forever
#   absent        never seen, or the last attempt raised — (re)run it
#
# Marking an event done up front would be the bug: a failed prediction would
# look handled. In-memory, so it resets on restart or redeploy; see
# docs/advanced.md for durable options.
_seen_lock = threading.Lock()
_seen_webhooks: dict[str, str] = {}


def _claim(webhook_id: str | None) -> bool:
    """Reserve this webhook_id. False means it's already in flight or done."""
    if not webhook_id:
        return True
    with _seen_lock:
        if webhook_id in _seen_webhooks:
            return False
        _seen_webhooks[webhook_id] = "in_flight"
        return True


def _release(webhook_id: str | None, *, submitted: bool) -> None:
    """Mark the claim done on success, or drop it so a redelivery can retry."""
    if not webhook_id:
        return
    with _seen_lock:
        if submitted:
            _seen_webhooks[webhook_id] = "done"
        else:
            _seen_webhooks.pop(webhook_id, None)


def _predict_and_submit(event: dict, config: Config, webhook_id: str | None) -> None:
    """Run the model and submit the prediction. Synchronous on purpose.

    FastAPI runs *sync* background tasks in a worker thread, so the blocking
    httpx and OpenAI calls in here never touch the event loop. Making this
    `async def` would put them back on it and stall every other delivery
    arriving at the same time.
    """
    submitted = False
    try:
        predictions = neutral_predictions(event) if is_test(event) else predict(event)
        submit_predictions(
            event_id=event["event_id"],
            predictions=predictions,
            config=config,
        )
        submitted = True
    except Exception as exc:
        # The delivery was ACKed already, so nothing upstream will retry this.
        # Log loudly — `railway logs` is where you'll find it.
        print(f"[ERROR] prediction failed for event {event.get('event_id')}: {exc}")
    finally:
        _release(webhook_id, submitted=submitted)


@app.get("/")
def health() -> dict:
    return {"ok": True, "service": "explaining-markets-starter"}


@app.post("/")
@app.post("/competition/webhook")  # alias, so an explicit-path URL also works
async def competition_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    config = Config.from_env()

    raw_body = await request.body()  # raw bytes — never request.json()
    try:
        event = verify_webhook(
            raw_body=raw_body,
            headers=request.headers,
            secret=config.webhook_secret,
        )
    except WebhookVerificationError as exc:
        return Response(content=str(exc), status_code=401)

    webhook_id = event.get("id")
    if not _claim(webhook_id):
        return Response(status_code=200)

    log_deadline(event)
    # Everything slow happens after this 200 goes out. The portal's "Test
    # Webhook" button sends a synthetic TEST event; it takes the same path and
    # submits a neutral prediction (accepted by the API, never scored) so the
    # test exercises your full receive -> submit loop.
    background_tasks.add_task(_predict_and_submit, event, config, webhook_id)
    return Response(status_code=200)


if __name__ == "__main__":
    import uvicorn

    # Railway injects PORT; bind to it (and 0.0.0.0) or healthchecks will fail.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
