# Trader analysis streaming (issue #59)

`AnalyzeTraderStream(TraderStreamRequest) returns (stream TraderAnalysisFrame)` streams the model's
own deltas (`llm.astream`). The unary `AnalyzeTrader` RPC is unchanged and stays a buffered result.

Frame: `analysis_id`, `event_type` (`status`, `metadata`, `content`, `completion`, `error`),
`provider_call_id`, monotonic `sequence` from 1, `observed_at`, `replayed`, `text`, `end_state`
(`completed`, `failed`, `timeout`, `cancelled`, `truncated`), `error_code` (`RATE_LIMITED`,
`DEADLINE_EXCEEDED`, `PROVIDER_ERROR`, `RESUME_INCOMPLETE`, `RESUME_EXPIRED`, `OUTPUT_LIMIT`,
`INVALID_ARGUMENT`), sanitised `error_message`, `metadata`, `output_tokens` (provider reported;
`output_tokens_source` says whether it was available), `time_to_first_token_ms`, `total_ms`,
`stream_kind` (`model_tokens`, `buffered_replay`, `status`).

* Metadata frame carries provider and model (per-request `llm_config` is honoured). The completion
  frame carries `research_context` (the citations source) and `provider_streamed`, which is false when
  the provider returned a single chunk, so a UI can tell a real token stream from a one-shot answer.
* Deadlines: overall (`TRADER_STREAM_DEADLINE_SECONDS`, capped by the RPC deadline), first token and
  idle timeouts. A timeout keeps already sent content and ends with an error frame.
* Backpressure: the provider is read one delta at a time, only after the previous frame was written.
* Cancellation: a client disconnect closes the provider stream and stops generation.
* Reconnect: resend the request with `analysis_id` and `resume_after_sequence`. Buffered frames are
  replayed with `replayed=true` and `stream_kind=buffered_replay`. An unfinished original reports
  `RESUME_INCOMPLETE`; an unknown or expired id reports `RESUME_EXPIRED`. The replay buffer is bounded
  by TTL, stream count and frame count, and is per process (not shared across replicas).
* Text: bytes deltas are decoded incrementally, lone surrogates and NULs are removed, and large deltas
  are split on code point boundaries (`TRADER_STREAM_MAX_FRAME_CHARS`). Output is capped
  (`TRADER_STREAM_MAX_OUTPUT_CHARS`, ends `truncated`).
* Backend client: `AnalysisClient.analyze_trader_events` orders and de-duplicates by sequence and
  reconnects with the resume point (bounded). The existing `analyze_trader_stream` compatibility
  iterator and the `/trader/stream` SSE route are unchanged and still labelled `buffered_replay`.
