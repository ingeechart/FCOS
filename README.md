# FCOS for cityscapes


python -m torch.distributed.launch --nproc_per_node=4 --master_port=$((RANDOM + 10000)) tools/train_net.py --config-file configs/fcos/fcos_imprv_R_50_FPN_1x.yaml  DATALOADER.NUM_WORKERS 1 OUTPUT_DIR training_dir/fcos_imprv_R_50_FPN_1x
