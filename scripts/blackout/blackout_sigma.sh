#!/bin/bash

if [ -z "$1" ]
then
	echo "Please provide exactly 1 argument blackout amount"
else
	ratio=$1
	echo $ratio 
	name="fusion_sigma_blackout_"$ratio"_1"
	echo $name
	load="checkpoints/fusion_poe_blackout_"$ratio"_1-phase_3_e2e.ckpt"

	python train_fusion.py --data_size 1000 --batch 128 --epochs 5 --fusion_type sigma \
	--experiment_name $name --pretrain 5 --blackout $ratio \
	 --lr 1e-5 --train fusion --load_checkpoint $load

	python train_fusion.py --data_size 1000 --batch 128 --epochs 5 --fusion_type sigma \
	--experiment_name $name --pretrain 5 --blackout $ratio \
	--train fusion  --lr 1e-5 

	python train_fusion.py --data_size 1000 --batch 128 --epochs 5 --fusion_type sigma \
	--experiment_name $name --pretrain 5 --blackout $ratio \
	--train fusion  --lr 1e-5 

	#python train_fusion.py --data_size 1000 --batch 128 --epochs 5 --fusion_type sigma \
	#--experiment_name $name --pretrain 5 --blackout $ratio \
	#--train fusion  --lr 1e-5


	# python train_fusion.py --data_size 1000 --batch 128 --epochs 5 --fusion_type sigma \
	# --experiment_name $name --pretrain 5 --blackout $ratio \
	# --train fusion  --lr 1e-5 \
	# --init_state_noise 0.3 


	# python train_fusion.py --data_size 1000 --batch 128 --epochs 5 --fusion_type sigma \
	# --experiment_name $name --pretrain 5 --blackout $ratio \
	# --train fusion  --lr 1e-5 \
	# --init_state_noise 0.4 

fi 

