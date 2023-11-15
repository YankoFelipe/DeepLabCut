"""Post-process predictions made by models"""
from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import numpy as np

from deeplabcut.pose_estimation_pytorch.data.preprocessor import Context


class Postprocessor(ABC):
    """A post-processor can be called on the output of a model
    TODO: Documentation
    """

    @abstractmethod
    def __call__(self, predictions: Any, context: Context) -> Any:
        """
        Post-processes the outputs of a model into a single prediction.

        Args:
            predictions: the predictions made by the model on a single image
            context: the context returned by the pre-processor with the image

        Returns:
            a single post-processed prediction
        """
        pass


def build_bottom_up_postprocessor(
    max_individuals: int,
    num_bodyparts: int,
    num_unique_bodyparts: int,
) -> ComposePostprocessor:
    """Creates a postprocessor for bottom-up pose estimation (or object detection)

    Args:
        max_individuals: the maximum number of individuals in a single image
        num_bodyparts: the number of bodyparts output by the model
        num_unique_bodyparts: the number of unique_bodyparts output by the model

    Returns:
        A default bottom-up Postprocessor
    """
    keys_to_concatenate = {"bodyparts": ("bodypart", "poses")}
    empty_shapes = {"bodyparts": (num_bodyparts, 3)}
    keys_to_rescale = ["bodyparts"]
    if num_unique_bodyparts > 0:
        keys_to_concatenate["unique_bodyparts"] = ("unique_bodypart", "poses")
        empty_shapes = {"bodyparts": (num_bodyparts, 3)}
        keys_to_rescale.append("unique_bodyparts")
    return ComposePostprocessor(
        components=[
            ConcatenateOutputs(
                keys_to_concatenate=keys_to_concatenate,
                empty_shapes=empty_shapes,
                create_empty_outputs=True,
            ),
            RescaleAndOffset(
                keys_to_rescale=keys_to_rescale,
                data=RescaleAndOffset.DataType.KEYPOINT,
            ),
            PadOutputs(
                max_individuals={
                    "bodyparts": max_individuals,
                    "unique_bodyparts": 0,  # no need to pad
                },
                pad_value=-1,
            ),
        ]
    )


def build_top_down_postprocessor(
    max_individuals: int,
    num_bodyparts: int,
    num_unique_bodyparts: int,
) -> Postprocessor:
    """Creates a postprocessor for top-down pose estimation

    Args:
        max_individuals: the maximum number of individuals in a single image
        num_bodyparts: the number of bodyparts output by the model
        num_unique_bodyparts: the number of unique_bodyparts output by the model

    Returns:
        A default top-down Postprocessor
    """
    keys_to_concatenate = {"bodyparts": ("bodypart", "poses")}
    empty_shapes = {"bodyparts": (num_bodyparts, 3)}
    keys_to_rescale = ["bodyparts"]
    if num_unique_bodyparts > 0:
        keys_to_concatenate["unique_bodyparts"] = ("unique_bodypart", "poses")
        empty_shapes["unique_bodyparts"] = (num_unique_bodyparts, 3)
        keys_to_rescale.append("unique_bodyparts")

    return ComposePostprocessor(
        components=[
            ConcatenateOutputs(
                keys_to_concatenate=keys_to_concatenate,
                empty_shapes=empty_shapes,
                create_empty_outputs=True,
            ),
            RescaleAndOffset(
                keys_to_rescale=keys_to_rescale,
                data=RescaleAndOffset.DataType.KEYPOINT_TD,
            ),
            AddContextToOutput(keys=["bboxes", "bbox_scores"]),
            PadOutputs(
                max_individuals={
                    "bodyparts": max_individuals,
                    "bboxes": max_individuals,
                    "bbox_scores": max_individuals,
                    "unique_bodyparts": 0,  # no need to pad
                },
                pad_value=-1,
            ),
        ]
    )


def build_detector_postprocessor() -> Postprocessor:
    """Creates a postprocessor for top-down pose estimation

    Returns:
        A default top-down Postprocessor
    """
    return ComposePostprocessor(
        components=[
            ConcatenateOutputs(
                keys_to_concatenate={
                    "bboxes": ("detection", "bboxes"),
                    "bbox_scores": ("detection", "scores"),
                }
            ),
            BboxToCoco(bounding_box_keys=["bboxes"]),
            RescaleAndOffset(
                keys_to_rescale=["bboxes"],
                data=RescaleAndOffset.DataType.BBOX_XYWH,
            ),
        ]
    )


class ComposePostprocessor(Postprocessor):
    """
    Class to preprocess an image and turn it into a batch of
    inputs before running inference
    """

    def __init__(self, components: list[Postprocessor]) -> None:
        self.components = components

    def __call__(self, predictions: Any, context: Context) -> tuple[Any, Context]:
        for postprocessor in self.components:
            predictions, context = postprocessor(predictions, context)
        return predictions, context


class ConcatenateOutputs(Postprocessor):
    """Checks that there is a single prediction for the image and returns it"""

    def __init__(
        self,
        keys_to_concatenate: dict[str, tuple[str, str]],
        empty_shapes: dict[str, tuple[int, ...]] | None = None,
        create_empty_outputs: bool = False,
    ):
        self.keys_to_concatenate = keys_to_concatenate
        self.empty_shapes = empty_shapes
        self.create_empty_outputs = create_empty_outputs

        if self.create_empty_outputs:
            if not all([k in self.empty_shapes for k in self.keys_to_concatenate]):
                raise ValueError(
                    "You must provide the expected shape for all keys to concatenate"
                    f"when create_empty_outputs is true, found {self.empty_shapes}"
                )

    def __call__(
        self, predictions: Any, context: Context
    ) -> tuple[dict[str, np.ndarray], Context]:
        if len(predictions) == 0:
            outputs = {
                name: np.zeros((0, *self.empty_shapes[name]))
                for name in self.keys_to_concatenate.keys()
            }
            return outputs, context

        outputs = {}
        for output_name, head_key in self.keys_to_concatenate.items():
            head_name, val_name = head_key
            outputs[output_name] = np.concatenate(
                [p[head_name][val_name] for p in predictions]
            )

        return outputs, context


class PadOutputs(Postprocessor):
    """Pads the outputs to have the maximum number of individuals"""

    def __init__(
        self,
        max_individuals: dict[str, int],
        pad_value: int,
    ):
        self.max_individuals = max_individuals
        self.pad_value = pad_value

    def __call__(
        self, predictions: dict[str, np.ndarray], context: Context
    ) -> tuple[dict[str, np.ndarray], Context]:
        for name in predictions:
            output = predictions[name]
            if len(output) < self.max_individuals[name]:
                pad_size = self.max_individuals[name] - len(output)
                tail_shape = output.shape[1:]
                padding = -np.ones((pad_size, *tail_shape))
                predictions[name] = np.concatenate([output, padding])

        return predictions, context


class RescaleAndOffset(Postprocessor):
    """Rescales and offsets predictions back to their position in the original image

    This can be done in 3 ways:
        BBOX_XYWH: the data has shape (num_individuals, 4), in xywh format, and there
            is a single scale and offset for all bounding boxes (e.g., because the image
            was resized before being passed to a detector)
        KEYPOINT: the data has shape (num_individuals, num_keypoints, 2/3), and there
            is a single scale and offset for all individuals (e.g., because the image
            was resized before being passed to a BU pose model)
        KEYPOINT_TD: the data has shape (num_individuals, num_keypoints, 2/3), and there
            are num_individuals scales and offsets (one for each individual, as TD crops
            one image per individual)

    If no scale and no offsets are given, then this postprocessor simply forwards the
    predictions and context.
    """

    class Mode(Enum):
        BBOX_XYWH = "bbox_xywh"
        KEYPOINT = "keypoint"
        KEYPOINT_TD = "keypoint_td"

    def __init__(
        self,
        keys_to_rescale: list[str],
        mode: RescaleAndOffset.Mode,
    ) -> None:
        super().__init__()
        self.keys_to_rescale = keys_to_rescale
        self.mode = mode

    def __call__(
        self, predictions: dict[str, np.ndarray], context: Context
    ) -> tuple[dict[str, np.ndarray], Context]:
        if "scales" not in context and "offsets" not in context:
            # no rescaling needed
            return predictions, context

        updated_predictions = {}
        scales, offsets = np.array(context["scales"]), np.array(context["offsets"])
        for name, outputs in predictions.items():
            if name in self.keys_to_rescale:
                if self.mode == self.Mode.BBOX_XYWH:
                    rescaled = outputs.copy()
                    rescaled[:, 0] = outputs[:, 0] * scales[0] + offsets[0]
                    rescaled[:, 1] = outputs[:, 1] * scales[1] + offsets[1]
                    rescaled[:, 2] = outputs[:, 2] * scales[0]
                    rescaled[:, 3] = outputs[:, 3] * scales[1]
                elif self.mode == self.Mode.KEYPOINT:
                    rescaled = outputs.copy()
                    rescaled[:, :, 0] = outputs[:, :, 0] * scales[0] + offsets[0]
                    rescaled[:, :, 1] = outputs[:, :, 1] * scales[1] + offsets[1]
                else:  # Mode.KEYPOINT_TD
                    if not len(outputs) == len(scales) == len(offsets):
                        raise ValueError(
                            "There must be as many 'scales' and 'offsets' as outputs, found "
                            f"{len(outputs)}, {len(scales)}, {len(offsets)}"
                        )

                    if len(outputs) == 0:
                        rescaled = outputs
                    else:
                        rescaled_individuals = []
                        for output, scale, offset in zip(outputs, scales, offsets):
                            output_rescaled = output.copy()
                            output_rescaled[:, 0] = output[:, 0] * scale[0] + offset[0]
                            output_rescaled[:, 1] = output[:, 1] * scale[1] + offset[1]
                            rescaled_individuals.append(output_rescaled)
                        rescaled = np.stack(rescaled_individuals)

                updated_predictions[name] = rescaled
            else:
                updated_predictions[name] = outputs.copy()

        return updated_predictions, context


class BboxToCoco(Postprocessor):
    """Transforms bounding boxes from xyxy to COCO format (xywh)"""

    def __init__(self, bounding_box_keys: list[str]) -> None:
        super().__init__()
        self.bounding_box_keys = bounding_box_keys

    def __call__(
        self, predictions: dict[str, np.ndarray], context: Context
    ) -> tuple[dict[str, np.ndarray], Context]:
        for bbox_key in self.bounding_box_keys:
            predictions[bbox_key][:, 2] -= predictions[bbox_key][:, 0]
            predictions[bbox_key][:, 3] -= predictions[bbox_key][:, 1]

        return predictions, context


class AddContextToOutput(Postprocessor):
    """
    Adds items from the context to the output, such as the bounding boxes contained
    during top-down inference.
    """

    def __init__(self, keys: list[str]) -> None:
        super().__init__()
        self.keys = keys

    def __call__(
        self,
        predictions: dict[str, np.ndarray],
        context: Context,
    ) -> tuple[dict[str, np.ndarray], Context]:
        for k in self.keys:
            if k in context:
                predictions[k] = context[k].copy()
        return predictions, context
