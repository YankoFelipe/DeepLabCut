#
# DeepLabCut Toolbox (deeplabcut.org)
# © A. & M.W. Mathis Labs
# https://github.com/DeepLabCut/DeepLabCut
#
# Please see AUTHORS for contributors.
# https://github.com/DeepLabCut/DeepLabCut/blob/main/AUTHORS
#
# Licensed under GNU Lesser General Public License v3.0
#
from __future__ import annotations

import copy
import os
import pickle
import time
from pathlib import Path
from typing import Any

import albumentations as A
import numpy as np
import pandas as pd
from tqdm import tqdm

from deeplabcut.core.engine import Engine
from deeplabcut.pose_estimation_pytorch.apis.convert_detections_to_tracklets import (
    convert_detections2tracklets,
)
from deeplabcut.pose_estimation_pytorch.apis.utils import (
    get_detector_snapshots,
    get_model_snapshots,
    get_runners,
    list_videos_in_folder,
)
from deeplabcut.pose_estimation_pytorch.data import DLCLoader
from deeplabcut.pose_estimation_pytorch.post_processing.identity import assign_identity
from deeplabcut.pose_estimation_pytorch.runners import InferenceRunner, Task
from deeplabcut.refine_training_dataset.stitch import stitch_tracklets
from deeplabcut.utils import auxfun_multianimal, auxiliaryfunctions, VideoReader


class VideoIterator(VideoReader):
    """A class to iterate over videos, with possible added context"""

    def __init__(
        self, video_path: str, context: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(video_path)
        self._context = context
        self._index = 0

    def get_context(self) -> list[dict[str, Any]] | None:
        if self._context is None:
            return None

        return copy.deepcopy(self._context)

    def set_context(self, context: list[dict[str, Any]] | None) -> None:
        if context is None:
            self._context = None
            return

        self._context = copy.deepcopy(context)

    def __iter__(self):
        return self

    def __next__(self) -> np.ndarray | tuple[str, dict[str, Any]]:
        frame = self.read_frame()
        if frame is None:
            self._index = 0
            self.reset()
            raise StopIteration

        # Otherwise ValueError: At least one stride in the given numpy array is negative,
        # and tensors with negative strides are not currently supported. (You can probably
        # work around this by making a copy of your array  with array.copy().)
        frame = frame.copy()
        if self._context is None:
            self._index += 1
            return frame

        context = copy.deepcopy(self._context[self._index])
        self._index += 1
        return frame, context


def video_inference(
    video_path: str | Path,
    task: Task,
    pose_runner: InferenceRunner,
    detector_runner: InferenceRunner | None = None,
    with_identity: bool = False,
    return_video_metadata: bool = False,
) -> list[dict[str, np.ndarray]]:
    """Runs inference on a video"""
    video = VideoIterator(str(video_path))
    n_frames = video.get_n_frames()
    vid_w, vid_h = video.dimensions
    print(
        f"Video metadata: \n"
        f"  n_frames:   {n_frames}\n"
        f"  fps:        {video.fps}\n"
        f"  resolution: w={vid_w}, h={vid_h}\n"
    )
    video_metadata = {
        "n_frames": n_frames,
        "fps": video.fps,
        "resolution": (vid_w, vid_h),
    }

    if task == Task.TOP_DOWN:
        # Get bounding boxes for context
        if detector_runner is None:
            raise ValueError("Must use a detector for top-down video analysis")

        print("Running Detector")
        bbox_predictions = detector_runner.inference(images=tqdm(video))
        video.set_context(bbox_predictions)

    print("Running Pose Prediction")
    predictions = pose_runner.inference(images=tqdm(video))

    if with_identity:
        bodypart_predictions = assign_identity(
            [p["bodyparts"] for p in predictions],
            [p["identity_scores"] for p in predictions],
        )
        for i, p_with_id in enumerate(bodypart_predictions):
            predictions[i]["bodyparts"] = p_with_id
    if return_video_metadata:
        return predictions, video_metadata
    return predictions


def analyze_videos(
    config: str,
    videos: str | list[str],
    videotype: str | None = None,
    shuffle: int = 1,
    trainingsetindex: int = 0,
    snapshotindex: int | None = None,
    device: str | None = None,
    destfolder: str | None = None,
    batchsize: int | None = None,
    modelprefix: str = "",
    transform: A.Compose | None = None,
    auto_track: bool | None = True,
    identity_only: bool | None = False,
    overwrite: bool = False,
) -> list[tuple[str, pd.DataFrame]]:
    """Makes prediction based on a trained network.

    # TODO:
        - allow batch size greater than 1
        - other options such as save_as_csv
        - pass detector path or detector runner
        - add TQDM to runner

    The index of the trained network is specified by parameters in the config file
    (in particular the variable 'snapshot_index').

    Args:
        config: full path of the config.yaml file for the project
        videos: a str (or list of strings) containing the full paths to videos for
            analysis or a path to the directory, where all the videos with same
            extension are stored.
        videotype: checks for the extension of the video in case the input to the video
            is a directory. Only videos with this extension are analyzed. If left
            unspecified, keeps videos with extensions ('avi', 'mp4', 'mov', 'mpeg', 'mkv').
        shuffle: An integer specifying the shuffle index of the training dataset used for
            training the network.
        trainingsetindex: Integer specifying which TrainingsetFraction to use.
        device: the device to use for video analysis
        destfolder: specifies the destination folder for analysis data. If ``None``,
            the path of the video is used. Note that for subsequent analysis this
            folder also needs to be passed
        snapshotindex: index (starting at 0) of the snapshot to use to analyze the
            videos. To evaluate the last one, use -1. For example if we have
                - snapshot-0.pt
                - snapshot-50.pt
                - snapshot-100.pt
            and we want to evaluate snapshot-50.pt, snapshotindex should be 1. If None,
            the snapshotindex is loaded from the project configuration.
        modelprefix: directory containing the deeplabcut models to use when evaluating
            the network. By default, they are assumed to exist in the project folder.
        batchsize: the batch size to use for inference. Takes the value from the
            PyTorch config as a default
        transform: Optional custom transforms to apply to the video
        overwrite: Overwrite any existing videos
        auto_track: By default, tracking and stitching are automatically performed,
            producing the final h5 data file. This is equivalent to the behavior for
            single-animal projects.

            If ``False``, one must run ``convert_detections2tracklets`` and
            ``stitch_tracklets`` afterwards, in order to obtain the h5 file.
        identity_only: sub-call for auto_track. If ``True`` and animal identity was
            learned by the model, assembly and tracking rely exclusively on identity
            prediction.

    Returns:
        A list containing tuples (video_name, df_video_predictions)
    """
    # Create the output folder
    _validate_destfolder(destfolder)

    # Load the project configuration
    cfg = auxiliaryfunctions.read_config(config)
    project_path = Path(cfg["project_path"])
    train_fraction = cfg["TrainingFraction"][trainingsetindex]
    model_folder = project_path / auxiliaryfunctions.get_model_folder(
        train_fraction, shuffle, cfg, modelprefix=modelprefix, engine=Engine.PYTORCH,
    )
    model_path = _get_model_path(model_folder, snapshotindex, cfg)
    model_epochs = int(model_path.stem.split("-")[-1])
    dlc_scorer, dlc_scorer_legacy = auxiliaryfunctions.get_scorer_name(
        cfg,
        shuffle,
        train_fraction,
        trainingsiterations=model_epochs,
        engine=Engine.PYTORCH,
        modelprefix=modelprefix,
    )
    # Get general project parameters
    bodyparts = auxiliaryfunctions.get_bodyparts(cfg)
    unique_bodyparts = auxiliaryfunctions.get_unique_bodyparts(cfg)
    individuals = cfg.get("individuals", ["animal"])
    max_num_animals = len(individuals)
    num_keypoints = len(bodyparts)

    # Read the inference configuration, load the model
    pytorch_config = auxiliaryfunctions.read_plainconfig(
        model_folder / "train" / "pytorch_config.yaml"
    )
    pose_cfg_path = model_folder / "test" / "pose_cfg.yaml"
    pose_cfg = auxiliaryfunctions.read_plainconfig(pose_cfg_path)
    pose_task = Task(pytorch_config.get("method", "BU"))

    if device is not None:
        pytorch_config["device"] = device

    detector_path = None
    if pose_task == Task.TOP_DOWN:
        # TODO: Choose which detector to use
        detector_path = _get_detector_path(model_folder, -1, cfg)

    with_identity = DLCLoader.has_identity_head(pytorch_config)

    print(f"Analyzing videos with {model_path}")
    pose_runner, detector_runner = get_runners(
        pytorch_config=pytorch_config,
        snapshot_path=str(model_path),
        max_individuals=max_num_animals,
        num_bodyparts=len(bodyparts),
        num_unique_bodyparts=len(unique_bodyparts),
        with_identity=with_identity,
        transform=transform,
        detector_path=detector_path,
        detector_transform=None,
    )

    # Reading video and init variables
    videos = list_videos_in_folder(videos, videotype)
    results = []
    for video in videos:
        if destfolder is None:
            output_path = video.parent
        else:
            output_path = Path(destfolder)

        output_prefix = video.stem + dlc_scorer
        output_h5 = output_path / f"{output_prefix}.h5"
        output_pkl = output_path / f"{output_prefix}_full.pickle"

        if not overwrite and output_pkl.exists():
            print(f"Video already analyzed at {output_pkl}!")
        else:
            runtime = [time.time()]
            predictions = video_inference(
                video_path=video,
                pose_runner=pose_runner,
                task=pose_task,
                detector_runner=detector_runner,
            )
            runtime.append(time.time())
            metadata = _generate_metadata(
                cfg=cfg,
                pytorch_config=pytorch_config,
                dlc_scorer=dlc_scorer,
                train_fraction=train_fraction,
                batch_size=batchsize,
                runtime=(runtime[0], runtime[1]),
                video=VideoReader(str(video)),
            )
            output_data = _generate_output_data(pose_cfg, predictions)
            _ = auxfun_multianimal.SaveFullMultiAnimalData(
                output_data, metadata, str(output_h5)
            )

            pred_bodyparts = np.stack([p["bodyparts"][..., :3] for p in predictions])
            pred_unique_bodyparts = None
            if len(predictions) > 0 and "unique_bodyparts" in predictions[0]:
                pred_unique_bodyparts = np.stack(
                    [p["unique_bodyparts"] for p in predictions]
                )

            df = create_df_from_prediction(
                pred_bodyparts=pred_bodyparts,
                pred_unique_bodyparts=pred_unique_bodyparts,
                cfg=cfg,
                dlc_scorer=dlc_scorer,
                output_path=output_path,
                output_prefix=output_prefix,
            )
            results.append((str(video), df))

            if cfg["multianimalproject"] and len(individuals) > 1:
                pred_bodypart_ids = None
                if with_identity:
                    # reshape from (num_assemblies, num_bpts, num_individuals)
                    # to (num_assemblies, num_bpts) by taking the maximum
                    # likelihood individual for each bodypart
                    pred_bodypart_ids = np.stack(
                        [np.argmax(p["identity_scores"], axis=2) for p in predictions]
                    )

                _save_assemblies(
                    output_path,
                    output_prefix,
                    pred_bodyparts,
                    pred_bodypart_ids,
                    pred_unique_bodyparts,
                    with_identity,
                )
                if auto_track:
                    convert_detections2tracklets(
                        config=config,
                        videos=str(video),
                        videotype=videotype,
                        shuffle=shuffle,
                        trainingsetindex=trainingsetindex,
                        overwrite=False,
                        identity_only=identity_only,
                        destfolder=destfolder,
                    )
                    stitch_tracklets(
                        config,
                        [str(video)],
                        videotype,
                        shuffle,
                        trainingsetindex,
                        destfolder=destfolder,
                    )

    return results


def create_df_from_prediction(
    pred_bodyparts: np.ndarray,
    pred_unique_bodyparts: np.ndarray,
    dlc_scorer: str,
    cfg: dict,
    output_path: str | Path,
    output_prefix: str | Path,
) -> pd.DataFrame:
    output_h5 = Path(output_path) / f"{output_prefix}.h5"
    output_pkl = Path(output_path) / f"{output_prefix}_full.pickle"

    print(f"Saving results in {output_h5} and {output_pkl}")
    cols = [
        [dlc_scorer],
        list(auxiliaryfunctions.get_bodyparts(cfg)),
        ["x", "y", "likelihood"],
    ]
    cols_names = ["scorer", "bodyparts", "coords"]
    individuals = cfg.get("individuals", ["animal"])
    n_individuals = len(individuals)
    if n_individuals > 1:
        cols.insert(1, individuals)
        cols_names.insert(1, "individuals")

    results_df_index = pd.MultiIndex.from_product(cols, names=cols_names)
    pred_bodyparts = pred_bodyparts[:, :n_individuals]
    df = pd.DataFrame(
        pred_bodyparts.reshape((len(pred_bodyparts), -1)),
        columns=results_df_index,
        index=range(len(pred_bodyparts)),
    )
    if pred_unique_bodyparts is not None:
        coordinate_labels_unique = ["x", "y", "likelihood"]
        results_unique_df_index = pd.MultiIndex.from_product(
            [
                [dlc_scorer],
                auxiliaryfunctions.get_unique_bodyparts(cfg),
                coordinate_labels_unique,
            ],
            names=["scorer", "bodyparts", "coords"],
        )
        df_u = pd.DataFrame(
            pred_unique_bodyparts.reshape((len(pred_unique_bodyparts), -1)),
            columns=results_unique_df_index,
            index=range(len(pred_unique_bodyparts)),
        )
        df = df.join(df_u, how="outer")

    df.to_hdf(output_h5, "df_with_missing", format="table", mode="w")
    return df


def _save_assemblies(
    output_path: Path,
    output_prefix: str,
    pred_bodyparts: np.ndarray,
    pred_bodypart_ids: np.ndarray,
    pred_unique_bodyparts: np.ndarray,
    with_identity: bool,
) -> None:
    output_ass = output_path / f"{output_prefix}_assemblies.pickle"
    assemblies = {}
    for i, bpt in enumerate(pred_bodyparts):
        if with_identity:
            extra_column = np.expand_dims(pred_bodypart_ids[i], axis=-1)
        else:
            extra_column = np.full(
                (bpt.shape[0], bpt.shape[1], 1),
                -1.0,
                dtype=np.float32,
            )
        ass = np.concatenate((bpt, extra_column), axis=-1)
        assemblies[i] = ass

    if pred_unique_bodyparts is not None:
        assemblies["single"] = {}
        for i, unique_bpt in enumerate(pred_unique_bodyparts):
            extra_column = np.full((unique_bpt.shape[1], 1), -1.0, dtype=np.float32)
            ass = np.concatenate((unique_bpt[0], extra_column), axis=-1)
            assemblies["single"][i] = ass

    with open(output_ass, "wb") as handle:
        pickle.dump(assemblies, handle, protocol=pickle.HIGHEST_PROTOCOL)


def _validate_destfolder(destfolder: str | None) -> None:
    """Checks that the destfolder for video analysis is valid"""
    if destfolder is not None and destfolder != "":
        output_folder = Path(destfolder)
        if not output_folder.exists():
            print(f"Creating the output folder {output_folder}")
            output_folder.mkdir(parents=True)

        assert Path(
            output_folder
        ).is_dir(), f"Output folder must be a directory: you passed '{output_folder}'"


def _generate_metadata(
    cfg: dict,
    pytorch_config: dict,
    dlc_scorer: str,
    train_fraction: int,
    batch_size: int,
    runtime: tuple[float, float],
    video: VideoReader,
) -> dict:
    w, h = video.dimensions
    cropping = cfg.get("cropping", False)
    if cropping:
        cropping_parameters = [cfg["x1"], cfg["x2"], cfg["y1"], cfg["y2"]]
    else:
        cropping_parameters = [0, w, 0, h]

    metadata = {
        "start": runtime[0],
        "stop": runtime[1],
        "run_duration": runtime[1] - runtime[0],
        "Scorer": dlc_scorer,
        "pytorch-config": pytorch_config,
        "fps": video.fps,
        "batch_size": batch_size,
        "frame_dimensions": (w, h),
        "nframes": video.get_n_frames(),
        "iteration (active-learning)": cfg["iteration"],
        "training set fraction": train_fraction,
        "cropping": cropping,
        "cropping_parameters": cropping_parameters,
    }
    return {"data": metadata}


def _get_model_path(model_folder: Path, snapshot_index: int, config: dict) -> Path:
    trained_models = get_model_snapshots(model_folder / "train")

    if snapshot_index is None:
        snapshot_index = config["snapshotindex"]

    if snapshot_index == "all":
        print(
            "snapshotindex is set to 'all' in the config.yaml file. Running video "
            "analysis with all snapshots is very costly! Use the function "
            "'evaluate_network' to choose the best the snapshot. For now, changing "
            "snapshot index to -1. To evaluate another snapshot, you can change the "
            "value in the config file or call `analyze_videos` with your desired "
            "snapshot index."
        )
        snapshot_index = -1

    assert isinstance(
        snapshot_index, int
    ), f"snapshotindex must be an integer but was '{snapshot_index}'"
    return trained_models[snapshot_index]


def _get_detector_path(
    model_folder: Path, snapshot_index: int | str, config: dict | None
) -> Path:
    trained_models = get_detector_snapshots(model_folder / "train")

    if snapshot_index is None:
        snapshot_index = config["snapshotindex"]

    if snapshot_index == "all":
        print(
            "snapshotindex is set to 'all' in the config.yaml file. Running video "
            "analysis with all snapshots is very costly! Use the function "
            "'evaluate_network' to choose the best the snapshot. For now, changing "
            "snapshot index to -1. To evaluate another snapshot, you can change the "
            "value in the config file or call `analyze_videos` with your desired "
            "snapshot index."
        )
        snapshot_index = -1

    assert isinstance(
        snapshot_index, int
    ), f"snapshotindex must be an integer but was '{snapshot_index}'"
    return trained_models[snapshot_index]


def _generate_output_data(
    pose_config: dict,
    predictions: list[dict[str, np.ndarray]],
) -> dict:
    output = {
        "metadata": {
            "nms radius": pose_config.get("nmsradius"),
            "minimal confidence": pose_config.get("minconfidence"),
            "sigma": pose_config.get("sigma", 1),
            "PAFgraph": pose_config.get("partaffinityfield_graph"),
            "PAFinds": pose_config.get(
                "paf_best",
                np.arange(len(pose_config.get("partaffinityfield_graph", []))),
            ),
            "all_joints": [[i] for i in range(len(pose_config["all_joints"]))],
            "all_joints_names": [
                pose_config["all_joints_names"][i]
                for i in range(len(pose_config["all_joints"]))
            ],
            "nframes": len(predictions),
        }
    }

    str_width = int(np.ceil(np.log10(len(predictions))))
    for frame_num, frame_predictions in enumerate(predictions):
        # TODO: Do we want to keep the same format as in the TensorFlow version?
        #  On the one hand, it's "more" backwards compatible.
        #  On the other, might as well simplify the code. These files should only be loaded
        #    by the PyTorch version, and only predictions made by PyTorch models should be
        #    loaded using them

        key = "frame" + str(frame_num).zfill(str_width)
        bodyparts = frame_predictions[
            "bodyparts"
        ]  # shape (num_assemblies, num_bpts, 3)
        bodyparts = bodyparts.transpose(
            (1, 0, 2)
        )  # shape (num_bpts, num_assemblies, 3)
        coordinates = [bpt[:, :2] for bpt in bodyparts]
        scores = [bpt[:, 2:] for bpt in bodyparts]

        # full pickle has bodyparts and unique bodyparts in same array
        if "unique_bodyparts" in frame_predictions:
            unique_bpts = frame_predictions["unique_bodyparts"].transpose((1, 0, 2))
            coordinates += [bpt[:, :2] for bpt in unique_bpts]
            scores += [bpt[:, 2:] for bpt in unique_bpts]

        output[key] = {
            "coordinates": (coordinates,),
            "confidence": scores,
            "costs": None,
        }

        if "identity_scores" in frame_predictions:
            # Reshape id scores from (num_assemblies, num_bpts, num_individuals)
            # to the original DLC full pickle format: (num_bpts, num_assem, num_ind)
            id_scores = frame_predictions["identity_scores"]
            id_scores = id_scores.transpose((1, 0, 2))
            output[key]["identity"] = [bpt_id_scores for bpt_id_scores in id_scores]

    return output
