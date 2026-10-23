from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT.parent / "ff7r-navigation-probe" / "Scripts" / "navigation_system_inventory.lua"
INSTALLER = ROOT.parent / "ff7r-navigation-probe" / "Install-NavigationSystemInventory.ps1"
DISABLER = ROOT.parent / "ff7r-navigation-probe" / "Disable-NavigationSystemInventory.ps1"


def test_inventory_probe_is_read_only_and_does_not_enumerate_actors():
    source = SCRIPT.read_text(encoding="utf-8")
    executable_lines = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("--")
    )
    assert "FindAllOf" not in executable_lines
    assert "MoveTo" not in executable_lines
    assert "TestMove" not in executable_lines
    assert "movement_sent = false" in executable_lines
    assert "native_path_query_called = false" in executable_lines
    assert "ExecuteInGameThread" in executable_lines


def test_inventory_probe_targets_live_world_navigation_objects_only():
    source = SCRIPT.read_text(encoding="utf-8")
    for token in ("NavigationSystem", "MainNavData", "DefaultAgentName", "NavDataConfig", "RuntimeGeneration", "DataVersion"):
        assert token in source
    assert "main_nav_data_available" in source
    assert "world_navigation_system_missing" in source


def test_inventory_installation_is_process_guarded_and_has_disable_path():
    installer = INSTALLER.read_text(encoding="utf-8")
    disabler = DISABLER.read_text(encoding="utf-8")
    assert "FFVII Rebirth is still running; close it before deployment." in installer
    assert "FFVII Rebirth is still running; disable the inventory probe during a maintenance window." in disabler
    assert "mods.txt" in installer and "mods.txt" in disabler
    assert "FF7RNavigationSystemInventory" in installer and "FF7RNavigationSystemInventory" in disabler
