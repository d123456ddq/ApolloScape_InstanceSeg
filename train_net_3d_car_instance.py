import argparse
import os
os.environ['CUDA_VISIBLE_DEVICES'] = '1, 2, 3'
#os.environ['CUDA_VISIBLE_DEVICES'] = '0'

import sys
import pickle
import resource
import traceback
import logging
from collections import defaultdict

import yaml
import torch
from torch.autograd import Variable
import cv2

cv2.setNumThreads(0)  # pytorch issue 1355: possible deadlock in dataloader
import _init_paths    # pylint: disable=unused-import
import nn as mynn
import utils.net as net_utils
import utils.misc as misc_utils
from core.config import cfg, merge_cfg_from_file, assert_and_infer_cfg
from datasets.roidb import combined_roidb_for_training
from roi_data.loader import RoiDataLoader, MinibatchSampler, collate_minibatch
from modeling.model_builder import Generalized_RCNN
from utils.detectron_weight_helper import load_detectron_weight
from utils.logging import setup_logging
from utils.timer import Timer
from utils.training_stats import TrainingStats

# Set up logging and load config options
logger = setup_logging(__name__)
logging.getLogger('roi_data.loader').setLevel(logging.INFO)

# RuntimeError: received 0 items of ancdata. Issue: pytorch/pytorch#973
rlimit = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (4096, rlimit[1]))


def parse_args():
    """Parse input arguments"""
    parser = argparse.ArgumentParser(description='Train a X-RCNN network')
    # The following cfg and ckpt will be changed accordingly.
    parser.add_argument('--cfg', dest='cfg_file', default='./configs/e2e_3d_car_101_FPN_triple_head_non_local.yaml', help='Config file for training (and optionally testing)')
    parser.add_argument('--load_ckpt', default='/media/samsumg_1tb/ApolloScape/ApolloScape_InstanceSeg/e2e_3d_car_101_FPN_triple_head_non_local/Oct02-11-35-29_N606-TITAN32_step/ckpt/model_step49741.pth', help='checkpoint path to load')

    parser.add_argument('--dataset', dest='dataset', default='ApolloScape', help='Dataset to use')
    parser.add_argument('--set', dest='set_cfgs', help='Set config keys. Key value sequence seperate by whitespace.''e.g. [key] [value] [key] [value]', default=[], nargs='+')
    parser.add_argument('--disp_interval', help='Display training info every N iterations', default=20, type=int)
    parser.add_argument('--no_cuda', dest='cuda', help='Do not use CUDA device', action='store_false')
    parser.add_argument('--dataset_dir', default='/media/samsumg_1tb/ApolloScape/ECCV2018_apollo/train')
    parser.add_argument('--output_dir', default='/media/samsumg_1tb/ApolloScape/ApolloScape_InstanceSeg')

    # Optimization
    # These options has the highest prioity and can overwrite the values in config file or values set by set_cfgs. `None` means do not overwrite.
    parser.add_argument('--bs', dest='batch_size', help='Explicitly specify to overwrite the value comed from cfg_file.', type=int)
    parser.add_argument('--nw', dest='num_workers', help='Explicitly specify to overwrite number of workers to load data. Defaults to 4', type=int)
    parser.add_argument('--iter_size', help='Update once every iter_size steps, as in Caffe.', default=1, type=int)
    parser.add_argument('--o', dest='optimizer', help='Training optimizer.', default=None)
    parser.add_argument('--lr', help='Base learning rate.', default=None, type=float)
    parser.add_argument('--lr_decay_gamma', help='Learning rate decay rate.', default=None, type=float)

    # Epoch
    parser.add_argument('--start_step', help='Starting step count for training epoch. 0-indexed.', default=0, type=int)
    # Resume training: requires same iterations per epoch
    parser.add_argument('--resume', default=False, help='resume to training on a checkpoint', action='store_true')
    parser.add_argument('--no_save', help='do not save anything', action='store_true')
    parser.add_argument('--ckpt_ignore_head', default=[], help='heads parameters will be ignored during loading')
    parser.add_argument('--load_detectron', help='path to the detectron weight pickle file')
    parser.add_argument('--use_tfboard', default=True, help='Use tensorflow tensorboard to log training info', action='store_true')

    return parser.parse_args()


def save_ckpt(output_dir, args, step, train_size, model, optimizer):
    """Save checkpoint"""
    if args.no_save:
        return
    ckpt_dir = os.path.join(output_dir, 'ckpt')
    if not os.path.exists(ckpt_dir):
        os.makedirs(ckpt_dir)
    save_name = os.path.join(ckpt_dir, 'model_step{}.pth'.format(step))
    if isinstance(model, mynn.DataParallel):
        model = model.module
    torch.save({
        'step': step,
        'train_size': train_size,
        'batch_size': args.batch_size,
        'model': model.state_dict(),
        'optimizer': optimizer.state_dict()}, save_name)
    logger.info('save model: %s', save_name)


def main():
    """Main function"""
    args = parse_args()
    print('Called with args:')
    print(args)

    if not torch.cuda.is_available():
        sys.exit("Need a CUDA device to run the code.")

    if args.cuda or cfg.NUM_GPUS > 0:
        cfg.CUDA = True
    else:
        raise ValueError("Need Cuda device to run !")

    merge_cfg_from_file(args.cfg_file)

    # Some manual adjustment for the ApolloScape dataset parameters here
    cfg.OUTPUT_DIR = args.output_dir
    cfg.TRAIN.DATASETS = 'Car3D'
    cfg.MODEL.NUM_CLASSES = 8
    if cfg.CAR_CLS.SIM_MAT_LOSS:
        cfg.MODEL.NUMBER_CARS = 79
    else:
        # Loss is only cross entropy, hence, we detect only car categories in the training set.
        cfg.MODEL.NUMBER_CARS = 34
    cfg.TRAIN.MIN_AREA = 196   # 14*14
    cfg.TRAIN.USE_FLIPPED = False  # Currently I don't know how to handle the flipped case
    cfg.TRAIN.IMS_PER_BATCH = 1

    cfg.NUM_GPUS = torch.cuda.device_count()
    effective_batch_size = cfg.TRAIN.IMS_PER_BATCH * cfg.NUM_GPUS * args.iter_size

    ### Adaptively adjust some configs ###
    original_batch_size = cfg.NUM_GPUS * cfg.TRAIN.IMS_PER_BATCH
    original_ims_per_batch = cfg.TRAIN.IMS_PER_BATCH
    original_num_gpus = cfg.NUM_GPUS
    if args.batch_size is None:
        args.batch_size = original_batch_size

    assert (args.batch_size % cfg.NUM_GPUS) == 0, 'batch_size: %d, NUM_GPUS: %d' % (args.batch_size, cfg.NUM_GPUS)

    print('effective_batch_size = batch_size * iter_size = %d * %d' % (args.batch_size, args.iter_size))
    print('Adaptive config changes:')
    print('    effective_batch_size: %d --> %d' % (original_batch_size, effective_batch_size))
    print('    NUM_GPUS:             %d --> %d' % (original_num_gpus, cfg.NUM_GPUS))
    print('    IMS_PER_BATCH:        %d --> %d' % (original_ims_per_batch, cfg.TRAIN.IMS_PER_BATCH))

    ### Adjust learning based on batch size change linearly
    # For iter_size > 1, gradients are `accumulated`, so lr is scaled based
    # on batch_size instead of effective_batch_size
    old_base_lr = cfg.SOLVER.BASE_LR
    cfg.SOLVER.BASE_LR *= args.batch_size / original_batch_size
    print('Adjust BASE_LR linearly according to batch_size change:\n BASE_LR: {} --> {}'.format(old_base_lr, cfg.SOLVER.BASE_LR))

    ### Adjust solver steps
    step_scale = original_batch_size / effective_batch_size
    old_solver_steps = cfg.SOLVER.STEPS
    old_max_iter = cfg.SOLVER.MAX_ITER
    cfg.SOLVER.STEPS = list(map(lambda x: int(x * step_scale + 0.5), cfg.SOLVER.STEPS))
    cfg.SOLVER.MAX_ITER = int(cfg.SOLVER.MAX_ITER * step_scale + 0.5)
    print('Adjust SOLVER.STEPS and SOLVER.MAX_ITER linearly based on effective_batch_size change:\n'
          '    SOLVER.STEPS: {} --> {}\n'
          '    SOLVER.MAX_ITER: {} --> {}'.format(old_solver_steps, cfg.SOLVER.STEPS, old_max_iter, cfg.SOLVER.MAX_ITER))

    # Scale FPN rpn_proposals collect size (post_nms_topN) in `collect` function
    # of `collect_and_distribute_fpn_rpn_proposals.py`
    #
    # post_nms_topN = int(cfg[cfg_key].RPN_POST_NMS_TOP_N * cfg.FPN.RPN_COLLECT_SCALE + 0.5)
    if cfg.FPN.FPN_ON and cfg.MODEL.FASTER_RCNN:
        cfg.FPN.RPN_COLLECT_SCALE = cfg.TRAIN.IMS_PER_BATCH / original_ims_per_batch
        print('Scale FPN rpn_proposals collect size directly propotional to the change of IMS_PER_BATCH:\n'
              '    cfg.FPN.RPN_COLLECT_SCALE: {}'.format(cfg.FPN.RPN_COLLECT_SCALE))

    if args.num_workers is not None:
        cfg.DATA_LOADER.NUM_THREADS = args.num_workers
    print('Number of data loading threads: %d' % cfg.DATA_LOADER.NUM_THREADS)

    ### Overwrite some solver settings from command line arguments
    if args.optimizer is not None:
        cfg.SOLVER.TYPE = args.optimizer
    if args.lr is not None:
        cfg.SOLVER.BASE_LR = args.lr
    if args.lr_decay_gamma is not None:
        cfg.SOLVER.GAMMA = args.lr_decay_gamma
    assert_and_infer_cfg()

    timers = defaultdict(Timer)

    ### Dataset ###
    timers['roidb'].tic()
    if cfg.MODEL.LOSS_3D_2D_ON:
        roidb, ratio_list, ratio_index, ds = combined_roidb_for_training(cfg.TRAIN.DATASETS, args.dataset_dir)
    else:
        roidb, ratio_list, ratio_index = combined_roidb_for_training(cfg.TRAIN.DATASETS, args.dataset_dir)

    timers['roidb'].toc()
    roidb_size = len(roidb)
    logger.info('{:d} roidb entries'.format(roidb_size))
    logger.info('Takes %.2f sec(s) to construct roidb', timers['roidb'].average_time)

    # Effective training sample size for one epoch
    train_size = roidb_size // args.batch_size * args.batch_size

    sampler = MinibatchSampler(ratio_list, ratio_index)
    dataset = RoiDataLoader(
        roidb,
        cfg.MODEL.NUM_CLASSES,
        training=True,
        valid_keys=['has_visible_keypoints', 'boxes', 'seg_areas', 'gt_classes', 'gt_overlaps', 'box_to_gt_ind_map',
                    'is_crowd', 'car_cat_classes', 'poses', 'quaternions', 'im_info'])
    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        drop_last=True,
        sampler=sampler,
        num_workers=cfg.DATA_LOADER.NUM_THREADS,
        collate_fn=collate_minibatch)
    dataiterator = iter(dataloader)

    ### Model ###
    if cfg.MODEL.LOSS_3D_2D_ON:
        maskRCNN = Generalized_RCNN(ds.Car3D)
    else:
        maskRCNN = Generalized_RCNN()

    if cfg.CUDA:
        maskRCNN.cuda()

    ### Optimizer ###
    bias_params = []
    bias_param_names = []
    nonbias_params = []
    nonbias_param_names = []
    for key, value in dict(maskRCNN.named_parameters()).items():
        if value.requires_grad:
            if 'bias' in key:
                bias_params.append(value)
                bias_param_names.append(key)
            else:
                nonbias_params.append(value)
                nonbias_param_names.append(key)
    # Learning rate of 0 is a dummy value to be set properly at the start of training
    params = [
        {'params': nonbias_params,
         'lr': 0,
         'weight_decay': cfg.SOLVER.WEIGHT_DECAY},
        {'params': bias_params,
         'lr': 0 * (cfg.SOLVER.BIAS_DOUBLE_LR + 1),
         'weight_decay': cfg.SOLVER.WEIGHT_DECAY if cfg.SOLVER.BIAS_WEIGHT_DECAY else 0}
    ]

    if cfg.SOLVER.TYPE == "SGD":
        optimizer = torch.optim.SGD(params, momentum=cfg.SOLVER.MOMENTUM)
    elif cfg.SOLVER.TYPE == "Adam":
        optimizer = torch.optim.Adam(params)

    ### Load checkpoint
    if args.load_ckpt:
        load_name = args.load_ckpt
        logging.info("loading checkpoint %s", load_name)
        checkpoint = torch.load(load_name, map_location=lambda storage, loc: storage)
        net_utils.load_ckpt(maskRCNN, checkpoint['model'], ignore_list=args.ckpt_ignore_head)
        if args.resume:
            args.start_step = checkpoint['step'] + 1
            if 'train_size' in checkpoint:  # For backward compatibility
                if checkpoint['train_size'] != train_size:
                    print('train_size value: %d different from the one in checkpoint: %d'
                          % (train_size, checkpoint['train_size']))

            # reorder the params in optimizer checkpoint's params_groups if needed
            # misc_utils.ensure_optimizer_ckpt_params_order(param_names, checkpoint)

            # There is a bug in optimizer.load_state_dict on Pytorch 0.3.1.
            # However it's fixed on master.
            # optimizer.load_state_dict(checkpoint['optimizer'])
            misc_utils.load_optimizer_state_dict(optimizer, checkpoint['optimizer'])
        del checkpoint
        torch.cuda.empty_cache()

    if args.load_detectron:  # TODO resume for detectron weights (load sgd momentum values)
        logging.info("loading Detectron weights %s", args.load_detectron)
        load_detectron_weight(maskRCNN, args.load_detectron)

    lr = optimizer.param_groups[0]['lr']  # lr of non-bias parameters, for commmand line outputs.

    maskRCNN = mynn.DataParallel(maskRCNN, cpu_keywords=['im_info', 'roidb'], minibatch=True)

    ### Training Setups ###
    args.run_name = misc_utils.get_run_name() + '_step'
    output_dir = misc_utils.get_output_dir(args, args.run_name)
    args.cfg_filename = os.path.basename(args.cfg_file)

    if not args.no_save:
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        blob = {'cfg': yaml.dump(cfg), 'args': args}
        with open(os.path.join(output_dir, 'config_and_args.pkl'), 'wb') as f:
            pickle.dump(blob, f, pickle.HIGHEST_PROTOCOL)

        if args.use_tfboard:
            from tensorboardX import SummaryWriter
            # Set the Tensorboard logger
            tblogger = SummaryWriter(output_dir)

    ### Training Loop ###
    maskRCNN.train()
    CHECKPOINT_PERIOD = int(cfg.TRAIN.SNAPSHOT_ITERS / cfg.NUM_GPUS)
    # Set index for decay steps
    decay_steps_ind = None
    for i in range(1, len(cfg.SOLVER.STEPS)):
        if cfg.SOLVER.STEPS[i] >= args.start_step:
            decay_steps_ind = i
            break
    if decay_steps_ind is None:
        decay_steps_ind = len(cfg.SOLVER.STEPS)

    training_stats = TrainingStats(
        args,
        args.disp_interval,
        tblogger if args.use_tfboard and not args.no_save else None)
    try:
        logger.info('Training starts !')
        step = args.start_step
        for step in range(args.start_step, cfg.SOLVER.MAX_ITER):

            # Warm up
            if step < cfg.SOLVER.WARM_UP_ITERS:
                method = cfg.SOLVER.WARM_UP_METHOD
                if method == 'constant':
                    warmup_factor = cfg.SOLVER.WARM_UP_FACTOR
                elif method == 'linear':
                    alpha = step / cfg.SOLVER.WARM_UP_ITERS
                    warmup_factor = cfg.SOLVER.WARM_UP_FACTOR * (1 - alpha) + alpha
                    # warmup_factor_trans = cfg.SOLVER.WARM_UP_FACTOR_TRANS * (1 - alpha) + alpha
                    # warmup_factor_trans *= cfg.TRANS_HEAD.LOSS_BETA
                    warmup_factor_trans = 1.0
                else:
                    raise KeyError('Unknown SOLVER.WARM_UP_METHOD: {}'.format(method))
                lr_new = cfg.SOLVER.BASE_LR * warmup_factor
                net_utils.update_learning_rate(optimizer, lr, lr_new)
                lr = optimizer.param_groups[0]['lr']
                assert lr == lr_new
            elif step == cfg.SOLVER.WARM_UP_ITERS:
                net_utils.update_learning_rate(optimizer, lr, cfg.SOLVER.BASE_LR)
                lr = optimizer.param_groups[0]['lr']
                assert lr == cfg.SOLVER.BASE_LR

            # Learning rate decay
            if decay_steps_ind < len(cfg.SOLVER.STEPS) and \
                            step == cfg.SOLVER.STEPS[decay_steps_ind]:
                logger.info('Decay the learning on step %d', step)
                lr_new = lr * cfg.SOLVER.GAMMA
                net_utils.update_learning_rate(optimizer, lr, lr_new)
                lr = optimizer.param_groups[0]['lr']
                assert lr == lr_new
                decay_steps_ind += 1

            training_stats.IterTic()
            optimizer.zero_grad()
            for inner_iter in range(args.iter_size):
                try:
                    input_data = next(dataiterator)
                except StopIteration:
                    dataiterator = iter(dataloader)
                    input_data = next(dataiterator)

                for key in input_data:
                    if key != 'roidb':  # roidb is a list of ndarrays with inconsistent length
                        input_data[key] = list(map(Variable, input_data[key]))

                net_outputs = maskRCNN(**input_data)

                net_outputs['losses']['loss_car_cls'] *= cfg.CAR_CLS.CAR_CLS_LOSS_BETA
                net_outputs['losses']['loss_rot'] *= cfg.CAR_CLS.ROT_LOSS_BETA
                if cfg.MODEL.TRANS_HEAD_ON:
                    net_outputs['losses']['loss_trans'] *= cfg.TRANS_HEAD.TRANS_LOSS_BETA

                training_stats.UpdateIterStats_car_3d(net_outputs)

                # start training
                # loss_car_cls: 2.233790, loss_rot: 0.296853, loss_trans: ~100
                loss = net_outputs['losses']['loss_car_cls'] + net_outputs['losses']['loss_rot']
                if cfg.MODEL.TRANS_HEAD_ON:
                    loss += net_outputs['losses']['loss_trans']
                if cfg.MODEL.LOSS_3D_2D_ON:
                    loss += net_outputs['losses']['UV_projection_loss']
                if not cfg.TRAIN.FREEZE_CONV_BODY and not cfg.TRAIN.FREEZE_RPN and not cfg.TRAIN.FREEZE_FPN:
                    loss += net_outputs['total_loss_conv']

                loss.backward()
            optimizer.step()
            training_stats.IterToc()

            training_stats.LogIterStats(step, lr, warmup_factor_trans)

            if (step + 1) % CHECKPOINT_PERIOD == 0:
                save_ckpt(output_dir, args, step, train_size, maskRCNN, optimizer)

        # ---- Training ends ----
        # Save last checkpoint
        save_ckpt(output_dir, args, step, train_size, maskRCNN, optimizer)

    except (RuntimeError, KeyboardInterrupt):
        del dataiterator
        logger.info('Save ckpt on exception ...')
        save_ckpt(output_dir, args, step, train_size, maskRCNN, optimizer)
        logger.info('Save ckpt done.')
        stack_trace = traceback.format_exc()
        print(stack_trace)

    finally:
        if args.use_tfboard and not args.no_save:
            tblogger.close()


if __name__ == '__main__':
    main()
