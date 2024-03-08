"""
The following example demonstrates how to represent torchrec's embedding
sharding with the DTensor API.
"""
import argparse
import os

import torch

from torch.distributed._tensor import (
    DeviceMesh,
    DTensor,
    init_device_mesh,
    Replicate,
    Shard,
)
from torch.distributed._tensor.debug.visualize_sharding import visualize_sharding


def get_device_type():
    return (
        "cuda"
        if torch.cuda.is_available() and torch.cuda.device_count() >= 4
        else "cpu"
    )


def run_torchrec_row_wise_even_sharding_example(rank, world_size):
    # row-wise even sharding example:
    #   One table is evenly sharded by rows within the global ProcessGroup.
    #   In our example, the table's num_embedding is 8, and the embedding dim is 16
    #   The global ProcessGroup has 4 ranks, so each rank will have one 2 by 16 local
    #   shard.

    # device mesh is a representation of the worker ranks
    # create a 1-D device mesh that includes every rank
    device_type = get_device_type()
    device = torch.device(device_type)
    device_mesh = init_device_mesh(device_type=device_type, mesh_shape=(world_size,))

    # manually create the embedding table's local shards
    num_embeddings = 8
    embedding_dim = 16
    emb_table_shape = torch.Size([num_embeddings, embedding_dim])
    local_shard_shape = torch.Size(
        [num_embeddings // world_size, embedding_dim]  # (local_rows, local_cols)
    )
    # in our case, the embedding table will be sharded row-wisely into 4 local shards
    # and each rank will have 1 of them.
    local_shards = [
        torch.randn(local_shard_shape, device=device) for _ in range(world_size)
    ]
    # row-wise sharding: one shard per rank
    local_shard = local_shards[rank]

    ###########################################################################
    # example 1: transform local_shards into DTensor
    # usage in TorchRec:
    #   ShardedEmbeddingCollection stores model parallel params in
    #   _model_parallel_name_to_sharded_tensor which is initialized in
    #   _initialize_torch_state() and torch.Tensor params are transformed
    #   into ShardedTensor by ShardedTensor._init_from_local_shards().
    #
    #   This allows state_dict() to always return ShardedTensor objects.

    # this is the sharding placement we use in DTensor to represent row-wise sharding
    # row_wise_sharding_placements means that the global tensor is sharded by first dim
    # over the 1-d mesh.
    row_wise_sharding_placements = [Shard(0)]
    # create a DTensor from the local shard
    dtensor = DTensor.from_local(
        local_shard, device_mesh, row_wise_sharding_placements, run_check=False
    )

    # display the DTensor's sharding
    visualize_sharding(dtensor, header="Row-wise even sharding example in DTensor")

    # get the global tensor from the DTensor
    dtensor_full = dtensor.full_tensor()  # torch.Tensor
    # manually compose the global tensor from the local shards
    global_tensor = torch.cat(local_shards, dim=0)
    # we demonstrate that the DTensor constructed has the same
    # global view as the actual global tensor
    assert torch.equal(dtensor_full, global_tensor)

    ###########################################################################
    # example 2: transform DTensor into local_shards
    # usage in TorchRec:
    #   In ShardedEmbeddingCollection's load_state_dict pre hook
    #   _pre_load_state_dict_hook, if the source param is a ShardedTensor
    #   then we need to transform it into its local_shards.

    # transform DTensor into local_shards
    local_shard = dtensor.to_local()

    # another case is that the source param is a torch.Tensor. In this case,
    # the source param is the global tensor rather than shards so we need to
    # splice the global tensor into local shards. This will be identical to
    # existing code in TorchRec.
    local_shard_shape_list = list(local_shard_shape)
    local_shard_spliced = global_tensor[
        local_shard_shape_list[0] * rank : local_shard_shape_list[0] * (rank + 1),
        :,
    ]
    # the local shard obtained from both approaches should be identical
    assert torch.equal(local_shard, local_shard_spliced)

    ###########################################################################
    # example 3: load state dict
    # usage in TorchRec:
    #   In case where the source param and the destination param are both
    #   DTensors, we can directly call DTensor.copy_() to load the state.
    src_dtensor = torch.distributed._tensor.ones(
        emb_table_shape,
        device_mesh=device_mesh,
        placements=row_wise_sharding_placements,
    )
    dtensor.copy_(src_dtensor)
    # these two DTensors should have the same global view after loading
    assert torch.equal(dtensor.full_tensor(), src_dtensor.full_tensor())


def run_torchrec_row_wise_uneven_sharding_example(rank, world_size):
    # row-wise uneven sharding example:
    #   One table is unevenly sharded by rows within the global ProcessGroup.
    #   In our example, the table's num_embedding is 8, and the embedding dim is 16
    #   The global ProcessGroup has 4 ranks, and each rank will have the local shard
    #   of shape:
    #       rank 0: [1, 16]
    #       rank 1: [3, 16]
    #       rank 2: [1, 16]
    #       rank 3: [3, 16]

    # device mesh is a representation of the worker ranks
    # create a 1-D device mesh that includes every rank
    device_type = get_device_type()
    device = torch.device(device_type)
    device_mesh = init_device_mesh(device_type=device_type, mesh_shape=(world_size,))

    # manually create the embedding table's local shards
    num_embeddings = 8
    embedding_dim = 16
    emb_table_shape = torch.Size([num_embeddings, embedding_dim])
    local_shard_shape_list = [
        torch.Size([1, embedding_dim]),
        torch.Size([3, embedding_dim]),
    ]
    local_shards = [
        torch.randn(local_shard_shape_list[i % 2], device=device)
        for i in range(world_size)
    ]
    # row-wise sharding: one shard per rank
    local_shard = local_shards[rank]

    ###########################################################################
    # example 1: transform local_shards into DTensor
    row_wise_sharding_placements = [Shard(0)]
    # note: for uneven sharding, we need to specify the shape and stride because
    # DTensor would assume even sharding and compute shape/stride based on the
    # assumption. Torchrec needs to pass in this information explicitely.
    dtensor = DTensor.from_local(
        local_shard,
        device_mesh,
        row_wise_sharding_placements,
        run_check=False,
        shape=emb_table_shape,  # this is required for uneven sharding
        stride=(embedding_dim, 1),
    )
    # TODO (xilunwu): so far visualize_sharding() cannot print correctly for uneven
    # sharded DTensor because it relies on offset computation which assumes even sharding
    visualize_sharding(dtensor, header="Row-wise uneven sharding example in DTensor")
    # check the dtensor has the correct shape and stride on all ranks
    assert dtensor.shape == emb_table_shape
    assert dtensor.stride() == (embedding_dim, 1)
    assert dtensor.to_local().shape == local_shard.shape

    ###########################################################################
    # example 2: transform DTensor into local_shards
    assert torch.equal(dtensor.to_local(), local_shard)


def run_torchrec_table_wise_sharding_example(rank, world_size):
    # table-wise example:
    #   each rank in the global ProcessGroup holds one different table.
    #   In our example, the table's num_embedding is 8, and the embedding dim is 16
    #   The global ProcessGroup has 4 ranks, so each rank will have one 8 by 16 complete
    #   table as its local shard.

    device_type = get_device_type()
    device = torch.device(device_type)

    # manually create the embedding table's local shards
    num_embeddings = 8
    embedding_dim = 16
    emb_table_shape = torch.Size([num_embeddings, embedding_dim])
    local_shard_shape = emb_table_shape  # each rank holds a complete table
    table_to_local_shard = {}  # map {table_id: local shard of table_id}
    # create 4 embedding tables and place them on different ranks
    # each rank will hold one complete table, and the dict will store
    # the corresponding local shard.
    for i in range(world_size):
        local_shard = torch.randn(*local_shard_shape, device=device)
        # embedding table i is placed on rank i
        table_to_local_shard[i] = (
            local_shard if rank == i else torch.empty(0, device=device)
        )

    ###########################################################################
    # example 1: transform local_shards into DTensor
    table_to_dtensor = {}  # same purpose as _model_parallel_name_to_sharded_tensor
    table_wise_sharding_placements = [Replicate()]  # table-wise sharding

    for table_id, local_shard in table_to_local_shard.items():
        # create a submesh that only contains the rank we place the table
        # note that we cannot use ``init_device_mesh'' to create a submesh
        # so we choose to use the `DeviceMesh` api to directly create a DeviceMesh
        device_submesh = DeviceMesh(
            device_type=device_type,
            mesh=torch.tensor(
                [table_id], dtype=torch.int64
            ),  # table ``table_id`` is placed on rank ``table_id``
        )
        # create a DTensor from the local shard for the current table
        # note: for uneven sharding, we need to specify the shape and stride because
        # DTensor would assume even sharding and compute shape/stride based on the
        # assumption. Torchrec needs to pass in this information explicitely.
        dtensor = DTensor.from_local(
            local_shard,
            device_submesh,
            table_wise_sharding_placements,
            run_check=False,
            shape=emb_table_shape,  # this is required for uneven sharding
            stride=(embedding_dim, 1),
        )
        table_to_dtensor[table_id] = dtensor

    # print each table's sharding
    for table_id, dtensor in table_to_dtensor.items():
        visualize_sharding(
            dtensor,
            header=f"Table-wise sharding example in DTensor for Table {table_id}",
        )
        # check the dtensor has the correct shape and stride on all ranks
        assert dtensor.shape == emb_table_shape
        assert dtensor.stride() == (embedding_dim, 1)

    ###########################################################################
    # example 2: transform DTensor into torch.Tensor
    for table_id, local_shard in table_to_local_shard.items():
        dtensor_local_shard = table_to_dtensor[table_id].to_local()
        assert torch.equal(dtensor_local_shard, local_shard)


def run_example(rank, world_size, example_name):
    # the dict that stores example code
    name_to_example_code = {
        "row-wise-even": run_torchrec_row_wise_even_sharding_example,
        "row-wise-uneven": run_torchrec_row_wise_uneven_sharding_example,
        "table-wise": run_torchrec_table_wise_sharding_example,
    }
    if example_name not in name_to_example_code:
        print(f"example for {example_name} does not exist!")
        return

    # the example to run
    example_func = name_to_example_code[example_name]

    # set manual seed
    torch.manual_seed(0)

    # run the example
    example_func(rank, world_size)


if __name__ == "__main__":
    # this script is launched via torchrun which automatically manages ProcessGroup
    rank = int(os.environ["RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    assert world_size == 4  # our example uses 4 worker ranks
    # parse the arguments
    parser = argparse.ArgumentParser(
        description="torchrec sharding examples",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    example_prompt = (
        "choose one sharding example from below:\n"
        "\t1. row-wise-even;\n"
        "\t2. row-wise-uneven\n"
        "\t3. table-wise\n"
        "e.g. you want to try the row-wise even sharding example, please input 'row-wise-even'\n"
    )
    parser.add_argument("-e", "--example", help=example_prompt, required=True)
    args = parser.parse_args()
    run_example(rank, world_size, args.example)
