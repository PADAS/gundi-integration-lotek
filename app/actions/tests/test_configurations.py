import pydantic
import pytest

from app.actions.configurations import BackfillObservationsConfig, PullObservationsConfig
from app.actions.core import InternalActionConfiguration


def test_max_data_age_hours_defaults_to_12_and_is_bounded_1_to_12():
    assert PullObservationsConfig().max_data_age_hours == 12
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfig(max_data_age_hours=0)
    with pytest.raises(pydantic.ValidationError):
        PullObservationsConfig(max_data_age_hours=13)


def test_max_data_age_hours_renders_as_range_slider():
    ui = PullObservationsConfig.ui_schema()
    assert ui["max_data_age_hours"]["ui:widget"] == "range"


def test_ui_order_lists_every_property_including_hidden_run_on_schedule():
    # rjsf + ajv strict mode fails silently when ui:order misses a property.
    ui = PullObservationsConfig.ui_schema()
    assert set(ui["ui:order"]) == set(PullObservationsConfig.schema()["properties"].keys())


def test_backfill_config_is_internal_and_fieldless():
    # InternalActionConfiguration subclasses are skipped at registration —
    # backfill must never appear in the portal.
    assert issubclass(BackfillObservationsConfig, InternalActionConfiguration)
    assert BackfillObservationsConfig.schema().get("properties", {}) == {}
