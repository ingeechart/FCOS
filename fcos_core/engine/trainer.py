# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
import datetime
import logging
import time

import torch
import torch.distributed as dist

# added for eval, synchronize
from tqdm import tqdm
from fcos_core.data import make_data_loader
from fcos_core.utils.comm import get_world_size, is_pytorch_1_1_0_or_later, synchronize
from fcos_core.utils.metric_logger import MetricLogger
# added for eval,
import os
from fcos_core.engine.inference import inference
# ingee add for tensorboard
from tensorboardX import SummaryWriter

def reduce_loss_dict(loss_dict):
    """
    Reduce the loss dictionary from all processes so that process with rank
    0 has the averaged results. Returns a dict with the same fields as
    loss_dict, after reduction.
    """
    world_size = get_world_size()
    if world_size < 2:
        return loss_dict
    with torch.no_grad():
        loss_names = []
        all_losses = []
        for k in sorted(loss_dict.keys()):
            loss_names.append(k)
            all_losses.append(loss_dict[k])
        all_losses = torch.stack(all_losses, dim=0)
        dist.reduce(all_losses, dst=0)
        if dist.get_rank() == 0:
            # only main process gets accumulated, so only divide by
            # world_size in this case
            all_losses /= world_size
        reduced_losses = {k: v for k, v in zip(loss_names, all_losses)}
    return reduced_losses

# added for eval
def do_train(
    cfg,
    model,
    data_loader,
    data_loader_val,
    optimizer,
    scheduler,
    checkpointer,
    device,
    checkpoint_period,
    test_period,
    arguments,
):
    logger = logging.getLogger("fcos_core.trainer")
    logger.info("Start training")
    meters = MetricLogger(delimiter="  ")
    max_iter = len(data_loader)
    start_iter = arguments["iteration"]
    model.train()
    start_training_time = time.time()
    end = time.time()
    # added for eval
    iou_types = ("bbox",)
    if cfg.MODEL.MASK_ON:
        iou_types = iou_types + ("segm",)
    if cfg.MODEL.KEYPOINT_ON:
        iou_types = iou_types + ("keypoints",)
    dataset_names = cfg.DATASETS.TEST    
    # end
    # added for tensorboard
    writer = SummaryWriter()
    pytorch_1_1_0_or_later = is_pytorch_1_1_0_or_later()
    for iteration, (images, targets, _) in enumerate(data_loader, start_iter):
        data_time = time.time() - end
        iteration = iteration + 1
        arguments["iteration"] = iteration

        # in pytorch >= 1.1.0, scheduler.step() should be run after optimizer.step()
        if not pytorch_1_1_0_or_later:
            scheduler.step()

        images = images.to(device)
        targets = [target.to(device) for target in targets]

        loss_dict = model(images, targets)

        losses = sum(loss for loss in loss_dict.values())

        # reduce losses over all GPUs for logging purposes
        loss_dict_reduced = reduce_loss_dict(loss_dict)
        losses_reduced = sum(loss for loss in loss_dict_reduced.values())
        meters.update(loss=losses_reduced, **loss_dict_reduced)

        optimizer.zero_grad()
        losses.backward()
        optimizer.step()

        if pytorch_1_1_0_or_later:
            scheduler.step()

        batch_time = time.time() - end
        end = time.time()
        meters.update(time=batch_time, data=data_time)

        eta_seconds = meters.time.global_avg * (max_iter - iteration)
        eta_string = str(datetime.timedelta(seconds=int(eta_seconds)))

        if iteration % 20 == 0 or iteration == max_iter:
            # ingee add for tensorboard
            writer.add_scalar('r50Coco/learning_rate', optimizer.param_groups[0]["lr"],iteration)
            writer.add_scalar('r50Coco/lossSum_avg',meters.returnAvg('loss'),iteration)
            writer.add_scalars('r50Coco/losses', {'loss_cls_avg': meters.returnAvg('loss_cls'),
                                                    'loss_reg_avg': meters.returnAvg('loss_reg'),
                                                    'loss_centerness_avg': meters.returnAvg('loss_centerness')},iteration)
            logger.info(
                meters.delimiter.join(
                    [
                        "eta: {eta}",
                        "iter: {iter}",
                        "{meters}",
                        "lr: {lr:.6f}",
                        "max mem: {memory:.0f}",
                    ]
                ).format(
                    eta=eta_string,
                    iter=iteration,
                    meters=str(meters),
                    lr=optimizer.param_groups[0]["lr"],
                    memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                )
            )
        if iteration % checkpoint_period == 0:
            checkpointer.save("model_{:07d}".format(iteration), **arguments)
        #ingee add
        if data_loader_val is not None and test_period > 0 and iteration % test_period ==0:
            meters_val = MetricLogger(delimiter="  ")
            synchronize()
            # _ = inference(  # The result can be used for additional logging, e. g. for TensorBoard
            #     model,
            #     # The method changes the segmentation mask format in a data loader,
            #     # so every time a new data loader is created:
            #     make_data_loader(cfg, is_train=False, is_distributed=(get_world_size() > 1), is_for_period=True),
            #     dataset_name="[Validation]",
            #     iou_types=iou_types,
            #     box_only=False if cfg.MODEL.RETINANET_ON else cfg.MODEL.RPN_ONLY,
            #     device=cfg.MODEL.DEVICE,
            #     expected_results=cfg.TEST.EXPECTED_RESULTS,
            #     expected_results_sigma_tol=cfg.TEST.EXPECTED_RESULTS_SIGMA_TOL,
            #     output_folder=None,
            # )
            # synchronize()
            # model.train()
            # add in second adding loss logging properly
            with torch.no_grad():
                for iteration_val, (images_val, targets_val, _) in enumerate(tqdm(data_loader_val)):
                    images_val = images_val.to(device)
                    targets_val = [target.to(device) for target in targets_val]
                    loss_dict = model(images_val, targets_val)
                    losses = sum(loss for loss in loss_dict.values())
                    loss_dict_reduced = reduce_loss_dict(loss_dict)
                    losses_reduced = sum(loss for loss in loss_dict_reduced.values())
                    meters_val.update(loss=losses_reduced, **loss_dict_reduced)
            #{'loss_Global': meters.returnGlobalAvg('loss'),
            writer.add_scalars('r50Coco/global', {'loss_train': meters.returnAvg('loss'),
                                                'loss_val': meters_val.returnGlobalAvg('loss'),
                                                'valossAvg': meters_val.returnAvg('loss')}, iteration) 
            synchronize()
            logger.info(
                meters_val.delimiter.join(
                    [
                        "[Validation]: ",
                        "eta: {eta}",
                        "iter: {iter}",
                        "{meters}",
                        "lr: {lr:.6f}",
                        "max mem: {memory:.0f}",
                    ]
                ).format(
                    eta=eta_string,
                    iter=iteration,
                    meters=str(meters_val),
                    lr=optimizer.param_groups[0]["lr"],
                    memory=torch.cuda.max_memory_allocated() / 1024.0 / 1024.0,
                )
            )

                          
        # ingee end 
        if iteration == max_iter:
            checkpointer.save("model_final", **arguments)

    total_training_time = time.time() - start_training_time
    total_time_str = str(datetime.timedelta(seconds=total_training_time))
    writer.close()
    logger.info(
        "Total training time: {} ({:.4f} s / it)".format(
            total_time_str, total_training_time / (max_iter)
        )
    )
