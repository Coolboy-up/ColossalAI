import functools
from collections import OrderedDict
from typing import Any, Optional

import torch
import torch.distributed as dist
import torch.nn as nn
from colossalai.context.parallel_mode import ParallelMode
from colossalai.core import global_context as gpc
from colossalai.engine.ophooks import register_ophooks_recursively
from colossalai.engine.ophooks.zero_hook import ZeroHook
from colossalai.engine.paramhooks import BaseParamHookMgr
from colossalai.logging import get_dist_logger
from colossalai.utils.memory_tracer.memstats_collector import MemStatsCollector
from colossalai.utils.memory_tracer.model_data_memtracer import \
    GLOBAL_MODEL_DATA_TRACER
from colossalai.utils.memory_utils.utils import colo_cuda_memory_capacity
from colossalai.zero.shard_utils import BaseShardStrategy
from colossalai.zero.sharded_model.reduce_scatter import ReduceScatterBucketer
from torch.distributed import ProcessGroup
from torch.nn.parameter import Parameter

from ._utils import (cast_float_arguments, cast_tensor_to_fp16, cast_tensor_to_fp32, chunk_and_pad, free_storage,
                     get_gradient_predivide_factor)


class ShardedModelV2(nn.Module):
    """
    A wrapper for the PyTorch module shards the model parameters among multiple GPU memory.
    Only 1/#nproc of parameters, gradients are stored in local CUDA memory, so forward and backward
    passes can be executed with limited CUDA memory budget.

    Note that you must use `ShardedModelV2` with `ShardedOptimizerV2`.

    Args:
        module (nn.Module): A sharded module, which must be initialized by `ZeroInitContext`.
        shard_strategy (BaseShardStrategy): A shard strategy to manage shard behavior.
        process_group (Optional[ProcessGroup], optional): Data parallel process group. Defaults to None.
        reduce_scatter_process_group (Optional[ProcessGroup], optional): Reduce-scatter process group. 
            Generally, it should be `None`, and it's the same as `process_group`. Defaults to None.
        reduce_scatter_bucket_size_mb (int, optional): Reduce-scatter bucket size in *MB*. Defaults to 25.
        fp32_reduce_scatter (bool, optional): If set to `True`, gradients are forced to FP32 before reduce-scatter. Defaults to False.
        offload_config (Optional[dict], optional): We currently only support CPU offload. Set to `{"device": "cpu"}` to enable CPU offload. Defaults to None.
        gradient_predivide_factor (Optional[float], optional): Gradient is divived by this value before reduce-scatter. Defaults to 1.0.
        use_memory_tracer (bool, optional): Whether to use memoty tracer. Defaults to False.
        reuse_fp16_shard (bool, optional): Whether to reuse fp16 shard for param and grad. 
            Enabling this can reduce GPU memory usage, but you have to make sure you disable it when using gradient accumulation. 
            In this mode, grad will be fp16. Make sure your optimizer supports mixed precision (fp32 param and fp16 grad). 
            We find that PyTorch's optimizers don't support mixed precision, 
            so we recommend you enable this only when using our CPUAdam with CPU offload. Defaults to False.
    """

    def __init__(self,
                 module: nn.Module,
                 shard_strategy: BaseShardStrategy,
                 process_group: Optional[ProcessGroup] = None,
                 reduce_scatter_process_group: Optional[ProcessGroup] = None,
                 reduce_scatter_bucket_size_mb: int = 25,
                 fp32_reduce_scatter: bool = False,
                 offload_config: Optional[dict] = None,
                 gradient_predivide_factor: Optional[float] = 1.0,
                 use_memory_tracer: bool = False,
                 reuse_fp16_shard: bool = False):
        super().__init__()
        self.logger = get_dist_logger()

        # We force users to use ZeroInitContext
        sharded = []
        unsharded = []
        for param in module.parameters():
            assert hasattr(param, 'col_attr'), 'You must use ZeroInitContext to init your module first.'
            sharded.append(param.col_attr.param_is_sharded)
            unsharded.append(not param.col_attr.param_is_sharded)
        assert all(sharded) or all(
            unsharded), 'Parameters must be all sharded or all unsharded! Parameters are partially sharded now.'
        self.shard_param = all(sharded)
        self.module = module

        self.process_group = process_group or gpc.get_group(ParallelMode.DATA)
        self.reduce_scatter_process_group = reduce_scatter_process_group or self.process_group
        self.world_size = dist.get_world_size(self.process_group)
        self.rank = dist.get_rank(self.process_group)
        self.shard_strategy = shard_strategy

        # Init Memory Statistics Collector
        self._use_memory_tracer = use_memory_tracer
        if self._use_memory_tracer:
            GLOBAL_MODEL_DATA_TRACER.register_model(self)
            self._memstats_collector = MemStatsCollector()
        else:
            self._memstats_collector = None
        self._iter_cnter = 0

        # Register hooks
        self._ophook_list = [ZeroHook(self.shard_strategy, self._memstats_collector, self.process_group)]
        register_ophooks_recursively(self.module, self._ophook_list)
        self.param_hook_mgr = BaseParamHookMgr(list(self.module.parameters()))
        self.param_hook_mgr.register_backward_hooks(self._grad_post_backward_hook)

        self.fp32_reduce_scatter = fp32_reduce_scatter
        self._cpu_offload: bool = offload_config.get('device', None) == 'cpu' if offload_config else False
        for param in module.parameters():
            # Init `offload_grad`
            param.col_attr.offload_grad = self._cpu_offload

        # We find if gradient_predivide_factor != 1.0, there may be wrong precision problem
        # So we use 1.0 as the default gradient_predivide_factor
        # However, if you set gradient_predivide_factor to None, we will set
        # gradient_predivide_factor to a value >= 1.0 automatically
        self.gradient_predivide_factor: float = gradient_predivide_factor if \
            gradient_predivide_factor is not None else \
            get_gradient_predivide_factor(self.world_size)
        self.gradient_postdivide_factor: float = self.world_size / self.gradient_predivide_factor

        self.comm_stream: torch.cuda.Stream = torch.cuda.Stream()
        self.reducer = ReduceScatterBucketer(reduce_scatter_bucket_size_mb)
        self._require_backward_grad_sync: bool = True

        self._cuda_margin_space = 0
        self.reuse_fp16_shard = reuse_fp16_shard

    @property
    def cuda_margin_space(self):
        return self._cuda_margin_space

    @property
    def cpu_offload(self):
        return self._cpu_offload

    def forward(self, *args: Any, **kwargs: Any) -> torch.Tensor:
        if self._iter_cnter == 0 and self._memstats_collector:
            # the opeartion will affect the flag in ZeroHook
            self._memstats_collector.start_collection()
        args, kwargs = cast_float_arguments(cast_tensor_to_fp16, *args, **kwargs)
        outputs = self.module(*args, **kwargs)
        return outputs

    def backward(self, loss):
        loss.backward()
        self._post_backward_operations()
        for ophook in self._ophook_list:
            ophook.post_iter()

    def backward_by_grad(self, tensor, grad):
        torch.autograd.backward(tensors=tensor, grad_tensors=grad)
        self._post_backward_operations()
        for ophook in self._ophook_list:
            ophook.post_iter()

    def _update_memstats(self):
        if self._iter_cnter == 0 and self._memstats_collector:
            self._memstats_collector.finish_collection()
            self.logger.info(f'model data cuda, {self._memstats_collector.model_data_cuda}')
            self.logger.info(f'non-model data cuda, {self._memstats_collector.non_model_data_cuda}')

        if self._memstats_collector:
            self._memstats_collector.reset_sampling_cnter()
            # cuda margin space = cuda mem capacity - max fwd/bwd cuda mem used.
            # the way to calculate margin space is based on the assumption that
            # model data is fixed in cuda during training.
            # cuda margin space can be used to store OS.
            self._cuda_margin_space = colo_cuda_memory_capacity() - max(self._memstats_collector.overall_cuda)
        self._iter_cnter += 1

    @torch.no_grad()
    def _post_backward_operations(self) -> None:
        """
        The method includes operations required to be processed after backward
        """
        self._update_memstats()

        if self._require_backward_grad_sync:
            # Flush any unreduced buckets in the post_backward stream.
            with torch.cuda.stream(self.comm_stream):
                self.reducer.flush()
            torch.cuda.current_stream().wait_stream(self.comm_stream)
            if self._cpu_offload:
                # Wait for the non-blocking GPU -> CPU grad transfers to finish.
                torch.cuda.current_stream().synchronize()
        self.reducer.free()
        # In case some post bwd hook is not fired
        if self.shard_param:
            tensor_list = []
            for p in self.module.parameters():
                if not p.col_attr.param_is_sharded:
                    tensor_list.append(p.col_attr.sharded_data_tensor)
            self.shard_strategy.shard(tensor_list, self.process_group)
        for p in self.module.parameters():
            p.col_attr.bwd_count = 0
            if not p.requires_grad:
                continue
            # Leave the gradient accumulation state as-is if not synchronizing this pass. This ensures p.grad
            # remains the unsharded gradient accumulated from prior no-sync passes, and _saved_grad_shard
            # remains the sharded gradient from the last synchronized pass. This also allows interleaved no-sync and
            # sync passes, if desired.
            if not self._require_backward_grad_sync:
                continue
            # Write grad back to p.grad and set p.col_attr.grad to None
            # As sharded optimizer only update a shard of param,
            # no matter whether we shard param in sharded model
            # We have to make sure the grad is a flat tensor shard
            # If world size == 1 and sharded param,
            # the shape `grad` is the same as unsharded param
            # So we can just use `view(-1)` to ensure grad is a flat tensor shard
            if self.reuse_fp16_shard:
                grad_payload = p.col_attr.sharded_data_tensor.payload
            else:
                grad_payload = cast_tensor_to_fp32(p.col_attr.fp16_grad)
            if p.col_attr.offload_grad:
                grad_payload.data = grad_payload.data.cpu()
            if p.col_attr.fp32_grad is not None:
                assert not self.reuse_fp16_shard, 'Gradien accumulation is not supported when reuse_fp16_shard=True'
                p.col_attr.fp32_grad.add_(grad_payload.view_as(p.col_attr.fp32_grad))
                grad_payload = p.col_attr.fp32_grad
            p.grad.data = grad_payload
            p.col_attr.fp16_grad = None
            p.col_attr.fp32_grad = None

    @torch.no_grad()
    def _grad_post_backward_hook(self, param: Parameter, grad: torch.Tensor) -> Optional[torch.Tensor]:
        """
        At the start of :func:`_grad_post_backward_hook`, ``param.grad`` contains the
        full gradient for the local batch. The reduce-scatter op will save
        a single shard of the summed gradient across all
        GPUs to param.col_attr.grad. This shard will align with the current GPU rank. For example::

            before reduce_scatter:
                param.grad (GPU #0): [1, 2, 3, 4]
                param.grad (GPU #1): [5, 6, 7, 8]

            after reduce_scatter:
                param.grad (GPU #0): [6, 8]    # 1+5, 2+6
                param.grad (GPU #1): [10, 12]  # 3+7, 4+8

        The local GPU's ``optim.step`` is responsible for updating a single
        shard of params, also corresponding to the current GPU's rank. This
        alignment is created by `param.col_attr.grad`, which ensures that
        the local optimizer only sees the relevant parameter shard.
        """
        if grad is None:
            return
        assert not grad.requires_grad, 'ShardedModel only works with gradients that don\'t require gradients'
        if not self._require_backward_grad_sync:
            return
        self.comm_stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(self.comm_stream):
            new_grad = grad.clone()
            if self.fp32_reduce_scatter:
                new_grad.data = new_grad.data.to(param.dtype)
            if self.gradient_predivide_factor > 1.0:
                # Average grad by world_size for consistency with PyTorch DDP.
                new_grad.data.div_(self.gradient_predivide_factor)
            orig_grad_data = new_grad.data
            if self.world_size > 1:
                grad_chunks = chunk_and_pad(orig_grad_data, self.reduce_scatter_process_group.size())
                self.reducer.reduce_scatter_async(grad_chunks,
                                                  group=self.reduce_scatter_process_group,
                                                  callback_fn=functools.partial(self._reduce_scatter_callback, param))
            else:
                self._reduce_scatter_callback(param, new_grad)
            orig_grad_data.record_stream(self.comm_stream)
        torch.cuda.current_stream().wait_stream(self.comm_stream)
        empty_grad = torch.empty_like(grad)
        free_storage(empty_grad)
        return empty_grad

    def _reduce_scatter_callback(self, param: Parameter, reduced_grad: torch.Tensor) -> None:
        reduced_grad = reduced_grad.view(-1)
        if self.gradient_postdivide_factor > 1:
            # Average grad by world_size for consistency with PyTorch DDP.
            reduced_grad.data.div_(self.gradient_postdivide_factor)
        if self.reuse_fp16_shard:
            param.col_attr.sharded_data_tensor.reset_payload(reduced_grad.data)
            param.col_attr.sharded_data_tensor.is_sharded = True
        else:
            param.col_attr.fp16_grad = reduced_grad.data

    def state_dict(self, destination=None, prefix='', keep_vars=False) -> 'OrderedDict[str, torch.Tensor]':
        self.shard_strategy.gather([p.col_attr.sharded_data_tensor for p in self.module.parameters()],
                                   self.process_group)
        prev_params = {}
        for p in self.module.parameters():
            prev_params[p] = p.data
            p.data = p.col_attr.sharded_data_tensor.payload
        gathered_state_dict = self.module.state_dict(destination, prefix, keep_vars)
        self.shard_strategy.shard([p.col_attr.sharded_data_tensor for p in self.module.parameters()],
                                  self.process_group)
        for p in self.module.parameters():
            p.data = prev_params[p]
        return gathered_state_dict

    def load_state_dict(self, state_dict: 'OrderedDict[str, torch.Tensor]', strict: bool = True):
        raise NotImplementedError

    def __getitem__(self, idx: int):
        assert isinstance(self.module, nn.ModuleList)
        return self.module[idx]

    def __len__(self):
        assert isinstance(self.module, nn.ModuleList)
        return len(self.module)

    def __iter__(self):
        assert isinstance(self.module, nn.ModuleList)
        return iter(self.module)
