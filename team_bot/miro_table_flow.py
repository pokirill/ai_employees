"""Opt-in Team Helper flow that syncs the real Miro task table via MCP.

The existing ``SprintFlow`` and legacy Card/Frame sync remain untouched. When
``MIRO_TABLE_URL`` is configured this subclass swaps only the Miro leg of the
star topology; Reminders, sprint lifecycle and Telegram commands keep using the
same code paths as before.
"""

from __future__ import annotations

from shared.miro_table_sync import board_from_env, sync_miro_table
from shared.sync_engine import SyncResult, sync_miro, sync_reminders
from team_bot.sprint_flow import SprintFlow as LegacySprintFlow


class TableAwareSprintFlow(LegacySprintFlow):
    """Use Miro Table/MCP when configured, otherwise legacy Miro Cards."""

    def board_for(self, sprint):
        table = board_from_env()
        if table is not None:
            return table
        return super().board_for(sprint)

    def _sync_blocking(self, sprint):
        result = SyncResult()
        board = self.board_for(sprint)
        if board is not None:
            if getattr(board, "table_mode", False):
                result.extend(
                    sync_miro_table(
                        self.tasks,
                        self.sync_state,
                        board,
                        sprint_id=sprint.id if sprint else None,
                    )
                )
            else:
                result.extend(
                    sync_miro(
                        self.tasks,
                        self.sync_state,
                        board,
                        sprint_id=sprint.id if sprint else None,
                    )
                )

        reminders = self.reminders()
        if reminders is not None:
            result.extend(sync_reminders(self.tasks, self.sync_state, reminders))
        return result
