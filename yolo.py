"""
Copyright (C) 2017, 申瑞珉 (Ruimin Shen)

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Lesser General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import os
import argparse
import configparser
import math
import pickle
import numpy as np
import pandas as pd
import tensorflow as tf
import tensorflow.contrib.slim as slim
import voc
import utils


def transform_labels_voc(imageshapes, labels, width, height, cell_width, cell_height, boxes_per_cell, classes):
    cells = cell_height * cell_width
    mask = np.zeros([len(labels), cells, 1])
    pred = np.zeros([len(labels), cells, classes])
    coords = np.zeros([len(labels), cells, boxes_per_cell, 4])
    xy_min = np.zeros([len(labels), cells, boxes_per_cell, 2])
    xy_max = np.zeros([len(labels), cells, boxes_per_cell, 2])
    for i, ((image_height, image_width, _), objects) in enumerate(zip(imageshapes, labels)):
        for xmin, ymin, xmax, ymax, c in objects:
            x = (xmin + xmax) / 2
            y = (ymin + ymax) / 2
            cell_x = x * cell_width / image_width
            cell_y = y * cell_height / image_height
            assert 0 <= cell_x < cell_width
            assert 0 <= cell_y < cell_height
            ix = math.floor(cell_x)
            iy = math.floor(cell_y)
            index = iy * cell_width + ix
            offset_x = cell_x - ix
            offset_y = cell_y - iy
            _w = float(xmax - xmin) / image_width
            _h = float(ymax - ymin) / image_height
            mask[i, index, :] = 1
            pred[i, index, :] = [0] * classes
            pred[i, index, c] = 1
            coords[i, index, :, :] = [[offset_x, offset_y, math.sqrt(_w), math.sqrt(_h)]] * boxes_per_cell
            xy_min[i, index, :, :] = [[offset_x - _w / 2 * cell_width, offset_y - _h / 2 * cell_height]] * boxes_per_cell
            xy_max[i, index, :, :] = [[offset_x + _w / 2 * cell_width, offset_y + _h / 2 * cell_height]] * boxes_per_cell
    wh = xy_max - xy_min
    assert np.all(wh >= 0)
    areas = np.multiply.reduce(wh, -1)
    return mask, pred, coords, xy_min, xy_max, areas


class ParamConv(object):
    def __init__(self, channels, layers, seed=None):
        self.channels = channels
        self.weight = []
        self.bais = []
        for i, (size, kernel1, kernel2) in enumerate(layers[['size', 'kernel1', 'kernel2']].values):
            with tf.variable_scope('conv%d' % i):
                weight = tf.Variable(tf.truncated_normal([kernel1, kernel2, channels, size], stddev=1.0 / math.sqrt(channels * kernel1 * kernel2), seed=seed), name='weight')
                self.weight.append(weight)
                bais = tf.Variable(tf.zeros([size]), name='bais')
                self.bais.append(bais)
                channels = size


class ParamFC(object):
    def __init__(self, inputs, layers, outputs, seed=None):
        self.weight = []
        self.bais = []
        for i, size in enumerate(layers['size'].values):
            with tf.variable_scope('fc%d' % i):
                weight = weight = tf.Variable(tf.truncated_normal([inputs, size], stddev=1.0 / math.sqrt(inputs), seed=seed), name='weight')
                self.weight.append(weight)
                bais = tf.Variable(tf.zeros([size]), name='bais')
                self.bais.append(bais)
                inputs = size
        with tf.variable_scope('fc'):
            weight = weight = tf.Variable(tf.truncated_normal([inputs, outputs], stddev=1.0 / math.sqrt(inputs), seed=seed), name='weight')
            self.weight.append(weight)
            bais = tf.Variable(tf.zeros([outputs]), name='bais')
            self.bais.append(bais)


class ModelConv(list):
    def __init__(self, image, param, layers, train=False, seed=None):
        for i, (weight, bais, (stride1, stride2, pooling1, pooling2, act, norm)) in enumerate(zip(param.weight, param.bais, layers[['stride1', 'stride2', 'pooling1', 'pooling2', 'act', 'norm']].values)):
            with tf.name_scope('conv%d' % i):
                layer = {}
                image = tf.nn.conv2d(image, weight, strides=[1, stride1, stride2, 1], padding='SAME')
                layer['conv'] = image
                if norm == 'batch':
                    image = slim.batch_norm(image, is_training=train)
                    layer['norm'] = image
                elif norm == 'lrn':
                    image = tf.nn.lrn(image, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75)
                    layer['norm'] = image
                image = tf.nn.bias_add(image, bais)
                layer['add'] = image
                if act == 'relu':
                    image = tf.nn.relu(image)
                    layer['act'] = image
                elif act == 'lrelu':
                    image = tf.maximum(.1 * image, image, name='lrelu')
                    layer['act'] = image
                if pooling1 > 0 and pooling2 > 0:
                    image = tf.nn.max_pool(image, ksize=[1, pooling1, pooling2, 1], strides=[1, pooling1, pooling2, 1], padding='SAME')
                    layer['pool'] = image
                layer['output'] = image
                list.append(self, layer)
        self.output = image


class ModelFC(list):
    def __init__(self, data, param, layers, train=False, seed=None):
        for i, (weight, bais, (act, norm, dropout)) in enumerate(zip(param.weight[:-1], param.bais[:-1], layers[['act', 'norm', 'dropout']].values)):
            with tf.name_scope('fc%d' % i):
                layer = {}
                data = tf.matmul(data, weight)
                layer['matmul'] = data
                data = data + bais
                layer['add'] = data
                if norm == 'batch':
                    data = tf.contrib.layers.batch_norm(data, is_training=train)
                    layer['norm'] = data
                elif norm == 'lrn':
                    data = tf.nn.lrn(data, 4, bias=1.0, alpha=0.001 / 9.0, beta=0.75)
                    layer['norm'] = data
                if act == 'relu':
                    data = tf.nn.relu(data)
                    layer['act'] = data
                elif act == 'lrelu':
                    data = tf.maximum(.1 * data, data, name='lrelu')
                    layer['act'] = data
                if train:
                    data = tf.nn.dropout(data, dropout, seed=seed)
                    layer['dropout'] = data
                layer['output'] = data
                list.append(self, layer)
        with tf.name_scope('fc'):
            layer = {}
            data = tf.matmul(data, param.weight[-1])
            layer['matmul'] = data
            data = data + param.bais[-1]
            layer['add'] = data
            layer['output'] = data
            list.append(self, layer)
        self.output = data


class Model(object):
    def __init__(self, image, param_conv, param_fc, layers_conv, layers_fc, classes, boxes_per_cell, train=False, seed=None):
        self.image = image
        with tf.name_scope('model'):
            self.conv = ModelConv(self.image, param_conv, layers_conv, train, seed)
            data_fc = tf.reshape(self.conv.output, [self.conv.output.get_shape()[0].value, -1], name='data_fc')
            self.fc = ModelFC(data_fc, param_fc, layers_fc, train, seed)
            _, cell_height, cell_width, _ = self.conv.output.get_shape().as_list()
            cells = cell_height * cell_width
            with tf.name_scope('output'):
                pred = cells * classes
                self.pred = tf.reshape(self.fc.output[:, :pred], [-1, cells, classes], name='pred')
                confs = cells * boxes_per_cell
                self.confs = tf.reshape(self.fc.output[:, pred:pred + confs], [-1, cells, boxes_per_cell], name='confs')
                self.coords = tf.reshape(self.fc.output[:, pred + confs:], [-1, cells, boxes_per_cell, 4], name='coords')
                with tf.name_scope('coords'):
                    self.offset_xy = self.coords[:, :, :, :2]
                    self.wh_sqrt = self.coords[:, :, :, 2:4]
                    self.wh = self.wh_sqrt ** 2
                    self.wh_sqrt = tf.abs(self.wh_sqrt, name='wh_sqrt')
                    wh = self.wh * [cell_width, cell_height]
                    _wh = wh / 2
                    self.xy_min = self.offset_xy - _wh
                    self.xy_max = self.offset_xy + _wh
                    self.areas = wh[:, :, :, 0] * wh[:, :, :, 1]
                self.prob = tf.reshape(self.pred, [-1, cells, 1, classes]) * tf.expand_dims(self.confs, -1)
            self.regularizer = tf.reduce_sum([tf.nn.l2_loss(weight) for weight in param_fc.weight], name='regularizer')
        self.param_conv = param_conv
        self.param_fc = param_fc
        self.classes = classes
        self.boxes_per_cell = boxes_per_cell
        self.cell_xy = self.calc_cell_xy().reshape([1, cells, 1, 2])
    
    def calc_cell_xy(self):
        _, cell_height, cell_width, _ = self.conv.output.get_shape().as_list()
        cell_base = np.zeros([cell_height, cell_width, 2])
        for y in range(cell_height):
            for x in range(cell_width):
                cell_base[y, x, :] = [x, y]
        return cell_base


class Loss(dict):
    def __init__(self, model, mask, pred, coords, xy_min, xy_max, areas):
        self.model = model
        self.mask = mask
        self.pred = pred
        self.coords = coords
        self.xy_min = xy_min
        self.xy_max = xy_max
        self.areas = areas
        
        with tf.name_scope('iou'):
            _xy_min = tf.maximum(model.xy_min, self.xy_min) 
            _xy_max = tf.minimum(model.xy_max, self.xy_max)
            _wh = tf.maximum(_xy_max - _xy_min, 0.0)
            _areas = _wh[:, :, :, 0] * _wh[:, :, :, 1]
            iou = tf.truediv(_areas, tf.maximum(self.areas + model.areas - _areas, 1e-10), name='iou')
        with tf.name_scope('confs'):
            mask = tf.equal(iou, tf.reduce_max(iou, 2, True))
            mask = tf.to_float(mask)
            mask1 = self.mask * mask
            mask0 = 1 - mask1
        self['pred'] = tf.nn.l2_loss(self.mask * model.pred - self.pred, name='pred')
        confs = model.confs - iou
        self['confs1'] = tf.nn.l2_loss(mask1 * confs, name='confs1')
        self['confs0'] = tf.nn.l2_loss(mask0 * confs, name='confs0')
        self['coords'] = tf.nn.l2_loss(tf.expand_dims(mask1, -1) * (tf.concat([model.offset_xy, model.wh_sqrt], -1) - self.coords), name='coords')


def main():
    config = configparser.ConfigParser()
    config.read('config.ini')
    section = os.path.splitext(os.path.basename(__file__))[0]
    with open(os.path.expanduser(os.path.expandvars(config.get(section, 'names'))), 'r') as f:
        names = [line.strip() for line in f]
    path = os.path.expanduser(os.path.expandvars(config.get('voc', 'path')))
    print('loading dataset from ' + path)
    imagenames, imageshapes, labels = voc.load_dataset(path, names)
    width = config.getint(section, 'width')
    height = config.getint(section, 'height')
    layers_conv = pd.read_csv(os.path.expanduser(os.path.expandvars(config.get(section, 'conv'))), sep='\t')
    cell_width = utils.calc_pooled_size(width, layers_conv['pooling1'].values)
    cell_height = utils.calc_pooled_size(height, layers_conv['pooling2'].values)
    boxes_per_cell = config.getint(section, 'boxes_per_cell')
    print('size=%d, (width, height)=(%d, %d), (cell_width, cell_height)=(%d, %d), boxes_per_cell=%d' % (len(imagenames), width, height, cell_width, cell_height, boxes_per_cell))
    labels = transform_labels_voc(imageshapes, labels, width, height, cell_width, cell_height, boxes_per_cell, len(names))
    imagepaths = [os.path.join(path, 'JPEGImages', name) for name in imagenames]
    path = os.path.expanduser(os.path.expandvars(config.get(section, 'cache')))
    with open(path, 'wb') as f:
        pickle.dump((imagepaths, *labels), f)
    print('cache saved into ' + path)


def make_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-c', '--config', default='config.ini', help='config file')
    return parser.parse_args()

if __name__ == '__main__':
    args = make_args()
    config = configparser.ConfigParser()
    assert os.path.exists(args.config)
    config.read(args.config)
    main()