from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Dict, Iterable, List, Tuple

import structlog
import typer
from typing_extensions import Annotated

# Get relative imports to work when the package is not installed on the PYTHONPATH.
if __name__ == "__main__" and __package__ is None:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from buildscripts.client.jiraclient import JiraAuth, JiraClient
from buildscripts.monitor_build_status.bfs_report import BFsReport
from buildscripts.monitor_build_status.evergreen_service import (
    EvergreenService,
    EvgProjectsInfo,
    TaskStatusCounts,
)
from buildscripts.monitor_build_status.jira_service import (
    BfTemperature,
    JiraCustomFieldNames,
    JiraService,
    TestType,
)
from buildscripts.resmokelib.utils.evergreen_conn import get_evergreen_api
from buildscripts.util.cmdutils import enable_logging

LOGGER = structlog.get_logger(__name__)

JIRA_SERVER = "https://jira.mongodb.org"
DEFAULT_REPO = "10gen/mongo"
DEFAULT_BRANCH = "master"
SLACK_CHANNEL = "#10gen-mongo-code-lockdown"
EVERGREEN_LOOKBACK_DAYS = 14

OVERALL_HOT_BF_COUNT_THRESHOLD = 30
OVERALL_COLD_BF_COUNT_THRESHOLD = 100
OVERALL_PERF_BF_COUNT_THRESHOLD = 30
PER_TEAM_HOT_BF_COUNT_THRESHOLD = 7
PER_TEAM_COLD_BF_COUNT_THRESHOLD = 20
PER_TEAM_PERF_BF_COUNT_THRESHOLD = 10


def iterable_to_jql(entries: Iterable[str]) -> str:
    return ", ".join(f'"{entry}"' for entry in entries)


JIRA_PROJECTS = {"Build Failures"}
END_STATUSES = {"Needs Triage", "Open", "In Progress", "Waiting for Bug Fix"}
PRIORITIES = {"Blocker - P1", "Critical - P2", "Major - P3", "Minor - P4"}
ACTIVE_BFS_QUERY = (
    f"project in ({iterable_to_jql(JIRA_PROJECTS)})"
    f" AND status in ({iterable_to_jql(END_STATUSES)})"
    f" AND priority in ({iterable_to_jql(PRIORITIES)})"
)


class MonitorBuildStatusOrchestrator:
    def __init__(
        self,
        jira_service: JiraService,
        evg_service: EvergreenService,
    ) -> None:
        self.jira_service = jira_service
        self.evg_service = evg_service

    def evaluate_build_redness(self, repo: str, branch: str, notify: bool) -> None:
        status_message = f"\n`[STATUS]` '{repo}' repo '{branch}' branch"
        threshold_percentages = []

        LOGGER.info("Getting Evergreen projects data")
        evg_projects_info = self.evg_service.get_evg_project_info(repo, branch)
        evg_project_names = evg_projects_info.branch_to_projects_map[branch]
        LOGGER.info("Got Evergreen projects data")

        bfs_report = self._make_bfs_report(evg_projects_info)
        bf_count_status_msg, bf_count_percentages = self._get_bf_counts_status(bfs_report)
        status_message = f"{status_message}\n{bf_count_status_msg}\n"
        threshold_percentages.extend(bf_count_percentages)

        # We are looking for Evergreen versions that started before the beginning of yesterday
        # to give them time to complete
        window_end = datetime.utcnow().replace(
            hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
        ) - timedelta(days=1)
        window_start = window_end - timedelta(days=EVERGREEN_LOOKBACK_DAYS)

        waterfall_report = self._make_waterfall_report(
            evg_project_names=evg_project_names, window_end=window_end
        )
        waterfall_failure_rate_status_msg = self._get_waterfall_redness_status(
            waterfall_report=waterfall_report, window_start=window_start, window_end=window_end
        )
        status_message = f"{status_message}\n{waterfall_failure_rate_status_msg}\n"

        if any(percentage > 100 for percentage in threshold_percentages):
            status_message = (
                f"{status_message}\n"
                f"`[ACTION]` RED: At least one metric exceeds 100% of its threshold."
                f" Lock the branch if it is not already.\n"
            )
        elif all(percentage < 50 for percentage in threshold_percentages):
            status_message = (
                f"{status_message}\n"
                f"`[ACTION]` GREEN: All metrics are within 50% of their thresholds."
                f" The branch should be unlocked if it is not already.\n"
            )
        else:
            status_message = (
                f"{status_message}\n"
                f"`[ACTION]` YELLOW: At least one metric exceeds 50% of its threshold,"
                f" but none exceed 100%. No action is required.\n"
            )

        for line in status_message.split("\n"):
            LOGGER.info(line)

        if notify:
            LOGGER.info("Notifying slack channel with results", slack_channel=SLACK_CHANNEL)
            self.evg_service.evg_api.send_slack_message(
                target=SLACK_CHANNEL,
                msg=status_message.strip(),
            )

    def _make_bfs_report(self, evg_projects_info: EvgProjectsInfo) -> BFsReport:
        query = (
            f'{ACTIVE_BFS_QUERY} AND "{JiraCustomFieldNames.EVERGREEN_PROJECT}" in'
            f" ({iterable_to_jql(evg_projects_info.active_project_names)})"
        )
        LOGGER.info("Getting active BFs from Jira", query=query)

        active_bfs = self.jira_service.fetch_bfs(query)
        LOGGER.info("Got active BFs", count=len(active_bfs))

        bfs_report = BFsReport.empty()
        for bf in active_bfs:
            bfs_report.add_bf_data(bf, evg_projects_info)

        return bfs_report

    @staticmethod
    def _get_bf_counts_status(bfs_report: BFsReport) -> Tuple[str, List[float]]:
        percentages = []
        status_message = "`[STATUS]` The current BF count"
        status_message = f"{status_message}\n" f"```\n" f"{bfs_report.as_str_table()}\n" f"```"

        def _make_status_msg(scope_: str, bf_type: str, bf_count: int, threshold: int) -> str:
            percentage = bf_count / threshold * 100
            percentages.append(percentage)
            return (
                f"{scope_} {bf_type} BFs: {bf_count} ({percentage:.2f}% of threshold {threshold})"
            )

        overall_hot_bf_count = bfs_report.get_bf_count(
            test_types=[TestType.CORRECTNESS],
            bf_temperatures=[BfTemperature.HOT],
        )
        overall_cold_bf_count = bfs_report.get_bf_count(
            test_types=[TestType.CORRECTNESS],
            bf_temperatures=[BfTemperature.COLD, BfTemperature.NONE],
        )
        overall_perf_bf_count = bfs_report.get_bf_count(
            test_types=[TestType.PERFORMANCE],
            bf_temperatures=[BfTemperature.HOT, BfTemperature.COLD, BfTemperature.NONE],
        )

        scope = "Overall"
        status_message = (
            f"{status_message}"
            f"\n{_make_status_msg(scope, 'Hot', overall_hot_bf_count, OVERALL_HOT_BF_COUNT_THRESHOLD)}"
            f"\n{_make_status_msg(scope, 'Cold', overall_cold_bf_count, OVERALL_COLD_BF_COUNT_THRESHOLD)}"
            f"\n{_make_status_msg(scope, 'Perf', overall_perf_bf_count, OVERALL_PERF_BF_COUNT_THRESHOLD)}"
        )

        max_per_team_hot_bf_count = 0
        max_per_team_cold_bf_count = 0
        max_per_team_perf_bf_count = 0

        for team in bfs_report.all_assigned_teams:
            per_team_hot_bf_count = bfs_report.get_bf_count(
                test_types=[TestType.CORRECTNESS],
                bf_temperatures=[BfTemperature.HOT],
                assigned_team=team,
            )
            if per_team_hot_bf_count > max_per_team_hot_bf_count:
                max_per_team_hot_bf_count = per_team_hot_bf_count

            per_team_cold_bf_count = bfs_report.get_bf_count(
                test_types=[TestType.CORRECTNESS],
                bf_temperatures=[BfTemperature.COLD, BfTemperature.NONE],
                assigned_team=team,
            )
            if per_team_cold_bf_count > max_per_team_cold_bf_count:
                max_per_team_cold_bf_count = per_team_cold_bf_count

            per_team_perf_bf_count = bfs_report.get_bf_count(
                test_types=[TestType.PERFORMANCE],
                bf_temperatures=[BfTemperature.HOT, BfTemperature.COLD, BfTemperature.NONE],
                assigned_team=team,
            )
            if per_team_perf_bf_count > max_per_team_perf_bf_count:
                max_per_team_perf_bf_count = per_team_perf_bf_count

        scope = "Max per Assigned Team"
        status_message = (
            f"{status_message}"
            f"\n{_make_status_msg(scope, 'Hot', max_per_team_hot_bf_count, PER_TEAM_HOT_BF_COUNT_THRESHOLD)}"
            f"\n{_make_status_msg(scope, 'Cold', max_per_team_cold_bf_count, PER_TEAM_COLD_BF_COUNT_THRESHOLD)}"
            f"\n{_make_status_msg(scope, 'Perf', max_per_team_perf_bf_count, PER_TEAM_PERF_BF_COUNT_THRESHOLD)}"
        )

        return status_message, percentages

    def _make_waterfall_report(
        self, evg_project_names: List[str], window_end: datetime
    ) -> Dict[str, List[TaskStatusCounts]]:
        task_status_counts = []
        for day in range(EVERGREEN_LOOKBACK_DAYS):
            day_window_end = window_end - timedelta(days=day)
            day_window_start = day_window_end - timedelta(days=1)
            LOGGER.info(
                "Getting Evergreen waterfall data",
                projects=evg_project_names,
                window_start=day_window_start.isoformat(),
                window_end=day_window_end.isoformat(),
            )
            waterfall_status = self.evg_service.get_waterfall_status(
                evg_project_names=evg_project_names,
                window_start=day_window_start,
                window_end=day_window_end,
            )
            task_status_counts.extend(
                self._accumulate_project_statuses(evg_project_names, waterfall_status)
            )

        waterfall_report = {evg_project_name: [] for evg_project_name in evg_project_names}
        for task_status_count in task_status_counts:
            waterfall_report[task_status_count.project].append(task_status_count)

        return waterfall_report

    @staticmethod
    def _accumulate_project_statuses(
        evg_project_names: List[str], build_statuses: List[TaskStatusCounts]
    ) -> List[TaskStatusCounts]:
        project_statuses = []

        for evg_project_name in evg_project_names:
            project_status = TaskStatusCounts(project=evg_project_name)
            for build_status in build_statuses:
                if build_status.project == evg_project_name:
                    project_status = project_status.add(build_status)
            project_statuses.append(project_status)

        return project_statuses

    @staticmethod
    def _get_waterfall_redness_status(
        waterfall_report: Dict[str, List[TaskStatusCounts]],
        window_start: datetime,
        window_end: datetime,
    ) -> str:
        date_format = "%Y-%m-%d"
        status_message = (
            f"`[STATUS]` Evergreen waterfall red and purple boxes median count per day"
            f" between {window_start.strftime(date_format)}"
            f" and {window_end.strftime(date_format)}"
        )

        for evg_project_name, daily_task_status_counts in waterfall_report.items():
            daily_per_project_red_box_counts = [
                task_status_counts.failed for task_status_counts in daily_task_status_counts
            ]
            LOGGER.info(
                "Daily per project red box counts",
                project=evg_project_name,
                daily_red_box_counts=daily_per_project_red_box_counts,
            )
            median_per_day_red_box_count = median(daily_per_project_red_box_counts)
            status_message = (
                f"{status_message}\n{evg_project_name}: {median_per_day_red_box_count:.0f}"
            )

        return status_message


def main(
    github_repo: Annotated[
        str, typer.Option(help="Github repository name that Evergreen projects track")
    ] = DEFAULT_REPO,
    branch: Annotated[
        str, typer.Option(help="Branch name that Evergreen projects track")
    ] = DEFAULT_BRANCH,
    notify: Annotated[
        bool, typer.Option(help="Whether to send slack notification with the results")
    ] = False,  # default to the more "quiet" setting
) -> None:
    """
    Analyze Jira BFs count and Evergreen redness data.

    For Jira API authentication please use `JIRA_AUTH_PAT` env variable.
    More about Jira Personal Access Tokens (PATs) here:

    - https://wiki.corp.mongodb.com/pages/viewpage.action?pageId=218995581

    For Evergreen API authentication please create `~/.evergreen.yml`.
    More about Evergreen auth here:

    - https://spruce.mongodb.com/preferences/cli

    Example:

    JIRA_AUTH_PAT=<auth-token> python buildscripts/monitor_build_status/cli.py --help
    """
    enable_logging(verbose=False)

    jira_client = JiraClient(JIRA_SERVER, JiraAuth())
    evg_api = get_evergreen_api()

    jira_service = JiraService(jira_client=jira_client)
    evg_service = EvergreenService(evg_api=evg_api)
    orchestrator = MonitorBuildStatusOrchestrator(
        jira_service=jira_service, evg_service=evg_service
    )

    orchestrator.evaluate_build_redness(github_repo, branch, notify)


if __name__ == "__main__":
    typer.run(main)
