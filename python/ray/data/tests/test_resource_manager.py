import math
import time
from unittest.mock import MagicMock, patch

import pytest

import ray
from ray.data._internal.execution.interfaces.execution_options import (
    ExecutionOptions,
    ExecutionResources,
)
from ray.data._internal.execution.operators.input_data_buffer import InputDataBuffer
from ray.data._internal.execution.operators.map_operator import MapOperator
from ray.data._internal.execution.resource_manager import ResourceManager
from ray.data._internal.execution.streaming_executor_state import (
    build_streaming_topology,
)
from ray.data._internal.execution.util import make_ref_bundles
from ray.data.tests.test_streaming_executor import make_map_transformer


class TestResourceManager:
    """Unit tests for ResourceManager."""

    def test_global_limits(self):
        cluster_resources = {"CPU": 10, "GPU": 5, "object_store_memory": 1000}
        default_object_store_memory_limit = math.ceil(
            cluster_resources["object_store_memory"]
            * ResourceManager.DEFAULT_OBJECT_STORE_MEMORY_LIMIT_FRACTION
        )

        with patch("ray.cluster_resources", return_value=cluster_resources):
            # Test default resource limits.
            # When no resource limits are set, the resource limits should default to
            # the cluster resources for CPU/GPU, and
            # DEFAULT_OBJECT_STORE_MEMORY_LIMIT_FRACTION of cluster object store memory.
            options = ExecutionOptions()
            resource_manager = ResourceManager(MagicMock(), options)
            expected = ExecutionResources(
                cpu=cluster_resources["CPU"],
                gpu=cluster_resources["GPU"],
                object_store_memory=default_object_store_memory_limit,
            )
            assert resource_manager.get_global_limits() == expected

            # Test setting resource_limits
            options = ExecutionOptions()
            options.resource_limits = ExecutionResources(
                cpu=1, gpu=2, object_store_memory=100
            )
            resource_manager = ResourceManager(MagicMock(), options)
            expected = ExecutionResources(
                cpu=1,
                gpu=2,
                object_store_memory=100,
            )
            assert resource_manager.get_global_limits() == expected

            # Test setting exclude_resources
            # The actual limit should be the default limit minus the excluded resources.
            options = ExecutionOptions()
            options.exclude_resources = ExecutionResources(
                cpu=1, gpu=2, object_store_memory=100
            )
            resource_manager = ResourceManager(MagicMock(), options)
            expected = ExecutionResources(
                cpu=cluster_resources["CPU"] - 1,
                gpu=cluster_resources["GPU"] - 2,
                object_store_memory=default_object_store_memory_limit - 100,
            )
            assert resource_manager.get_global_limits() == expected

            # Test that we don't support setting both resource_limits
            # and exclude_resources.
            with pytest.raises(ValueError):
                options = ExecutionOptions()
                options.resource_limits = ExecutionResources(cpu=2)
                options.exclude_resources = ExecutionResources(cpu=1)
                options.validate()

    def test_global_limits_cache(self):
        resources = {"CPU": 4, "GPU": 1, "object_store_memory": 0}
        cache_interval_s = 0.1
        with patch.object(
            ResourceManager,
            "GLOBAL_LIMITS_UPDATE_INTERVAL_S",
            cache_interval_s,
        ):
            with patch(
                "ray.cluster_resources",
                return_value=resources,
            ) as ray_cluster_resources:
                resource_manager = ResourceManager(MagicMock(), ExecutionOptions())
                expected_resource = ExecutionResources(4, 1, 0)
                # The first call should call ray.cluster_resources().
                assert resource_manager.get_global_limits() == expected_resource
                assert ray_cluster_resources.call_count == 1
                # The second call should return the cached value.
                assert resource_manager.get_global_limits() == expected_resource
                assert ray_cluster_resources.call_count == 1
                time.sleep(cache_interval_s)
                # After the cache interval, the third call should call
                # ray.cluster_resources() again.
                assert resource_manager.get_global_limits() == expected_resource
                assert ray_cluster_resources.call_count == 2

    def test_calculating_usage(self):
        inputs = make_ref_bundles([[x] for x in range(20)])
        o1 = InputDataBuffer(inputs)
        o2 = MapOperator.create(
            make_map_transformer(lambda block: [b * -1 for b in block]), o1
        )
        o3 = MapOperator.create(
            make_map_transformer(lambda block: [b * 2 for b in block]), o2
        )
        o2.current_processor_usage = MagicMock(
            return_value=ExecutionResources(cpu=5, gpu=0)
        )
        o2.metrics.obj_store_mem_internal_outqueue = 500
        o3.current_processor_usage = MagicMock(
            return_value=ExecutionResources(cpu=10, gpu=0)
        )
        o3.metrics.obj_store_mem_internal_outqueue = 1000
        topo, _ = build_streaming_topology(o3, ExecutionOptions())
        inputs[0].size_bytes = MagicMock(return_value=200)
        topo[o2].add_output(inputs[0])

        resource_manager = ResourceManager(topo, ExecutionOptions())
        resource_manager.update_usages()
        assert resource_manager.get_global_usage() == ExecutionResources(15, 0, 1700)

        assert resource_manager.get_op_usage(o1) == ExecutionResources(0, 0, 0)
        assert resource_manager.get_op_usage(o2) == ExecutionResources(5, 0, 700)
        assert resource_manager.get_op_usage(o3) == ExecutionResources(10, 0, 1000)

        assert resource_manager.get_downstream_fraction(o1) == 1.0
        assert resource_manager.get_downstream_fraction(o2) == 1.0
        assert resource_manager.get_downstream_fraction(o3) == 0.5

        assert resource_manager.get_downstream_object_store_memory(o1) == 1700
        assert resource_manager.get_downstream_object_store_memory(o2) == 1700
        assert resource_manager.get_downstream_object_store_memory(o3) == 1000

    def test_object_store_usage(self, restore_data_context):
        input = make_ref_bundles([[x] for x in range(1)])[0]
        input.size_bytes = MagicMock(return_value=1)

        o1 = InputDataBuffer([input])
        o2 = MapOperator.create(MagicMock(), o1)
        o3 = MapOperator.create(MagicMock(), o2)

        topo, _ = build_streaming_topology(o3, ExecutionOptions())
        resource_manager = ResourceManager(topo, ExecutionOptions())
        ray.data.DataContext.get_current()._max_num_blocks_in_streaming_gen_buffer = 1
        ray.data.DataContext.get_current().target_max_block_size = 2

        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 0
        assert resource_manager.get_op_usage(o3).object_store_memory == 0

        # Objects in an operator's internal inqueue typically count toward the previous
        # operator's object store memory usage. However, data from an
        # `InputDataBuffer` aren't counted because they were created outside of this
        # execution.
        o2.metrics.on_input_queued(input)
        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 0
        assert resource_manager.get_op_usage(o3).object_store_memory == 0

        # Operators estimate pending task outputs using the target max block size.
        # In this case, the target max block size is 2 and there is at most 1 block
        # in the streaming generator buffer, so the estimated usage is 2.
        o2.metrics.on_input_dequeued(input)
        o2.metrics.on_task_submitted(0, input)
        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 2
        assert resource_manager.get_op_usage(o3).object_store_memory == 0

        # When the task finishes, we move the data from the streaming generator to the
        # operator's internal outqueue.
        o2.metrics.on_output_queued(input)
        o2.metrics.on_task_finished(0, None)
        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 1
        assert resource_manager.get_op_usage(o3).object_store_memory == 0

        o2.metrics.on_output_dequeued(input)
        topo[o2].outqueue.append(input)
        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 1
        assert resource_manager.get_op_usage(o3).object_store_memory == 0

        # Objects in the current operator's internal inqueue count towards the previous
        # operator's object store memory usage.
        o3.metrics.on_input_queued(topo[o2].outqueue.pop())
        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 1
        assert resource_manager.get_op_usage(o3).object_store_memory == 0

        # Task inputs count toward the previous operator's object store memory usage,
        # and task outputs count toward the current operator's object store memory
        # usage.
        o3.metrics.on_input_dequeued(input)
        o3.metrics.on_task_submitted(0, input)
        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 1
        assert resource_manager.get_op_usage(o3).object_store_memory == 2

        # Task inputs no longer count once the task is finished.
        o3.metrics.on_output_queued(input)
        o3.metrics.on_task_finished(0, None)
        resource_manager.update_usages()
        assert resource_manager.get_op_usage(o1).object_store_memory == 0
        assert resource_manager.get_op_usage(o2).object_store_memory == 0
        assert resource_manager.get_op_usage(o3).object_store_memory == 1


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main(["-v", __file__]))