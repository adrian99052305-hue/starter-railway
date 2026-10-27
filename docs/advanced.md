# Advanced notes

The hero README keeps the path to a first deploy as short as possible. This file
collects everything intentionally left out of it.

## The webhook verifier is vendored

`src/explaining_markets/webhook_verification.py` is a verbatim copy of the
competition's reference verifier (stdlib-only, zero runtime dependencies). It's
vendored — not installed from PyPI — so the starter is self-contained.

If the competition later publishes the verifier as a package, you can delete the
vendored file and depend on the pinned package instead, importing
`verify_webhook` / `WebhookVerificationError` from there. The frozen test vectors
in `tests/test_vectors.json` will keep passing either way, since both
implementations pin to the same values.

## Credentials: local `.env` vs Railway Variables

Locally, `app.py` loads `.env` via `python-dotenv`, so runs and tests just work.
Deployed services read **Railway Variables** instead — Railway never sees your
local `.env`. The README's one-liner pushes every non-comment line of `.env` to
the linked service:

```bash
railway variable set $(grep -v '^#' .env | xargs)
```

Setting variables triggers a redeploy by default (add `--skip-deploys` to batch
several `set` calls). The dashboard equivalent is the service's **Variables** tab,
whose raw editor accepts pasted `KEY=value` lines — the same format the portal's
credentials dialog gives you.

## Webhook path and URL

The handler is registered on both `POST /` (primary) and `POST /competition/webhook`
(alias), so either URL works. We serve it at the root so the URL `railway domain`
prints *is* your webhook URL, with nothing to append.

`railway domain` mints a `https://<service>.up.railway.app` subdomain; run
`railway domain example.com` instead to attach a custom domain.

## How the build works

`railway.json` pins the [Railpack](https://railpack.com) builder, which detects
`pyproject.toml` + `uv.lock` and installs with `uv sync --locked --no-dev` — the
project itself (the `src/explaining_markets` package) is installed into the venv,
which is why `app.py` can import it in production. Two consequences:

- **Commit `uv.lock` and keep it in sync.** `--locked` fails the build if the
  lockfile has drifted from `pyproject.toml`; run `uv sync` after editing
  dependencies and commit the updated lockfile.
- The `dev` dependency group (pytest) is not installed in production.

The start command is `python app.py`, which binds `0.0.0.0:$PORT` — Railway
injects `PORT` and healthchecks `GET /` before routing traffic to a new deploy.

## Two clocks: 20 seconds to ACK, 5 minutes to predict

`app.py` verifies → ACKs 200 → predicts and submits in a background task. The
platform runs two independent timers:

| Clock | Budget | Starts | Miss it and… |
|---|---|---|---|
| Delivery ACK | 20 s | when the platform POSTs to you | the delivery is retried up to 5 times over ~30 min; 5 consecutive failures emails your admins, ~50 disables your webhook |
| Prediction window | 5 min | when you ACK 200 | your prediction is tagged late and dropped at scoring |

The 5-minute window only opens once you ACK. Predicting before the ACK spends it
inside the 20-second budget — a 25-second model call is fine against your
prediction window and a hard failure against your delivery budget.

Once you ACK, the platform considers the delivery done and will not redeliver, so
a crash or redeploy mid-flight loses that event's prediction. `predict.py` retries
the model call once for this reason. To make the work itself durable, push the
verified event onto a queue (e.g. a Railway Redis service) and process it in a
separate worker service.

Background tasks run in FastAPI's worker threadpool, which is why
`_predict_and_submit` in `app.py` is a plain `def` and not `async def` — blocking
httpx and OpenAI calls in an `async def` task would run on the event loop and
stall every other delivery arriving at the same time.

## Idempotency

Deliveries are deduped on the `Webhook-Id` header (equal to `event["id"]`), which
is stable across retries. The guard has three states:

* **in flight** — claimed on arrival, before any work. A duplicate landing while
  the first job runs is skipped, so you never pay for the same model call twice.
* **done** — set only after the API accepts your prediction. Permanent.
* **released** — if the job raises, the claim is dropped, so the next delivery of
  that `Webhook-Id` re-runs it.

Marking an event done up front would be the bug: a failed prediction would look
handled.

A replay of an older event arrives with a fresh `Webhook-Id`, so a "done" marker
never blocks one.

The store is an in-memory dict behind a lock and resets on restart or redeploy.
That's fine for a starter — duplicate submissions are harmless (the API tags them
`accepted_duplicate`; only your first accepted POST is scored). For dedupe that
survives restarts, add a Railway Redis or Postgres service keyed on `Webhook-Id`.

## Not included by design

No Docker, Terraform, GitHub Actions, custom CLI, or multiple deployment modes —
Railway's Railpack builds, healthchecked deploys, and service variables cover the
starter. Add those only if your own setup needs them.
