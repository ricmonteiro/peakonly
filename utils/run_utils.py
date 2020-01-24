import os
import torch
import numpy as np
import matplotlib.pyplot as plt
from collections import defaultdict
from scipy.interpolate import interp1d
from utils.matching import intersected, conv2correlation


def find_mzML(path, array=None):
    if array is None:
        array = []
    for obj in os.listdir(path):
        obj_path = os.path.join(path, obj)
        if os.path.isdir(obj_path):  # object is a directory
            find_mzML(obj_path, array)
        elif obj_path[-4:] == 'mzML':  # object is mzML file
            array.append(obj_path)
    return array


def preprocess(signal, device, points):
    """
    :param signal: intensities in roi
    :param device: cpu or gpu
    :param points: number of point needed for CNN
    :return: preprocessed intensities which can be used in CNN
    """
    interpolate = interp1d(np.arange(len(signal)), signal, kind='linear')
    signal = interpolate(np.arange(points) / (points - 1) * (len(signal) - 1))
    signal = torch.tensor(signal / np.max(signal), dtype=torch.float32, device=device)
    return signal.view(1, 1, -1)


def classifier_prediction(roi, classifier, device, points=256):
    """
    :param roi: an ROI object
    :param classifier: CNN for classification
    :param device: cpu or gpu
    :param points: number of point needed for CNN
    :return: class/label
    """
    signal = preprocess(roi.i, device, points)
    proba = classifier(signal)[0].softmax(0)
    return np.argmax(proba.cpu().detach().numpy())


def correct_classification(labels):
    """
    :param labels: a dictionary, where key is a file name and value is a prediction
    :return: None (in-place correction)
    """
    pred = np.array([v for v in labels.values()])
    counter = [np.sum(pred[pred == label]) for label in [0, 1, 2]]
    # if the majority is "ones" change "two" to "one"
    if counter[1] >= len(labels) // 3:
        for k, v in labels.items():
            if v == 2:
                labels[k] = 1


def border_prediction(roi, integrator, device, peak_minimum_points, points=256, split_threshold=0.95, threshold=0.5):
    """
    :param roi: an ROI object
    :param integrator: CNN for border prediction
    :param device: cpu or gpu
    :param peak_minimum_points: minimum points in peak
    :param points: number of point needed for CNN
    :param split_threshold: threshold for probability of splitter
    :return: borders as an list of size n_peaks x 2
    """
    signal = preprocess(roi.i, device, points)
    logits = integrator(signal).sigmoid()
    splitter = logits[0, 0, :].cpu().detach().numpy()
    domain = (1 - splitter) * logits[0, 1, :].cpu().detach().numpy() > threshold

    borders_signal = []
    borders_roi = []
    begin = 0 if domain[0] else -1
    peak_wide = 1 if domain[0] else 0
    number_of_peaks = 0
    for n in range(len(domain) - 1):
        if domain[n + 1] and not domain[n]:  # peak begins
            begin = n + 1
            peak_wide = 1
        elif domain[n + 1] and begin != -1:  # peak continues
            peak_wide += 1
        elif not domain[n + 1] and begin != -1:  # peak ends
            if peak_wide / points * len(roi.i) > peak_minimum_points:
                number_of_peaks += 1
                borders_signal.append([begin, n + 2])  # to do: why n+2?
                borders_roi.append([np.max((int((begin + 1) * len(roi.i) // points - 1), 0)),
                                    int((n + 2) * len(roi.i) // points - 1) + 1])
            begin = -1
            peak_wide = 0
    # to do: if the peak doesn't end?
    # delete the smallest peak if there is no splitter between them
    n = 0
    while n < number_of_peaks - 1:
        if not np.any(splitter[borders_signal[n][1]:borders_signal[n + 1][0]] > split_threshold):
            intensity1 = np.sum(roi.i[borders_roi[n][0]:borders_signal[n][1]])
            intensity2 = np.sum(roi.i[borders_roi[n + 1][0]:borders_signal[n + 1][1]])
            smallest = n if intensity1 < intensity2 else n + 1
            borders_signal.pop(smallest)
            borders_roi.pop(smallest)
            number_of_peaks -= 1
        else:
            n += 1
    return borders_roi


def border_intersection(border, avg_border):
    """
    Check if borders intersect
    :param border: the real border in number of scans
    :param avg_border: averaged within similarity group border in number of scans
    :return: True/False
    """
    # to do: adjustable parameter?
    return intersected(border[0], border[1], avg_border[0], avg_border[1], 0.6)


def border2average_correction(borders, averaged_borders):
    """
    Correct borders based on averaged borders
    :param borders: borders for current ROI in number of scans
    :param averaged_borders: averaged within similarity group borders in number of scans
    :return: corrected borders for current ROI in number of scan
    """
    # to do: use that borders are sorted in fact
    if len(borders) == len(averaged_borders):  # to do: not the best solution
        mapping_matrix = np.eye(len(borders), dtype=np.int)
    else:
        mapping_matrix = np.zeros((len(borders), len(averaged_borders)), dtype=np.int)
        for i, border in enumerate(borders):
            for j, avg_border in enumerate(averaged_borders):
                mapping_matrix[i, j] += border_intersection(border, avg_border)

    # 'many-to-many' case resolution
    # to do: 'many-to-many' should be impossible ?
    for i, line in enumerate(mapping_matrix):
        if np.sum(line) > 1:
            for j in np.where(line == 1)[0][:-1]:
                if j + 1 < len(mapping_matrix) and mapping_matrix[j + 1, j] == 1:
                    mapping_matrix[j + 1, j] = 0
                if j + 1 < len(mapping_matrix) and mapping_matrix[j + 1, j + 1] == 1:
                    mapping_matrix[j, j + 1] = 0

    corrected_borders = []
    added = np.zeros(len(borders), dtype=np.uint8)
    for i, line in enumerate(mapping_matrix):
        if np.sum(line) > 1:  # misssing separation (even multiple almost impossible case)
            current = 1
            total = np.sum(line)
            for j in np.where(line == 1)[0]:
                if current == 1:
                    begin = min((borders[i][0], averaged_borders[j][0]))
                    corrected_borders.append([begin, averaged_borders[j][1]])
                elif current < total:
                    corrected_borders.append([averaged_borders[j][0], averaged_borders[j][1]])
                else:
                    end = max((borders[i][1], averaged_borders[j][1]))
                    corrected_borders.append([end, borders[i][1]])
                current += 1
            added[i] = 1  # added border from original borders
        elif np.sum(line) == 0:  # extra peak
            # label that added to exclude
            added[i] = 1

    for j, column in enumerate(mapping_matrix.T):
        if np.sum(column) > 1:  # redundant separation
            begin, end = None, None
            for i in np.where(column == 1)[0]:
                if begin is None and end is None:
                    begin, end = borders[i]
                else:
                    begin = np.min((begin, borders[i][0]))
                    end = np.max((end, borders[i][1]))
                assert added[i] != 1, '"many-to-many" case here must be impossible!'
                added[i] = 1
            corrected_borders.append([begin, end])
        elif np.sum(column) == 0:  # missed peak
            # added averaged borders
            corrected_borders.append(averaged_borders[j])

    # add the ramaining ("one-to-one") cases
    for i in np.where(added == 0)[0]:
        corrected_borders.append(borders[i])

    # sort corrected borders
    corrected_borders.sort()
    return corrected_borders


def border_correction(component, borders):
    """
    https://cs.stackexchange.com/questions/10713/algorithm-to-return-largest-subset-of-non-intersecting-intervals
    :param component: a groupedROI object
    :param borders: dict - key is a sample name, value is a n_borders x 2 matrix;
        predicted, corrected and transformed to normal values borders
    :return: None (in-place correction)
    """
    # to do: to average not borders, but predictions of CNNs
    n_samples = len(component.samples)
    scan_borders = defaultdict(list)  # define borders in shifted scans (size n_samples x n_borders*2 (may vary))
    for k, sample in enumerate(component.samples):
        scan_begin, _ = component.rois[k].scan
        shift = component.shifts[k]
        shifted_borders = []
        for border in borders[sample]:
            shifted_borders.append([border[0] + scan_begin + shift, border[1] + scan_begin + shift])
        scan_borders[sample] = shifted_borders

    # border correction within the similarity group
    labels = np.unique(component.grouping)
    for label in labels:
        # find total begin and end in one similarity group
        total_begin, total_end = None, None
        for i, sample in enumerate(component.samples):
            # to do: it would be better to have mapping from group to samples and numbers
            if component.grouping[i] == label:
                if total_begin is None and total_end is None:
                    total_begin, total_end = component.rois[i].scan
                else:
                    begin, end = component.rois[i].scan
                    total_begin = min((total_begin, begin))
                    total_end = min((total_end, end))

        # find averaged integration domains
        averaged_domain = np.zeros(total_end - total_begin)
        samples_in_group = 0
        for i, sample in enumerate(component.samples):
            # to do: it would be better to have mapping from group to samples and numbers
            if component.grouping[i] == label:
                samples_in_group += 1
                for border in scan_borders[sample]:
                    averaged_domain[border[0] - total_begin:border[1] - total_begin] += 1
        averaged_domain = averaged_domain / samples_in_group

        # calculate number of peaks within similarity group
        averaged_domain = averaged_domain > 0.5  # to do: adjustable parameter?
        number_of_peaks = 0
        averaged_borders = []
        begin = 0 if averaged_domain[0] else - 1
        # to do: think about peak wide and peak minimum points
        # to do: the following code is almost the exact copy
        # of the part of border_prediction function. Separate function?
        for n in range(len(averaged_domain) - 1):
            if averaged_domain[n + 1] and not averaged_domain[n]:  # peak begins
                begin = n + 1
            elif not averaged_domain[n + 1] and begin != -1:  # peak ends
                number_of_peaks += 1
                averaged_borders.append([begin + total_begin, n + 2 + total_begin])  # to do: why n+2?
                begin = -1
        if begin != -1:
            number_of_peaks += 1
            averaged_borders.append([begin + total_begin, len(averaged_domain) + 1 + total_begin])  # to do: why n+2?
            begin = -1

        # finally border correctrion
        for i, sample in enumerate(component.samples):
            # to do: it would be better to have mapping from group to samples and numbers
            if component.grouping[i] == label:
                scan_borders[sample] = border2average_correction(scan_borders[sample], averaged_borders)
                # to do: add border2borders_correction

        # change initial borders (reverse shift of scan_borders)
        for k, sample in enumerate(component.samples):
            scan_begin, _ = component.rois[k].scan
            shift = component.shifts[k]
            shifted_borders = []
            for border in scan_borders[sample]:
                shifted_borders.append([max((border[0] - scan_begin - shift, 0)),
                                        min((border[1] - scan_begin - shift, len(component.rois[k].i)))])
            borders[sample] = shifted_borders


class Feature:
    def __init__(self, samples, rois, borders, shifts,
                 intensities, mz, rtmin, rtmax,
                 mzrtgroup, similarity_group):
        # common information
        self.samples = samples
        self.rois = rois
        self.borders = borders
        self.shifts = shifts
        # feature specific information
        self.intensities = intensities
        self.mz = mz
        self.rtmin = rtmin
        self.rtmax = rtmax
        # extra information
        self.mzrtgroup = mzrtgroup  # from the same or separate groupedROI object
        self.similarity_group = similarity_group

    def __len__(self):
        return len(self.samples)

    def append(self, sample, roi, border, shift,
               intensity, mz, rtmin, rtmax):
        if self.samples:
            self.mz = (self.mz * len(self) + mz) / (len(self) + 1)
            self.rtmin = min((self.rtmin, rtmin))
            self.rtmax = max((self.rtmax, rtmax))
        else:  # feature is empty
            self.mz = mz
            self.rtmin = rtmin
            self.rtmax = rtmax

        self.samples.append(sample)
        self.rois.append(roi)
        self.borders.append(border)
        self.shifts.append(shift)
        self.intensities.append(intensity)

    def extend(self, feature):
        if self.samples:
            self.mz = (self.mz * len(self) + feature.mz * len(feature)) / (len(self) + len(feature))
            self.rtmin = min((self.rtmin, feature.rtmin))
            self.rtmax = max((self.rtmax, feature.rtmax))
        else:  # feature is empty
            self.mz = feature.mz
            self.rtmin = feature.rtmin
            self.rtmax = feature.rtmax

        self.samples.extend(feature.samples)
        self.rois.extend(feature.rois)
        self.borders.extend(feature.borders)
        self.shifts.extend(feature.shifts)
        self.intensities.extend(feature.intensities)

    def plot(self, shifted=True):
        """
        Visualize Feature object
        """
        name2label = {}
        label2class = {}
        labels = set()
        for sample in self.samples:
            end = sample.rfind('/')
            begin = sample[:end].rfind('/') + 1
            label = sample[begin:end]
            labels.add(label)
            name2label[sample] = label

        for i, label in enumerate(labels):
            label2class[label] = i

        m = len(labels)
        for sample, roi, shift, border in zip(self.samples, self.rois, self.shifts, self.borders):
            y = roi.i
            if shifted:
                x = np.linspace(roi.scan[0] + shift, roi.scan[1] + shift, len(y))
            else:
                x = np.linspace(roi.scan[0], roi.scan[1], len(y))
            label = label2class[name2label[sample]]
            c = [label / m, 0.0, (m - label) / m]
            plt.plot(x, y, color=c)
            plt.fill_between(x[border[0]:border[1]], y[border[0]:border[1]], color=c, alpha=0.5)
        plt.title('mz = {:.4f}, rt = {:.2f} -{:.2f}'.format(self.mz, self.rtmin, self.rtmax))


def build_features(component, borders, initial_group):
    """
    Integrate peaks within similarity components and build features
    :param component: a groupedROI object
    :param borders: dict - key is a sample name, value is a (n_borders x 2) matrix;
        predicted, corrected and transformed to normal values borders
    :param initial_group: a number of mzrt group
    :return: None (in-place correction)
    """
    rtdiff = (component.rois[0].rt[1] - component.rois[0].rt[0])
    scandiff = (component.rois[0].scan[1] - component.rois[0].scan[0])
    frequency = scandiff / rtdiff

    features = []
    labels = np.unique(component.grouping)
    for label in labels:
        # compute number of peaks
        peak_number = None
        for i, sample in enumerate(component.samples):
            # to do: it would be better to have mapping from group to samples and numbers
            if component.grouping[i] == label:
                peak_number = len(borders[sample])

        for p in range(peak_number):
            # build feature
            intensities = []
            samples = []
            rois = []
            feature_borders = []
            shifts = []
            rtmin, rtmax, mz = None, None, None
            for i, sample in enumerate(component.samples):
                # to do: it would be better to have mapping from group to samples and numbers
                if component.grouping[i] == label:
                    assert len(borders[sample]) == peak_number
                    begin, end = borders[sample][p]
                    intensity = np.sum(component.rois[i].i[begin:end])
                    intensities.append(intensity)
                    samples.append(sample)
                    rois.append(component.rois[i])
                    feature_borders.append(borders[sample][p])
                    shifts.append(component.shifts[i])
                    if mz is None:
                        mz = component.rois[i].mzmean
                        rtmin = component.rois[i].rt[0] + begin / frequency
                        rtmax = component.rois[i].rt[0] + end / frequency
                    else:
                        mz = (mz * i + component.rois[i].mzmean) / (i + 1)
                        rtmin = min((rtmin, component.rois[i].rt[0] + begin / frequency))
                        rtmax = max((rtmax, component.rois[i].rt[0] + end / frequency))
            features.append(Feature(samples, rois, feature_borders, shifts,
                                    intensities, mz, rtmin, rtmax,
                                    initial_group, label))
    # to do: there are a case, when borders are empty
    # assert len(features) != 0
    return features


def collapse_mzrtgroup(mzrtgroup, code):
    """
    Collapse features from the same component based on peaks similarities
    :param mzrtgroup: list of Feature objects from the same component
    :param code: a number (code) of mzrtgroup
    :return: new list of collapsed Feature objects
    """
    new_features = []
    label2idx = defaultdict(list)
    for idx, feature in enumerate(mzrtgroup):
        label2idx[feature.similarity_group].append(idx)
    unique_labels = list(set(label for label in label2idx))  # to do: not the best way :)

    # find most intense peaks in each feature
    idx2basepeak = dict()  # feature id in mzrtgroup to basepeak (np.array)
    for idx, feature in enumerate(mzrtgroup):
        n = 0
        for k in range(1, len(feature)):
            if feature.intensities[k] > feature.intensities[n]:
                n = k
        b, e = feature.borders[n]
        basepeak = np.array(feature.rois[n].i[b:e])
        idx2basepeak[idx] = basepeak

    used_features = set()  # a set of already used features
    for i, label in enumerate(unique_labels):  # iter over similarity group
        for idx in label2idx[label]:  # iter over features in one similarity group
            if idx in used_features:
                continue
            base_peak = idx2basepeak[idx]

            compose_idx = [None] * len(unique_labels)
            compose_idx[i] = idx

            for j, comp_label in enumerate(unique_labels[i + 1:]):
                # compute correlation coeffecients
                correlation_coefficients = np.zeros(len(label2idx[comp_label]))
                features_jdxs = np.zeros(len(label2idx[comp_label]), dtype=np.int)
                for n, jdx in enumerate(label2idx[comp_label]):
                    if jdx in used_features:
                        correlation_coefficients[n] = 0
                    else:
                        comp_peak = idx2basepeak[jdx]

                        # calculate cross-correlation
                        conv_vector = np.convolve(base_peak[::-1], comp_peak, mode='full')
                        corr_vector = conv2correlation(base_peak, comp_peak, conv_vector)

                        correlation_coefficients[n] = np.max(corr_vector)
                    features_jdxs[n] = jdx

                if np.max(correlation_coefficients) > 0.8:  # to do: adjustable threshold?
                    jdx = features_jdxs[np.argmax(correlation_coefficients)]
                    compose_idx[j + i + 1] = jdx

            # create 'new' feature
            feature = Feature([], [], [], [], [], None, None, None, code, None)
            for jdx in compose_idx:
                if jdx is not None:
                    feature.extend(mzrtgroup[jdx])
                    used_features.add(jdx)
            new_features.append(feature)

    return new_features


def feature_collapsing(features):
    """
    Collapse features from the same component based on peaks similarities
    with the use of 'collapse_mzrtgroup'
    :param features: list of Feature objects
    :return: new list of collapsed Feature objects
    """
    new_features = []
    group_number = 0
    mzrtgroup = []
    for feature in features:
        if feature.mzrtgroup == group_number:
            mzrtgroup.append(feature)
        else:
            # assert feature.mzrtgroup == group_number + 1  # to do: there are a case, when borders are empty
            new_features.extend(collapse_mzrtgroup(mzrtgroup, group_number))
            mzrtgroup = [feature]
            group_number = feature.mzrtgroup
    new_features.extend(collapse_mzrtgroup(mzrtgroup, group_number))
    return new_features
