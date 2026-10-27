# Equipment, Materia and Combat Settings

`field_menu_async(action="change_loadout", change=..., member=..., execute=true)` changes equipment, Materia, weapon skills, shortcuts or party composition through the game's menus. Poll `get_field_menu_status` and inspect its nested result. The action does not save the game. With `execute=false`, it reads the member's loadout and party from the current save data without sending input.

| Change | Arguments | Verification |
| --- | --- | --- |
| `equipment` | `category` (`weapon`, `armor` or `accessory`), `item`, optional `retain_upgrade_setting` | The screen shows the item and the save-data diff is limited to the appropriate equipment field. |
| `materia` | `row`, `slot`, `copy_id` from `read_materia_inventory`, optional `take_from` | The socket and copy ownership agree, and the diff is limited to the affected sockets and copies. |
| `weapon_skill` | `slot`, `skill`, optional `disable_auto_upgrade` | The screen and the weapon's skill fields show the change. |
| `shortcut` | `slot` 1–4, `category`, `entry` | The screen and shortcut table show the change. |
| `party` | `member` to remove from the frontline, `member_in` from the backline | The save's party lists match the swap. |

Selection and prompt highlights are read before each Confirm. The action stops on ambiguous or stalled UI evidence and reports unexpected save-data changes instead of claiming verification. The result screen stays open unless `close_menu=true` is specified. These controls require a compatible Windows game build, verified process and live telemetry; the offline tests use fixtures and cannot establish native acceptance for this standalone build.

`read_loadout_menu` reads a request-scoped equipment or Combat Settings screen without input. Optional stage waits and bounded choice scans stop on incomplete or changed owner evidence. `read_materia_inventory` reads the currently open Materia list, including save-scoped copy identities. `preview_loadout_change` is a data-only preview of a supplied normalized snapshot and does not establish a game outcome; no live normalized-snapshot adapter is available yet. `switch_combat_character` controls the active combat member with readback and refuses Tactical Mode. Its native combat-switch acceptance is still pending.
