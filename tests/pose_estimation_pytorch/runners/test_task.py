""" Tests the Task enum """
import pytest

from deeplabcut.pose_estimation_pytorch.runners.base import Task


@pytest.mark.parametrize(
    "task, task_strings",
    [
        (Task.BOTTOM_UP, ["bu", "BU", "bU", "Bu"]),
        (Task.TOP_DOWN, ["TD", "tD"]),
        (Task.DETECT, ["dt", "DT"]),
    ],
)
def test_build_task(task: Task, task_strings: list[str]):
    for s in task_strings:
        assert task == Task(s)
