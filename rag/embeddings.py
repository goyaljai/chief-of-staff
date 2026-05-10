"""Stage 1: Databricks gte-large-en (1024-dim) embeddings + warmup helpers.

We talk to the Databricks AI Gateway through its OpenAI-compatible endpoint
because (a) we already have DATABRICKS_TOKEN configured for chat, and (b)
the gateway's batch interface is identical to OpenAI's, which keeps the
client code trivial.

Why a hand-rolled class instead of langchain-databricks:
  • langchain-databricks pulls in a heavy dependency tree we otherwise
    don't need
  • we want exact control over retry/backoff (R5-1, R7-1) and over the
    empty-input handling that the langchain wrapper doesn't surface
  • the LangChain Embeddings interface is two methods — embed_query and
    embed_documents — so the implementation cost is minimal

Module state (singletons via _LOCK):
  _EMBED   — the DatabricksEmbeddings instance (constructed once on first use)
  _READY   — toggled True after a successful warmup query

R-fixes baked in:
  R5-1 — jittered exponential backoff on transient failures
  R6-1 — empty-string substitution preserves caller's input/output ordering
         (the prior version filtered empties and silently misaligned
         metadatas with vectors — a critical data-corruption bug)
  R6-2 — actionable RuntimeError if DATABRICKS_TOKEN/BASE_URL are missing
  R7-1 — disable the OpenAI SDK's own retry loop so retry layers don't
         compound (was producing up to ~15 attempts on a single 429)

Public API:
  DatabricksEmbeddings — the class
  _get_embed()         — singleton accessor
  warm_embedding_fn(timeout_secs=120) -> bool
  is_ready() -> bool
"""
import os
import threading


_LOCK = threading.RLock()
_EMBED = None
_READY = False


class DatabricksEmbeddings:
    """OpenAI-compatible embeddings client pointing at the Databricks AI
    Gateway's /v1/embeddings endpoint. Returns 1024-dim vectors from
    `databricks-gte-large-en` (configurable via the `model` constructor arg)."""

    def __init__(self, model: str = "databricks-gte-large-en"):
        # Lazy SDK import — keeps `import rag.embeddings` cheap and makes
        # the import optional in environments that don't have the OpenAI
        # SDK installed (we always do, but principle-of-least-surprise wins).
        from openai import OpenAI

        # R6-2: actionable error if creds are missing. Default os.environ[]
        # raises a bare KeyError that crashes deep inside add_texts with no
        # hint about how to fix it.
        token = os.environ.get("DATABRICKS_TOKEN", "").strip()
        base_url = os.environ.get("DATABRICKS_BASE_URL", "").strip()
        if not token or not base_url:
            raise RuntimeError(
                "DatabricksEmbeddings: DATABRICKS_TOKEN and DATABRICKS_BASE_URL "
                "must both be set. Add them to .env (see config.py)."
            )

        # R7-1: max_retries=0 disables the OpenAI SDK's own retry loop.
        # Our outer _create_with_retry already retries 4 times with backoff;
        # nested retry layers compound — a sustained 429 used to attempt up
        # to 15 times (3 SDK × ~5 outer-attempt-aware paths). Now: ours is
        # the only retry layer. Cleaner, predictable, faster failure.
        self._client = OpenAI(
            api_key=token,
            base_url=base_url,
            timeout=60.0,
            max_retries=0,
        )
        self._model = model
        self._batch_size = 96  # Databricks gateway accepts batches; 96 is safe

    def _create_with_retry(self, inputs: list[str]):
        """R5-1: jittered exponential backoff on transient failures.
        Mirrors orchestrator._chat's pattern. 4 attempts total: 1s, 2s, 4s,
        8s base waits + 0–1s jitter."""
        from openai import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
        import random as _random
        import time as _time

        transient_excs = (
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
        )
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return self._client.embeddings.create(model=self._model, input=inputs)
            except transient_excs as e:
                last_exc = e
                if attempt == 3:
                    raise
                wait = (2 ** attempt) + _random.uniform(0, 1.0)
                print(
                    f"[rag] embed transient {type(e).__name__} "
                    f"attempt {attempt+1}/4 — retrying in {wait:.1f}s"
                )
                _time.sleep(wait)
        if last_exc:  # pragma: no cover (loop exits via raise)
            raise last_exc

    def embed_query(self, text: str) -> list[float]:
        """Embed a single query. Used by retrieval calls."""
        # Single placeholder for fully-empty queries so callers don't crash;
        # the resulting vector is the embedding of " " which is fine for
        # the rare empty-query edge case (returns no useful matches).
        resp = self._create_with_retry([text or " "])
        return resp.data[0].embedding

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of documents. Splits into chunks of `_batch_size`
        to respect gateway limits.

        R6-1 fix (CRITICAL — silent data corruption):
            Previously we filtered empty strings BEFORE sending to the API.
            That broke the input-ordering contract: callers passing
            `[s1, '', s2]` got back 2 vectors for 3 metadata entries, and
            PGVector.add_texts wrote vectors against the WRONG metadatas.
            Now we substitute empties with a single space placeholder so
            the API still accepts the call AND the output count matches
            the input count exactly. Caller's metadatas/ids align.
        """
        if not texts:
            return []
        clean_texts = [t if (t and t.strip()) else " " for t in texts]
        out: list[list[float]] = []
        for i in range(0, len(clean_texts), self._batch_size):
            chunk = clean_texts[i:i + self._batch_size]
            resp = self._create_with_retry(chunk)
            chunk_out = [d.embedding for d in resp.data]
            if len(chunk_out) != len(chunk):
                # Defensive — should never happen now, but if it does we
                # fail loudly rather than silently misalign downstream.
                raise RuntimeError(
                    f"DatabricksEmbeddings: API returned {len(chunk_out)} "
                    f"vectors for {len(chunk)} inputs — would corrupt mapping"
                )
            out.extend(chunk_out)
        return out


def _get_embed() -> DatabricksEmbeddings:
    """Singleton accessor for the embedding client. Thread-safe via _LOCK."""
    global _EMBED
    if _EMBED is not None:
        return _EMBED
    with _LOCK:
        if _EMBED is None:
            _EMBED = DatabricksEmbeddings()
        return _EMBED


def warm_embedding_fn(timeout_secs: int = 120) -> bool:
    """Pre-warm the embedding client by issuing one query embed. Useful at
    startup so the first real request doesn't pay the connection setup cost.
    Returns True on success, False on failure (caller decides to retry /
    fail-soft)."""
    global _READY
    if _READY:
        return True
    try:
        emb = _get_embed()
        emb.embed_query("warmup")
        _READY = True
        print("[rag] T2 Databricks gte-large-en embedding warmed up (1024-dim)")
        return True
    except Exception as e:
        print(f"[rag] warmup failed: {e}")
        return False


def is_ready() -> bool:
    return _READY
