import sys
from typing import Dict

from transformers import EvalPrediction, HfArgumentParser, TrainingArguments, is_torch_available
from transformers.testing_utils import TestCasePlus, execute_subprocess_async, require_torch_multigpu
from transformers.utils import logging


logger = logging.get_logger(__name__)


if is_torch_available():
    import torch
    from torch import nn
    from torch.utils.data.dataset import Dataset

    from transformers import Trainer

    class DummyDataset(Dataset):
        def __init__(self, length: int = 101):
            self.length = length

        def __len__(self):
            return self.length

        def __getitem__(self, i) -> int:
            return i

    class DummyDataCollator:
        def __call__(self, features):
            return {"input_ids": torch.tensor(features), "labels": torch.tensor(features)}

    class DummyModel(nn.Module):
        def __init__(self):
            super().__init__()
            # Add some (unused) params otherwise DDP will complain.
            self.fc = nn.Linear(120, 80)

        def forward(self, input_ids, labels=None):
            if labels is not None:
                return torch.tensor(0.0, device=input_ids.device), input_ids
            else:
                return input_ids


class TestTrainerDistributed(TestCasePlus):
    @require_torch_multigpu
    def test_trainer(self):
        import os
        print(os.system("nvidia-smi"))

        distributed_args = f"""
            -m torch.distributed.launch
            --nproc_per_node={torch.cuda.device_count()}
            {self.test_file_dir}/test_trainer_distributed.py
        """.split()
        output_dir = self.get_auto_remove_tmp_dir()
        args = f"--output_dir {output_dir}".split()
        cmd = [sys.executable] + distributed_args + args
        execute_subprocess_async(cmd, env=self.get_env())
        # successful return here == success - any errors would have caused an error in the sub-call


if __name__ == "__main__":
    # The script below is meant to be run under torch.distributed, on a machine with multiple GPUs:
    #
    # PYTHONPATH="src" python -m torch.distributed.launch --nproc_per_node 2 --output_dir output_dir ./tests/test_trainer_distributed.py

    parser = HfArgumentParser((TrainingArguments,))
    training_args = parser.parse_args_into_dataclasses()[0]

    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s",
        training_args.local_rank,
        training_args.device,
        training_args.n_gpu,
        training_args.local_rank != -1,
    )

    # Essentially, what we want to verify in the distributed case is that we get all samples back,
    # in the right order. (this is crucial for prediction for instance)
    for dataset_length in [101, 40, 7]:
        dataset = DummyDataset(dataset_length)

        def compute_metrics(p: EvalPrediction) -> Dict:
            sequential = list(range(len(dataset)))
            success = p.predictions.tolist() == sequential and p.label_ids.tolist() == sequential
            return {"success": success}

        trainer = Trainer(
            model=DummyModel(),
            args=training_args,
            data_collator=DummyDataCollator(),
            eval_dataset=dataset,
            compute_metrics=compute_metrics,
        )
        metrics = trainer.evaluate()
        logger.info(metrics)
        if metrics["eval_success"] is not True:
            logger.error(metrics)
            exit(1)

        p = trainer.predict(dataset)
        logger.info(p.metrics)
        if p.metrics["eval_success"] is not True:
            logger.error(p.metrics)
            exit(1)

        trainer.args.eval_accumulation_steps = 2

        metrics = trainer.evaluate()
        logger.info(metrics)
        if metrics["eval_success"] is not True:
            logger.error(metrics)
            exit(1)

        p = trainer.predict(dataset)
        logger.info(p.metrics)
        if p.metrics["eval_success"] is not True:
            logger.error(p.metrics)
            exit(1)

        trainer.args.eval_accumulation_steps = None
