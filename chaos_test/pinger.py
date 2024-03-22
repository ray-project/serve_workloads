import time
import logging
from pathlib import Path
from pydantic import BaseModel
from typing import Dict, List

from ray.serve import Application
from anyscale.sdk.anyscale_client.sdk import AnyscaleSDK
from anyscale.sdk.anyscale_client.models import (
    ApplyServiceModel,
    ServiceEventCurrentState,
    ServiceModel,
)

from chaos_test.constants import (
    ACTIVE_SERVICE_STATUSES,
    NODE_KILLER_KEY,
    KillOptions,
)
from chaos_test.pinger_deployments import (
    Router,
    Pinger,
    Reaper,
    ReceiverHelmsman,
)

from utilities.anyscale_sdk_utils import create_anyscale_sdk, get_service_model
from utilities.deployment_utils import get_receiver_serve_config


logger = logging.getLogger("ray.serve")


class PingerArgs(BaseModel):
    """The arguments needed to build a Pinger application.
    
    receiver_service_name: The name of the Receiver service.
    receiver_build_id: The build ID of the Receiver service.
    receiver_compute_config_id: The compute config ID of the Receiver service.
    receiver_gcs_external_storage_config: The GCS FT config for the Receiver.
    pinger_service_name: The name of the Pinger service.
    project_id: The ID of the project that Pinger and Receiver should run in.
    receiver_qps: The QPS to send to the Receiver application.
    pinger_qps: The QPS to send to the Pinger application.
    kill_interval_s: The seconds between each request to kill a Receiver
        node.
    upgrade_interval_s: The seconds between each request to upgrade the
        Receiver service.
    upgrade_types: The types of upgrades to perform.
    """

    receiver_service_name: str
    receiver_build_id: str
    receiver_compute_config_id: str
    receiver_gcs_external_storage_config: Dict
    pinger_service_name: str
    project_id: str
    receiver_qps: int
    pinger_qps: int
    kill_interval_s: int
    upgrade_interval_s: int
    upgrade_types: List[str] = ["IN_PLACE", "ROLLOUT"]


def wait_for_service_in_state(
    sdk: AnyscaleSDK,
    service_id: str,
    expected_state: ServiceEventCurrentState,
    timeout_s: int = 1200,
):
    wait_start, last_log_time = time.time(), time.time()
    while time.time() - wait_start < timeout_s:
        curr_state = sdk.get_service(service_id).result.current_state

        if curr_state == expected_state:
            break
        elif time.time() - last_log_time >= 10:
            # Log the current state every 10s.
            logger.info(
                "Waiting for Receiver to start running. Currently in state: "
                f"{curr_state}."
            )
            last_log_time = time.time()
        
        time.sleep(0.5)
    else:
        raise TimeoutError(
            f"Service did not enter {expected_state} after {timeout_s} seconds."
        )
    
    logger.info(f"The Receiver service {service_id} has started running.")


def start_receiver_service(
    sdk: AnyscaleSDK,
    receiver_service_name: str,
    receiver_build_id: str,
    receiver_compute_config_id: str,
    receiver_gcs_external_storage_config: Dict,
    project_id: str,
):
    """Starts the Receiver service.

    Terminates the existing Receiver service if it's currently running.

    Args:
        receiver_service_name: The name of the Receiver service.
        receiver_build_id: The build ID of the Receiver service.
        receiver_compute_config_id: The compute config ID of the Receiver.
        receiver_gcs_external_storage_config: The GCS FT config for the Receiver.
        project_id: The project ID that the Receiver and Pinger services
            should run in.
    """
    
    service_models: List[ServiceModel] = sdk.list_services(
        name=receiver_service_name,
        project_id=project_id,
        state_filter=ACTIVE_SERVICE_STATUSES
    ).results

    if len(service_models) > 1:
        raise RuntimeError(
            f"Found {len(service_models)} services with name "
            f"{receiver_service_name} in project {project_id}. "
            "Expected only 0 or 1."
        )
    elif len(service_models) == 1:
        logger.info("A Receiver service already exists. Terminating it.")
        receiver_service_model: ServiceModel = service_models[0]
        receiver_service_id = receiver_service_model.id
        sdk.terminate_service(receiver_service_id)
        wait_for_service_in_state(
            sdk=sdk,
            service_id=receiver_service_id,
            expected_state=ServiceEventCurrentState.TERMINATED,
            timeout_s=1200,
        )

    logger.info("Starting the Receiver service.")
    service_config = ApplyServiceModel(
        name=receiver_service_name,
        project_id=project_id,
        build_id=receiver_build_id,
        compute_config_id=receiver_compute_config_id,
        ray_serve_config=get_receiver_serve_config(str(Path(__file__).parent)),
        ray_gcs_external_storage_config=receiver_gcs_external_storage_config,
    )
    receiver_service_model: ServiceModel = sdk.rollout_service(service_config)
    receiver_service_id = receiver_service_model.id
    
    # Wait for the Receiver service to start running.
    wait_for_service_in_state(
        sdk=sdk,
        service_id=receiver_service_id,
        expected_state=ServiceEventCurrentState.RUNNING,
        timeout_s=1200,
    )


def app_builder(args: PingerArgs) -> Application:
    """Builds the Pinger application.

    Starts the Receiver application if it hasn't started already.
    """

    sdk = create_anyscale_sdk()

    start_receiver_service(
        sdk=sdk,
        receiver_service_name=args.receiver_service_name,
        receiver_build_id=args.receiver_build_id,
        receiver_compute_config_id=args.receiver_compute_config_id,
        receiver_gcs_external_storage_config=args.receiver_gcs_external_storage_config,
        project_id=args.project_id,
    )

    receiver_service_model = get_service_model(
        sdk, args.receiver_service_name, args.project_id
    )
    receiver_url = receiver_service_model.base_url
    receiver_bearer_token = receiver_service_model.auth_token

    pinger_service_model = get_service_model(
        sdk, args.pinger_service_name, args.project_id
    )
    pinger_url = pinger_service_model.base_url
    pinger_bearer_token = pinger_service_model.auth_token

    receiver_pinger_config = {
        "url": receiver_url,
        "bearer_token": receiver_bearer_token,
        "max_qps": args.receiver_qps,
    }

    self_pinger_config = {
        "url": pinger_url,
        "bearer_token": pinger_bearer_token,
        "max_qps": args.pinger_qps,
    }

    reaper_config = {
        "receiver_url": receiver_url,
        "receiver_bearer_token": receiver_bearer_token,
        "kill_interval_s": args.kill_interval_s,
    }

    helmsman_config = {
        "project_id": args.project_id,
        "receiver_service_name": args.receiver_service_name,
        "receiver_build_id": args.receiver_build_id,
        "receiver_compute_config_id": args.receiver_compute_config_id,
        "receiver_gcs_external_storage_config": args.receiver_gcs_external_storage_config,
        "receiver_url": receiver_url,
        "receiver_bearer_token": receiver_bearer_token,
        "upgrade_interval_s": args.upgrade_interval_s,
        "upgrade_types": args.upgrade_types,
    }

    app = Router.bind(
        [
            Pinger.options(
                name="receiver_Pinger",
                user_config=receiver_pinger_config,
                ray_actor_options={"num_cpus": 0},
            ).bind(
                target_tag="Receiver", payload={NODE_KILLER_KEY: KillOptions.SPARE}
            ),
            Pinger.options(
                name="self_Pinger",
                user_config=self_pinger_config,
                ray_actor_options={"num_cpus": 0},
            ).bind(target_tag="Pinger", payload=None),
        ],
        Reaper.options(
            user_config=reaper_config, ray_actor_options={"num_cpus": 0}
        ).bind(),
        ReceiverHelmsman.options(
            user_config=helmsman_config, ray_actor_options={"num_cpus": 0}
        ).bind(),
    )

    return app
