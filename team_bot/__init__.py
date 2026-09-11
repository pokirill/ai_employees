"""Team bot package bootstrap.

Miro Table sync is opt-in so existing deployments keep the legacy Card/Frame
integration until ``MIRO_TABLE_URL`` is explicitly configured.
"""

from __future__ import annotations

import os

if os.getenv("MIRO_TABLE_URL", "").strip():
    # ``team_bot.main`` imports SprintFlow from this already-loaded module.
    # Swapping the class here avoids a risky 90k-line main.py rewrite while
    # preserving all existing command registration and sprint behaviour.
    from team_bot import sprint_flow as _sprint_flow
    from team_bot.miro_table_flow import TableAwareSprintFlow

    _sprint_flow.SprintFlow = TableAwareSprintFlow
