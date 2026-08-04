from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_playwright_package_and_image_have_exact_parity():
    requirements = (ROOT / "requirements.txt").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "playwright==1.61.0" in requirements
    assert "playwright>=" not in requirements
    assert "mcr.microsoft.com/playwright/python:v1.61.0-noble" in dockerfile
    assert "PLAYWRIGHT_VERSION=1.61.0" in dockerfile


def test_compose_has_persistent_state_healthcheck_and_one_service():
    compose = (ROOT / "docker-compose.yml").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert compose.startswith("services:")
    assert compose.count("\n  watcher:") == 1
    assert "sv-data:/app/data" in compose
    assert "volumes:\n  sv-data:" in compose
    assert "HEALTHCHECK" in dockerfile
    assert "USER pwuser" in dockerfile
