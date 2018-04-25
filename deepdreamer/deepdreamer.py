###############################################################################
# Deep Dreamer
# Based on https://github.com/google/deepdream/blob/master/dream.ipynb
# Author: Kesara Rathnayake ( kesara [at] kesara [dot] lk )
###############################################################################

from os import mkdir, listdir
from subprocess import PIPE, Popen

import numpy as np
from caffe import Classifier, set_device, set_mode_gpu
from deepdreamer.images2gif import writeGif
from scipy.ndimage import affine_transform, zoom
from PIL.Image import BILINEAR, fromarray as img_fromarray, open as img_open
import logging

logging.basicConfig(
    filename='log.txt',
    format='%(asctime)s %(message)s',
    datefmt='%m/%d/%Y %I:%M:%S %p',
    level=logging.NOTSET)


def _select_network(netname):
    if netname == 'bvlc_googlenet':
        NET_FN = "deploy.prototxt"  # Make sure force_backward: true
        PARAM_FN = "bvlc_googlenet.caffemodel"
        CHANNEL_SWAP = (2, 1, 0)
        # ImageNet mean, training set dependent
        CAFFE_MEAN = np.float32([104.0, 116.0, 122.0])
        return NET_FN, PARAM_FN, CHANNEL_SWAP, CAFFE_MEAN
    elif netname == 'googlenet_place205':
        # TODO: refit SWAP and MEAN for places205? These work for now.
        NET_FN = "deploy_places205.protxt"  # Make sure force_backward: true
        PARAM_FN = "googlelet_places205_train_iter_2400000.caffemodel"
        CHANNEL_SWAP = (2, 1, 0)
        # ImageNet mean, training set dependent
        CAFFE_MEAN = np.float32([104.0, 116.0, 122.0])
        return NET_FN, PARAM_FN, CHANNEL_SWAP, CAFFE_MEAN
    else:
        print("Error: network {} not implemented".format(netname))


def _preprocess(net, img):
    return np.float32(np.rollaxis(img, 2)[::-1]) - net.transformer.mean["data"]


def _deprocess(net, img):
    return np.dstack((img + net.transformer.mean["data"])[::-1])


def _make_step(
        net, step_size=1.5, end="inception_4c/output", jitter=32, clip=True,
        guide_features=np.zeros(0)):
    """ Basic gradient ascent step. """

    src = net.blobs["data"]
    dst = net.blobs[end]

    ox, oy = np.random.randint(-jitter, jitter+1, 2)

    # apply jitter shift
    src.data[0] = np.roll(np.roll(src.data[0], ox, -1), oy, -2)

    net.forward(end=end)
    # specify the optimization objective
    if guide_features.any():
        _guided_objective(dst, guide_features)
    else:
        _unguided_objective(dst)

    net.backward(start=end)
    g = src.diff[0]
    # apply normalized ascent step to the input image
    src.data[:] += step_size/np.abs(g).mean() * g
    # unshift image
    src.data[0] = np.roll(np.roll(src.data[0], -ox, -1), -oy, -2)

    if clip:
        bias = net.transformer.mean["data"]
        src.data[:] = np.clip(src.data, -bias, 255-bias)


def _unguided_objective(dst):
    dst.diff[:] = dst.data


def _guided_objective(dst, guide_features):
    # Instead of maximizing the L2-norm of current image activations, we try to
    # maximize the dot-products between activations of current image, and their
    # best matching correspondences from the guide image.
    x = dst.data[0].copy()
    y = guide_features
    ch = x.shape[0]
    x = x.reshape(ch, -1)
    y = y.reshape(ch, -1)
    A = x.T.dot(y) # compute the matrix of dot-products with guide features
    dst.diff[0].reshape(ch, -1)[:] = y[:, A.argmax(1)] # select best matches


def _prep_guide_get_features(
        net, guideimage_path, end="inception_4c/output", target_w=244,
        target_h=244):
    pimage = img_open(guideimage_path)

    (w, h) = pimage.size

    # For best results, the guide image should be limited to the same
    # maximum width and height that the network being used was trained
    # with.  Default to 244 pixels for GoogLeNet.
    # FIXME this doesn't maintain aspect ratio, maybe use PIL.thumbnail ?
    if w != target_w or h != target_h:
        pimage = pimage.resize((target_w, target_h), BILINEAR)
    image = np.float32(pimage)
    src = net.blobs["data"]
    dst = net.blobs[end]
    src.reshape(1, 3, target_h, target_w)
    src.data[0] = _preprocess(net, image)
    net.forward(end=end)
    return dst.data[0].copy()


def _deepdream(
        net, base_img, iter_n=10, octave_n=4, octave_scale=1.4,
        end="inception_4c/output", clip=True, **step_params):
    # prepare base images for all octaves
    octaves = [_preprocess(net, base_img)]

    for i in range(octave_n-1):
        octaves.append(zoom(
            octaves[-1], (1, 1.0/octave_scale, 1.0/octave_scale), order=1))

    src = net.blobs["data"]

    # allocate image for network-produced details
    detail = np.zeros_like(octaves[-1])

    for octave, octave_base in enumerate(octaves[::-1]):
        h, w = octave_base.shape[-2:]
        if octave > 0:
            # upscale details from the previous octave
            h1, w1 = detail.shape[-2:]
            detail = zoom(detail, (1, 1.0*h/h1, 1.0*w/w1), order=1)

        src.reshape(1, 3, h, w)  # resize the network's input image size
        src.data[0] = octave_base+detail

        for i in range(iter_n):
            _make_step(net, end=end, clip=clip, **step_params)

            # visualization
            vis = _deprocess(net, src.data[0])
            if not clip:  # adjust image contrast if clipping is disabled
                vis = vis*(255.0/np.percentile(vis, 99.98))

        # extract details produced on the current octave
        detail = src.data[0]-octave_base

    # returning the resulting image
    return _deprocess(net, src.data[0])


def _output_video_dir(video):
    return "{}_images".format(video)


def _extract_video(video):
    output_dir = _output_video_dir(video)
    mkdir(output_dir)
    output = Popen(
        "ffmpeg -loglevel quiet -i {} -f image2 {}/img_%4d.jpg".format(
            video, output_dir), shell=True, stdout=PIPE).stdout.read()


def _create_video(video, frame_rate=24):
    output_dir = _output_video_dir(video)
    output = Popen((
        "ffmpeg -loglevel quiet -r {} -f image2 -pattern_type glob "
        "-i \"{}/img_*.jpg\" {}.mp4").format(
            frame_rate, output_dir, video),
        shell=True, stdout=PIPE).stdout.read()


def list_layers(network="bvlc_googlenet"):
    # Load DNN model
    NET_FN, PARAM_FN, CHANNEL_SWAP, CAFFE_MEAN = _select_network(network)
    net = Classifier(
        NET_FN, PARAM_FN, mean=CAFFE_MEAN, channel_swap=CHANNEL_SWAP)
    net.blobs.keys()


def deepdream(
        img_path, zoom=True, scale_coefficient=0.05, irange=100, iter_n=10,
        octave_n=4, octave_scale=1.4, end="inception_4c/output", clip=True,
        network="bvlc_googlenet", gif=False, reverse=False, duration=0.1,
        loop=False, gpu=False, gpuid=0, guideimage=""):
    img = np.float32(img_open(img_path))
    s = scale_coefficient
    h, w = img.shape[:2]

    if gpu:
        print("Enabling GPU {}...".format(gpuid))
        set_device(gpuid)
        set_mode_gpu()

    # Select, load DNN model
    NET_FN, PARAM_FN, CHANNEL_SWAP, CAFFE_MEAN = _select_network(network)
    net = Classifier(
        NET_FN, PARAM_FN, mean=CAFFE_MEAN, channel_swap=CHANNEL_SWAP)

    img_pool = [img_path]

    if guideimage != "":
        guide_features = _prep_guide_get_features(net, guideimage)
    else:
        guide_features = np.zeros(0)

    # Save settings used in a log file
    logging.info((
        "{} zoom={}, scale_coefficient={}, irange={}, iter_n={}, "
        "octave_n={}, octave_scale={}, end={}, clip={}, network={}, gif={}, "
        "reverse={}, duration={}, loop={}").format(
            img_path, zoom, scale_coefficient, irange, iter_n, octave_n,
            octave_scale, end, clip, network, gif, reverse, duration, loop))

    print("Dreaming...")
    for i in range(irange):
        img = _deepdream(
            net, img, iter_n=iter_n, octave_n=octave_n,
            octave_scale=octave_scale, end=end, clip=clip,
            guide_features=guide_features)
        img_fromarray(np.uint8(img)).save("{}_{}.jpg".format(
            img_path, i))
        if gif:
            img_pool.append("{}_{}.jpg".format(img_path, i))
        print("Dream {} saved.".format(i))
        if zoom:
            img = affine_transform(
                img, [1-s, 1-s, 1], [h*s/2, w*s/2, 0], order=1)
    if gif:
        print("Creating gif...")
        frames = None
        if reverse:
            frames = [img_open(f) for f in img_pool[::-1]]
        else:
            frames = [img_open(f) for f in img_pool]
        writeGif(
            "{}.gif".format(img_path), frames, duration=duration,
            repeat=loop)
        print("gif created.")


def deepdream_video(
        video, iter_n=10, octave_n=4, octave_scale=1.4,
        end="inception_4c/output", clip=True, network="bvlc_googlenet",
        frame_rate=24):

    # Select, load DNN model
    NET_FN, PARAM_FN, CHANNEL_SWAP, CAFFE_MEAN = _select_network(network)
    net = Classifier(
        NET_FN, PARAM_FN, mean=CAFFE_MEAN, channel_swap=CHANNEL_SWAP)

    print("Extracting video...")
    _extract_video(video)

    output_dir = _output_video_dir(video)
    images = listdir(output_dir)

    print("Dreaming...")
    for image in images:
        image = "{}/{}".format(output_dir, image)
        img = np.float32(img_open(image))
        img = _deepdream(
            net, img, iter_n=iter_n, octave_n=octave_n,
            octave_scale=octave_scale, end=end, clip=clip)
        img_fromarray(np.uint8(img)).save(image)

    print("Creating dream video...")
    _create_video(video, frame_rate)
    print("Dream video created.")
