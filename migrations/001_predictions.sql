-- Async results store. One row per async-predicted request. request_id is
-- the dedup key: worker.py's at-least-once redelivery (see pulse_serve/queue.py)
-- can attempt this insert more than once for the same job; ON CONFLICT DO
-- NOTHING makes the second attempt a no-op rather than an error, which is
-- what turns "at-least-once delivery" + "idempotent write" into
-- "effectively-once" from the caller's point of view (see README.md's
-- delivery-semantics section).
CREATE TABLE IF NOT EXISTS predictions (
    request_id uuid PRIMARY KEY,
    route_id text NOT NULL,
    direction_id int NOT NULL,
    stop_id text NOT NULL,
    trip_id text NOT NULL,
    horizon_min int NOT NULL,
    result jsonb NOT NULL,
    model_version text NOT NULL,
    is_baseline boolean NOT NULL,
    queued_at timestamptz NOT NULL,
    completed_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS predictions_route_id_idx ON predictions (route_id);
CREATE INDEX IF NOT EXISTS predictions_completed_at_idx ON predictions (completed_at);
