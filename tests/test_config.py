"""Configuration: the deployment files, and the CI that gates them.

The suite never runs the Dockerfile, the compose file or the workflows, so the
properties that matter in them are pinned here as text.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compose_publishes_on_loopback_unless_told_otherwise():
    """Docker's published ports bypass ufw and firewalld, so the default has to
    be the safe one rather than a comment recommending it."""
    text = (ROOT / "docker-compose.yml").read_text()
    assert '"${SMOLPLAN_BIND:-127.0.0.1}:${SMOLPLAN_PORT:-8107}:8000"' in text


def test_compose_hands_the_container_its_host_allowlist_and_timezone():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "SMOLPLAN_ALLOWED_HOSTS: ${SMOLPLAN_ALLOWED_HOSTS:-}" in text
    assert "TZ: ${TZ:-UTC}" in text


def test_compose_runs_the_container_locked_down():
    text = (ROOT / "docker-compose.yml").read_text()
    for line in ("read_only: true", "- /tmp", "cap_drop:", "- ALL", "- no-new-privileges:true"):
        assert line in text, line


def test_env_example_documents_the_network_settings():
    text = (ROOT / ".env.example").read_text()
    for name in ("SMOLPLAN_BIND", "SMOLPLAN_ALLOWED_HOSTS", "TZ"):
        assert f"{name}=" in text, name


def test_the_base_image_is_pinned_by_digest():
    first = next(line for line in (ROOT / "Dockerfile").read_text().splitlines()
                 if line.startswith("FROM "))
    assert re.fullmatch(r"FROM python:\d+\.\d+-slim@sha256:[0-9a-f]{64}", first), first


WORKFLOWS = ROOT / ".github" / "workflows"


def test_every_action_is_pinned_to_a_commit():
    """A tag can be moved to other code; a commit cannot. Worst in the automerge
    job, which runs with a write token."""
    for path in WORKFLOWS.glob("*.yml"):
        for use in re.findall(r"uses:\s*(\S+)", path.read_text()):
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use), f"{path.name}: {use}"


def test_automerge_checks_who_wrote_the_pr_not_only_who_triggered_it():
    workflow = (WORKFLOWS / "dependabot-automerge.yml").read_text()
    assert "github.event.pull_request.user.login == 'dependabot[bot]'" in workflow
    assert "endsWith(github.repository, '-private')" in workflow


def test_ci_tests_the_python_the_image_ships():
    image = re.search(r"FROM python:(\d+\.\d+)", (ROOT / "Dockerfile").read_text()).group(1)
    for name in ("ci.yml", "dependabot-automerge.yml"):
        workflow = (WORKFLOWS / name).read_text()
        assert image in workflow and "3.10" in workflow, name


def test_ci_installs_nothing_unpinned():
    for name in ("ci.yml", "dependabot-automerge.yml"):
        assert "pip install pytest" not in (WORKFLOWS / name).read_text(), name


def test_automerge_builds_and_health_checks_the_image_before_merging():
    workflow = (WORKFLOWS / "dependabot-automerge.yml").read_text()
    build = workflow.index("docker compose up -d --build --wait")
    assert build < workflow.index("gh pr merge")
    assert "docker compose up -d --build --wait" in (WORKFLOWS / "ci.yml").read_text()
