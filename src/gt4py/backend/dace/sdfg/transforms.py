import copy
from collections import defaultdict

import dace
from dace import registry, symbolic
from dace.properties import Property, make_properties
from dace.sdfg import SDFG, nodes
from dace.sdfg import utils as sdutils
from dace.transformation.transformation import Transformation


def global_ij_tiling(sdfg, tile_size=(8, 8)):
    input_arrays = dict()
    output_arrays = dict()
    for state in sdfg.nodes():
        for node in state.nodes():
            if isinstance(node, dace.nodes.AccessNode):
                if (
                    node.access is dace.AccessType.ReadOnly
                    or node.access is dace.AccessType.ReadWrite
                ) and not sdfg.arrays[node.data].transient:
                    num_accesses = input_arrays.get(node.data, 0)
                    input_arrays[node.data] = num_accesses + sum(
                        [e.data.num_accesses for e in state.out_edges(node)]
                    )

                if (
                    node.access is dace.AccessType.WriteOnly
                    or node.access is dace.AccessType.ReadWrite
                ) and not sdfg.arrays[node.data].transient:
                    num_accesses = output_arrays.get(node.data, 0)
                    output_arrays[node.data] = num_accesses + sum(
                        [e.data.num_accesses for e in state.in_edges(node)]
                    )

    # nest state
    import copy

    tmp_sdfg = copy.deepcopy(sdfg)
    for s in sdfg.nodes():
        sdfg.remove_node(s)
    state = sdfg.add_state()
    nsdfg_node = state.add_nested_sdfg(
        tmp_sdfg, sdfg, list(input_arrays.keys()), list(output_arrays.keys())
    )
    nsdfg_node.symbol_mapping.update(
        # I=dace.symbolic.pystr_to_symbolic(f"Min({tile_size[0]}, I-tile_i*{tile_size[0]})"),
        # J=dace.symbolic.pystr_to_symbolic(f"Min({tile_size[1]}, J-tile_j*{tile_size[1]})"),
        I=dace.symbolic.pystr_to_symbolic(f"Min({tile_size[0]}, I-tile_i)"),
        J=dace.symbolic.pystr_to_symbolic(f"Min({tile_size[1]}, J-tile_j)"),
    )
    # map
    map_entry, map_exit = state.add_map(
        "global_tiling",
        ndrange=dict(
            # tile_i=f"0:int_ceil(I, {tile_size[0]})", tile_j=f"0:int_ceil(J, {tile_size[1]})"
            tile_i=f"0:I:{tile_size[0]}",
            tile_j=f"0:J:{tile_size[1]}",
        ),
    )
    map_entry.map.collapse = 2

    # conn_id = 0
    for array_name, num_accesses in input_arrays.items():
        array = sdfg.arrays[array_name]

        if not array.transient:
            map_entry.add_in_connector("IN_" + array_name)
            map_entry.add_out_connector("OUT_" + array_name)

            state.add_edge(
                state.add_read(array_name),
                None,
                map_entry,
                "IN_" + array_name,
                # f"IN_{conn_id}",
                memlet=dace.Memlet.simple(
                    array_name,
                    subset_str=",".join(
                        [f"0:{limit}" if str(limit) != "1" else "0" for limit in array.shape]
                    ),
                    num_accesses=num_accesses,
                ),
            )
            from dace.data import Scalar

            if isinstance(array, dace.data.Scalar):
                subset_str = "0"
            else:
                frame_i = dace.symbolic.pystr_to_symbolic(str(array.shape[0]) + "-I")
                frame_j = dace.symbolic.pystr_to_symbolic(str(array.shape[1]) + "-J")
                subset_str = ",".join(
                    [
                        # f"{tile_size[0]}*tile_i:Min({tile_size[0]}*(tile_i+1),I)+{frame_i}",
                        # f"{tile_size[1]}*tile_j:Min({tile_size[1]}*(tile_j+1),J)+{frame_j}",
                        f"tile_i:Min(tile_i+{tile_size[0]},I)+{frame_i}",
                        f"tile_j:Min(tile_j+{tile_size[1]},J)+{frame_j}",
                        f"0:{array.shape[2]}",
                    ]
                )

            state.add_edge(
                map_entry,
                "OUT_" + array_name,
                nsdfg_node,
                array_name,
                memlet=dace.Memlet.simple(
                    array_name, subset_str=subset_str, num_accesses=num_accesses
                ),
            )
        # conn_id += 1
    # conn_id = 0
    for array_name, num_accesses in output_arrays.items():
        array = sdfg.arrays[array_name]

        if not array.transient:
            map_exit.add_in_connector("IN_" + array_name)
            map_exit.add_out_connector("OUT_" + array_name)
            state.add_edge(
                map_exit,
                "OUT_" + array_name,
                state.add_write(array_name),
                None,
                memlet=dace.Memlet.simple(
                    array_name,
                    subset_str=",".join(
                        [f"0:{limit}" if str(limit) != "1" else "0" for limit in array.shape]
                    ),
                    num_accesses=num_accesses,
                ),
            )
            from dace.data import Scalar

            if isinstance(array, dace.data.Scalar):
                subset_str = "0"
            else:
                frame_i = dace.symbolic.pystr_to_symbolic(str(array.shape[0]) + "-I")
                frame_j = dace.symbolic.pystr_to_symbolic(str(array.shape[1]) + "-J")
                subset_str = ",".join(
                    [
                        # f"{tile_size[0]}*tile_i:Min({tile_size[0]+1}*tile_i,I)+{frame_i}",
                        # f"{tile_size[1]}*tile_j:Min({tile_size[1]+1}*tile_j,J)+{frame_j}",
                        f"tile_i:Min(tile_i+{tile_size[0]},I)+{frame_i}",
                        f"tile_j:Min(tile_j+{tile_size[1]},J)+{frame_j}",
                        f"0:{array.shape[2]}",
                    ]
                )

            state.add_edge(
                nsdfg_node,
                array_name,
                map_exit,
                "IN_" + array_name,
                memlet=dace.Memlet.simple(
                    array_name, subset_str=subset_str, num_accesses=num_accesses
                ),
            )

    if len(input_arrays) == 0:
        state.add_edge(map_entry, None, nsdfg_node, None, dace.Memlet())
    if len(output_arrays) == 0:
        state.add_edge(nsdfg_node, None, map_exit, None, dace.Memlet())

    # dace.dtypes.StorageType.register("CPU_Threadprivate_Persistent")
    import sympy

    # symbols = dict(_tile_I=dace.symbol("_tile_I"), _tile_J=dace.symbol("_tile_J"))
    # symbols['_tile_I'].set(tile_size[0])
    # symbols['_tile_J'].set(tile_size[1])
    # tile_sizes = dict(I=tile_size[0], J=tile_size[1], K="K")
    for array_name, array in nsdfg_node.sdfg.arrays.items():
        if array.transient:
            # array.shape = [
            #     f"{tile_sizes[str(s)]}"
            #     if isinstance(s, dace.symbolic.symbol)
            #     else s.subs({a: tile_sizes[str(a)] for a in s.args if str(a) in "IJ"})
            #     for s in array.shape
            # ]
            array.tile_size = tile_size
            # print()
            array.storage = dace.dtypes.StorageType.CPU_ThreadLocal


import dace.sdfg.utils
from dace import nodes
from dace.properties import Property, ShapeProperty, make_properties
from dace.transformation.transformation import Transformation

import gt4py
from gt4py.backend.dace.sdfg import library


@registry.autoregister_params(singlestate=True)
class PruneTransientOutputs(Transformation):

    _library_node = dace.nodes.LibraryNode("")
    _access_node = nodes.AccessNode("")

    @staticmethod
    def expressions():
        return [
            dace.sdfg.utils.node_path_graph(
                PruneTransientOutputs._library_node, PruneTransientOutputs._access_node
            )
        ]

    @staticmethod
    def _overlap(subset_a: dace.memlet.subsets.Subset, subset_b: dace.memlet.subsets.Subset):
        return True

    @staticmethod
    def _check_reads(state: dace.SDFGState, candidate_subset, sorted_accesses):

        for acc in sorted_accesses:
            out_edges = state.out_edges(acc)
            if len(out_edges) == 0:
                assert acc.access == dace.dtypes.AccessType.WriteOnly
            for edge in out_edges:
                if not edge.data.data == acc.data:
                    return False
                if PruneTransientOutputs._overlap(edge.data.subset, candidate_subset):
                    return False
        return True

    @staticmethod
    def can_be_applied(
        graph: dace.sdfg.SDFGState, candidate, expr_index, sdfg: dace.SDFG, strict=False
    ):
        # TODO improvement: state-graphs that are not just sequences
        # TODO improvement: can still apply if read is shadowed by another write

        library_node: dace.nodes.LibraryNode = graph.node(
            candidate[PruneTransientOutputs._library_node]
        )

        if not isinstance(library_node, library.StencilLibraryNode):
            return False
        access_node: dace.nodes.AccessNode = graph.node(
            candidate[PruneTransientOutputs._access_node]
        )

        edges = graph.edges_between(library_node, access_node)
        if len(edges) != 1:
            return False
        candidate_edge = edges[0]
        assert candidate_edge.data.data == access_node.data
        assert access_node.access != dace.dtypes.AccessType.ReadOnly

        candidate_subset = candidate_edge.data.subset
        if not sdfg.arrays[access_node.data].transient:
            return False

        import networkx as nx

        sorted_accesses = [access_node] + [
            node
            for node in nx.algorithms.dag.topological_sort(graph.nx)
            if isinstance(node, dace.nodes.AccessNode) and node.data == access_node.data
        ]

        if not PruneTransientOutputs._check_reads(graph, candidate_subset, sorted_accesses):
            return False

        boundary_states = sdfg.successors(graph)
        visited_states = {graph}
        while len(boundary_states) == 1:
            state = boundary_states[0]
            if state in visited_states:
                return False  # currently only apply if is linear sequence of states.
            visited_states.add(state)
            sorted_accesses = [
                node
                for node in nx.algorithms.dag.topological_sort(state.nx)
                if isinstance(node, dace.nodes.AccessNode) and node.data == access_node.data
            ]

            if not PruneTransientOutputs._check_reads(state, candidate_subset, sorted_accesses):
                return False

            boundary_states = sdfg.successors(state)

        return True

    def apply(self, sdfg: dace.SDFG):
        graph: dace.sdfg.SDFGState = sdfg.nodes()[self.state_id]
        library_node: library.StencilLibraryNode = graph.node(
            self.subgraph[PruneTransientOutputs._library_node]
        )
        access_node: dace.nodes.AccessNode = graph.node(
            self.subgraph[PruneTransientOutputs._access_node]
        )
        edges = graph.edges_between(library_node, access_node)

        in_edge = edges[0]

        data = access_node.data

        library_node.remove_out_connector("OUT_" + data)
        library_node.outputs.remove(data)
        for name, acc in dict(library_node.write_accesses.items()).items():
            if acc.outer_name == data:
                del library_node.write_accesses[name]
        for int in library_node.intervals:
            # if data in int.input_extents:
            #     del int.input_extents[data]
            for state in int.sdfg.nodes():
                tasklets = [n for n in state.nodes() if isinstance(n, dace.nodes.Tasklet)]
                assert len(tasklets) == 1
                tasklet: dace.nodes.Tasklet = tasklets[0]
                remove_connectors = set()
                for conn in tasklet.out_connectors:
                    if conn.startswith(f"_gt_loc_out__{data}_"):
                        remove_connectors.add(conn)
                for conn in remove_connectors:
                    tasklet.remove_out_connector(conn)

                output_accessors = [
                    n
                    for n in state.nodes()
                    if isinstance(n, dace.nodes.AccessNode)
                    and n.access != dace.dtypes.AccessType.ReadOnly
                    and n.data == data
                ]
                assert len(output_accessors) == 1
                acc = output_accessors[0]
                assert acc.access == dace.dtypes.AccessType.WriteOnly
                inner_in_edge = state.in_edges(acc)
                assert len(inner_in_edge) == 1
                state.remove_edge(inner_in_edge[0])
                state.remove_node(acc)
                if (
                    len(
                        [
                            n
                            for n in state.nodes()
                            if isinstance(n, dace.nodes.AccessNode) and n.data == data
                        ]
                    )
                    == 0
                ):
                    int.sdfg.remove_data(data)
        graph.remove_edge(in_edge)
        if access_node.access == dace.dtypes.AccessType.ReadWrite:
            access_node.access = dace.dtypes.AccessType.WriteOnly
        if len(graph.out_edges(access_node)) == 0:
            graph.remove_node(access_node)

        remove = True
        for state in sdfg.nodes():
            for node in state.nodes():
                if isinstance(node, dace.nodes.AccessNode) and node.data == data:
                    remove = False
        if remove:
            sdfg.remove_data(data)


@registry.autoregister_params(singlestate=True)
@make_properties
class TaskletAsKLoop(Transformation):
    """Docstring TODO"""

    _map_entry = nodes.MapEntry(nodes.Map("", [], []))
    _tasklet = nodes.Tasklet("")
    _map_exit = nodes.MapExit(nodes.Map("", [], []))

    # Properties
    init = Property(default=0, desc="initial value for k")
    condition = Property(default="k<K", desc="stopping condition for the loop")
    step = Property(default="k+1", desc="value assigned to k every step (e.g. increment k+1)")

    @staticmethod
    def annotates_memlets():
        return True

    @staticmethod
    def expressions():
        return [
            dace.sdfg.utils.node_path_graph(
                TaskletAsKLoop._map_entry, TaskletAsKLoop._tasklet, TaskletAsKLoop._map_exit
            )
        ]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        return True

    def _k_range(self):
        if "<" in self.condition:
            k_min = self.init
            _, k_max = self.condition.split("<")
            k_max = k_max + " - 1"
        else:
            k_max = str(self.init)
            _, k_min = self.condition.split(">=")
        return k_min, k_max

    def apply(self, sdfg):
        graph: dace.sdfg.SDFGState = sdfg.nodes()[self.state_id]
        map_entry: dace.nodes.MapEntry = graph.nodes()[self.subgraph[TaskletAsKLoop._map_entry]]
        tasklet: dace.nodes.Tasklet = graph.nodes()[self.subgraph[TaskletAsKLoop._tasklet]]
        map_exit: dace.nodes.MapExit = graph.nodes()[self.subgraph[TaskletAsKLoop._map_exit]]

        k_min, k_max = self._k_range()
        # fix outer edges to ij map
        import sympy

        k_symbol = dace.symbolic.symbol("k")
        for e in graph.in_edges(map_entry) + graph.out_edges(map_exit):
            for i, r in enumerate(e.data.subset.ranges):
                e.data.subset.ranges[i] = (
                    r[0].subs(dace.symbolic.symbol("k"), k_min),
                    r[1].subs(dace.symbolic.symbol("k"), k_max),
                    r[2],
                )

        # node = nest_state_subgraph(sdfg, graph, dace.sdfg.ScopeSubgraphView(graph, [tasklet]))
        nsdfg: SDFG = dace.SDFG(f"nested_k_loop_{graph.name}")
        nstate = nsdfg.add_state()
        nstate.add_nodes_from([tasklet])
        # nsdfg.add_nodes_from(dace.sdfg.ScopeSubgraphView(graph, [nstate]))

        in_prefix = f"__in_"
        out_prefix = f"__out_"

        nsdfg_in_arrays = set()
        for e in graph.out_edges(map_entry):
            nsdfg_in_arrays.add(in_prefix + e.data.data)
        nsdfg_out_arrays = set()
        for e in graph.in_edges(map_exit):
            nsdfg_out_arrays.add(out_prefix + e.data.data)

        for name in set(
            n.data
            for n in graph.nodes()
            if isinstance(n, dace.nodes.AccessNode) and n.access == dace.dtypes.AccessType.ReadOnly
        ):
            nsdfg.add_datadesc(in_prefix + name, copy.deepcopy(sdfg.arrays[name]))
        for name in set(
            n.data
            for n in graph.nodes()
            if isinstance(n, dace.nodes.AccessNode)
            and n.access == dace.dtypes.AccessType.WriteOnly
        ):
            nsdfg.add_datadesc(out_prefix + name, copy.deepcopy(sdfg.arrays[name]))

        read_accessors = dict()
        for name in nsdfg_in_arrays:
            read_accessors[name] = nstate.add_read(name)
        write_accessors = dict()
        for name in nsdfg_out_arrays:
            write_accessors[name] = nstate.add_write(name)

        for e in graph.out_edges(map_entry):
            nstate.add_edge(
                read_accessors[in_prefix + e.data.data],
                None,
                tasklet,
                e.dst_conn,
                memlet=dace.Memlet.simple(
                    in_prefix + e.data.data,
                    subset_str=str(e.data.subset),
                    num_accesses=e.data.num_accesses,
                ),
            )
        for e in graph.in_edges(map_exit):
            nstate.add_edge(
                tasklet,
                e.src_conn,
                write_accessors[out_prefix + e.data.data],
                None,
                memlet=dace.Memlet.simple(
                    out_prefix + e.data.data,
                    subset_str=str(e.data.subset),
                    num_accesses=e.data.num_accesses,
                ),
            )

        node = graph.add_nested_sdfg(nsdfg, sdfg, nsdfg_in_arrays, nsdfg_out_arrays)
        nstate = nsdfg.nodes()[0]

        conn_map_entry_to_nsdfg = dict()
        subsets_map_entry_to_nsdfg = dict()
        num_map_entry_to_nsdfg = dict()
        for e in graph.out_edges(map_entry):
            conn_map_entry_to_nsdfg[e.src_conn] = e.data.data

            subset = subsets_map_entry_to_nsdfg.get(e.data.data, e.data.subset)
            num = num_map_entry_to_nsdfg.get(e.data.data, e.data.num_accesses)
            for i, r in enumerate(subset.ranges):
                if "i" in dace.symbolic.symlist(r) or "j" in dace.symbolic.symlist(r):
                    subset.ranges[i] = (
                        min(subset.ranges[i][0], e.data.subset[i][0]),
                        max(subset.ranges[i][1], e.data.subset[i][1]),
                        1,
                    )
                elif "k" in dace.symbolic.symlist(r):
                    subset.ranges[i] = (
                        0,
                        dace.symbolic.pystr_to_symbolic("K-1"),
                        1,
                    )  # graph.edges_between(
                    #     [
                    #         n
                    #         for n in graph.nodes()
                    #         if isinstance(n, dace.nodes.AccessNode)
                    #         and n.access == dace.AccessType.ReadOnly
                    #         and n.data == e.data.data
                    #     ][0],
                    #     map_entry,
                    # )[0].data.subset.ranges[i]
                subsets_map_entry_to_nsdfg[e.data.data] = subset
                num_map_entry_to_nsdfg[e.data.data] = num + e.data.num_accesses

        conn_map_exit_to_nsdfg = dict()
        for e in graph.in_edges(map_exit):
            conn_map_exit_to_nsdfg[e.dst_conn] = e.data.data

        for conn in map_entry.out_connectors:
            data_name = conn_map_entry_to_nsdfg[conn]
            graph.add_edge(
                map_entry,
                conn,
                node,
                in_prefix + conn_map_entry_to_nsdfg[conn],
                memlet=dace.Memlet.simple(
                    data=data_name,
                    subset_str=str(subsets_map_entry_to_nsdfg[data_name]),
                    num_accesses=num_map_entry_to_nsdfg[data_name],
                ),
            )

        conn_nsdfg_to_map_exit = dict()
        subsets_nsdfg_to_map_exit = dict()
        num_nsdfg_to_map_exit = dict()
        for e in graph.in_edges(map_exit):
            conn_nsdfg_to_map_exit[e.dst_conn] = e.data.data

            subset = subsets_nsdfg_to_map_exit.get(e.data.data, e.data.subset)
            num = num_nsdfg_to_map_exit.get(e.data.data, e.data.num_accesses)
            for i, r in enumerate(subset.ranges):
                if "i" in dace.symbolic.symlist(r) or "j" in dace.symbolic.symlist(r):
                    subset.ranges[i] = (
                        min(subset.ranges[i][0], e.data.subset[i][0]),
                        max(subset.ranges[i][1], e.data.subset[i][1]),
                        1,
                    )
                elif "k" in dace.symbolic.symlist(r):
                    subset.ranges[i] = (
                        0,
                        dace.symbolic.pystr_to_symbolic("K-1"),
                        1,
                    )  # graph.edges_between(
                    #     map_exit,
                    #     [
                    #         n
                    #         for n in graph.nodes()
                    #         if isinstance(n, dace.nodes.AccessNode)
                    #         and n.access == dace.AccessType.WriteOnly
                    #         and n.data == e.data.data
                    #     ][0],
                    # )[0].data.subset.ranges[i]
                subsets_nsdfg_to_map_exit[e.data.data] = subset
                num_nsdfg_to_map_exit[e.data.data] = num + e.data.num_accesses
        for conn in map_exit.in_connectors:
            data_name = conn_nsdfg_to_map_exit[conn]
            graph.add_edge(
                node,
                out_prefix + conn_map_exit_to_nsdfg[conn],
                map_exit,
                conn,
                memlet=dace.Memlet.simple(
                    data=data_name,
                    subset_str=str(subsets_nsdfg_to_map_exit[data_name]),
                    num_accesses=num_nsdfg_to_map_exit[data_name],
                ),
            )
        for e in graph.in_edges(map_entry) + graph.out_edges(map_exit):
            if len(e.data.subset.ranges) >= 3 and "k" in dace.symbolic.symlist(
                e.data.subset.ranges[2]
            ):
                e.data.subset.ranges[2] = (0, dace.symbolic.pystr_to_symbolic("K-1"), 1)

        for e in nstate.in_edges(tasklet):
            outer_subset = subsets_map_entry_to_nsdfg[e.data.data[len(in_prefix) :]]
            for i, r in enumerate(e.data.subset.ranges):
                if "i" in dace.symbolic.symlist(r) or "j" in dace.symbolic.symlist(r):
                    e.data.subset.ranges[i] = (
                        r[0] - outer_subset.ranges[i][0],
                        r[1] - outer_subset.ranges[i][0],
                        1,
                    )

        for e in nstate.out_edges(tasklet):
            outer_subset = subsets_nsdfg_to_map_exit[e.data.data[len(out_prefix) :]]
            for i, r in enumerate(e.data.subset.ranges):
                if "i" in dace.symbolic.symlist(r) or "j" in dace.symbolic.symlist(r):
                    e.data.subset.ranges[i] = (
                        r[0] - outer_subset.ranges[i][0],
                        r[1] - outer_subset.ranges[i][0],
                        1,
                    )

        # Create a loop inside the nested SDFG
        nsdfg.add_loop(None, nstate, None, "k", self.init, self.condition, self.step)
        graph.remove_node(tasklet)
        # outer_in_edges = {e.dst_conn: e for e in graph.in_edges(node)}
        # outer_out_edges = {e.src_conn: e for e in graph.out_edges(node)}
        #
        # for e in nstate.in_edges(tasklet):
        #     assert all(r == (0, 0, 1) for r in e.data.subset.ranges)
        #     assert e.src.data in outer_in_edges
        #     outer_edge = outer_in_edges[e.src.data]
        #     for i, r in enumerate(outer_edge.data.subset.ranges):
        #         e.data.subset.ranges[i] = r
        #
        # for e in nstate.out_edges(tasklet):
        #     assert all(r == (0, 0, 1) for r in e.data.subset.ranges)
        #     assert e.dst.data in outer_out_edges
        #     outer_edge = outer_out_edges[e.dst.data]
        #     for i, r in enumerate(outer_edge.data.subset.ranges):
        #         e.data.subset.ranges[i] = r

        #     e.data.subset.ranges[i] = r
        # if len(e.data.subset.ranges) > 2:
        #     e.data.subset.ranges[2] = (
        #         dace.symbolic.pystr_to_symbolic("k"),
        #         dace.symbolic.pystr_to_symbolic("k"),
        #         dace.symbolic.pystr_to_symbolic("1"),
        #     )


from dace.transformation.interstate.loop_detection import DetectLoop
from dace.transformation.interstate.loop_unroll import LoopUnroll


class EnhancedDetectLoop(DetectLoop):
    """Detects a for-loop construct from an SDFG, with added utility function for finding
    context states."""

    def _get_context_subgraph(self, sdfg):
        # Obtain loop information
        guard: dace.SDFGState = sdfg.node(self.subgraph[DetectLoop._loop_guard])
        begin: dace.SDFGState = sdfg.node(self.subgraph[DetectLoop._loop_begin])
        after_state: dace.SDFGState = sdfg.node(self.subgraph[DetectLoop._exit_state])

        # Obtain iteration variable, range, and stride
        guard_inedges = sdfg.in_edges(guard)
        condition_edge = sdfg.edges_between(guard, begin)[0]
        itervar = list(guard_inedges[0].data.assignments.keys())[0]
        condition = condition_edge.data.condition_sympy()
        rng = LoopUnroll._loop_range(itervar, guard_inedges, condition)

        # Find the state prior to the loop
        if rng[0] == symbolic.pystr_to_symbolic(guard_inedges[0].data.assignments[itervar]):
            before_state: dace.SDFGState = guard_inedges[0].src
            last_state: dace.SDFGState = guard_inedges[1].src
        else:
            before_state: dace.SDFGState = guard_inedges[1].src
            last_state: dace.SDFGState = guard_inedges[0].src

        return guard, begin, last_state, before_state, after_state


from dace import symbolic


@registry.autoregister
@make_properties
class RemoveTrivialLoop(EnhancedDetectLoop):

    count = 1

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        if not EnhancedDetectLoop.can_be_applied(graph, candidate, expr_index, sdfg, strict):
            return False

        guard = graph.node(candidate[DetectLoop._loop_guard])
        begin = graph.node(candidate[DetectLoop._loop_begin])

        # Obtain iteration variable, range, and stride
        guard_inedges = graph.in_edges(guard)
        condition_edge = graph.edges_between(guard, begin)[0]
        itervar = list(guard_inedges[0].data.assignments.keys())[0]
        condition = condition_edge.data.condition_sympy()

        # If loop cannot be detected, fail
        rng = LoopUnroll._loop_range(itervar, guard_inedges, condition)
        if not rng:
            return False

        start, end, step = rng

        try:
            return bool(start == end)
        except TypeError:
            return False

    def apply(self, sdfg):
        guard, first_state, last_state, before_state, after_state = self._get_context_subgraph(
            sdfg
        )
        # guard_inedges = sdfg.in_edges(guard)
        # condition_edge = sdfg.edges_between(guard, first_state)[0]
        # itervar = list(guard_inedges[0].data.assignments.keys())[0]
        # condition = condition_edge.data.condition_sympy()

        init_edges = sdfg.edges_between(before_state, guard)
        assert len(init_edges) == 1
        init_edge = init_edges[0]
        sdfg.add_edge(
            before_state,
            first_state,
            dace.InterstateEdge(
                condition=init_edge.data.condition, assignments=init_edge.data.assignments
            ),
        )
        sdfg.remove_edge(init_edge)
        # add edge from pred directly to loop states

        # sdfg.add_edge(before_state, first_state, dace.InterstateEdge(assignments=init_edge.assignments))
        exit_edge = sdfg.edges_between(last_state, guard)[0]
        sdfg.add_edge(
            last_state, after_state, dace.InterstateEdge(assignments=exit_edge.data.assignments)
        )
        sdfg.remove_edge(exit_edge)

        # remove guard
        sdfg.remove_edge(sdfg.edges_between(guard, first_state)[0])
        sdfg.remove_edge(sdfg.edges_between(guard, after_state)[0])
        sdfg.remove_node(guard)


#
# def eliminate_trivial_k_loop(sdfg: dace.SDFG, state: dace.SDFGState):
#     sdfg.predecessor_states(state)
#     if not len(sdfg.successors(state)) == 2:
#         return
#     if not len(sdfg.predecessors(state)) == 2:
#         return
#     init, condition, step = None, None, None
#     for s in sdfg.predecessors(state):
#         edges = sdfg.edges_between(s, state)
#         if not len(edges) == 1:
#             return
#         if edges[0].data.condition.as_string == "" and s in sdfg.predecessor_states(state):
#             init = edges[0].data.assignments["k"]
#             init_state = s
#         elif not edges[0].data.condition.as_string == "":
#             return
#         else:
#             step = edges[0].data.assignments["k"]
#             loop_end_state = s
#     for s in sdfg.successors(state):
#         edges = sdfg.edges_between(state, s)
#         if edges:
#             if not len(edges) == 1:
#                 return
#             if not edges[0].data.condition.as_string == "":
#                 condition = edges[0].data.condition
#                 loop_start_state = s
#             else:
#                 exit_state = s
#
#     if "<" in condition.as_string:
#         k_min = init
#         _, k_max = condition.as_string.split("<")
#         k_max = k_max + " - 1"
#     else:
#         k_max = str(init)
#         _, k_min = condition.as_string.split(">=")
#
#     if not dace.symbolic.pystr_to_symbolic(f"({k_min})-({k_max})") == 0:
#         return
#
#     # add edge from pred directly to loop states
#     sdfg.add_edge(init_state, loop_start_state, dace.InterstateEdge(assignments={"k": init}))
#     # add edge from loop states directly to succ
#     sdfg.add_edge(loop_end_state, exit_state, dace.InterstateEdge())
#     # remove guard & edges involving guard
#     for s in sdfg.successors(state):
#         for edge in sdfg.edges_between(state, s):
#             sdfg.remove_edge(edge)
#     for s in sdfg.predecessors(state):
#         for edge in sdfg.edges_between(s, state):
#             sdfg.remove_edge(edge)
#     sdfg.remove_node(state)


def outer_k_loop_to_inner_map(sdfg: dace.SDFG, state: dace.SDFGState):
    sdfg.predecessor_states(state)
    if not len(sdfg.successors(state)) == 2:
        return
    if not len(sdfg.predecessors(state)) == 2:
        return
    init, condition, step = None, None, None
    for s in sdfg.predecessors(state):
        edges = sdfg.edges_between(s, state)
        if not len(edges) == 1:
            return
        if edges[0].data.condition.as_string == "" and s in sdfg.predecessor_states(state):
            init = edges[0].data.assignments["k"]
            init_state = s
        elif not edges[0].data.condition.as_string == "":
            return
        else:
            step = edges[0].data.assignments["k"]
            loop_end_state = s
    for s in sdfg.successors(state):
        edges = sdfg.edges_between(state, s)
        if edges:
            if not len(edges) == 1:
                return
            if not edges[0].data.condition.as_string == "":
                condition = edges[0].data.condition
                loop_start_state = s
            else:
                exit_state = s
    # for state in loop...
    loop_states = []
    s = loop_start_state
    while s is not state:
        if not len(sdfg.successors(s)) == 1:
            return
        else:
            loop_states.append(s)
            s = sdfg.successors(s)[0]
    assert loop_end_state is loop_states[-1]

    # replace tasklet with nestedsdfg
    for s in loop_states:
        sdfg.apply_transformations(
            TaskletAsKLoop,
            states=[s],
            validate=False,
            options=dict(init=init, step=step, condition=condition.as_string),
        )
    # add edge from pred directly to loop states
    sdfg.add_edge(init_state, loop_start_state, dace.InterstateEdge())
    # add edge from loop states directly to succ
    sdfg.add_edge(loop_end_state, exit_state, dace.InterstateEdge())
    # remove guard & edges involving guard
    for s in sdfg.successors(state):
        for edge in sdfg.edges_between(state, s):
            sdfg.remove_edge(edge)
    for s in sdfg.predecessors(state):
        for edge in sdfg.edges_between(s, state):
            sdfg.remove_edge(edge)
    sdfg.remove_node(state)


from dace.transformation.interstate import LoopPeeling


@registry.autoregister
@make_properties
class AlwaysApplyLoopPeeling(LoopPeeling):
    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        return True


@registry.autoregister
@make_properties
class PrefetchingKCachesTransform(Transformation):
    _nsdfg_node = dace.nodes.LibraryNode("")

    storage_type = dace.properties.Property(
        dtype=dace.dtypes.StorageType,
        default=dace.dtypes.StorageType.Default,
        desc="the StorageType of local buffers",
    )
    arrays = dace.properties.Property(
        dtype=list, default=None, allow_none=True, desc="The arrays to apply the trafo on."
    )

    @staticmethod
    def expressions():
        return [dace.sdfg.utils.node_path_graph(PrefetchingKCachesTransform._nsdfg_node)]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        return True

    def apply(self, sdfg):
        graph: dace.sdfg.SDFGState = sdfg.nodes()[self.state_id]
        nsdfg = graph.node(self.subgraph[PrefetchingKCachesTransform._nsdfg_node])
        apply_count = 0
        for state in nsdfg.sdfg.nodes():
            for node in state.nodes():
                if isinstance(node, dace.nodes.NestedSDFG):
                    kcache_subgraph = {
                        PrefetchingKCachesTransform._nsdfg_node: state.node_id(node)
                    }
                    trafo = PrefetchingKCachesTransform(
                        nsdfg.sdfg.sdfg_id, nsdfg.sdfg.node_id(state), kcache_subgraph, 0
                    )
                    trafo.storage_type = dace.dtypes.StorageType.CPU_Heap
                    trafo.apply(nsdfg.sdfg)
                    apply_count += 1

        if apply_count > 0:
            return
        nsdfg_node = nsdfg
        names = dict()
        for name, array in dict(nsdfg_node.sdfg.arrays).items():
            if self.arrays is not None and name not in self.arrays:
                continue
            store = False
            outer_edges = [e for e in graph.out_edges(nsdfg_node) if e.data.data == name]
            for edge in outer_edges:
                path = graph.memlet_path(edge)
                if not (
                    isinstance(path[-1].dst, dace.nodes.AccessNode)
                    and path[-1].dst.access != dace.dtypes.AccessType.ReadOnly
                    and len(graph.out_edges(path[-1].dst)) == 0
                    and sdfg.arrays[name].transient
                ):
                    store = True
            names[name] = store

            if not store:
                remove_edges = set()
                remove_nodes = set()
                for edge in outer_edges:
                    path = graph.memlet_path(edge)
                    for e in path:
                        remove_edges.add(e)
                    remove_nodes.add(path[-1].dst)
                for edge in remove_edges:
                    graph.remove_edge_and_connectors(edge)
                for node in remove_nodes:
                    graph.remove_node(node)

        if self.arrays is None:
            array_list = list(name for name in names.keys() if not name.startswith("__tmp"))
        else:
            array_list = self.arrays
        apply_count = nsdfg_node.sdfg.apply_transformations(
            PrefetchFieldTransform,
            options={
                "storage_type": self.storage_type,
                "arrays": list(n for n in names.keys() if n in array_list),
                "store": list(k for k, v in names.items() if v),
            },
            validate=False,
        )


@registry.autoregister
@make_properties
class PrefetchFieldTransform(LoopPeeling):

    storage_type = dace.properties.Property(
        dtype=dace.dtypes.StorageType,
        default=dace.dtypes.StorageType.Default,
        desc="the StorageType of local buffers",
    )

    arrays = dace.properties.Property(
        dtype=list, default=None, allow_none=True, desc="List of  names of the array to prefetch"
    )

    store = dace.properties.Property(
        dtype=list,
        default=[],
        desc="List of arrays for which to write out the results to the original array",
    )

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        if not DetectLoop.can_be_applied(graph, candidate, expr_index, sdfg, strict):
            return False

        guard = graph.node(candidate[DetectLoop._loop_guard])
        begin = graph.node(candidate[DetectLoop._loop_begin])

        # Obtain iteration variable, range, and stride
        guard_inedges = graph.in_edges(guard)
        condition_edge = graph.edges_between(guard, begin)[0]
        itervar = list(guard_inedges[0].data.assignments.keys())[0]
        condition = condition_edge.data.condition_sympy()

        # If loop cannot be detected, fail
        rng = LoopUnroll._loop_range(itervar, guard_inedges, condition)
        if not rng:
            return False

        return True

    def collect_subset_info(self, state, name, var_idx):
        in_subsets = set()
        out_subsets = set()
        for edge in state.edges():
            if isinstance(edge.dst, dace.nodes.CodeNode) and edge.data.data == name:
                in_subsets.add(copy.deepcopy(edge.data.subset))
            if (
                isinstance(edge.dst, dace.nodes.AccessNode)
                and edge.dst.access == dace.dtypes.AccessType.ReadWrite
                and edge.data.data == name
            ):
                in_subsets.add(copy.deepcopy(edge.data.subset))
            if isinstance(edge.src, dace.nodes.CodeNode) and edge.data.data == name:
                out_subsets.add(copy.deepcopy(edge.data.subset))
            if (
                isinstance(edge.src, dace.nodes.AccessNode)
                and edge.src.access == dace.dtypes.AccessType.ReadWrite
                and edge.data.data == name
            ):
                out_subsets.add(copy.deepcopy(edge.data.subset))

        outer_in_subsets = set()
        outer_out_subsets = set()

        for edge in state.edges():
            if (
                isinstance(edge.src, dace.nodes.AccessNode)
                and edge.src.access == dace.dtypes.AccessType.ReadOnly
                and edge.data.data == name
            ):
                outer_in_subsets.add(copy.deepcopy(edge.data.subset))
            if (
                isinstance(edge.dst, dace.nodes.AccessNode)
                and edge.dst.access == dace.dtypes.AccessType.WriteOnly
                and edge.data.data == name
            ):
                outer_out_subsets.add(copy.deepcopy(edge.data.subset))

        indices = set(subs.ranges[var_idx][0] for subs in in_subsets | out_subsets)
        indices |= set(subs.ranges[var_idx][1] for subs in in_subsets | out_subsets)
        length = max(indices) - min(indices) + 3

        outer_in_subset = None
        if len(outer_in_subsets) > 0:
            from dace import subsets

            outer_in_subset = next(iter(outer_in_subsets))
            for subset in outer_in_subsets:
                outer_in_subset = dace.subsets.union(outer_in_subset, subset)
            for i in range(3):
                if i != var_idx:
                    for subset in in_subsets:
                        subset.ranges[i] = outer_in_subset.ranges[i]

        outer_out_subset = None
        if len(outer_out_subsets) > 0:
            from dace import subsets

            outer_out_subset = next(iter(outer_out_subsets))
            for subset in outer_out_subsets:
                outer_out_subset = dace.subsets.union(outer_out_subset, subset)
            for i in range(3):
                if i != var_idx:
                    for subset in out_subsets:
                        subset.ranges[i] = outer_out_subset.ranges[i]

        if len(out_subsets) == 1:
            subset = next(iter(out_subsets))
            assert subset.ranges[var_idx][0] == subset.ranges[var_idx][1]
            write_idx = subset.ranges[var_idx][0]
        else:
            assert len(out_subsets) == 0
            write_idx = None

        if outer_in_subset is not None and outer_out_subset is not None:
            unified_subset = dace.subsets.union(outer_in_subset, outer_out_subset)
        else:
            unified_subset = outer_in_subset or outer_out_subset

        min_idx = unified_subset[var_idx][0]
        max_idx = unified_subset[var_idx][1]

        return dict(
            length=length,
            unified_subset=unified_subset,
            in_subsets=in_subsets,
            out_subsets=out_subsets,
            min_idx=min_idx,
            max_idx=max_idx,
            write_idx=write_idx,
        )

    @classmethod
    def specialize_subset(cls, subset, var_idx, itervar_sym, itervar_value):
        subset = copy.deepcopy(subset)
        ranges = list(subset.ranges[var_idx])
        for idx, sym_expr in enumerate(ranges):
            ranges[idx] = sym_expr.subs(itervar_sym, itervar_value)
        subset.ranges[var_idx] = tuple(ranges)
        return subset

    def add_prefetch_all(
        self, state, name, itervar, itervar_value, var_idx, subset_info, stride, offset
    ):
        if not subset_info["in_subsets"]:
            return

        import dace.subsets

        itervar_sym = dace.symbolic.pystr_to_symbolic(itervar)
        read_acc = state.add_read(name)
        write_acc = state.add_write(f"_loc_buf_{name}")

        outer_subset = self.specialize_subset(
            subset_info["unified_subset"], var_idx, itervar_sym, itervar_value
        )

        local_subset = copy.deepcopy(subset_info["unified_subset"])
        ranges = list(local_subset.ranges[var_idx])
        min_idx = ranges[0]
        ranges[0] = 1
        ranges[1] += 1 - min_idx
        local_subset.ranges[var_idx] = tuple(ranges)

        state.add_edge(
            read_acc,
            None,
            write_acc,
            None,
            dace.Memlet.simple(
                name, subset_str=str(outer_subset), other_subset_str=str(local_subset)
            ),
        )

    def add_store_all(
        self, state, name, itervar, itervar_value, var_idx, subset_info, stride, offset
    ):
        if not subset_info["out_subsets"]:
            return

        import dace
        import dace.subsets

        itervar_sym = dace.symbolic.pystr_to_symbolic(itervar)
        write_acc = state.add_write(name)
        read_acc = state.add_read(f"_loc_buf_{name}")

        out_subset = copy.deepcopy(next(iter(subset_info["out_subsets"])))
        write_idx = out_subset.ranges[var_idx][0]

        subset = self.specialize_subset(out_subset, var_idx, itervar_sym, itervar_value)

        other_subset = copy.deepcopy(out_subset)
        other_ranges = (
            write_idx - subset_info["min_idx"] + 1 + offset,
            write_idx - subset_info["min_idx"] + 1 + offset,
            1,
        )
        other_subset.ranges[var_idx] = other_ranges

        state.add_edge(
            read_acc,
            None,
            write_acc,
            None,
            dace.Memlet.simple(
                f"_loc_buf_{name}", other_subset_str=str(subset), subset_str=str(other_subset)
            ),
        )

    def add_shift_local(
        self,
        state: dace.SDFGState,
        name,
        itervar,
        itervar_value,
        var_idx,
        subset_info,
        stride,
        offset,
    ):
        sdfg: dace.SDFG = state.parent
        read_access = state.add_read(f"_loc_buf_{name}")
        tmp_access = state.add_access(f"_tmp_loc_buf_{name}")
        write_access = state.add_read(f"_loc_buf_{name}")
        sdfg.add_datadesc(f"_tmp_loc_buf_{name}", sdfg.arrays[f"_loc_buf_{name}"])
        state.add_edge(
            read_access,
            None,
            tmp_access,
            None,
            memlet=dace.Memlet.simple(
                f"_loc_buf_{name}",
                subset_str=",".join(f"0:{s}" for s in sdfg.arrays[f"_loc_buf_{name}"].shape),
            ),
        )

        in_subset = subset_info["unified_subset"]
        if stride > 0:
            in_subset.ranges[var_idx] = (1, subset_info["length"] - 1, 1)
        else:
            in_subset.ranges[var_idx] = (0, subset_info["length"] - 2, 1)
        other_subset = copy.deepcopy(in_subset)
        other_subset.offset(stride, True, {var_idx})
        state.add_edge(
            tmp_access,
            None,
            write_access,
            None,
            memlet=dace.Memlet.simple(
                f"_tmp_loc_buf_{name}",
                subset_str=str(in_subset),
                other_subset_str=str(other_subset),
            ),
        )

    def localize_tasklet_memlets(
        self, state, name, itervar, itervar_value, var_idx, subset_info, stride, offset
    ):
        itervar_sym = dace.symbolic.pystr_to_symbolic(itervar)

        min_idx = None
        for node in [
            n
            for n in state.nodes()
            if isinstance(n, (dace.nodes.CodeNode, dace.nodes.EntryNode))
            or (
                isinstance(n, dace.nodes.AccessNode)
                and n.access == dace.dtypes.AccessType.ReadWrite
            )
        ]:
            for edge in [e for e in state.in_edges(node) if e.data.data == name]:
                if min_idx is None or min_idx > edge.data.subset.ranges[var_idx][0]:
                    min_idx = edge.data.subset.ranges[var_idx][0]
        for node in [
            n for n in state.nodes() if isinstance(n, (dace.nodes.CodeNode, dace.nodes.ExitNode))
        ]:
            for edge in [e for e in state.out_edges(node) if e.data.data == name]:
                if min_idx is None or min_idx > edge.data.subset.ranges[var_idx][0]:
                    min_idx = edge.data.subset.ranges[var_idx][0]

        # node.data = f"_loc_buf_{name}"

        # change write from tasklet

        # edges = []
        # for node in state.nodes():
        #     # if isinstance(
        #     #     node, (dace.nodes.CodeNode, dace.nodes.ExitNode, dace.nodes.EntryNode)
        #     # ) or (
        #     #     isinstance(node, dace.nodes.AccessNode)
        #     #     and node.access != dace.dtypes.AccessType.ReadWrite
        #     # ):
        #     #     edges.extend([e for e in state.out_edges(node) if e.data.data == name])
        #     #     edges.extend([e for e in state.in_edges(node) if e.data.data == name])
        #     # # if isinstance(node, (dace.nodes.CodeNode, dace.nodes.EntryNode)) or (
        #     # #     isinstance(node, dace.nodes.AccessNode)
        #     # #     and node.access == dace.dtypes.AccessType.ReadOnly
        #     # # ):
        #     # #     edges.extend([e for e in state.in_edges(node) if e.data.data == name])
        # edges = set(edges)
        for edge in (e for e in state.edges() if e.data.data == name):
            edge.data.data = f"_loc_buf_{name}"
            if (
                isinstance(edge.src, dace.nodes.AccessNode)
                and edge.src.access == dace.AccessType.ReadOnly
            ):
                edge.src.data = f"_loc_buf_{name}"
            if (
                isinstance(edge.dst, dace.nodes.AccessNode)
                and edge.dst.access == dace.AccessType.WriteOnly
            ):
                edge.dst.data = f"_loc_buf_{name}"
            ranges = list(edge.data.subset.ranges[var_idx])
            ranges[0] += offset - min_idx
            ranges[1] += offset - min_idx
            edge.data.subset.ranges[var_idx] = tuple(ranges)

    def add_prefetch(
        self, state, name, itervar, itervar_value, var_idx, subset_info, stride, offset
    ):
        if not subset_info["in_subsets"]:
            return
        if subset_info["write_idx"]:
            in_indices = [subset.ranges[var_idx][0] for subset in subset_info["in_subsets"]]
            if (stride > 0 and max(in_indices) < subset_info["write_idx"]) or (
                stride < 0 and min(in_indices) > subset_info["write_idx"]
            ):
                return
        import dace

        itervar_sym = dace.symbolic.pystr_to_symbolic(itervar)
        itervar_value = dace.symbolic.pystr_to_symbolic(itervar_value)

        write_acc = state.add_write(f"_loc_buf_{name}")
        read_acc = state.add_read(name)
        #
        # newest_subset = next(iter(subset_info["in_subsets"]))
        # for subset in subset_info["in_subsets"]:
        #     if (stride > 0 and subset.ranges[var_idx][0] > newest_subset.ranges[var_idx][0]) or (
        #         stride < 0 and subset.ranges[var_idx][0] < newest_subset.ranges[var_idx][0]
        #     ):
        #         newest_subset = subset

        # import dace.subsets
        #
        # for subset in subset_info["in_subsets"]:
        #     if subset.ranges[var_idx][0] == newest_subset.ranges[var_idx][0]:
        #         newest_subset = dace.subsets.union(newest_subset, subset)
        if stride > 0:
            load_idx = subset_info["max_idx"] - subset_info["min_idx"] + 2 + offset
        else:
            load_idx = offset
        ranges = list(subset_info["unified_subset"].ranges[var_idx])
        ranges[0] = load_idx
        ranges[1] = load_idx
        subset = copy.deepcopy(subset_info["unified_subset"])
        subset.ranges[var_idx] = tuple(ranges)

        ranges = list(subset_info["unified_subset"].ranges[var_idx])
        ranges[0] = subset_info["max_idx"]
        ranges[1] = subset_info["max_idx"]
        newest_subset = copy.deepcopy(subset_info["unified_subset"])
        newest_subset.ranges[var_idx] = tuple(ranges)

        outer_subset = self.specialize_subset(
            newest_subset, var_idx, itervar_sym, itervar_value + stride
        )
        # if stride > 0:
        #     load_idx = subset_info["max_idx"] - subset_info["min_idx"] + 2 + offset
        # else:
        #     load_idx = offset

        # local_subset = copy.deepcopy(newest_subset)
        # local_ranges = list(local_subset.ranges[var_idx])
        # local_ranges[0] = load_idx
        # local_ranges[1] = load_idx
        # local_subset.ranges[var_idx] = tuple(local_ranges)
        # # local_subset = self.specialize_subset(local_subset, var_idx, itervar_sym, itervar_value)

        state.add_edge(
            read_acc,
            None,
            write_acc,
            None,
            dace.Memlet.simple(name, subset_str=str(outer_subset), other_subset_str=str(subset)),
        )

    def add_store(self, state, name, itervar, itervar_value, var_idx, subset_info, stride, offset):
        if not subset_info["out_subsets"]:
            return
        import dace

        itervar_sym = dace.symbolic.pystr_to_symbolic(itervar)
        itervar_value = dace.symbolic.pystr_to_symbolic(itervar_value)

        write_acc = state.add_write(name)
        read_acc = state.add_read(f"_loc_buf_{name}")

        assert len(subset_info["out_subsets"]) == 1
        subset = copy.deepcopy(next(iter(subset_info["out_subsets"])))
        outer_subset = self.specialize_subset(subset, var_idx, itervar_sym, itervar_value - stride)

        load_idx = subset_info["write_idx"] - subset_info["min_idx"] - stride + 1 + offset

        # else:
        #     load_idx = offset
        # store_idx = (subset_info["max_idx"] if stride > 0 else subset_info["min_idx"]) + offset

        local_subset = copy.deepcopy(subset)
        local_ranges = list(local_subset.ranges[var_idx])
        local_ranges[0] = load_idx
        local_ranges[1] = load_idx
        local_subset.ranges[var_idx] = tuple(local_ranges)
        # local_subset = self.specialize_subset(local_subset, var_idx, itervar_sym, 0)

        state.add_edge(
            read_acc,
            None,
            write_acc,
            None,
            dace.Memlet.simple(
                f"_loc_buf_{name}",
                subset_str=str(local_subset),
                other_subset_str=str(outer_subset),
            ),
        )

    def apply(self, sdfg):
        ####################################################################
        # Obtain loop information
        guard: dace.SDFGState = sdfg.node(self.subgraph[DetectLoop._loop_guard])
        begin: dace.SDFGState = sdfg.node(self.subgraph[DetectLoop._loop_begin])
        after_state: dace.SDFGState = sdfg.node(self.subgraph[DetectLoop._exit_state])

        # Obtain iteration variable, range, and stride
        guard_inedges = sdfg.in_edges(guard)
        condition_edge = sdfg.edges_between(guard, begin)[0]
        not_condition_edge = sdfg.edges_between(guard, after_state)[0]

        itervar = next(iter(sdfg.in_edges(guard)[0].data.assignments))
        itervar_sym = dace.symbolic.pystr_to_symbolic(itervar)
        condition = condition_edge.data.condition_sympy()
        rng = self._loop_range(itervar, guard_inedges, condition)
        # Find the state prior to the loop
        if rng[0] == symbolic.pystr_to_symbolic(guard_inedges[0].data.assignments[itervar]):
            init_edge: dace.InterstateEdge = guard_inedges[0]
            before_state: dace.SDFGState = guard_inedges[0].src
            last_state: dace.SDFGState = guard_inedges[1].src
        else:
            init_edge: dace.InterstateEdge = guard_inedges[1]
            before_state: dace.SDFGState = guard_inedges[1].src
            last_state: dace.SDFGState = guard_inedges[0].src

        # Get loop states
        loop_states = list(
            dace.sdfg.utils.dfs_conditional(
                sdfg, sources=[begin], condition=lambda _, child: child != guard
            )
        )
        assert len(loop_states) == 1
        loop_state = loop_states[0]

        var_idx = ["i", "j", "k"].index(str(itervar))

        try:
            niter = int(abs(rng[1] - rng[0]) / abs(rng[2])) + 1
        except TypeError:
            # if the range is non-constant, assume it is large enough to fully peel the loop
            niter = 3

        if niter == 1:
            # just inline the only present state, no prefetching needed
            apply_count = sdfg.apply_transformations(
                AlwaysApplyLoopPeeling, options={"count": 1, "begin": True}
            )
            assert apply_count == 1

        elif niter >= 2:

            if self.arrays is not None:
                arrays = {name: sdfg.arrays[name] for name in self.arrays}
            else:
                arrays = {
                    name: array
                    for name, array in sdfg.arrays.items()
                    if not name.startswith("__tmp")
                }

            subset_infos = {}
            for name in arrays.keys():
                subset_infos[name] = self.collect_subset_info(loop_state, name, var_idx=var_idx)
            ## peel both ends, add prefetching
            # peel first iteration
            apply_count = sdfg.apply_transformations(
                AlwaysApplyLoopPeeling, options={"count": 1, "begin": True}, validate=False
            )
            assert apply_count == 1
            assert guard in sdfg.nodes()
            peeled_first = next(
                edge.src for edge in sdfg.in_edges(guard) if edge.src is not loop_state
            )

            # peel last iteration
            apply_count = sdfg.apply_transformations(
                AlwaysApplyLoopPeeling, options={"count": 1, "begin": False}, validate=False
            )
            assert apply_count == 1
            assert guard in sdfg.nodes()
            peeled_last = next(
                edge.dst for edge in sdfg.out_edges(guard) if edge.dst is not loop_state
            )

            prefetch_state = sdfg.add_state(sdfg.label + "_prefetch_state")
            sdfg.add_edge(prefetch_state, peeled_first, dace.InterstateEdge())
            sdfg.add_edge(before_state, prefetch_state, dace.InterstateEdge())
            sdfg.remove_edge(sdfg.edges_between(before_state, peeled_first)[0])
            store_state = sdfg.add_state(sdfg.label + "_store_state")
            sdfg.add_edge(peeled_last, store_state, dace.InterstateEdge())
            sdfg.add_edge(store_state, after_state, dace.InterstateEdge())
            sdfg.remove_edge(sdfg.edges_between(peeled_last, after_state)[0])

            shift_state = sdfg.add_state_before(loop_state, sdfg.label + "_shift_state")
            # self.rename_arrays(sdfg)
            # add prefetching to peeled_first and loop_state, add prefetch-only state
            for name, array in arrays.items():
                subset_info = subset_infos[name]
                shape = list(array.shape)
                shape[var_idx] = subset_info["length"]
                sdfg.add_array(
                    name=f"_loc_buf_{name}",
                    shape=shape,
                    dtype=array.dtype,
                    storage=self.storage_type,
                    transient=True,
                    lifetime=dace.dtypes.AllocationLifetime.SDFG,
                )

                self.add_prefetch_all(
                    prefetch_state, name, itervar, rng[0], var_idx, subset_info, rng[2], 1
                )

                if name in self.store:
                    self.add_store_all(
                        store_state, name, itervar, rng[1], var_idx, subset_info, rng[2], rng[2]
                    )
                self.localize_tasklet_memlets(
                    peeled_first, name, itervar, 0, var_idx, subset_info, rng[2], 1
                )
                self.localize_tasklet_memlets(
                    loop_state, name, itervar, itervar, var_idx, subset_info, rng[2], 1
                )
                self.localize_tasklet_memlets(
                    peeled_last, name, itervar, rng[1], var_idx, subset_info, rng[2], 1 + rng[2]
                )
                self.add_prefetch(
                    loop_state, name, itervar, itervar, var_idx, subset_info, rng[2], 0
                )
                self.add_prefetch(
                    peeled_first, name, itervar, rng[0], var_idx, subset_info, rng[2], 0
                )
                self.add_shift_local(
                    shift_state, name, itervar, rng[0], var_idx, subset_info, rng[2], 0
                )
                if name in self.store:
                    self.add_store(
                        loop_state, name, itervar, itervar, var_idx, subset_info, rng[2], 0
                    )
                    self.add_store(
                        peeled_last, name, itervar, rng[1], var_idx, subset_info, rng[2], rng[2]
                    )

        if niter < 3:
            # remove loop after peeling if the loop is never actually executed.
            before_state = [e.src for e in sdfg.in_edges(guard) if e.src is not loop_state][0]
            after_state = [e.dst for e in sdfg.out_edges(guard) if e.dst is not loop_state][0]

            init_edges = sdfg.edges_between(before_state, guard)
            assert len(init_edges) == 1
            init_edge = init_edges[0]
            sdfg.remove_edge(init_edge)
            # add edge from pred directly to loop states

            # sdfg.add_edge(before_state, first_state, dace.InterstateEdge(assignments=init_edge.assignments))
            exit_edge = sdfg.edges_between(last_state, guard)[0]
            sdfg.remove_edge(exit_edge)
            sdfg.remove_node(loop_state)
            sdfg.remove_node(guard)

            sdfg.add_edge(
                before_state,
                after_state,
                dace.InterstateEdge(
                    condition=exit_edge.data.condition, assignments=init_edge.data.assignments
                ),
            )


import copy
from collections import defaultdict

import dace
from dace import registry, symbolic
from dace.properties import Property, make_properties
from dace.sdfg import nodes
from dace.sdfg import utils as sdutils
from dace.transformation.interstate.loop_detection import DetectLoop
from dace.transformation.interstate.loop_unroll import LoopUnroll
from dace.transformation.transformation import Transformation


@registry.autoregister
@make_properties
class BasicRegisterCache(Transformation):
    _before_state = dace.SDFGState()
    _loop_state = dace.SDFGState()
    _guard_state = dace.SDFGState()

    array = Property(dtype=str, desc="Name of the array to replace by a register cache")

    @staticmethod
    def expressions():
        sdfg = dace.SDFG("_")
        before_state, loop_state, guard_state = (
            BasicRegisterCache._before_state,
            BasicRegisterCache._loop_state,
            BasicRegisterCache._guard_state,
        )
        sdfg.add_nodes_from((before_state, loop_state, guard_state))
        sdfg.add_edge(before_state, guard_state, dace.InterstateEdge())
        sdfg.add_edge(guard_state, loop_state, dace.InterstateEdge())
        sdfg.add_edge(loop_state, guard_state, dace.InterstateEdge())
        return [sdfg]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        return True

    def _buffer_memlets(self, states):
        for state in states:
            for edge in state.edges():
                src, dst = edge.src, edge.dst
                if (
                    isinstance(src, nodes.AccessNode)
                    and src.data == self.array
                    or isinstance(dst, nodes.AccessNode)
                    and dst.data == self.array
                ):
                    yield edge.data

    def _get_loop_axis(self, loop_state, loop_var):
        def contains_loop_var(subset_range):
            return any(loop_var in {s.name for s in r.free_symbols} for r in subset_range)

        for memlet in self._buffer_memlets([loop_state]):
            return [contains_loop_var(r) for r in memlet.subset.ranges].index(True)

    def _get_buffer_size(self, state, loop_var, loop_axis):
        min_offset, max_offset = 1000, -1000
        for memlet in self._buffer_memlets([state]):
            rb, re, _ = memlet.subset.ranges[loop_axis]
            rb_offset = rb - symbolic.symbol(loop_var)
            re_offset = re - symbolic.symbol(loop_var)
            min_offset = min(min_offset, rb_offset, re_offset)
            max_offset = max(max_offset, rb_offset, re_offset)
        return max_offset - min_offset + 1

    def _replace_indices(self, states, loop_var, loop_axis, buffer_size):
        for memlet in self._buffer_memlets(states):
            rb, re, rs = memlet.subset.ranges[loop_axis]
            memlet.subset.ranges[loop_axis] = (rb % buffer_size, re % buffer_size, rs)

    def apply(self, sdfg: dace.SDFG):
        before_state = sdfg.node(self.subgraph[self._before_state])
        loop_state = sdfg.node(self.subgraph[self._loop_state])
        guard_state = sdfg.node(self.subgraph[self._guard_state])
        loop_var = next(iter(sdfg.in_edges(guard_state)[0].data.assignments))

        loop_axis = self._get_loop_axis(loop_state, loop_var)

        buffer_size = self._get_buffer_size(loop_state, loop_var, loop_axis)
        self._replace_indices(sdfg.states(), loop_var, loop_axis, buffer_size)

        array = sdfg.arrays[self.array]
        # TODO: generalize
        if array.shape[loop_axis] == array.total_size:
            array.shape = tuple(
                buffer_size if i == loop_axis else s for i, s in enumerate(array.shape)
            )
            array.total_size = buffer_size


@registry.autoregister_params(singlestate=True)
class OnTheFlyMapFusion(Transformation):
    _first_map_entry = nodes.MapEntry(nodes.Map("", [], []))
    _first_tasklet = nodes.Tasklet("")
    _first_map_exit = nodes.MapExit(nodes.Map("", [], []))
    _array_access = nodes.AccessNode("")
    _second_map_entry = nodes.MapEntry(nodes.Map("", [], []))
    _second_tasklet = nodes.Tasklet("")

    @staticmethod
    def expressions():
        return [
            sdutils.node_path_graph(
                OnTheFlyMapFusion._first_map_entry,
                OnTheFlyMapFusion._first_tasklet,
                OnTheFlyMapFusion._first_map_exit,
                OnTheFlyMapFusion._array_access,
                OnTheFlyMapFusion._second_map_entry,
                OnTheFlyMapFusion._second_tasklet,
            )
        ]

    @staticmethod
    def can_be_applied(graph, candidate, expr_index, sdfg, strict=False):
        first_map_entry = graph.node(candidate[OnTheFlyMapFusion._first_map_entry])
        first_tasklet = graph.node(candidate[OnTheFlyMapFusion._first_tasklet])
        first_map_exit = graph.node(candidate[OnTheFlyMapFusion._first_map_exit])
        array_access = graph.node(candidate[OnTheFlyMapFusion._array_access])

        if len(first_map_exit.in_connectors) != 1:
            return False

        if graph.in_degree(array_access) != 1 or graph.out_degree(array_access) != 1:
            return False
        return True

    @staticmethod
    def _memlet_offsets(base_memlet, offset_memlet):
        """Compute subset offset of `offset_memlet` relative to `base_memlet`."""

        def offset(base_range, offset_range):
            b0, e0, s0 = base_range
            b1, e1, s1 = offset_range
            assert e1 - e0 == b1 - b0 and s0 == s1
            return int(e1 - e0)

        return tuple(
            offset(b, o) for b, o in zip(base_memlet.subset.ranges, offset_memlet.subset.ranges)
        )

    @staticmethod
    def _update_map_connectors(state, array_access, first_map_entry, second_map_entry):
        """Remove unused connector (of the to-be-replaced array) from second
        map entry, add new connectors to second map entry for the inputs
        used in the first map’s tasklets.
        """
        # Remove edges and connectors from arrays access to second map entry
        for edge in state.edges_between(array_access, second_map_entry):
            state.remove_edge_and_connectors(edge)
        state.remove_node(array_access)

        # Add new connectors to second map
        # TODO: implement for the general case with random naming
        for edge in state.in_edges(first_map_entry):
            if second_map_entry.add_in_connector(edge.dst_conn):
                state.add_edge(edge.src, edge.src_conn, second_map_entry, edge.dst_conn, edge.data)

    @staticmethod
    def _read_offsets(state, array_name, first_map_exit, second_map_entry):
        """Compute offsets of read accesses in second map."""
        # Get output memlet of first tasklet
        output_edges = state.in_edges(first_map_exit)
        assert len(output_edges) == 1
        write_memlet = output_edges[0].data

        # Find read offsets by looping over second map entry connectors
        offsets = defaultdict(list)
        for edge in state.out_edges(second_map_entry):
            if edge.data.data == array_name:
                second_map_entry.remove_out_connector(edge.src_conn)
                state.remove_edge(edge)
                offset = OnTheFlyMapFusion._memlet_offsets(write_memlet, edge.data)
                offsets[offset].append(edge)

        return offsets

    @staticmethod
    def _copy_first_map_contents(sdfg, state, first_map_entry, first_map_exit):
        nodes = list(state.all_nodes_between(first_map_entry, first_map_exit) - {first_map_entry})
        new_nodes = [copy.deepcopy(node) for node in nodes]
        tmp_map = dict()
        for node in new_nodes:
            if isinstance(node, dace.nodes.AccessNode):
                data = sdfg.arrays[node.data]
                if isinstance(data, dace.data.Scalar) and data.transient:
                    tmp_name = sdfg.temp_data_name()
                    sdfg.add_scalar(tmp_name, data.dtype, transient=True)
                    tmp_map[node.data] = tmp_name
                    node.data = tmp_name
            state.add_node(node)
        id_map = {state.node_id(old): state.node_id(new) for old, new in zip(nodes, new_nodes)}

        def map_node(node):
            return state.node(id_map[state.node_id(node)])

        def map_memlet(memlet):
            memlet = copy.deepcopy(memlet)
            memlet.data = tmp_map.get(memlet.data, memlet.data)
            return memlet

        for edge in state.edges():
            if edge.src in nodes or edge.dst in nodes:
                src = map_node(edge.src) if edge.src in nodes else edge.src
                dst = map_node(edge.dst) if edge.dst in nodes else edge.dst
                edge_data = map_memlet(edge.data)
                state.add_edge(src, edge.src_conn, dst, edge.dst_conn, edge_data)

        return new_nodes

    def _replicate_first_map(
        self, sdfg, array_access, first_map_entry, first_map_exit, second_map_entry
    ):
        """Replicate tasklet of first map for reach read access in second map."""
        state = sdfg.node(self.state_id)
        array_name = array_access.data
        array = sdfg.arrays[array_name]

        read_offsets = self._read_offsets(state, array_name, first_map_exit, second_map_entry)

        # Replicate first map tasklets once for each read offset access and
        # connect them to other tasklets accordingly
        for offset, edges in read_offsets.items():
            nodes = self._copy_first_map_contents(sdfg, state, first_map_entry, first_map_exit)
            tmp_name = sdfg.temp_data_name()
            sdfg.add_scalar(tmp_name, array.dtype, transient=True)
            tmp_access = state.add_access(tmp_name)

            for node in nodes:
                for edge in state.edges_between(node, first_map_exit):
                    state.add_edge(
                        edge.src, edge.src_conn, tmp_access, None, dace.Memlet(tmp_name)
                    )
                    state.remove_edge(edge)

                for edge in state.edges_between(first_map_entry, node):
                    memlet = copy.deepcopy(edge.data)
                    memlet.subset.offset(list(offset), negative=False)
                    second_map_entry.add_out_connector(edge.src_conn)
                    state.add_edge(second_map_entry, edge.src_conn, node, edge.dst_conn, memlet)
                    state.remove_edge(edge)

            for edge in edges:
                state.add_edge(tmp_access, None, edge.dst, edge.dst_conn, dace.Memlet(tmp_name))

    def apply(self, sdfg: dace.SDFG):
        state = sdfg.node(self.state_id)
        first_map_entry = state.node(self.subgraph[self._first_map_entry])
        first_tasklet = state.node(self.subgraph[self._first_tasklet])
        first_map_exit = state.node(self.subgraph[self._first_map_exit])
        array_access = state.node(self.subgraph[self._array_access])
        second_map_entry = state.node(self.subgraph[self._second_map_entry])

        self._update_map_connectors(state, array_access, first_map_entry, second_map_entry)

        self._replicate_first_map(
            sdfg, array_access, first_map_entry, first_map_exit, second_map_entry
        )

        state.remove_nodes_from(
            state.all_nodes_between(first_map_entry, first_map_exit) | {first_map_exit}
        )