"""
Copyright 2017-2018 Fizyr (https://fizyr.com)

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import keras
import keras.backend as K
from . import backend


def focal(alpha=0.25, gamma=2.0):
    """ Create a functor for computing the focal loss.

    Args
        alpha: Scale the focal weight with alpha.
        gamma: Take the power of the focal weight with gamma.

    Returns
        A functor that computes the focal loss using the alpha and gamma.
    """
    def _focal(y_true, y_pred):
        """ Compute the focal loss given the target tensor and the predicted tensor.

        As defined in https://arxiv.org/abs/1708.02002

        Args
            y_true: Tensor of target data from the generator with shape (B, N, num_classes).
            y_pred: Tensor of predicted data from the network with shape (B, N, num_classes).

        Returns
            The focal loss of y_pred w.r.t. y_true.
        """
        labels         = y_true[:, :, :-1]
        anchor_state   = y_true[:, :, -1]  # -1 for ignore, 0 for background, 1 for object
        classification = y_pred

        # filter out "ignore" anchors
        indices        = backend.where(keras.backend.not_equal(anchor_state, -1))
        labels         = backend.gather_nd(labels, indices)
        classification = backend.gather_nd(classification, indices)

        # compute the focal loss
        alpha_factor = keras.backend.ones_like(labels) * alpha
        alpha_factor = backend.where(keras.backend.equal(labels, 1), alpha_factor, 1 - alpha_factor)
        focal_weight = backend.where(keras.backend.equal(labels, 1), 1 - classification, classification)
        focal_weight = alpha_factor * focal_weight ** gamma

        cls_loss = focal_weight * keras.backend.binary_crossentropy(labels, classification)

        # compute the normalizer: the number of positive anchors
        normalizer = backend.where(keras.backend.equal(anchor_state, 1))
        normalizer = keras.backend.cast(keras.backend.shape(normalizer)[0], keras.backend.floatx())
        normalizer = keras.backend.maximum(1.0, normalizer)

        return keras.backend.sum(cls_loss) / normalizer

    return _focal


def bbox_overlap_iou(bboxes1, bboxes2):
    """
    Args:
        bboxes1: shape (total_bboxes1, 4)
            with x1, y1, x2, y2 point order.
        bboxes2: shape (total_bboxes2, 4)
            with x1, y1, x2, y2 point order.
        p1 *-----
           |     |
           |_____* p2
    Returns:
        Tensor with shape (total_bboxes1, total_bboxes2)
        with the IoU (intersection over union) of bboxes1[i] and bboxes2[j]
        in [i, j].
    """

    x11, y11, x12, y12 = K.tf.split(bboxes1, 4, axis=1)
    x21, y21, x22, y22 = K.tf.split(bboxes2, 4, axis=1)

    xI1 = K.maximum(x11, K.transpose(x21))
    yI1 = K.maximum(y11, K.transpose(y21))

    xI2 = K.minimum(x12, K.transpose(x22))
    yI2 = K.minimum(y12, K.transpose(y22))

    inter_area = (xI2 - xI1 + 1) * (yI2 - yI1 + 1)

    bboxes1_area = (x12 - x11 + 1) * (y12 - y11 + 1)
    bboxes2_area = (x22 - x21 + 1) * (y22 - y21 + 1)

    union = (bboxes1_area + K.transpose(bboxes2_area)) - inter_area

    return K.maximum(inter_area / union, 0)


def bbox_iog(predicted, ground_truth):
    x11, y11, x12, y12 = K.tf.split(predicted, 4, axis=1)
    x21, y21, x22, y22 = K.tf.split(ground_truth, 4, axis=1)

    xI1 = K.maximum(x11, K.transpose(x21))
    yI1 = K.maximum(y11, K.transpose(y21))

    xI2 = K.minimum(x12, K.transpose(x22))
    yI2 = K.minimum(y12, K.transpose(y22))

    intersect_area = (xI2 - xI1 + 1) * (yI2 - yI1 + 1)

    gt_area = (x22 - x21 + 1) * (y22 - y21 + 1)

    return K.maximum(intersect_area / gt_area, 0)


def smooth_l1_distance(y_true, y_pred, delta=3.):
    sigma_squared = delta ** 2

    # compute smooth L1 loss
    # f(x) = 0.5 * (sigma * x)^2          if |x| < 1 / sigma / sigma
    #        |x| - 0.5 / sigma / sigma    otherwise
    regression_diff = y_pred - y_true

    regression_diff = K.abs(regression_diff)
    regression_loss = backend.where(
        K.less(regression_diff, 1.0 / sigma_squared),
        0.5 * sigma_squared * K.pow(regression_diff, 2),
        regression_diff - 0.5 / sigma_squared
    )
    return regression_loss


def smooth_ln(x, delta):
    cond = K.less_equal(x, delta)
    true_fn = -K.log(1 - x)
    false_fn = ((x - delta) / (1 - delta)) - K.log(1 - delta)
    return backend.where(cond, true_fn, false_fn)


def attraction_term(y_true, y_pred, iou_over_predicted):
    # Найти из y_true бокс с большим IOU для всех y_pred
    # Прогоняем его через smooth_l1
    # Суммируем
    # Делим на количество y_pred
    indices_highest_iou = K.argmax(iou_over_predicted, axis=1)
    gt_highest_iou = K.map_fn(lambda i: K.tf.gather_nd(y_true, [i]), indices_highest_iou, dtype=K.floatx())
    return K.sum(smooth_l1_distance(y_pred, gt_highest_iou)) / K.cast(K.shape(y_pred)[0], K.floatx())


def repulsion_term_gt(y_true, y_pred, iou_over_predicted, alpha):
    # Найти из y_true бокс с вторым по величине IOU
    # Находим IoG между этим боксом и y_true
    # Прогоняем IoG через smooth_ln
    # Суммиируем
    # Делим на количество y_pred

    def two_prediction_exists():
        _, indices_2highest_iou = K.tf.nn.top_k(iou_over_predicted, k=2)
        indices_2highest_iou = indices_2highest_iou[:, 1]
        gt_2highest_iou = K.map_fn(lambda i: K.tf.gather_nd(y_true, [i]), indices_2highest_iou, dtype=K.floatx())
        iog = K.map_fn(lambda x: bbox_iog([x[0]], [x[1]]), (y_pred, gt_2highest_iou), dtype=K.floatx())
        iog = K.squeeze(iog, axis=2)
        return K.sum(smooth_ln(iog, alpha)) / K.cast(K.shape(y_pred)[0], K.floatx())

    def predictions_empty():
        return K.variable(0.0, dtype=K.floatx())

    return K.tf.cond(K.greater(K.shape(iou_over_predicted)[1], 1), two_prediction_exists, predictions_empty)


def repulsion_term_box(y_true, y_pred, betta):
    # Делим все множество y_pred боксов на бокс + цель (Проходимся циклом и оставляем для каждой y_true бокс из y_pred с наибольшим IoU)
    # Находим IoU для каждой пары сочетания (Bi, Bj)
    # Для каждой пары находим отношение smooth_ln(IoU) / IoU + e
    # Суммиируем
    return K.variable(0.0, dtype=K.floatx())


def regression_loss_one(y_true, y_pred):
    # Фильтруем y_pred, оставляя те, у которых IOU > 0,5 хотябы с одним y_true

    image_w, image_h, annotations_len, _ = K.tf.split(y_true[0], 4, axis=0)
    image_w, image_h, annotations_len = image_w[0], image_h[0], K.tf.cast(annotations_len[0], K.tf.int32)
    y_true = y_true[1:annotations_len, :4]
    y_pred = y_pred[:, :4]

    y_true = y_true / K.tf.tile([[image_w, image_h] * 2], K.tf.convert_to_tensor([K.shape(y_true)[0], 1]))
    y_pred = y_pred / K.tf.tile([[image_w, image_h] * 2], K.tf.convert_to_tensor([K.shape(y_pred)[0], 1]))

    iou_over_predicted = bbox_overlap_iou(y_pred, y_true)
    highest_iou = K.max(iou_over_predicted, axis=1)

    iou_gt_05 = backend.where(K.greater(highest_iou, 0.5))
    y_pred = K.tf.gather_nd(y_pred, iou_gt_05)
    iou_over_predicted = K.tf.gather_nd(iou_over_predicted, iou_gt_05)

    alpha = 0.5
    beta = 0.5
    has_data = K.tf.logical_and(K.greater(K.shape(iou_over_predicted)[0], 0),
                                K.greater(K.shape(iou_over_predicted)[1], 0))
    has_data = K.tf.logical_and(has_data, K.greater(K.shape(y_true)[0], 0))

    return K.tf.cond(has_data,
                     lambda: K.sum([
                         attraction_term(y_true, y_pred, iou_over_predicted),
                         repulsion_term_gt(y_true, y_pred, iou_over_predicted, alpha),
                         # repulsion_term_box(y_true, y_pred_masked, beta)
                     ]),
                     lambda: K.variable(0.0, dtype=K.floatx()))


def repulsion_loss(y_true, y_pred):
    with K.tf.device('/cpu:0'):
        return K.map_fn(lambda x: regression_loss_one(x[0], x[1]), (y_true, y_pred), dtype=K.floatx())


# def repulsion_loss(y_true, y_pred):
#     return K.variable(0.0, dtype=K.floatx())


def smooth_l1(sigma=3.0):
    """ Create a smooth L1 loss functor.

    Args
        sigma: This argument defines the point where the loss changes from L2 to L1.

    Returns
        A functor for computing the smooth L1 loss given target data and predicted data.
    """
    sigma_squared = sigma ** 2

    def _smooth_l1(y_true, y_pred):
        """ Compute the smooth L1 loss of y_pred w.r.t. y_true.

        Args
            y_true: Tensor from the generator of shape (B, N, 5). The last value for each box is the state of the anchor (ignore, negative, positive).
            y_pred: Tensor from the network of shape (B, N, 4).

        Returns
            The smooth L1 loss of y_pred w.r.t. y_true.
        """
        # separate target and state
        regression        = y_pred
        regression_target = y_true[:, :, :4]
        anchor_state      = y_true[:, :, 4]

        # filter out "ignore" anchors
        indices           = backend.where(keras.backend.equal(anchor_state, 1))
        regression        = backend.gather_nd(regression, indices)
        regression_target = backend.gather_nd(regression_target, indices)

        # compute smooth L1 loss
        # f(x) = 0.5 * (sigma * x)^2          if |x| < 1 / sigma / sigma
        #        |x| - 0.5 / sigma / sigma    otherwise
        regression_diff = regression - regression_target

        regression_diff = K.abs(regression_diff)
        regression_loss = backend.where(
            K.less(regression_diff, 1.0 / sigma_squared),
            0.5 * sigma_squared * K.pow(regression_diff, 2),
            regression_diff - 0.5 / sigma_squared
        )

        # compute the normalizer: the number of positive anchors
        normalizer = keras.backend.maximum(1, keras.backend.shape(indices)[0])
        normalizer = keras.backend.cast(normalizer, dtype=keras.backend.floatx())
        return keras.backend.sum(regression_loss) / normalizer

    return _smooth_l1
