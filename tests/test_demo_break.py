"""demo/break.sh: scenario handling and the Compose wiring it relies on."""

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "demo" / "break.sh"
SCENARIOS = ["ros2-subscriber-down", "ros2-publisher-down", "fleet-faults", "restore"]


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("args", [(), ("no-such-scenario",)])
def test_unknown_or_missing_scenario_prints_usage_and_exits_2(args):
    result = run(*args)
    assert result.returncode == 2
    for scenario in SCENARIOS:
        assert scenario in result.stdout


def test_script_is_executable_and_valid_bash():
    assert SCRIPT.stat().st_mode & 0o111
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_fault_scale_can_be_set_from_the_shell():
    # fleet-faults runs `FLEET_FAULT_SCALE=5 docker compose up fleet-sim`;
    # Compose only forwards it if the service interpolates it.
    env = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]["fleet-sim"]["environment"]
    assert env["FLEET_FAULT_SCALE"] == "${FLEET_FAULT_SCALE:-1.0}"


@pytest.mark.parametrize("pattern", ["demo_nodes_py/listener", "demo_nodes_cpp/talker"])
def test_killed_nodes_exist_in_the_launch_file(pattern):
    pkg, exe = pattern.split("/")
    launch = (ROOT / "ros2" / "robot.launch.xml").read_text()
    assert f'pkg="{pkg}" exec="{exe}"' in launch


def scenario(name: str) -> str:
    body = SCRIPT.read_text().split(f"  {name})", 1)[1]
    return body.split(";;", 1)[0]


def test_restore_uses_the_configured_fault_scale():
    # An inline FLEET_FAULT_SCALE=... would override .env.
    assert "FLEET_FAULT_SCALE" not in scenario("restore")


def test_restore_does_not_hide_ros2_restart_failures():
    restore = scenario("restore")
    assert "|| true" not in restore and "2>/dev/null" not in restore
    # ...but skips the restart when ros2-robot is not part of the stack.
    assert "config --services" in restore
