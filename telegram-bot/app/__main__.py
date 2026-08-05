"""Entry point: python -m app

Loads the environment config, fails loudly on anything missing that would leave a
half-open surface, builds the bot, and runs long polling. Long polling holds one
outbound HTTPS connection to Telegram — no inbound port, no webhook — which is
what makes the sidecar usable from an isolated management VLAN.
"""

from __future__ import annotations

import logging
import sys

from . import config


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    log = logging.getLogger("picotty-telegram")

    try:
        cfg = config.load()
    except config.ConfigError as e:
        log.error("configuration error: %s", e)
        return 2

    # Fail early if the shell tier is on but its second-factor library is missing,
    # rather than discovering it only when someone tries to /arm.
    if cfg.shell_enabled:
        try:
            import pyotp  # noqa: F401
        except ImportError:
            log.error("SHELL_ENABLED is on but pyotp is not installed "
                      "(pip install pyotp), or set SHELL_ENABLED=false")
            return 2

    from .bot import build_application

    log.info("hub=%s ws=%s shell=%s alerts=%s allowlist=%d chat(s)",
             cfg.hub_base_url, cfg.ws_url, cfg.shell_enabled, cfg.alerts_enabled,
             len(cfg.allowed_chat_ids))

    app = build_application(cfg)
    # run_polling manages the event loop, signal handling, and post_init/shutdown.
    app.run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
