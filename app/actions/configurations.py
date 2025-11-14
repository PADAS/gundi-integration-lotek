import pydantic

from .core import AuthActionConfiguration, PullActionConfiguration, ExecutableActionMixin
from app.services.utils import GlobalUISchemaOptions


class AuthenticateConfig(AuthActionConfiguration, ExecutableActionMixin):
    username: str
    password: pydantic.SecretStr = pydantic.Field(..., format="password")

    ui_global_options: GlobalUISchemaOptions = GlobalUISchemaOptions(
        order=[
            "username",
            "password",
        ],
    )


class PullObservationsConfig(PullActionConfiguration):
    pass
