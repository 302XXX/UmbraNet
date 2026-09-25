from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def parse_requirements(name):
    return [Requirement(line.strip()) for line in (ROOT / name).read_text().splitlines()
            if line.strip() and not line.lstrip().startswith(("#", "-"))]


def test_all_runtime_dependencies_and_constraints_are_exact():
    for filename in ("requirements.txt", "constraints.txt"):
        for requirement in parse_requirements(filename):
            pins = list(requirement.specifier)
            assert len(pins) == 1 and pins[0].operator == "=="
            assert "*" not in pins[0].version
    assert "-c constraints.txt" in (ROOT / "requirements.txt").read_text()


def test_installer_uses_single_requirements_source_without_legacy_dpi():
    installer = (ROOT / "install.bat").read_text()
    commands = [line for line in installer.splitlines() if "pip install" in line]
    assert len(commands) == 2  # tooling bootstrap, then the single runtime list
    assert '-r "%APP_DIR%requirements.txt"' in commands[1]
    assert not any("pydivert" in command.lower() for command in commands)
    assert not any(req.name.lower() == "pydivert" for req in parse_requirements("requirements.txt"))


def test_direct_pins_match_constraint_pins():
    constraints = {req.name.lower(): str(req.specifier) for req in parse_requirements("constraints.txt")}
    for req in parse_requirements("requirements.txt"):
        assert constraints[req.name.lower()] == str(req.specifier)
