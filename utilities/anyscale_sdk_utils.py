import os
from typing import List

from chaos_test.constants import ACTIVE_SERVICE_STATUSES

from anyscale.sdk.anyscale_client.sdk import AnyscaleSDK
from anyscale.sdk.anyscale_client.models import ServiceModel


def create_anyscale_sdk():
    """Creates an Anyscale SDK object."""

    if "ANYSCALE_HOST" not in os.environ or "ANYSCALE_CLI_TOKEN" not in os.environ:
        raise RuntimeError(
            "Both ANYSCALE_HOST and ANYSCALE_CLI_TOKEN env vars must be set. "
            "The current values are "
            f"ANYSCALE_HOST=\"{os.environ.get('ANYSCALE_HOST', '')}\" and "
            f"ANYSCALE_CLI_TOKEN=\"{os.environ.get('ANYSCALE_CLI_TOKEN', '')}\"."
        )
    
    host = os.environ["ANYSCALE_HOST"]
    cli_token = os.environ["ANYSCALE_CLI_TOKEN"]

    return AnyscaleSDK(auth_token=cli_token, host=host)


def get_service_model(sdk: AnyscaleSDK, service_name: str, project_id: str) -> ServiceModel:
    """Gets the service model for a service."""

    service_models: List[ServiceModel] = sdk.list_services(
        name=service_name,
        project_id=project_id,
        state_filter=ACTIVE_SERVICE_STATUSES
    ).results

    if len(service_models) != 1:
        raise RuntimeError(
            f"Found {len(service_models)} services with name "
            f"{service_name} in project {project_id}. "
            "Expected 1."
        )
    else:
        return service_models[0]
