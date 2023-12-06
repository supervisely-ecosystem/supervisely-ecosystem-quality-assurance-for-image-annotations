import json
import os
import random
import re
import shutil
import time, math
from typing import List, Literal, Optional

import cv2
import requests
import supervisely as sly
import tqdm
from dotenv import load_dotenv
from PIL import Image
from supervisely._utils import camel_to_snake
from supervisely.io.fs import archive_directory, get_file_name, mkdir

import dataset_tools as dtools
from dataset_tools.repo import download
from dataset_tools.repo.sample_project import (
    download_sample_image_project,
    get_sample_image_infos,
)
from dataset_tools.templates import DatasetCategory, License
from dataset_tools.text.generate_summary import list2sentence
import src.globals as g


def get_images_flat(project_info):
    images_flat = []
    for dataset in g.api.dataset.get_list(project_info.id):
        images_flat += g.api.image.get_list(dataset.id)
    return images_flat


def get_updated_images(project_info: sly.ImageInfo, project_meta: sly.ProjectMeta):
    updated_images = []
    images_flat = get_images_flat(project_info)

    if g.META_CACHE.get(project_info.id) is not None:
        if len(g.META_CACHE[project_info.id].obj_classes) != len(
            project_meta.obj_classes
        ):
            sly.logger.warn(
                "Changes in the number of classes detected. Recalculate full stats... "  # TODO
            )
            g.META_CACHE[project_info.id] = project_meta
            return images_flat
    g.META_CACHE[project_info.id] = project_meta

    for image in images_flat:
        try:
            image: sly.ImageInfo
            cached = g.IMAGES_CACHE[image.id]
            if image.updated_at != cached.updated_at:
                updated_images.append(image)
                g.IMAGES_CACHE[image.id] = image
        except KeyError:
            updated_images.append(image)
            g.IMAGES_CACHE[image.id] = image

    sly.logger.info(f"The changes in {len(updated_images)} images detected")
    return updated_images


def get_indexes_dct(project_id):
    idx_to_infos, infos_to_idx = {}, {}

    for dataset in g.api.dataset.get_list(project_id):
        images_all = g.api.image.get_list(dataset.id)
        images_all = sorted(images_all, key=lambda x: x.id)

        for idx, image_batch in enumerate(sly.batched(images_all, g.CHUNK_SIZE)):
            identifier = f"chunk_{idx}_{dataset.id}_{project_id}"
            for image in image_batch:
                infos_to_idx[image.id] = identifier
            idx_to_infos[identifier] = image_batch

    return idx_to_infos, infos_to_idx


def pull_cache(tf_cache_dir: str):
    if not g.api.file.dir_exists(g.TEAM_ID, tf_cache_dir):
        return

    # files = g.api.file.list(g.TEAM_ID, tf_cache_dir, return_type="fileinfo")
    local_dir = f"{g.STORAGE_DIR}/_cache"

    if sly.fs.dir_exists(local_dir):
        sly.fs.clean_dir(local_dir)
    g.api.file.download_directory(g.TEAM_ID, tf_cache_dir, local_dir)
    files = sly.fs.list_files(local_dir, [".json"])

    for file in files:
        if "meta_cache.json" in file:
            with open(file, "r") as f:
                g.META_CACHE = json.load(f)
        if "images_cache.json" in file:
            with open(file, "r") as f:
                g.IMAGES_CACHE = json.load(f).get(str(g.PROJECT_ID), {})

    g.META_CACHE = {
        int(k): sly.ProjectMeta().from_json(v) for k, v in g.META_CACHE.items()
    }
    g.IMAGES_CACHE = {int(k): sly.ImageInfo(*v) for k, v in g.IMAGES_CACHE.items()}

    sly.logger.info("The cache was pulled from team files")


def push_cache(tf_cache_dir: str):
    local_cache_dir = f"{g.STORAGE_DIR}/_cache"
    os.makedirs(local_cache_dir, exist_ok=True)

    json_meta = {k: v.to_json() for k, v in g.META_CACHE.items()}

    with open(f"{local_cache_dir}/meta_cache.json", "w") as f:
        json.dump(json_meta, f)

    with open(f"{local_cache_dir}/images_cache.json", "w") as f:
        json.dump({g.PROJECT_ID: g.IMAGES_CACHE}, f)

    g.api.file.upload_directory(
        g.TEAM_ID,
        local_cache_dir,
        tf_cache_dir,
        change_name_if_conflict=False,
        replace_if_conflict=True,
    )

    sly.logger.info("The cache was pushed to team files")


def check_datasets_consistency(project_info, datasets, npy_paths, num_stats):
    for dataset in datasets:
        actual_ceil = math.ceil(dataset.items_count / g.CHUNK_SIZE)
        max_chunks = math.ceil(
            len(
                [
                    path
                    for path in npy_paths
                    if f"_{dataset.id}_" in sly.fs.get_file_name(path)
                ]
            )
            / num_stats
        )
        if actual_ceil < max_chunks:
            raise ValueError(
                f"The number of chunks per stat ({len(npy_paths)}) not match with the total items count of the project ({project_info.items_count}) using following batch size: {g.CHUNK_SIZE}. Details: DATASET_ID={dataset.id}; actual num of chunks: {actual_ceil}; max num of chunks: {max_chunks}"
            )
    sly.logger.info("The consistency of data is OK")
