-- T8 (P0 #1): notes_history append-only column.
--
-- Pre-T8, mid-flight notes lived only in TaskState.user_notes (pending
-- queue) which was cleared once consumed by a correction prompt. After
-- consumption, the supervisor + reviewer had no record of "the user
-- ever asked for X" — so the reviewer couldn't verify whether each
-- mid-flight ask was reflected in the deliverable, and Claude in
-- subsequent loops only saw the reviewer's lossy issue summary, not
-- the original verbatim note.
--
-- T8 splits the data model:
--   notes_history (this column) — append-only spec list, never cleared.
--                                  Reviewer arbitrates done-ness against
--                                  this on every final_review.
--   user_notes (existing T6b col) — pending queue for the next
--                                    correction loop, cleared on
--                                    injection.
--
-- Idempotent + nullable.

ALTER TABLE tasks
  ADD COLUMN IF NOT EXISTS notes_history JSONB;
