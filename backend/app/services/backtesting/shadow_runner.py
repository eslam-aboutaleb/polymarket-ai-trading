"""Shadow mode: decisions on live observations, orders simulated, nothing sent.

There is deliberately no gateway, signer, account or network object anywhere
in this module. A ShadowSession can only record observations and run the same
deterministic engine over them, so it cannot place, sign or cancel an order.
"""

from app.services.backtesting.contracts import EngineConfig, ReplayEvent
from app.services.backtesting.event_replay_engine import Decider, replay


class ShadowSession:
    def __init__(
        self,
        decider: Decider,
        config: EngineConfig,
        strategy_version: str,
        parameters: dict | None = None,
    ):
        self._decider = decider
        self._config = config
        self._version = strategy_version
        self._parameters = parameters or {}
        self._events: dict[str, ReplayEvent] = {}

    def observe(self, event: ReplayEvent) -> None:
        previous = self._events.get(event.event_id)
        if previous is not None and previous != event:
            raise ValueError(f"conflicting content for event {event.event_id}")
        self._events[event.event_id] = event

    def report(self) -> dict:
        report = replay(
            list(self._events.values()),
            self._decider,
            self._config,
            strategy_version=self._version,
            parameters=self._parameters,
            mode="shadow_not_executed",
        )
        report["shadow"] = {
            "orders_signed": 0,
            "orders_submitted_to_venue": 0,
            "note": "decisions and fills are simulated against observed books; nothing was sent",
        }
        return report


def run_shadow(
    events: list[ReplayEvent],
    decider: Decider,
    config: EngineConfig,
    strategy_version: str,
    parameters: dict | None = None,
) -> dict:
    session = ShadowSession(decider, config, strategy_version, parameters)
    for event in sorted(events, key=lambda e: (e.available_at, e.sequence, e.event_id)):
        session.observe(event)
    return session.report()


_PARITY_KEYS = ("orders", "fills", "ledger", "pnl", "settlements")


def parity(shadow: dict, paper: dict) -> dict:
    """Shadow and paper replay must agree exactly on identical observations."""
    differences = [key for key in _PARITY_KEYS if shadow.get(key) != paper.get(key)]
    return {
        "identical": not differences,
        "differences": differences,
        "same_data_snapshot": shadow["run"]["data_snapshot_id"] == paper["run"]["data_snapshot_id"],
    }
