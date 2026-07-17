import pydantic

from typing import Optional

from .core import AuthActionConfiguration, PullActionConfiguration, ExecutableActionMixin
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
        description=(
            "How many days of historic data to fetch for new devices or on the first run. "
            "Also caps how far back the connector catches up after an outage."
        ),
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

    run_on_schedule: bool = FieldWithUIOptions(
        True,
        ui_options=UIOptions(widget="hidden"),
    )

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "default_lookback_days",
            "max_pdop",
            "run_on_schedule",
        ],
    )
