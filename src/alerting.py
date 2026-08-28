"""
alerting.py - single entry point for anything that should reach a human, not
just wait in a log file to be read.

Stage 10 (30 Aug 2026): currently logs at ERROR with a distinctive prefix
only. runner.py's setup_logging() already attaches both a rotating file
handler and a console StreamHandler at a level that includes ERROR, so an
alert() call is visible today without any new infrastructure - it just isn't
PUSHED anywhere yet. LIVE_EXECUTION_PLAN.md named the choice of outbound
channel (Slack webhook, SMS, a paging service, email...) as a preference call
for the operator, not an engineering one, and deliberately left it unpicked.
This module exists so every call site that will eventually want a real
channel already goes through one place, rather than needing to be found and
rewired individually later.

    from alerting import alert
    alert("daily_loss_cap", "today's P&L -0.6000 SOL breached the -0.5000 cap")
"""

import logging

log = logging.getLogger("alerting")

ALERT_PREFIX = "[ALERT]"


def alert(level, message):
    """
    Single entry point for anything that should reach a human.

    level: a free-form short label describing severity/category (e.g.
    "critical", "daily_loss_cap", "fill_uncertain"). Not used to route
    anywhere today - just included in the log line so a future outbound
    channel can filter on it without this function's signature changing.

    message: human-readable text, already formatted - this function does
    not do any % / f-string interpolation itself, so a caller's own message
    is never silently mangled by a formatting mismatch here.

    Deliberately does NOT raise, retry, sleep, or block on anything network-
    related - alerting must never be the reason a monitor cycle stalls or a
    trade fails to execute. Once a real channel is wired at the extension
    point below, it must keep that same guarantee (wrap it in its own
    try/except there, not here).
    """
    log.error("%s [%s] %s", ALERT_PREFIX, level, message)

    # EXTENSION POINT: wire a real outbound channel here (Slack webhook, SMS,
    # email, a paging service, ...). Left unimplemented on purpose - see the
    # module docstring. Whatever is added here must stay non-blocking and
    # exception-safe (its own try/except), for the same reason alert()
    # itself must never raise: a broken alert channel must not be able to
    # take down the very loop it exists to warn about.
