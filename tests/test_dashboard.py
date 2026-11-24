"""The paste-ready Utility bill dashboard stays in step with the sensors and README."""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "dashboards" / "utility-bill.yaml"
README = ROOT / "README.md"
STRINGS = ROOT / "custom_components" / "lus" / "strings.json"


def _entity_ids(text: str) -> list[str]:
    return re.findall(r"^\s*- entity: (\S+)\s*$", text, re.MULTILINE)


def _expected_entity_ids() -> set[str]:
    """What Home Assistant names each sensor on a fresh install: device "LUS" + sensor name."""
    names = json.loads(STRINGS.read_text(encoding="utf-8"))["entity"]["sensor"]
    return {
        "sensor.lus_" + re.sub(r"[^a-z0-9]+", "_", n["name"].lower()).strip("_")
        for n in names.values()
    }


def test_dashboard_lists_every_sensor_once():
    ids = _entity_ids(DASHBOARD.read_text(encoding="utf-8"))
    assert len(ids) == len(set(ids))
    assert set(ids) == _expected_entity_ids()


def test_dashboard_is_a_two_column_sections_view():
    yaml = pytest.importorskip("yaml")
    view = yaml.safe_load(DASHBOARD.read_text(encoding="utf-8"))["views"][0]
    assert view["type"] == "sections"
    assert view["max_columns"] == 2
    # The two halves of each row must be the same height, or the cards don't line up.
    halves = [s for s in view["sections"] if "column_span" not in s]
    rows = [s["cards"][0]["grid_options"]["rows"] for s in halves]
    assert len(rows) == 4 and rows[0] == rows[1] and rows[2] == rows[3]


def test_dashboard_has_no_comments():
    # The raw configuration editor stops to warn about comments and drops them.
    for line in DASHBOARD.read_text(encoding="utf-8").splitlines():
        assert not line.lstrip().startswith("#"), line


def test_readme_block_matches_dashboard_file():
    readme = README.read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(views:\n.*?)```", readme, re.DOTALL)
    assert blocks == [DASHBOARD.read_text(encoding="utf-8")]


def test_readme_images_exist():
    readme = README.read_text(encoding="utf-8")
    images = re.findall(r"(docs/images/[\w.-]+\.png)", readme)
    assert images
    for image in images:
        assert (ROOT / image).is_file(), image
