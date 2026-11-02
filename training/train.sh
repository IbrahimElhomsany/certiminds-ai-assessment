#!/usr/bin/env bash
# One command to reproduce the model adaptation (run from the project root on an Apple Silicon Mac).
set -euo pipefail
mkdir -p training/logs training/results
python -m training.generate_dataset | tee training/logs/dataset_log.txt
echo "Started: $(date)" | tee training/logs/train_log.txt
mlx_lm.lora -c training/lora_config.yaml 2>&1 | tee -a training/logs/train_log.txt
echo "Finished: $(date)" | tee -a training/logs/train_log.txt
