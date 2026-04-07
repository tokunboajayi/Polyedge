"""
monitoring/credit_monitor.py — Anthropic API credit availability guard.

Anthropic does not expose a credits-balance REST endpoint, so availability
is tested by making a minimal 1-token completion to claude-haiku before
each trading session (and periodically during it).  The test call costs
a fraction of a cent and completes in < 1 s.

Detection logic
---------------
  402  Payment Required    → credits exhausted                       → block
  any message containing "credit" or "quota"  → credits exhausted  → block
  Other 4xx / 5xx / timeout                   → transient error     → allow
  Success                                      → credits OK          → allow

Once `_credits_exhausted` is set to True it stays True for the lifetime of
the process — clearing it requires a restart after topping up.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

import anthropic

from config import constants as C

if TYPE_CHECKING:
    from monitoring.slack_alerts import SlackAlerter

logger = logging.getLogger(__name__)

# Minimal prompt — we only care whether the API accepts the call.
_PROBE_SYSTEM = "Respond with the single word OK."
_PROBE_USER   = "OK"


class CreditMonitor:
    """Guard that blocks trading when Anthropic API credits are exhausted.

    Usage::

        monitor = CreditMonitor(alerter=slack_alerter)

        # At startup and periodically
        if not await monitor.check_credits():
            await engine.enter_safe_mode("credits_exhausted")
    """

    def __init__(
        self,
        alerter: "SlackAlerter",
        low_threshold_usd: float = 2.00,
    ) -> None:
        from config import settings as S
        self._alerter           = alerter
        self._low_threshold_usd = low_threshold_usd
        self._credits_exhausted: bool = False
        self._client = anthropic.AsyncAnthropic(api_key=S.ANTHROPIC_API_KEY)
        logger.debug(
            "CreditMonitor ready  threshold=$%.2f", low_threshold_usd
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def check_credits(
        self,
        bankroll: float = 0.0,
        open_positions: int = 0,
    ) -> bool:
        """Probe the Anthropic API.  Returns True if safe to trade.

        Args:
            bankroll:       Current bankroll — forwarded to Slack alert.
            open_positions: Open position count — forwarded to Slack alert.
        """
        if self._credits_exhausted:
            # Already known exhausted — no need to probe again
            logger.warning("credits_exhausted_flag_set — blocking trading")
            return False

        remaining = await self._get_remaining_credits(bankroll, open_positions)
        # None means the probe succeeded (or failed transiently)
        # False means credit exhaustion was detected
        return remaining is not False

    def is_exhausted(self) -> bool:
        """Return True once a credit-exhaustion error has been detected."""
        return self._credits_exhausted

    # ------------------------------------------------------------------
    # Internal — minimal test call
    # ------------------------------------------------------------------

    async def _get_remaining_credits(
        self,
        bankroll: float,
        open_positions: int,
    ) -> bool | None:
        """Make a 1-token probe call to detect credit exhaustion.

        Returns:
            False  — credit exhaustion confirmed (402 or credit/quota message)
            None   — probe succeeded or failed for a non-credit reason (allow)
        """
        try:
            await asyncio.wait_for(
                self._client.messages.create(
                    model=C.CLAUDE_SCAN_MODEL,
                    max_tokens=1,
                    system=_PROBE_SYSTEM,
                    messages=[{"role": "user", "content": _PROBE_USER}],
                ),
                timeout=15.0,
            )
            logger.debug("credit_probe_ok")
            return None  # success

        except anthropic.APIStatusError as exc:
            msg_lower = str(exc).lower()
            if exc.status_code == 402 or "credit" in msg_lower or "quota" in msg_lower:
                self._credits_exhausted = True
                logger.critical(
                    "anthropic_credits_exhausted  status=%d  error=%s",
                    exc.status_code, exc,
                )
                self._alerter.credits_exhausted(
                    bankroll=bankroll,
                    open_positions=open_positions,
                )
                return False
            # Other 4xx/5xx — transient; don't block trading
            logger.warning(
                "credit_probe_api_error  status=%d  error=%s — allowing trading",
                exc.status_code, exc,
            )
            return None

        except (asyncio.TimeoutError, anthropic.APIConnectionError) as exc:
            logger.warning(
                "credit_probe_transient_error  error=%s — allowing trading", exc
            )
            return None

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "credit_probe_unexpected_error  error=%s — allowing trading", exc
            )
            return None
