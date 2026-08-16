import pydantic

from typing import Optional

from .core import (
    AuthActionConfiguration,
    ExecutableActionMixin,
    InternalActionConfiguration,
    PullActionConfiguration,
)
from app.services.utils import GlobalUISchemaOptions, UIOptions, FieldWithUIOptions


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    username: str
    password: pydantic.SecretStr = pydantic.Field(..., format="password")

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "username",
            "password",
        ],
    )


class PullObservationsConfig(PullActionConfiguration, ExecutableActionMixin):
    default_lookback_days: int = pydantic.Field(
        7,
        ge=1,
        le=60,
        title="Default lookback (days)",
        description="How many days of historic data to import when a device is first seen.",
    )
    max_data_age_hours: int = FieldWithUIOptions(
        12,
        ge=1,
        le=12,
        title="Max data age (hours)",
        description=(
            "Freshness bound: every run fetches at most this many hours back. "
            "Positions uploaded longer ago than this that could not be fetched "
            "are skipped permanently."
        ),
        ui_options=UIOptions(widget="range"),
    )
    max_pdop: Optional[float] = pydantic.Field(
        None,
        ge=0,
        title="Max PDOP",
        description=(
            "If set, only observations with PDOP <= this value will be sent. "
            "Leave blank to send all observations."
        ),
    )

    # Intentionally hidden from the portal UI: scheduled execution for this
    # integration is managed out-of-band, not exposed as an operator toggle.
    # The field is kept (defaulting to True) so the action_runner's
    # skip-when-disabled logic still has a value to read.
    run_on_schedule: bool = FieldWithUIOptions(
        True,
        ui_options=UIOptions(widget="hidden"),
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "default_lookback_days",
            "max_data_age_hours",
            "max_pdop",
            "run_on_schedule",
        ],
    )


class BackfillObservationsConfig(InternalActionConfiguration):
    """Internal-only: triggered by pull_observations, never configured in the portal.

    Needs at least one real field: an InternalActionConfiguration is never
    portal-registered, so the action-runner has no persisted config to resolve
    for it (config_manager.get_action_configuration returns None). A fieldless
    model's .dict() is {}, which trigger_action publishes as an empty
    config_overrides — indistinguishable from "no override at all", which
    trips execute_action's "configuration is missing" 404 before the handler
    ever runs. triggered_by gives the override a non-empty payload and doubles
    as a diagnostic marker of what triggered this run.
    """
    triggered_by: str = pydantic.Field(
        "pull_observations",
        title="Triggered By",
        description="Which action triggered this backfill run.",
    )
