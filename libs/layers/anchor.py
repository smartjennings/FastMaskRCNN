from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import numpy as np

import libs.boxes.cython_bbox as cython_bbox
import libs.configs.config_v1 as cfg
from libs.boxes.bbox_transform import bbox_transform, bbox_transform_inv, clip_boxes
from libs.boxes.anchor import anchors_plane
# FLAGS = tf.app.flags.FLAGS

def encode(gt_boxes, all_anchors, height, width, stride):
  """Matching and Encoding groundtruth into learning targets
  Sampling
  
  Parameters
  ---------
  gt_boxes: an array of shape (G x 5), [x1, y1, x2, y2, class]
  all_anchors: an array of shape (h, w, A, 4),
  width: width of feature
  height: height of feature
  stride: downscale factor w.r.t the input size, e.g., [4, 8, 16, 32]
  Returns
  --------
  labels:   Nx1 array in [0, num_classes]
  anchors:  Sampled anchors
  bbox_targets: N x (4) regression targets
  bbox_inside_weights: N x (4), in {0, 1} indicating to which class is assigned.
  """
  # TODO: speedup this module
  if all_anchors is None:
    all_anchors = anchors_plane(height, width, stride=stride)

  # anchors, inds_inside, total_anchors
  border = cfg.FLAGS.allow_border
  all_anchors = all_anchors.reshape((-1, 4))
  inds_inside = np.where(
    (all_anchors[:, 0] >= -border) &
    (all_anchors[:, 1] >= -border) &
    (all_anchors[:, 2] < (width * stride) + border) &
    (all_anchors[:, 3] < (height * stride) + border))[0]
  anchors = all_anchors[inds_inside, :]
  total_anchors = all_anchors.shape[0]
  
  # choose boxes to assign to this stride
  # TODO gt assignment outside
  areas = (gt_boxes[:, 3] - gt_boxes[:, 1] + 1) * (gt_boxes[:, 2] - gt_boxes[:, 0] + 1)
  ks = np.floor(4 + np.log2(np.sqrt(areas) / 224.0))
  K = int(np.log2(stride))
  inds = np.where((K == ks + 4))[0]
  if inds.size > 0:
    gt_boxes = gt_boxes[inds]
  else:
    labels = np.zeros((total_anchors), dtype=np.float32)
    bbox_targets = np.zeros((total_anchors, 4), dtype=np.float32)
    bbox_inside_weights = np.zeros((total_anchors, 4), dtype=np.float32)
    return labels, bbox_targets, bbox_inside_weights

  labels = np.zeros((anchors.shape[0], ), dtype=np.float32)
  overlaps = cython_bbox.bbox_overlaps(
    np.ascontiguousarray(anchors, dtype=np.float),
    np.ascontiguousarray(gt_boxes[:, :4], dtype=np.float))

  gt_assignment = overlaps.argmax(axis=1)  # (A)
  max_overlaps = overlaps[np.arange(len(inds_inside)), gt_assignment]
  gt_argmax_overlaps = overlaps.argmax(axis=0)  # G
  gt_max_overlaps = overlaps[gt_argmax_overlaps,
                             np.arange(overlaps.shape[1])]
  
  if False:
    # this is sentive to boxes of little overlaps, no need!
    gt_argmax_overlaps = np.where(overlaps == gt_max_overlaps)[0]

  # fg label: for each gt, assign anchor with highest overlap despite its overlaps
  labels[gt_argmax_overlaps] = 1
  # fg label: above threshold IOU
  labels[max_overlaps >= cfg.FLAGS.fg_threshold] = 1
  # print (np.min(labels), np.max(labels))

  # subsample positive labels if there are too many
  num_fg = int(cfg.FLAGS.fg_rpn_fraction * cfg.FLAGS.rpn_batch_size)
  fg_inds = np.where(labels == 1)[0]
  if len(fg_inds) > num_fg:
    disable_inds = np.random.choice(fg_inds, size=(len(fg_inds) - num_fg), replace=False)
    labels[disable_inds] = -1

  # TODO: mild hard negative mining
  # subsample negative labels if there are too many
  num_bg = cfg.FLAGS.rpn_batch_size - np.sum(labels == 1)
  bg_inds = np.where(labels == 0)[0]
  if len(bg_inds) > num_bg:
    disable_inds = np.random.choice(bg_inds, size=(len(bg_inds) - num_bg), replace=False)
    labels[disable_inds] = -1

  bbox_targets = np.zeros((len(inds_inside), 4), dtype=np.float32)
  bbox_targets = _compute_targets(anchors, gt_boxes[gt_assignment, :])
  bbox_inside_weights = np.zeros((len(inds_inside), 4), dtype=np.float32)
  bbox_inside_weights[labels == 1, :] = 1

  # mapping to whole outputs
  labels = _unmap(labels, total_anchors, inds_inside, fill=-1)
  bbox_targets = _unmap(bbox_targets, total_anchors, inds_inside, fill=0)
  bbox_inside_weights = _unmap(bbox_inside_weights, total_anchors, inds_inside, fill=0)

  labels = labels.reshape((1, height, width, -1))
  bbox_targets = bbox_targets.reshape((1, height, width, -1))
  bbox_inside_weights = bbox_inside_weights.reshape((1, height, width, -1))

  return labels, bbox_targets, bbox_inside_weights

def decode(boxes, scores, all_anchors, ih, iw):
  """Decode outputs into boxes
  Parameters
  ---------
  boxes: an array of shape (1, h, w, Ax4)
  scores: an array of shape (1, h, w, Ax2),
  all_anchors: an array of shape (1, h, w, Ax4), [x1, y1, x2, y2]
  
  Returns
  --------
  final_boxes: of shape (R x 4)
  classes: of shape (R) in {0,1,2,3... K-1}
  scores: of shape (R) in [0 ~ 1]
  """
  h, w = boxes.shape[1], boxes.shape[2]
  if all_anchors == None:
    stride = 2 ** int(round(np.log2((iw + 0.0) / w)))
    all_anchors = anchors_plane(h, w, stride=stride)
  all_anchors = all_anchors.reshape((-1, 4))
  boxes = boxes.reshape((-1, 4))
  scores = scores.reshape((-1, 2))
  assert scores.shape[0] == boxes.shape[0] == all_anchors.reshape[0], \
    'Anchor layer shape error %d vs %d vs %d' % (scores.shape[0],boxes.shape[0],all_anchors.reshape[0])
  boxes = bbox_transform_inv(all_anchors, boxes)
  classes = np.argmax(scores, axis=1)
  scores = scores[:, 1]
  final_boxes = np.zeros((boxes.shape[0], 4))
  for i in np.arange(final_boxes.shape[0]):
    c = classes[i] * 4
    final_boxes[i, 0:4] = boxes[i, c:c+4]
  final_boxes = clip_boxes(final_boxes, (ih, iw))
  return final_boxes, classes, scores

def sample(boxes, scores, ih, iw, is_training):
  """
  Sampling the anchor layer outputs for next stage, mask or roi prediction or roi
  
  Params
  ----------
  boxes:  of shape (? ,4)
  scores: foreground prob
  ih:     image height
  iw:     image width
  is_training:  'test' or 'train'
  
  Returns
  ----------
  rois: of shape (N, 4)
  scores: of shape (N, 1)
  batch_ids:
  """
  return


def _unmap(data, count, inds, fill=0):
  """ Unmap a subset of item (data) back to the original set of items (of
  size count) """
  if len(data.shape) == 1:
    ret = np.empty((count,), dtype=np.float32)
    ret.fill(fill)
    ret[inds] = data
  else:
    ret = np.empty((count,) + data.shape[1:], dtype=np.float32)
    ret.fill(fill)
    ret[inds, :] = data
  return ret
  
def _compute_targets(ex_rois, gt_rois):
  """Compute bounding-box regression targets for an image."""

  assert ex_rois.shape[0] == gt_rois.shape[0]
  assert ex_rois.shape[1] == 4
  assert gt_rois.shape[1] == 5

  return bbox_transform(ex_rois, gt_rois[:, :4]).astype(np.float32, copy=False)

if __name__ == '__main__':
  
  import time
  t = time.time()
  
  for i in range(10):
    cfg.FLAGS.fg_threshold = 0.1
    classes = np.random.randint(0, 3, (50, 1))
    boxes = np.random.randint(10, 50, (50, 2))
    s = np.random.randint(20, 50, (50, 2))
    s = boxes + s
    boxes = np.concatenate((boxes, s), axis=1)
    gt_boxes = np.hstack((boxes, classes))
    # gt_boxes = boxes
    rois = np.random.randint(10, 50, (20, 2))
    s = np.random.randint(0, 20, (20, 2))
    s = rois + s
    rois = np.concatenate((rois, s), axis=1)
    labels, bbox_targets, bbox_inside_weights = encode(gt_boxes, all_anchors=None, height=200, width=300, stride=4)
    labels, bbox_targets, bbox_inside_weights = encode(gt_boxes, all_anchors=None, height=100, width=150, stride=8)
    labels, bbox_targets, bbox_inside_weights = encode(gt_boxes, all_anchors=None, height=50, width=75, stride=16)
    labels, bbox_targets, bbox_inside_weights = encode(gt_boxes, all_anchors=None, height=25, width=37, stride=32)
    # anchors, _, _ = anchors_plane(200, 300, stride=4, boarder=0)
  
  print('average time: %f' % ((time.time() - t)/10.0))