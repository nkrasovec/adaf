import glob
import os
import shutil
import time
from pathlib import Path
from time import localtime, strftime

import geopandas as gpd
import pandas as pd
import rasterio
from aitlas.models import FasterRCNN, HRNet
from pyproj import CRS
from rasterio.features import shapes
from shapely.geometry import box, shape
from torch import cuda

import grid_tools as gt
from adaf_utils import (make_predictions_on_patches_object_detection,
                        make_predictions_on_patches_segmentation,
                        build_vrt_from_list,
                        Logger)
from adaf_vis import tiled_processing


def object_detection_vectors(predictions_dirs_dict, threshold=0.5, keep_ml_paths=False):
    """Converts object detection bounding boxes from text to vector format.

    Parameters
    ----------
    predictions_dirs_dict : dict
        Key is ML label, value is path to directory with results for that label.
    threshold : float
        Probability threshold for predictions.
    keep_ml_paths : bool
        If true, add path to ML predictions file from which the label was created as an attribute.

    Returns
    -------
    output_path : str
        Path to vector file.
    """
    # Use Path from pathlib
    path_to_predictions = Path(list(predictions_dirs_dict.values())[0])
    # Prepare output path (GPKG file in the data folder)
    output_path = path_to_predictions.parent / "object_detection.gpkg"

    appended_data = []
    crs = None
    for label, predicts_dir in predictions_dirs_dict.items():
        predicts_dir = Path(predicts_dir)
        file_list = list(predicts_dir.glob(f"*.txt"))

        for file in file_list:
            # Set path to individual PREDICTIONS FILE
            file_path = path_to_predictions / file

            # Only read files that are not empty
            if not os.stat(file_path).st_size == 0:
                # Read predictions from TXT file
                data = pd.read_csv(file_path, sep=" ", header=None)
                data.columns = ["x0", "y0", "x1", "y1", "label", "score", "epsg", "res", "x_min", "y_max"]

                # EPSG code is added to every bbox, doesn't matter which we chose, it has to be the same for all entries
                if crs is None:
                    crs = CRS.from_epsg(int(data.epsg[0]))

                data.x0 = data.x_min + (data.res * data.x0)
                data.x1 = data.x_min + (data.res * data.x1)
                data.y0 = data.y_max - (data.res * data.y0)
                data.y1 = data.y_max - (data.res * data.y1)

                data["geometry"] = [box(*a) for a in zip(data.x0, data.y0, data.x1, data.y1)]
                data.drop(columns=["x0", "y0", "x1", "y1", "epsg", "res", "x_min", "y_max"], inplace=True)

                # Filter by probability threshold
                data = data[data['score'] > threshold]
                # Add paths to ML results
                if keep_ml_paths:
                    data["prediction_path"] = str(Path().joinpath(*file.parts[-3:]))
                # Don't append if there are no predictions left after filtering
                if data.shape[0] > 0:
                    appended_data.append(data)

    if appended_data:
        # We have at least one detection
        appended_data = gpd.GeoDataFrame(pd.concat(appended_data, ignore_index=True), crs=crs)

        # If same object from two different tiles overlap, join them into one
        appended_data = appended_data.dissolve(by="label").explode(index_parts=False).reset_index(drop=False)

        # Export file
        appended_data.to_file(str(output_path), driver="GPKG")
    else:
        output_path = ""

    return str(output_path)


def semantic_segmentation_vectors(predictions_dirs_dict, threshold=0.5, keep_ml_paths=False):
    """Converts semantic segmentation probability masks to polygons using a threshold. If more than one class, all
    predictions are stored in the same vector file, class is stored as label attribute.

    Parameters
    ----------
    predictions_dirs_dict : dict
        Key is ML label, value is path to directory with results for that label.
    threshold : float
        Probability threshold for predictions.
    keep_ml_paths : bool
        If true, add path to ML predictions file from which the label was created as an attribute.

    Returns
    -------
    output_path : str
        Path to vector file.
    """
    # Prepare paths, use Path from pathlib (select one from dict, we only need parent)
    path_to_predictions = Path(list(predictions_dirs_dict.values())[0])
    # Output path (GPKG file in the data folder)
    output_path = path_to_predictions.parent / "semantic_segmentation.gpkg"

    appended_data = []
    for label, predicts_dir in predictions_dirs_dict.items():
        predicts_dir = Path(predicts_dir)
        tif_list = list(predicts_dir.glob(f"*.tif"))

        # file = tif_list[4]

        for file in tif_list:
            with rasterio.open(file) as src:
                prob_mask = src.read()
                transform = src.transform
                crs = src.crs

                prediction = prob_mask.copy()

                # Mask probability map by threshold for extraction of polygons
                feature = prob_mask >= float(threshold)
                background = prob_mask < float(threshold)

                prediction[feature] = 1
                prediction[background] = 0

                # Outputs a list of (polygon, value) tuples
                output = list(shapes(prediction, transform=transform))

                # Find polygon covering valid data (value = 1) and transform to GDF friendly format
                poly = []
                for polygon, value in output:
                    if value == 1:
                        poly.append(shape(polygon))

            # If there is at least one polygon, convert to GeoDataFrame and append to list for output
            if poly:
                predicted_labels = gpd.GeoDataFrame(poly, columns=['geometry'], crs=crs)
                predicted_labels = predicted_labels.dissolve().explode(ignore_index=True)
                predicted_labels["label"] = label
                if keep_ml_paths:
                    predicted_labels["prediction_path"] = str(Path().joinpath(*file.parts[-3:]))
                appended_data.append(predicted_labels)

    if appended_data:
        # We have at least one detection
        appended_data = gpd.GeoDataFrame(pd.concat(appended_data, ignore_index=True), crs=crs)

        # If same object from two different tiles overlap, join them into one
        appended_data = appended_data.dissolve(by='label').explode(index_parts=False).reset_index(drop=False)

        # Export file
        appended_data.to_file(output_path.as_posix(), driver="GPKG")
    else:
        output_path = ""

    return str(output_path)


def run_visualisations(dem_path, tile_size, save_dir, nr_processes=1):
    """Calculates visualisations from DEM and saves them into VRT (Geotiff) file.

    Uses RVT (see adaf_vis.py).

    dem_path:
        Can be any raster file (GeoTIFF and VRT supported.)
    tile_size:
        In pixels
    save_dir:
        Save directory
    nr_processes:
        Number of processes for parallel computing

    """
    # Prepare paths
    in_file = Path(dem_path)

    # save_vis = save_dir / "vis"
    # save_vis.mkdir(parents=True, exist_ok=True)
    save_vis = save_dir  # TODO: figure out folder structure for outputs

    # === STEP 1 ===
    # We need polygon covering valid data
    valid_data_outline = gt.poly_from_valid(
        in_file.as_posix(),
        save_gpkg=save_vis  # directory where *_validDataMask.gpkg will be stored
    )

    # === STEP 2 ===
    # Create reference grid, filter it and save it to disk
    tiles_extents = gt.bounding_grid(
        in_file.as_posix(),
        tile_size,
        tag=False
    )
    refgrid_name = in_file.as_posix()[:-4] + "_refgrid.gpkg"
    tiles_extents = gt.filter_by_outline(
        tiles_extents,
        valid_data_outline,
        save_gpkg=True,
        save_path=refgrid_name
    )

    # === STEP 3 ===
    # Run visualizations
    print("Start RVT vis")
    out_paths = tiled_processing(
        input_vrt_path=in_file.as_posix(),
        ext_list=tiles_extents,
        nr_processes=nr_processes,
        ll_dir=Path(save_vis)
    )

    # Remove reference grid and valid data mask files
    Path(valid_data_outline).unlink()
    Path(refgrid_name).unlink()

    return out_paths


def run_aitlas_object_detection(labels, images_dir):
    """

    Parameters
    ----------
    labels
    images_dir

    Returns
    -------
    predictions_dirs: dict
        List of

    """
    images_dir = str(images_dir)

    models = {
        "barrow": r".\ml_models\OD_barrow.tar",
        "enclosure": r".\ml_models\OD_enclosure.tar",
        "ringfort": r".\ml_models\OD_ringfort.tar",
        "AO": r".\ml_models\OD_AO.tar"
    }

    if cuda.is_available():
        print("> CUDA is available, running predictions on GPU!")
    else:
        print("> No CUDA detected, running predictions on CPU!")

    predictions_dirs = {}
    for label in labels:
        # Prepare the model
        model_config = {
            "num_classes": 2,  # Number of classes in the dataset
            "learning_rate": 0.0001,  # Learning rate for training
            "pretrained": True,  # Whether to use a pretrained model or not
            "use_cuda": cuda.is_available(),  # Set to True if you want to use GPU acceleration
            "metrics": ["map"]  # Evaluation metrics to be used
        }
        model = FasterRCNN(model_config)
        model.prepare()

        # Load appropriate ADAF model
        model_path = models.get(label)
        model.load_model(model_path)
        print("Model successfully loaded.")

        preds_dir = make_predictions_on_patches_object_detection(
            model=model,
            label=label,
            patches_folder=images_dir
        )

        predictions_dirs[label] = preds_dir

    return predictions_dirs


def run_aitlas_segmentation(labels, images_dir):
    """

    Parameters
    ----------
    labels
    images_dir

    Returns
    -------
    predictions_dirs: dict
        List of

    """
    images_dir = str(images_dir)

    models = {
        "barrow": r".\ml_models\barrow_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar",
        "enclosure": r".\ml_models\enclosure_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar",
        "ringfort": r".\ml_models\ringfort_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar",
        "AO": r".\ml_models\AO_HRNet_SLRM_512px_pretrained_train_12_val_124_with_Transformation.tar"
    }

    if cuda.is_available():
        print("> CUDA is available, running predictions on GPU!")
    else:
        print("> No CUDA detected, running predictions on CPU!")

    predictions_dirs = {}
    for label in labels:
        # Prepare the model
        model_config = {
            "num_classes": 2,  # Number of classes in the dataset
            "learning_rate": 0.0001,  # Learning rate for training
            "pretrained": True,  # Whether to use a pretrained model or not
            "use_cuda": cuda.is_available(),  # Set to True if you want to use GPU acceleration
            "threshold": 0.5,
            "metrics": ["iou"]  # Evaluation metrics to be used
        }
        model = HRNet(model_config)
        model.prepare()

        # Load appropriate ADAF model
        model_path = models.get(label)
        model.load_model(model_path)
        print("Model successfully loaded.")

        # Run inference
        preds_dir = make_predictions_on_patches_segmentation(
            model=model,
            label=label,
            patches_folder=images_dir
        )

        predictions_dirs[label] = preds_dir

    return predictions_dirs


def main_routine(inp):
    dem_path = Path(inp.dem_path)

    # Create unique name for results
    time_started = localtime()

    # Save results to parent folder of input file
    save_dir = Path(dem_path).parent / (dem_path.stem + strftime("_%Y%m%d_%H%M%S", time_started))
    save_dir.mkdir(parents=True, exist_ok=True)

    # Create logfile
    log_path = save_dir / "logfile.txt"
    logger = Logger(log_path, log_time=time_started)

    # VISUALIZATIONS
    logger.log_vis_inputs(dem_path, inp.vis_exist_ok)
    # vis_path is folder where visualizations are stored
    if inp.vis_exist_ok:
        # Copy visualization to results folder
        vis_path = save_dir / "visualization"
        vis_path.mkdir(parents=True, exist_ok=True)
        vrt_path = None
        # Copy tif file into this folder
        shutil.copy(dem_path, Path(vis_path / dem_path.name))

        # TODO: Cut image into tiles if too large
    else:
        # Create visualisations
        t1 = time.time()

        # Determine nr_processes from available CPUs (leave two free)
        my_cpus = os.cpu_count() - 2
        if my_cpus < 1:
            my_cpus = 1

        # The processing of the image is done on tiles (for better performance)
        # TODO: Currently hardcoded, only tiling mode works with this tile size
        tile_size_px = 1024  # Tile size has to be in base 2 (512, 1024) for inference to work!

        out_paths = run_visualisations(
            dem_path,
            tile_size_px,
            save_dir=save_dir.as_posix(),
            nr_processes=my_cpus
        )

        vis_path = out_paths["output_directory"]
        vrt_path = out_paths["vrt_path"]
        t1 = time.time() - t1

        logger.log_vis_results(vis_path, vrt_path, t1)

    # Make sure it is a Path object!
    vis_path = Path(vis_path)

    # # Create patches
    # patches_dir = create_patches(
    #     vis_path,
    #     patch_size_px,
    #     save_dir,
    #     nr_processes=nr_processes
    # )
    # shutil.rmtree(vis_path)

    # INFERENCE
    logger.log_inference_inputs(inp.ml_type)

    if inp.ml_type == "object detection":
        print("Running object detection")
        predictions_dict = run_aitlas_object_detection(inp.labels, vis_path)

        vector_path = object_detection_vectors(predictions_dict, keep_ml_paths=inp.save_ml_output)
        print("Created vector file", vector_path)

        # Remove predictions files (bbox txt)
        if not inp.save_ml_output:
            for _, p_dir in predictions_dict.items():
                shutil.rmtree(p_dir)

    elif inp.ml_type == "segmentation":
        print("Running segmentation")
        predictions_dict = run_aitlas_segmentation(inp.labels, vis_path)

        vector_path = semantic_segmentation_vectors(predictions_dict, keep_ml_paths=inp.save_ml_output)
        print("Created vector file", vector_path)

        # Save predictions files (probability masks)
        if inp.save_ml_output:
            # Create VRT file for predictions
            for label, p_dir in predictions_dict.items():
                print("Creating vrt for", label)
                tif_list = glob.glob((Path(p_dir) / f"*{label}*.tif").as_posix())
                vrt_name = save_dir / (Path(p_dir).stem + f"_{label}.vrt")
                build_vrt_from_list(tif_list, vrt_name)
        else:
            for _, p_dir in predictions_dict.items():
                shutil.rmtree(p_dir)

    else:
        raise Exception("Wrong ml_type: choose 'object detection' or 'segmentation'")

    # Remove visualizations
    if not inp.save_vis:
        shutil.rmtree(vis_path)
        if vrt_path:
            Path(vrt_path).unlink()

    print("\n--\nFINISHED!")

    return vector_path
