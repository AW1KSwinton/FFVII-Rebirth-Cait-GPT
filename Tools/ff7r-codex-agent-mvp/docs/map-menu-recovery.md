# Map travel after a reconnect

`travel_to_map_spot_async(spot=..., execute=true)` can recover an open field Main Menu before opening the map. Poll `get_travel_to_map_spot_status` and inspect the nested result; completion alone does not prove arrival.

Recovery requires fresh field and process identity, live telemetry, an established Main Menu title and footer, and exactly one attached Main Menu owner in a complete root census. It refuses input for unknown or competing owners, dialogue, loading, combat, cutscenes, and Queen's Blood. A cached menu flag or an unexplained pause alone never authorizes Cancel. Detached retained widgets cannot displace a proven owner, while unknown attachments remain blockers.

Each attempt rechecks the control, arm, telemetry and foreground guards before a bounded Cancel. A lost or unverified input receipt stops without a replay. After the menu clears, normal settled-field and map-travel checks still apply. The async result retains `menu_recovery`, diagnostic reasons, source timestamps and the guarded press ledger. If the screen cannot be identified, capture the game window and inspect `read_ui_roots(details=true)` and `read_save_menu` before choosing a recovery action.

The request-only root census depends on the matching telemetry Lua source. Offline fixtures check recognized and ambiguous owners, stale observations, changed field identity, lost receipts and input-lock ownership. Offline tests do not prove native acceptance of this standalone build; verify the installed exporter and live behavior separately.
