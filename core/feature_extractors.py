# -*- coding: utf-8 -*-
""" core/feature_extractors """

import os
import json
from collections import Counter
from random import sample

import cv2 as cv
# import matplotlib.pyplot as plt
import numpy as np
import onnxruntime as rt
from fastprogress.fastprogress import master_bar, progress_bar
from gutils.decorators import timing
from gutils.image_processing import get_patches
from gutils.numpy_ import get_unique_rows
from sklearn.cluster import KMeans
from skl2onnx import to_onnx
from tqdm import tqdm

import settings
from utils.descriptors import get_sift_descriptors
from utils.utils import get_uint8_image, apply_pca


class SpatialPyramidFeatures:
    """
    Holds serveral method to create the codebook, extract the spatial pyramid features/histograms
    and save it in JSON files

    Usage:
        from utils.datasets.patchcamelyon import DBhandler

        spf = SpatialPyramidFeatures(DBhandler)
        spf.create_codebook()
        spf.create_spatial_pyramid_features()
    """

    def __init__(self, db_handler_class):
        """ Initializes the instance """
        self.db_handler_class = db_handler_class

    @staticmethod
    def get_spatial_pyramid_weight(current_level, total_levels=settings.PYRAMID_LEVELS):
        """
        Calculates and returns the weight spatial historgram weight for the current_level

        Args:
            current_level (int): current level to calculate
            total_levels  (int): total number of levels to apply

        Returns:
            spatial historgram weight (float)
        """
        assert isinstance(current_level, int)
        assert isinstance(total_levels, int)
        assert total_levels >= current_level

        if current_level == 0:
            return 1/2**total_levels

        return 1/2**(total_levels - current_level + 1)

    # @timing
    def get_concatenated_weighted_histogram(self, img, pyramid_levels=settings.PYRAMID_LEVELS):
        """
        Process an image considering the pyramid level specified and returns its long
        contatenated-weighted histogram using the SIFT descriptors

        Args:
            img     (np.ndarray): image loaded using numpy
            pyramid_levels (int): number of pyramid levels to be used

        Returns:
            histogram np.array with length = round(channels * (1/3) * (4**(pyramid_levels+1)-1))
        """
        assert isinstance(img, np.ndarray)
        assert isinstance(pyramid_levels, int)
        assert min(img.shape[:2]) > 2**pyramid_levels, \
            "the image dimensions {} must be bigger than 2**pyramid_levels"\
            .format(str(img.shape[:2]))

        sift = cv.SIFT_create()
        vector_length = round(settings.CHANNELS * (1/3) * (4**(pyramid_levels+1)-1))
        histogram = np.zeros(vector_length, dtype=np.float32)
        codebook = self.load_codebook()
        input_name = codebook.get_inputs()[0].name
        label_name = codebook.get_outputs()[0].name
        values_filled = 0

        for level in range(pyramid_levels+1):
            weight = self.get_spatial_pyramid_weight(level, pyramid_levels)
            # TODO: verify somewhere that patch_size % 2**level == 0
            grid_cells_counter = 0
            for idx, patch in enumerate(
                    get_patches(get_uint8_image(img), min(img.shape[:2])//2**level)):
                kp, des = sift.detectAndCompute(patch, None)
                grid_cells_counter = idx + 1

                if des is not None:
                    des = des if des.dtype == np.float32 else des.astype(np.float32)
                    des_counter = Counter([
                        codebook.run([label_name], {input_name: [des[row]]})[0][0]
                        for row in range(des.shape[0])
                    ])

                    for channel_id, counter in des_counter.items():
                        histogram[values_filled + idx + 2**(2*level) * channel_id] = counter * weight

            values_filled += grid_cells_counter * settings.CHANNELS

        return histogram

    def process_image(self, img, pyramid_levels=settings.PYRAMID_LEVELS,
                      patch_size=settings.PATCH_SIZE, step_size=settings.STEP_SIZE):
        """
        Process an image using mini-patches, calculates its overall concatenated-weighted
        histogram using the specified pyramid level and SIFT descriptors, and finally
        returns its L1 normalised version

        Args:
            img     (np.ndarray): image loaded using numpy
            pyramid_levels (int): number of pyramid levels to be used
            patch_size     (int): size of the patchw
            step_size      (int): stride

        Returns:
            np.ndarray with length = round(channels * (1/3) * (4**(pyramid_levels+1)-1))]
        """
        assert isinstance(img, np.ndarray)
        assert isinstance(pyramid_levels, int)
        assert img.shape[0] >= patch_size
        assert img.shape[1] >= patch_size
        assert step_size > 0

        length = round(settings.CHANNELS * (1/3) * (4**(pyramid_levels+1)-1))
        descriptors = np.empty([0, length], dtype=np.float32)

        for patch in get_patches(img, patch_size, patch_size-step_size):
            descriptors = np.r_[
                descriptors,
                [self.get_concatenated_weighted_histogram(patch, pyramid_levels)]
            ]

        overall_histogram = np.sum(descriptors, axis=0)

        l1_norm = np.linalg.norm(overall_histogram, 1)

        if l1_norm in (0., np.nan):
            return overall_histogram

        return overall_histogram/l1_norm

    @timing
    def create_codebook(self, patches_percentage=.5, pyramid_levels=settings.PYRAMID_LEVELS,
                        patch_size=settings.PATCH_SIZE, step_size=settings.STEP_SIZE, save=True):
        """
        Args:
            patches_percentage  (float): percentage of patches to be used to build the codebook
            pyramid_levels        (int): number of pyramid levels to be used
            patch_size            (int): size of the patchw
            step_size             (int): stride
            save                 (bool): persist the model in a ONNX file

        Returns:
            codebook (Kmeans classifier)
        """
        assert isinstance(patches_percentage, float)
        assert 0 < patches_percentage <= 1

        training_feats = self.db_handler_class(True)()[0]
        selected_descriptors = np.empty([0, 128], dtype=np.float32)
        mb = master_bar(range(training_feats.shape[1]))

        print("Processing images")
        # TODO: improve this with multiprocessing
        for col in mb:
            patches = [i for i in get_patches(training_feats[:, col].reshape(
                [settings.IMAGE_WIDTH, settings.IMAGE_HEIGHT]), patch_size, patch_size-step_size)]

            for patch in progress_bar(sample(patches, round(len(patches) * patches_percentage)),
                                      parent=mb):
                mb.child.comment = 'Processing patches'
                descriptors = get_sift_descriptors(patch, pyramid_levels)
                descriptors = get_unique_rows(descriptors)
                selected_descriptors = np.r_[selected_descriptors, descriptors]

            mb.main_bar.comment = 'Processing images'
        print("Image processing complete")

        print("Training KMeans classifer...")
        with tqdm(total=1) as pbar:
            # TODO: Maybe I need to use the get_histogram_intersection with Kmeans...
            kmeans = KMeans(n_clusters=settings.CHANNELS, random_state=42).fit(selected_descriptors)
            pbar.update(1)

        if save:
            # RuntimeError: Only 2-D tensor(s) can be input(s).
            print("Saving codebook/Kmeans classifier")
            onx = to_onnx(kmeans, selected_descriptors[:1].astype(np.float32))

            if not os.path.isdir(settings.GENERATED_DATA_DIRECTORY):
                os.mkdir(settings.GENERATED_DATA_DIRECTORY)

            with open(os.path.join(settings.GENERATED_DATA_DIRECTORY, settings.CODEBOOK_ONNX),
                      'wb') as file_:
                file_.write(onx.SerializeToString())

        return kmeans

    @staticmethod
    def load_codebook():
        """ Loads and returns the codebook """
        sess = rt.InferenceSession(
            os.path.join(settings.GENERATED_DATA_DIRECTORY, settings.CODEBOOK_ONNX))

        return sess

    def __get_histograms(self, dataset):
        """
        Gets the histograms/features from the dataset and resturns them

        Args:
            dataset (np.ndarray): Dataset

        Returns:
            np.ndarray [samples, features]
        """
        assert isinstance(dataset, np.ndarray)

        db_histograms = [self.process_image(
            dataset[:, 0].reshape([settings.IMAGE_WIDTH, settings.IMAGE_HEIGHT]))]

        for col in tqdm(range(1, dataset.shape[1])):
            histogram = [self.process_image(
                dataset[:, col].reshape([settings.IMAGE_WIDTH, settings.IMAGE_HEIGHT]))]
            db_histograms = np.r_[db_histograms, histogram]

        return db_histograms

    @timing
    def create_spatial_pyramid_features(self):
        """
        * Processes the dataset
        * Calculates all the histograms
        * Optionaly applies PCA to the histograms/feature vectors
        * Saves the processed dataset
        """
        train_feats, train_labels, test_feats, test_labels = self.db_handler_class(True)()

        print("Getting histograms from training dataset")
        train_histograms = self.__get_histograms(train_feats)
        print("Getting histograms from testing dataset")
        test_histograms = self.__get_histograms(test_feats)

        if settings.PCA_N_COMPONENTS != -1:
            train_histograms = apply_pca(train_histograms)
            test_histograms = apply_pca(test_histograms)

        for db_split, feats, labels in [
                ('train', train_histograms, train_labels), ('test', test_histograms, test_labels)]:
            formatted_data = dict(
                codes=feats.T.tolist(),
                labels=labels.tolist()
            )
            saving_path = os.path.join(
                settings.GENERATED_DATA_DIRECTORY,
                settings.GENERATED_FEATS_FILENAME_TEMPLATE.format(db_split)
            )

            with open(saving_path, 'w') as file_:
                json.dump(formatted_data, file_)