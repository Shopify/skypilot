"""The strategy to handle launching/recovery/termination of spot clusters."""
import os
import time
import typing
from typing import Callable, Optional

import sky
from sky import exceptions
from sky import global_user_state
from sky import sky_logging
from sky.backends import backend_utils
from sky.skylet import job_lib
from sky.spot import spot_state
from sky.spot import spot_utils
from sky.usage import usage_lib
from sky.utils import common_utils
from sky.utils import ux_utils

if typing.TYPE_CHECKING:
    from sky import backends
    from sky import task as task_lib

logger = sky_logging.init_logger(__name__)

SPOT_STRATEGIES = dict()
SPOT_DEFAULT_STRATEGY = None


class StrategyExecutor:
    """Handle each launching, recovery and termination of the spot clusters."""

    RETRY_INIT_GAP_SECONDS = 60

    def __init__(self, cluster_name: str, backend: 'backends.Backend',
                 task: 'task_lib.Task', retry_until_up: bool,
                 signal_handler: Callable, job_id: int) -> None:
        """Initialize the strategy executor.

        Args:
            cluster_name: The name of the cluster.
            backend: The backend to use. Only CloudVMRayBackend is supported.
            task: The task to execute.
            retry_until_up: Whether to retry until the cluster is up.
            signal_handler: The signal handler that will raise an exception if a
                SkyPilot signal is received.
            job_id: int, id of the job.
        """
        self.dag = sky.Dag()
        self.dag.add(task)
        self.cluster_name = cluster_name
        self.backend = backend
        self.retry_until_up = retry_until_up
        self.signal_handler = signal_handler
        self._job_id = job_id

        self._blocked_regions = set()
        self._blocked_zones = set()

    def __init_subclass__(cls, name: str, default: bool = False):
        SPOT_STRATEGIES[name] = cls
        if default:
            global SPOT_DEFAULT_STRATEGY
            assert SPOT_DEFAULT_STRATEGY is None, (
                'Only one strategy can be default.')
            SPOT_DEFAULT_STRATEGY = name

    @classmethod
    def make(cls, cluster_name: str, backend: 'backends.Backend',
             task: 'task_lib.Task', retry_until_up: bool,
             signal_handler: Callable, job_id: int) -> 'StrategyExecutor':
        """Create a strategy from a task."""
        resources = task.resources
        assert len(resources) == 1, 'Only one resource is supported.'
        resources: 'sky.Resources' = list(resources)[0]

        spot_recovery = resources.spot_recovery
        assert spot_recovery is not None, (
            'spot_recovery is required to use spot strategy.')
        # Remove the spot_recovery field from the resources, as the strategy
        # will be handled by the strategy class.
        task.set_resources({resources.copy(spot_recovery=None)})
        return SPOT_STRATEGIES[spot_recovery](cluster_name, backend, task,
                                              retry_until_up, signal_handler,
                                              job_id)

    def launch(self) -> Optional[float]:
        """Launch the spot cluster for the first time.

        It can fail if resource is not available. Need to check the cluster
        status, after calling.

        Returns: The job's start timestamp, or None if failed to start.
        """
        if self.retry_until_up:
            return self._launch(max_retry=None)
        return self._launch()

    def recover(self) -> float:
        """Relaunch the spot cluster after failure and wait until job starts.

        When recover() is called the cluster should be in STOPPED status (i.e.
        partially down).

        Returns: The timestamp job started.
        """
        raise NotImplementedError

    def terminate_cluster(self, max_retry: int = 3) -> None:
        """Terminate the spot cluster."""
        handle = global_user_state.get_handle_from_cluster_name(
            self.cluster_name)
        if handle is None:
            return
        retry_cnt = 0
        while True:
            success = self.backend.teardown(handle, terminate=True)
            if success:
                return

            retry_cnt += 1
            if retry_cnt >= max_retry:
                raise RuntimeError('Failed to terminate the spot cluster '
                                   f'{self.cluster_name}.')
            logger.error('Failed to terminate the spot cluster '
                         f'{self.cluster_name}. Retrying.')

    def _launch(self, max_retry=3, raise_on_failure=True) -> Optional[float]:
        """Implementation of launch().

        Args:
            max_retry: The maximum number of retries. If None, retry forever.
            raise_on_failure: Whether to raise an exception if the launch fails.
        """
        # TODO(zhwu): handle the failure during `preparing sky runtime`.
        retry_cnt = 0
        backoff = common_utils.Backoff(self.RETRY_INIT_GAP_SECONDS)
        while True:
            retry_cnt += 1
            # Check the signal every time to be more responsive to user
            # signals, such as Cancel.
            self.signal_handler()
            try:
                usage_lib.messages.usage.set_internal()
                sky.launch(
                    self.dag,
                    cluster_name=self.cluster_name,
                    detach_run=True,
                    blocked_regions=self._blocked_regions,
                    blocked_zones=self._blocked_zones,
                )
                logger.info('Spot cluster launched.')
            except Exception as e:  # pylint: disable=broad-except
                # If the launch fails, it will be recovered by the following
                # code.
                logger.info(
                    f'Failed to launch the spot cluster with error: {e}')

                # NOTE(hack): leaking knowledge of the specific strategy
                # TODO: Failed to provision all possible launchable resources. Relax the task's resource requirements: 1x {GCP(n1-highmem-96[Spot])}
                # clear zones
                if ('Failed to provision all possible launchable resources'
                        in str(e)):
                    self._blocked_regions.clear()
                    self._blocked_zones.clear()
                    logger.info('Cleared blocked zones.')

                if max_retry is not None and retry_cnt >= max_retry:
                    # Retry forever if max_retry is None.
                    if raise_on_failure:
                        with ux_utils.print_exception_no_traceback():
                            raise exceptions.ResourcesUnavailableError(
                                'Failed to launch the spot cluster after '
                                f'{max_retry} retries.') from e
                    else:
                        return None
                gap_seconds = backoff.current_backoff()
                logger.info(
                    f'Retrying to launch the spot cluster in {gap_seconds:.1f} '
                    'seconds.')
                time.sleep(gap_seconds)
                continue

            record = global_user_state.get_cluster_from_name(self.cluster_name)
            if record is not None:
                # Update whenever after sky.launch() is run.
                logger.info(
                    f'Calling update_num_tried_locations(), {record["num_tried_locations"]}'
                )
                spot_state.update_num_tried_locations(
                    self._job_id, record['num_tried_locations'])

            status = None
            retry_launch = False
            job_checking_retry_cnt = 0
            launch_time = None
            while (status is None or status in [
                    job_lib.JobStatus.INIT,
                    job_lib.JobStatus.PENDING,
            ]) and job_checking_retry_cnt < spot_utils.MAX_JOB_CHECKING_RETRY:
                job_checking_retry_cnt += 1
                try:
                    cluster_status, _ = backend_utils.refresh_cluster_status_handle(
                        self.cluster_name, force_refresh=True)
                except Exception as e:
                    logger.error('refresh_cluster_status_handle failed')
                    logger.error(e)
                    retry_launch = True
                    break
                if cluster_status != global_user_state.ClusterStatus.UP:
                    # The cluster can be preempted before the job is launched.
                    # In this case, we will terminate the cluster and retry
                    # the launch.
                    retry_launch = True
                    break
                # Wait the job to be started
                time.sleep(spot_utils.JOB_STARTED_STATUS_CHECK_GAP_SECONDS)
                try:
                    status = spot_utils.get_job_status(self.backend,
                                                       self.cluster_name)
                    if status is not None:
                        launch_time = spot_utils.get_job_timestamp(
                            self.backend, self.cluster_name, get_end_time=False)
                        return launch_time
                except Exception as e:
                    logger.error('get_job_status or get_job_timestamp failed')
                    logger.error(e)
                    retry_launch = True
                    break
            if retry_launch:
                # self.terminate_cluster()
                gap_seconds = backoff.current_backoff()
                logger.info(
                    'Failed to check the job status, probably due to the '
                    'preemption or job submission process failure. Retrying '
                    f'to launch the cluster in {gap_seconds:.1f} seconds.')
                time.sleep(gap_seconds)
                continue
            return launch_time


class FailoverStrategyExecutor(StrategyExecutor, name='FAILOVER', default=True):
    """Failover strategy: wait in same region and failover after timout."""

    _MAX_RETRY_CNT = 240  # Retry for 4 hours.

    def recover(self) -> float:
        # 1. Cancel the jobs and launch the cluster with the STOPPED status,
        #    so that it will try on the current region first until timeout.
        # 2. Tear down the cluster, if the step 1 failed to launch the cluster.
        # 3. Launch the cluster with no cloud/region constraint or respect the
        #    original user specification.

        # Step 1
        handle = global_user_state.get_handle_from_cluster_name(
            self.cluster_name)
        try:
            self.backend.cancel_jobs(handle, jobs=None)
        except exceptions.CommandError:
            # Ignore the failure as the cluster can be totally stopped, and the
            # job canceling can get connection error.
            logger.info('Ignoring the job cancellation failure; the spot '
                        'cluster is likely completely stopped. Recovering.')

        launched_zone = handle.launched_resources.zone
        logger.info(f'Cluster is preempted from zone {launched_zone}.')
        self._blocked_zones.add(launched_zone)

        # Retry the entire block until the cluster is up, so that the ratio of
        # the time spent in the current region and the time spent in the other
        # region is consistent during the retry.
        while True:
            # Add region constraint to the task, to retry on the same region
            # first.
            task = self.dag.tasks[0]
            resources = list(task.resources)[0]
            original_resources = resources
            # logger.info(f'orig: {original_resources}; best_resources: {task.best_resources}')

            launched_cloud = handle.launched_resources.cloud
            launched_region = handle.launched_resources.region

            # new_resources = resources.copy(cloud=launched_cloud,
            #                                region=launched_region)
            # task.set_resources({new_resources})
            # Not using self.launch to avoid the retry until up logic.
            # launched_time = self._launch(raise_on_failure=False)
            # Restore the original dag, i.e. reset the region constraint.
            # task.set_resources({original_resources})
            # if launched_time is not None:
            #     return launched_time

            # Step 2
            logger.debug('Terminating unhealthy spot cluster.')
            self.terminate_cluster()

            # Step 3
            logger.debug('Relaunch the cluster without constraining to prior '
                         'cloud/region.')
            # Not using self.launch to avoid the retry until up logic.
            # launched_time = self._launch(max_retry=self._MAX_RETRY_CNT,
            #                              raise_on_failure=False)
            launched_time = self._launch(max_retry=3, raise_on_failure=False)

            if launched_time is None:
                # Under current blocked constraints self._launch() failed 3
                # times (it can be sky.launch() w/ or w/o failover; it can be
                # getting job status or timestamp; regardless, it indicates
                # preemption). So, block this zone.
                handle = global_user_state.get_handle_from_cluster_name(
                    self.cluster_name)
                if handle is not None:
                    self._blocked_zones.add(handle.launched_resources.zone)

                # self._blocked_zones.clear()
                # logger.info('Cleared blocked zones.')

                # Failed to launch the cluster.
                if self.retry_until_up:
                    gap_seconds = self.RETRY_INIT_GAP_SECONDS
                    logger.info('Retrying to recover the spot cluster in '
                                f'{gap_seconds:.1f} seconds.')
                    time.sleep(gap_seconds)
                    continue
                with ux_utils.print_exception_no_traceback():
                    raise exceptions.ResourcesUnavailableError(
                        f'Failed to recover the spot cluster after retrying '
                        f'{self._MAX_RETRY_CNT} times.')

            return launched_time


class CrossRegion(StrategyExecutor, name='CROSS_REGION'):

    _MAX_RETRY_CNT = 240  # Retry for 4 hours.

    def recover(self) -> float:
        # 1. Cancel the jobs and launch the cluster with the STOPPED status,
        #    so that it will try on the current region first until timeout.
        # 2. Tear down the cluster, if the step 1 failed to launch the cluster.
        # 3. Launch the cluster with no cloud/region constraint or respect the
        #    original user specification.

        # Step 1
        handle = global_user_state.get_handle_from_cluster_name(
            self.cluster_name)
        try:
            self.backend.cancel_jobs(handle, jobs=None)
        except exceptions.CommandError:
            # Ignore the failure as the cluster can be totally stopped, and the
            # job canceling can get connection error.
            logger.info('Ignoring the job cancellation failure; the spot '
                        'cluster is likely completely stopped. Recovering.')

        launched_zone = handle.launched_resources.zone
        logger.info(f'Cluster is preempted from zone {launched_zone}.')
        self._blocked_zones.add(launched_zone)

        if os.environ.get('_CROSS_REGION') is not None:
            logger.info(f'CrossRegion strategy, blocking {handle.launched_resources.region}')
            # Block the current region, so that we "jump" to the next region.
            self._blocked_regions.add(handle.launched_resources.region)

        # HACK: gcp.py uses _GCP_RANDOMIZE_ZONES to pick a random zone.

        # Retry the entire block until the cluster is up, so that the ratio of
        # the time spent in the current region and the time spent in the other
        # region is consistent during the retry.
        while True:
            # Step 2
            logger.debug('Terminating unhealthy spot cluster.')
            self.terminate_cluster()

            # Step 3
            logger.debug('Relaunch the cluster without constraining to prior '
                         'cloud/region.')
            # Not using self.launch to avoid the retry until up logic.
            # launched_time = self._launch(max_retry=self._MAX_RETRY_CNT,
            #                              raise_on_failure=False)
            launched_time = self._launch(max_retry=3, raise_on_failure=False)

            if launched_time is None:
                # Under current blocked constraints self._launch() failed 3
                # times (it can be sky.launch() w/ or w/o failover; it can be
                # getting job status or timestamp; regardless, it indicates
                # preemption). So, block this zone.
                handle = global_user_state.get_handle_from_cluster_name(
                    self.cluster_name)
                if handle is not None:
                    self._blocked_regions.add(handle.launched_resources.region)
                    self._blocked_zones.add(handle.launched_resources.zone)

                # Failed to launch the cluster.
                if self.retry_until_up:
                    gap_seconds = self.RETRY_INIT_GAP_SECONDS
                    logger.info('Retrying to recover the spot cluster in '
                                f'{gap_seconds:.1f} seconds.')
                    time.sleep(gap_seconds)
                    continue
                with ux_utils.print_exception_no_traceback():
                    raise exceptions.ResourcesUnavailableError(
                        f'Failed to recover the spot cluster after retrying '
                        f'{self._MAX_RETRY_CNT} times.')

            return launched_time
