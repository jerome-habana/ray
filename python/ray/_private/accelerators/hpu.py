import os
import logging
from typing import Optional, List, Tuple
from functools import lru_cache
from importlib.util import find_spec

from ray._private.accelerators.accelerator import AcceleratorManager

logger = logging.getLogger(__name__)

HABANA_VISIBLE_DEVICES_ENV_VAR = "HABANA_VISIBLE_MODULES"
NOSET_HABANA_VISIBLE_MODULES_ENV_VAR = "RAY_EXPERIMENTAL_NOSET_HABANA_VISIBLE_MODULES"

@lru_cache()
def is_package_present(package_name: str) -> bool:
    try:
        return find_spec(package_name) is not None
    except ModuleNotFoundError:
        return False


HPU_PACKAGE_AVAILABLE = is_package_present("habana_frameworks")


class HPUAcceleratorManager(AcceleratorManager):
    """Intel Habana(HPU) accelerators."""

    @staticmethod
    def get_resource_name() -> str:
        return "HPU"

    @staticmethod
    def get_visible_accelerator_ids_env_var() -> str:
        return HABANA_VISIBLE_DEVICES_ENV_VAR

    @staticmethod
    def get_current_process_visible_accelerator_ids() -> Optional[List[str]]:
        hpu_visible_devices = os.environ.get(
            HPUAcceleratorManager.get_visible_accelerator_ids_env_var(), None
        )

        if hpu_visible_devices is None:
            return None

        if hpu_visible_devices == "":
            return []

        return list(hpu_visible_devices.split(","))

    @staticmethod
    def get_current_node_num_accelerators() -> int:
        """Attempt to detect the number of HPUs on this machine.
        Returns:
            The number of HPUs if any were detected, otherwise 0.
        """
        if HPU_PACKAGE_AVAILABLE:
            import habana_frameworks.torch.hpu as torch_hpu
            if torch_hpu.is_available():
                return torch_hpu.device_count()
            else:
                logging.info("HPU devices not available")
                return 0
        else:
            return 0

    @staticmethod
    def get_current_node_accelerator_type() -> Optional[str]:
        """Attempt to HPU family type.
        Returns:
            The device name if detected else an empty string.
        """
        if HPU_PACKAGE_AVAILABLE:
            import habana_frameworks.torch.hpu as torch_hpu
            if torch_hpu.is_available():
                #Fix me, if device is not initialized, we open a device to get the name
                return "GAUDI" #torch_hpu.get_device_name()
            else:
                logging.info("HPU type cannot be detected")
                return ""
        else:
            return ""

    @staticmethod
    def validate_resource_request_quantity(
        quantity: float,
    ) -> Tuple[bool, Optional[str]]:
        return (True, None)

    @staticmethod
    def set_current_process_visible_accelerator_ids(
        visible_hpu_devices: List[str],
    ) -> None:
        if os.environ.get(NOSET_HABANA_VISIBLE_MODULES_ENV_VAR):
            return

        os.environ[
            HPUAcceleratorManager.get_visible_accelerator_ids_env_var()
        ] = ",".join([str(i) for i in visible_hpu_devices])

    @staticmethod
    def get_ec2_instance_num_accelerators(
        instance_type: str, instances: dict
    ) -> Optional[int]:
        if instance_type not in instances:
            return None

        #TBD
        return None

    @staticmethod
    def get_ec2_instance_accelerator_type(
        instance_type: str, instances: dict
    ) -> Optional[str]:
        if instance_type not in instances:
            return None

        #TBD
        return None
